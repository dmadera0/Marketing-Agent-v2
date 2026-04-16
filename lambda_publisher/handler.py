"""
Lambda 2 — Publisher
Handles Approved and Rejected replies (replies/ prefix only).
Publishes to Medium, LinkedIn, and Google Drive on approval.
Uses Google API SDK from Lambda layer.
"""
import json
import os
import re
import time
import email
import boto3
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ── AWS clients ───────────────────────────────────────────────────────────────
s3        = boto3.client("s3")
dynamodb  = boto3.resource("dynamodb")
ses       = boto3.client("ses", region_name=os.environ.get("AWS_REGION", "us-east-1"))
ssm       = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))

# ── Environment ───────────────────────────────────────────────────────────────
DYNAMODB_TABLE   = os.environ["DYNAMODB_TABLE"]
SES_SENDER_EMAIL = os.environ["SES_SENDER_EMAIL"]
APPROVAL_EMAIL   = os.environ["APPROVAL_EMAIL"]
# MEDIUM_TOKEN     = os.environ["MEDIUM_TOKEN"]
LINKEDIN_TOKEN   = os.environ["LINKEDIN_TOKEN"]
DRIVE_SA_PARAM   = os.environ["DRIVE_SA_PARAM"]

# ── Helpers ───────────────────────────────────────────────────────────────────

def detect_intent(body: str):
    """Mirror of Lambda 1 intent detection — returns (intent, payload)."""
    stripped  = body.strip()
    first_line = stripped.splitlines()[0].strip().lower() if stripped else ""

    if re.match(r"(?i)^topic\s*:", stripped):
        return "new_brief", None
    if first_line == "approved":
        return "approved", None
    if first_line == "rejected":
        return "rejected", None

    m = re.match(r"(?i)^Revise\s*:\s*(.+)", stripped, re.DOTALL)
    if m:
        return "revise", m.group(1).strip()

    m = re.match(r"(?i)^Edit\s+Blog\s*:\s*(.+)", stripped, re.DOTALL)
    if m:
        return "edit_blog", m.group(1).strip()

    m = re.match(r"(?i)^Edit\s+LinkedIn\s*:\s*(.+)", stripped, re.DOTALL)
    if m:
        return "edit_linkedin", m.group(1).strip()

    return "unknown", None


def extract_draft_id(body: str) -> str | None:
    m = re.search(r"Draft ID:\s*([0-9a-f\-]{36})", body, re.IGNORECASE)
    return m.group(1) if m else None


def get_table():
    return dynamodb.Table(DYNAMODB_TABLE)


def _http_json(url: str, method: str = "GET", data: dict = None,
               headers: dict = None) -> tuple[int, dict]:
    """Minimal HTTP helper using urllib."""
    body = json.dumps(data).encode("utf-8") if data else None
    req  = Request(url, data=body, method=method, headers=headers or {})
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        print(f"HTTPError {exc.code} for {url}: {raw}")
        raise


# ── Platform publishers ───────────────────────────────────────────────────────

def publish_medium(blog: str, topic: str, tags: list) -> str:
    """Publish blog to Medium. Returns post URL."""
    # Get user ID
    _, me = _http_json(
        "https://api.medium.com/v1/me",
        headers={"Authorization": f"Bearer {MEDIUM_TOKEN}"},
    )
    user_id = me["data"]["id"]

    # Publish post (max 5 tags)
    status, post_resp = _http_json(
        f"https://api.medium.com/v1/users/{user_id}/posts",
        method="POST",
        data={
            "title":         topic,
            "contentFormat": "markdown",
            "content":       blog,
            "tags":          tags[:5],
            "publishStatus": "public",
        },
        headers={"Authorization": f"Bearer {MEDIUM_TOKEN}"},
    )
    url = post_resp["data"]["url"]
    print(f"Medium published: {url}")
    return url


def publish_linkedin(linkedin_text: str) -> str:
    """Post to LinkedIn. Returns post URN/ID."""
    # Get person ID
    _, me = _http_json(
        "https://api.linkedin.com/v2/me",
        headers={
            "Authorization":             f"Bearer {LINKEDIN_TOKEN}",
            "X-Restli-Protocol-Version": "2.0.0",
        },
    )
    person_id  = me["id"]
    author_urn = f"urn:li:person:{person_id}"

    # Create UGC post
    status, post_resp = _http_json(
        "https://api.linkedin.com/v2/ugcPosts",
        method="POST",
        data={
            "author":          author_urn,
            "lifecycleState":  "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {
                        "text": linkedin_text,
                    },
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC",
            },
        },
        headers={
            "Authorization":             f"Bearer {LINKEDIN_TOKEN}",
            "X-Restli-Protocol-Version": "2.0.0",
        },
    )
    post_id = post_resp.get("id", "unknown")
    linkedin_url = f"https://www.linkedin.com/feed/update/{post_id}/"
    print(f"LinkedIn published: {linkedin_url}")
    return linkedin_url


def publish_google_drive(blog: str, linkedin_text: str, topic: str, folder_name: str) -> str:
    """Save Google Doc to Drive. Returns webViewLink."""
    # Import Google SDK (available via Lambda layer)
    import google.oauth2.service_account as sa_module
    from googleapiclient.discovery import build as g_build

    # Fetch service account JSON from SSM
    param = ssm.get_parameter(Name=DRIVE_SA_PARAM, WithDecryption=True)
    sa_info = json.loads(param["Parameter"]["Value"])

    scopes = [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/documents",
    ]
    creds        = sa_module.Credentials.from_service_account_info(sa_info, scopes=scopes)
    drive_svc    = g_build("drive", "v3", credentials=creds, cache_discovery=False)
    docs_svc     = g_build("docs", "v1", credentials=creds, cache_discovery=False)

    # Find or create folder by name
    q = (
        f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    results = drive_svc.files().list(q=q, fields="files(id, name)").execute()
    files   = results.get("files", [])
    if files:
        folder_id = files[0]["id"]
        print(f"Found existing Drive folder: {folder_id}")
    else:
        folder_meta = {
            "name":     folder_name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        folder = drive_svc.files().create(body=folder_meta, fields="id").execute()
        folder_id = folder["id"]
        print(f"Created Drive folder: {folder_id}")

    # Create Google Doc
    doc_meta = {
        "name":    topic,
        "parents": [folder_id],
        "mimeType": "application/vnd.google-apps.document",
    }
    doc_file   = drive_svc.files().create(body=doc_meta, fields="id, webViewLink").execute()
    doc_id     = doc_file["id"]
    web_link   = doc_file.get("webViewLink", "")

    # Build document content
    divider       = "\n" + "─" * 60 + "\n"
    li_header     = "LINKEDIN POST"
    full_content  = f"{blog}\n{divider}{li_header}\n{divider}\n{linkedin_text}"

    # Insert text
    docs_svc.documents().batchUpdate(
        documentId=doc_id,
        body={
            "requests": [
                {
                    "insertText": {
                        "location": {"index": 1},
                        "text":     full_content,
                    }
                }
            ]
        },
    ).execute()

    # Bold + 18pt the first line (title)
    title_end = len(topic) + 1  # +1 for newline
    docs_svc.documents().batchUpdate(
        documentId=doc_id,
        body={
            "requests": [
                {
                    "updateTextStyle": {
                        "range":     {"startIndex": 1, "endIndex": title_end},
                        "textStyle": {"bold": True, "fontSize": {"magnitude": 18, "unit": "PT"}},
                        "fields":    "bold,fontSize",
                    }
                }
            ]
        },
    ).execute()

    print(f"Google Doc created: {web_link}")
    return web_link


# ── Confirmation email ────────────────────────────────────────────────────────

def send_confirmation_email(topic: str, linkedin_url: str,
                            drive_url: str, errors: list) -> None:
    lines = [f"Your content for '{topic}' has been published.\n"]
    lines.append(f"LinkedIn: {linkedin_url or '(failed)'}")
    lines.append(f"Drive:    {drive_url or '(failed)'}")
    if errors:
        lines.append("\nPartial errors:")
        for e in errors:
            lines.append(f"  - {e}")

    ses.send_email(
        Source=SES_SENDER_EMAIL,
        Destination={"ToAddresses": [APPROVAL_EMAIL]},
        Message={
            "Subject": {"Data": f"Published: {topic}", "Charset": "UTF-8"},
            "Body":    {"Text": {"Data": "\n".join(lines), "Charset": "UTF-8"}},
        },
    )
    print("Confirmation email sent.")


def send_rejection_email(topic: str, draft_id: str) -> None:
    body = (
        f"Your draft '{topic}' (ID: {draft_id}) has been marked as rejected.\n\n"
        "No content was published. You can start a new brief at any time."
    )
    ses.send_email(
        Source=SES_SENDER_EMAIL,
        Destination={"ToAddresses": [APPROVAL_EMAIL]},
        Message={
            "Subject": {"Data": f"Rejected: {topic}", "Charset": "UTF-8"},
            "Body":    {"Text": {"Data": body, "Charset": "UTF-8"}},
        },
    )
    print("Rejection acknowledgement sent.")


# ── Main handler ──────────────────────────────────────────────────────────────

def handler(event, context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key    = record["s3"]["object"]["key"]
        print(f"Processing s3://{bucket}/{key}")

        # Read raw email from S3
        obj       = s3.get_object(Bucket=bucket, Key=key)
        raw_bytes = obj["Body"].read()

        # Parse RFC-2822 email
        msg  = email.message_from_bytes(raw_bytes)
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    charset = part.get_content_charset() or "utf-8"
                    body    = part.get_payload(decode=True).decode(charset, errors="replace")
                    break
        else:
            charset = msg.get_content_charset() or "utf-8"
            body    = msg.get_payload(decode=True).decode(charset, errors="replace")

        # Detect intent
        intent, _ = detect_intent(body)
        print(f"Intent detected: {intent}")

        # Lambda 2 only owns approved and rejected
        if intent in ("revise", "edit_blog", "edit_linkedin", "new_brief"):
            print(f"Intent '{intent}' handled by Agent Lambda. Skipping.")
            return {"statusCode": 200, "body": "Delegated to Agent Lambda."}
        if intent == "unknown":
            print("Unknown intent — ignoring.")
            return {"statusCode": 200, "body": "Unknown intent."}

        # Extract draft ID
        draft_id = extract_draft_id(body)
        if not draft_id:
            print("WARN: No Draft ID in reply — cannot process.")
            return {"statusCode": 400, "body": "No Draft ID found."}

        # Fetch from DynamoDB
        table = get_table()
        resp  = table.get_item(Key={"draft_id": draft_id})
        item  = resp.get("Item")
        if not item:
            print(f"WARN: Draft {draft_id} not found.")
            return {"statusCode": 404, "body": "Draft not found."}
        if item.get("status") != "PENDING":
            print(f"WARN: Draft {draft_id} status={item.get('status')} — skipping (idempotency guard).")
            return {"statusCode": 200, "body": "Draft not in PENDING state."}

        fields  = json.loads(item["fields"])
        topic   = fields.get("topic", "Untitled")
        tags    = fields.get("tags", [])
        folder  = fields.get("drive_folder", "Marketing Blog Posts")
        blog    = item["blog"]
        linkedin_text = item["linkedin"]

        # ── Rejected ─────────────────────────────────────────────────────────
        if intent == "rejected":
            table.update_item(
                Key={"draft_id": draft_id},
                UpdateExpression="SET #status = :st",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":st": "REJECTED"},
            )
            send_rejection_email(topic, draft_id)
            return {"statusCode": 200, "body": f"Draft {draft_id} rejected."}

        # ── Approved ──────────────────────────────────────────────────────────
        # Set PROCESSING immediately to prevent double-publish on retry
        table.update_item(
            Key={"draft_id": draft_id},
            UpdateExpression="SET #status = :st",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":st": "PROCESSING"},
        )

        # medium_url   = ""
        linkedin_url = ""
        drive_url    = ""
        errors       = []

        # Medium
        # try:
        #     medium_url = publish_medium(blog, topic, tags)
        # except Exception as exc:
        #     msg = f"Medium publish failed: {exc}"
        #     print(f"ERROR: {msg}")
        #     errors.append(msg)

        # LinkedIn
        try:
            linkedin_url = publish_linkedin(linkedin_text)
        except Exception as exc:
            msg = f"LinkedIn publish failed: {exc}"
            print(f"ERROR: {msg}")
            errors.append(msg)

        # Google Drive
        try:
            drive_url = publish_google_drive(blog, linkedin_text, topic, folder)
        except Exception as exc:
            msg = f"Google Drive save failed: {exc}"
            print(f"ERROR: {msg}")
            errors.append(msg)

        # Update DynamoDB final status
        final_status = "PARTIAL" if errors else "PUBLISHED"
        table.update_item(
            Key={"draft_id": draft_id},
            UpdateExpression=(
                "SET #status = :st, published_at = :pa, publish_results = :pr"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":st": final_status,
                ":pa": int(time.time()),
                ":pr": json.dumps({
                    # "medium":   medium_url,
                    "linkedin": linkedin_url,
                    "drive":    drive_url,
                    "errors":   errors,
                }),
            },
        )

        send_confirmation_email(topic, linkedin_url, drive_url, errors)
        return {
            "statusCode": 200,
            "body": f"Draft {draft_id} published ({final_status}).",
        }

    return {"statusCode": 200, "body": "No records to process."}

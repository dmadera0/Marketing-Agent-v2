"""
Lambda 1 — Content Agent
Handles new briefs (inbound/) and revision replies (replies/).
Triggers: S3 ObjectCreated on inbound/ and replies/ prefixes.
"""
import json
import os
import re
import time
import uuid
import email
import boto3
from urllib.request import urlopen, Request
from urllib.error import URLError

# ── AWS clients ──────────────────────────────────────────────────────────────
s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
ses = boto3.client("ses", region_name=os.environ.get("AWS_REGION", "us-east-1"))

# ── Environment ───────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DYNAMODB_TABLE    = os.environ["DYNAMODB_TABLE"]
SES_SENDER_EMAIL  = os.environ["SES_SENDER_EMAIL"]
APPROVAL_EMAIL    = os.environ["APPROVAL_EMAIL"]

CLAUDE_MODEL      = "claude-opus-4-5"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"

# ── Helpers ───────────────────────────────────────────────────────────────────

def call_claude(system_prompt: str, user_prompt: str, max_tokens: int = 4096) -> str:
    """Call Claude API using urllib (no external HTTP libs in Lambda 1)."""
    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode("utf-8")

    req = Request(
        ANTHROPIC_URL,
        data=payload,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    with urlopen(req, timeout=90) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["content"][0]["text"].strip()


def detect_intent(body: str):
    """
    Returns (intent, payload) where intent is one of:
      new_brief | approved | rejected | revise | edit_blog | edit_linkedin | unknown
    """
    stripped = body.strip()
    first_line = stripped.splitlines()[0].strip().lower() if stripped else ""

    # New brief: starts with "Topic:" field
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


def parse_brief(body: str) -> dict:
    """Parse the email template fields from the body."""
    fields = {}

    def grab(label, text=body):
        # Greedy match to next uppercase label or end of string
        pattern = rf"(?i)^{re.escape(label)}\s*:\s*(.+?)(?=\n[A-Z][A-Za-z ]+\s*:|$)"
        m = re.search(pattern, text, re.MULTILINE | re.DOTALL)
        return m.group(1).strip() if m else ""

    fields["topic"]          = grab("Topic")
    fields["tone"]           = grab("Tone")
    fields["audience"]       = grab("Audience")
    fields["call_to_action"] = grab("Call to Action")
    fields["blog_length"]    = grab("Blog Length") or "medium"
    fields["industry"]       = grab("Industry")
    fields["drive_folder"]   = grab("Drive Folder") or "Marketing Blog Posts"

    # Tags: comma or newline separated
    tags_raw = grab("Tags")
    fields["tags"] = [t.strip() for t in re.split(r"[,\n]", tags_raw) if t.strip()]

    # Key Points: multiline bullet list
    m = re.search(r"(?i)^Key Points\s*:\s*\n((?:\s*[-•*]?.+\n?)+?)(?=\n[A-Z]|\Z)", body, re.MULTILINE)
    if m:
        lines = [l.strip().lstrip("-•* ") for l in m.group(1).splitlines() if l.strip()]
        fields["key_points"] = lines
    else:
        fields["key_points"] = []

    return fields


BUENAVISTAAI_VOICE = """
You are the content strategist for BuenaVista AI Solutions, an AI agency based in
Los Angeles that helps businesses leverage artificial intelligence to streamline
operations, boost growth, and stay ahead of the curve.

Brand voice rules — follow these precisely:
- Professional yet approachable: expert without being jargon-heavy
- Forward-thinking and optimistic about AI's practical benefits for real businesses
- Story-driven: open with a hook, anchor ideas in real-world impact
- Never salesy. Educate first, inspire action second.
- Tagline: "Intelligent solutions. Real results."

Company details to weave in naturally where relevant:
- Services: AI strategy consulting, custom AI integrations, automation workflows,
  LLM-powered products, data analytics
- Target audience: SMB owners, operations leaders, marketing directors,
  forward-thinking executives
- Website: https://buenavista-ai.com
"""

SEO_INSTRUCTIONS = """
SEO requirements — apply to every blog post:
- Include the primary keyword in: H1 title, first 100 words, at least 2 H2 subheadings
- Natural keyword density: 1–2% (do not stuff)
- Write a meta description of 150–155 characters at the very top, labelled:
  META: [your meta description here]
- Suggest a URL slug on the second line, labelled:
  SLUG: [your-url-slug-here]
- Use H2 subheadings every 200–250 words
- Sentences under 25 words where possible
- End with an H2 section: "How BuenaVista AI Solutions Can Help" (100 words, soft CTA)
"""


def build_blog_prompt(fields: dict) -> tuple[str, str]:
    length_map = {"short": "~500 words", "medium": "~900 words", "long": "~1500 words"}
    length = length_map.get(fields.get("blog_length", "medium").lower(), "~900 words")
    key_points = "\n".join(f"- {p}" for p in fields.get("key_points", []))
    tags = ", ".join(fields.get("tags", []))
    keyword = fields.get("target_keyword", fields.get("topic", ""))

    system = (
        f"{BUENAVISTAAI_VOICE}\n\n"
        "You are writing a blog post for the BuenaVista AI Solutions website and Medium channel. "
        "Write only the blog post content in markdown format. "
        "Start with META: and SLUG: lines, then the H1 title, then the post body. "
        "Do not include any preamble or closing remarks outside the post itself."
    )
    user = f"""Write a {length} SEO-optimised blog post with the following details:

Topic: {fields['topic']}
Primary SEO keyword: {keyword}
Audience: {fields['audience']}
Industry: {fields.get('industry', 'AI / Technology')}
Tone: {fields['tone']}
Key Points:
{key_points}
Call to Action: {fields['call_to_action']}
Tags: {tags}

{SEO_INSTRUCTIONS}

Write the full blog post in markdown. Include an engaging H1 title, well-structured
H2 sections, key takeaways, and end with the BuenaVista CTA section."""
    return system, user
# def build_blog_prompt(fields: dict) -> tuple[str, str]:
#     length_map = {"short": "~500 words", "medium": "~900 words", "long": "~1500 words"}
#     length = length_map.get(fields.get("blog_length", "medium").lower(), "~900 words")
#     key_points = "\n".join(f"- {p}" for p in fields.get("key_points", []))
#     tags = ", ".join(fields.get("tags", []))

#     system = (
#         "You are an expert content writer. Write only the blog post content in markdown format. "
#         "Do not include any preamble, explanation, or closing remarks outside the post itself. "
#         "Start directly with the title as a markdown H1."
#     )
#     user = f"""Write a {length} blog post with the following details:

# Topic: {fields['topic']}
# Audience: {fields['audience']}
# Industry: {fields['industry']}
# Tone: {fields['tone']}
# Key Points:
# {key_points}
# Call to Action: {fields['call_to_action']}
# Tags: {tags}

# Write the full blog post in markdown. Include an engaging H1 title, well-structured H2 sections, \
# and end with a clear call-to-action paragraph."""
#     return system, user


def build_linkedin_prompt(blog: str, fields: dict) -> tuple[str, str]:
    system = (
        "You are an expert LinkedIn content writer. Write only the LinkedIn post text — "
        "no preamble, no explanation, no surrounding quotes."
    )
    user = f"""Adapt the following blog post into a LinkedIn post.

Requirements:
- Maximum 1300 characters (hard limit)
- Hook on the very first line to grab attention
- Do NOT start with the word "I"
- 3–5 short paragraphs separated by blank lines
- Use emojis sparingly (1–3 total)
- End with a clear call-to-action
- Include 5–8 relevant hashtags on the last line
- Tone: {fields['tone']}
- Audience: {fields['audience']}

Blog post to adapt:
{blog[:3000]}"""
    return system, user


def send_review_email(to: str, topic: str, draft_id: str, revision_num: int,
                      blog: str, linkedin: str) -> None:
    subject_prefix = f"[Draft {revision_num}]" if revision_num == 1 else f"[Revision {revision_num}]"
    subject = f"{subject_prefix} {topic} — {draft_id[:8]}"

    blog_preview = blog[:2500]
    if len(blog) > 2500:
        blog_preview += "\n\n... [truncated — full content saved] ..."

    linkedin_char_count = len(linkedin)

    body = f"""Draft ID: {draft_id}
Topic: {topic}

━━━━━━━━━━━━━━━━━━━━ BLOG POST PREVIEW ━━━━━━━━━━━━━━━━━━━━

{blog_preview}

━━━━━━━━━━━━━━━━━━━━ LINKEDIN POST ({linkedin_char_count} chars) ━━━━━━━━━━━━━━━━━━━━

{linkedin}

━━━━━━━━━━━━━━━━━━━━ REPLY COMMANDS ━━━━━━━━━━━━━━━━━━━━

  Approved                → publishes to Medium, LinkedIn, and Google Drive
  Rejected                → discards this draft
  Revise: <feedback>      → Claude rewrites both pieces with your feedback
  Edit Blog: <text>       → replaces blog exactly; LinkedIn auto-adapted
  Edit LinkedIn: <text>   → replaces LinkedIn post exactly (blog unchanged)

Make sure to include the Draft ID line above in all replies.
"""

    ses.send_email(
        Source=SES_SENDER_EMAIL,
        Destination={"ToAddresses": [to]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body":    {"Text": {"Data": body, "Charset": "UTF-8"}},
        },
    )
    print(f"Review email sent: subject={subject!r}, to={to}")


def get_table():
    return dynamodb.Table(DYNAMODB_TABLE)


# ── Intent handlers ───────────────────────────────────────────────────────────

def handle_new_brief(body: str) -> dict:
    fields    = parse_brief(body)
    topic     = fields.get("topic", "Untitled")
    draft_id  = str(uuid.uuid4())

    print(f"New brief: topic={topic!r}, draft_id={draft_id}")

    # Generate blog
    sys_p, usr_p = build_blog_prompt(fields)
    blog = call_claude(sys_p, usr_p, max_tokens=4096)

    # Generate LinkedIn
    sys_p2, usr_p2 = build_linkedin_prompt(blog, fields)
    linkedin = call_claude(sys_p2, usr_p2, max_tokens=1024)

    # Cap LinkedIn at 1300 chars
    if len(linkedin) > 1300:
        linkedin = linkedin[:1297] + "..."

    # Store in DynamoDB
    now = int(time.time())
    get_table().put_item(Item={
        "draft_id":         draft_id,
        "status":           "PENDING",
        "revision_num":     1,
        "created_at":       now,
        "ttl":              now + 14 * 86400,
        "fields":           json.dumps(fields),
        "blog":             blog,
        "linkedin":         linkedin,
        "revision_history": [],
    })

    # Send review email
    send_review_email(APPROVAL_EMAIL, topic, draft_id, 1, blog, linkedin)

    return {"statusCode": 200, "body": f"Draft {draft_id} created and emailed."}


def handle_revise(body: str, feedback: str) -> dict:
    draft_id = extract_draft_id(body)
    if not draft_id:
        print("WARN: No Draft ID found in revise request")
        return {"statusCode": 400, "body": "No Draft ID found."}

    table = get_table()
    resp  = table.get_item(Key={"draft_id": draft_id})
    item  = resp.get("Item")
    if not item:
        print(f"WARN: Draft {draft_id} not found")
        return {"statusCode": 404, "body": "Draft not found."}
    if item.get("status") != "PENDING":
        print(f"WARN: Draft {draft_id} status={item.get('status')} — skipping")
        return {"statusCode": 200, "body": "Draft not in PENDING state."}

    fields       = json.loads(item["fields"])
    topic        = fields.get("topic", "Untitled")
    old_blog     = item["blog"]
    old_linkedin = item["linkedin"]
    revision_num = int(item.get("revision_num", 1)) + 1

    print(f"Revising draft {draft_id} → revision {revision_num}, feedback={feedback[:80]!r}")

    # Rewrite blog with feedback
    system = (
        "You are an expert content writer. Rewrite the blog post in markdown based on the "
        "provided feedback. Return only the updated blog post — no preamble or commentary."
    )
    user = f"""Rewrite the following blog post based on this feedback: {feedback}

Original brief fields:
Topic: {fields.get('topic')}
Audience: {fields.get('audience')}
Industry: {fields.get('industry')}
Tone: {fields.get('tone')}
Key Points: {', '.join(fields.get('key_points', []))}
Call to Action: {fields.get('call_to_action')}

Original blog post:
{old_blog}"""
    new_blog = call_claude(system, user, max_tokens=4096)

    # Rewrite LinkedIn
    sys_p2, usr_p2 = build_linkedin_prompt(new_blog, fields)
    new_linkedin = call_claude(sys_p2, usr_p2, max_tokens=1024)
    if len(new_linkedin) > 1300:
        new_linkedin = new_linkedin[:1297] + "..."

    # Snapshot old revision
    snapshot = {
        "revision_num": int(item.get("revision_num", 1)),
        "blog":         old_blog,
        "linkedin":     old_linkedin,
        "feedback":     feedback,
        "timestamp":    int(time.time()),
    }
    history = list(item.get("revision_history", []))
    history.append(snapshot)

    # Update DynamoDB
    table.update_item(
        Key={"draft_id": draft_id},
        UpdateExpression=(
            "SET #blog = :blog, linkedin = :li, revision_num = :rn, "
            "#status = :st, revision_history = :rh"
        ),
        ExpressionAttributeNames={"#blog": "blog", "#status": "status"},
        ExpressionAttributeValues={
            ":blog": new_blog,
            ":li":   new_linkedin,
            ":rn":   revision_num,
            ":st":   "PENDING",
            ":rh":   history,
        },
    )

    send_review_email(APPROVAL_EMAIL, topic, draft_id, revision_num, new_blog, new_linkedin)
    return {"statusCode": 200, "body": f"Revision {revision_num} sent for draft {draft_id}."}


def handle_edit_blog(body: str, new_blog_text: str) -> dict:
    draft_id = extract_draft_id(body)
    if not draft_id:
        return {"statusCode": 400, "body": "No Draft ID found."}

    table = get_table()
    resp  = table.get_item(Key={"draft_id": draft_id})
    item  = resp.get("Item")
    if not item:
        return {"statusCode": 404, "body": "Draft not found."}
    if item.get("status") != "PENDING":
        return {"statusCode": 200, "body": "Draft not in PENDING state."}

    fields       = json.loads(item["fields"])
    topic        = fields.get("topic", "Untitled")
    revision_num = int(item.get("revision_num", 1)) + 1

    # Re-generate LinkedIn from new blog
    sys_p, usr_p = build_linkedin_prompt(new_blog_text, fields)
    new_linkedin = call_claude(sys_p, usr_p, max_tokens=1024)
    if len(new_linkedin) > 1300:
        new_linkedin = new_linkedin[:1297] + "..."

    snapshot = {
        "revision_num": int(item.get("revision_num", 1)),
        "blog":         item["blog"],
        "linkedin":     item["linkedin"],
        "feedback":     "Edit Blog direct replacement",
        "timestamp":    int(time.time()),
    }
    history = list(item.get("revision_history", []))
    history.append(snapshot)

    table.update_item(
        Key={"draft_id": draft_id},
        UpdateExpression=(
            "SET #blog = :blog, linkedin = :li, revision_num = :rn, "
            "#status = :st, revision_history = :rh"
        ),
        ExpressionAttributeNames={"#blog": "blog", "#status": "status"},
        ExpressionAttributeValues={
            ":blog": new_blog_text,
            ":li":   new_linkedin,
            ":rn":   revision_num,
            ":st":   "PENDING",
            ":rh":   history,
        },
    )

    send_review_email(APPROVAL_EMAIL, topic, draft_id, revision_num, new_blog_text, new_linkedin)
    return {"statusCode": 200, "body": f"Blog replaced, revision {revision_num} sent."}


def handle_edit_linkedin(body: str, new_linkedin_text: str) -> dict:
    draft_id = extract_draft_id(body)
    if not draft_id:
        return {"statusCode": 400, "body": "No Draft ID found."}

    table = get_table()
    resp  = table.get_item(Key={"draft_id": draft_id})
    item  = resp.get("Item")
    if not item:
        return {"statusCode": 404, "body": "Draft not found."}
    if item.get("status") != "PENDING":
        return {"statusCode": 200, "body": "Draft not in PENDING state."}

    fields       = json.loads(item["fields"])
    topic        = fields.get("topic", "Untitled")
    blog         = item["blog"]
    revision_num = int(item.get("revision_num", 1)) + 1

    if len(new_linkedin_text) > 1300:
        new_linkedin_text = new_linkedin_text[:1297] + "..."

    snapshot = {
        "revision_num": int(item.get("revision_num", 1)),
        "blog":         blog,
        "linkedin":     item["linkedin"],
        "feedback":     "Edit LinkedIn direct replacement",
        "timestamp":    int(time.time()),
    }
    history = list(item.get("revision_history", []))
    history.append(snapshot)

    table.update_item(
        Key={"draft_id": draft_id},
        UpdateExpression=(
            "SET linkedin = :li, revision_num = :rn, "
            "#status = :st, revision_history = :rh"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":li":  new_linkedin_text,
            ":rn":  revision_num,
            ":st":  "PENDING",
            ":rh":  history,
        },
    )

    send_review_email(APPROVAL_EMAIL, topic, draft_id, revision_num, blog, new_linkedin_text)
    return {"statusCode": 200, "body": f"LinkedIn replaced, revision {revision_num} sent."}


# ── Main handler ──────────────────────────────────────────────────────────────

def handler(event, context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key    = record["s3"]["object"]["key"]
        print(f"Processing s3://{bucket}/{key}")

        # Read raw email from S3
        obj      = s3.get_object(Bucket=bucket, Key=key)
        raw_bytes = obj["Body"].read()

        # Parse RFC-2822 email
        msg       = email.message_from_bytes(raw_bytes)
        body      = ""
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
        intent, payload = detect_intent(body)
        print(f"Intent detected: {intent}")

        if intent == "new_brief":
            return handle_new_brief(body)
        elif intent == "revise":
            return handle_revise(body, payload)
        elif intent == "edit_blog":
            return handle_edit_blog(body, payload)
        elif intent == "edit_linkedin":
            return handle_edit_linkedin(body, payload)
        elif intent in ("approved", "rejected"):
            print(f"Intent '{intent}' is handled by Lambda 2 (Publisher). Skipping.")
            return {"statusCode": 200, "body": f"Intent '{intent}' delegated to Publisher Lambda."}
        else:
            print(f"Unknown intent — ignoring.")
            return {"statusCode": 200, "body": "Unknown intent — no action taken."}

    return {"statusCode": 200, "body": "No records to process."}

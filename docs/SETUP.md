# Setup Guide — Email-Driven Content Marketing Agent

End-to-end instructions for deploying the blog automation system from scratch.

---

## Prerequisites

| Tool | Minimum version |
|------|----------------|
| AWS account | — |
| AWS CLI | configured with credentials |
| Terraform | >= 1.5 |
| Python | 3.12 |
| pip | latest |

---

## 1. Register a Domain in Route 53

You need a dedicated domain for the agent email address (e.g. `blogagent@yourdomain.com`).

1. Go to **AWS Console → Route 53 → Registered domains**
2. Click **Register domain** and search for your desired name
3. Complete purchase (~$12/year for `.com`)
4. AWS automatically creates a **Hosted Zone** for the domain — note the hosted zone ID

> If you already own a domain registered elsewhere, you can delegate DNS to Route 53 by pointing your registrar's nameservers to the ones listed in the hosted zone.

---

## 2. Verify the Domain in SES

1. Go to **AWS Console → SES → Verified identities → Create identity**
2. Choose **Domain**, enter your domain name
3. Under **DNS records**, check **Use Route 53** — AWS will insert the required CNAME records automatically
4. Click **Create identity** and wait ~5 minutes for DNS propagation and verification

---

## 3. Add an MX Record in Route 53

SES needs an MX record to receive inbound email.

1. Go to **Route 53 → Hosted zones → your domain → Create record**
2. Set:
   - **Record type:** MX
   - **Value:** `10 inbound-smtp.us-east-1.amazonaws.com`
   - **TTL:** 300
3. Click **Create records**

> If you deploy to a region other than `us-east-1`, replace the region in the MX value accordingly. Supported regions for SES inbound: `us-east-1`, `us-west-2`, `eu-west-1`.

---

## 4. Exit the SES Sandbox

By default new AWS accounts are in the SES sandbox, which only allows sending to verified email addresses. You need production access to deliver drafts to your personal inbox.

1. Go to **SES → Account dashboard → Request production access**
2. Fill out the form — describe your use case (transactional automated email, low volume)
3. Approval typically takes 24–48 hours

While waiting for approval, you can test by verifying your personal email as an identity in SES (**Verified identities → Create identity → Email address**).

---

## 5. Verify Sender Addresses in SES

Even in production, verify both addresses to avoid sending issues:

1. Go to **SES → Verified identities → Create identity → Email address**
2. Add `blogagent@yourdomain.com` — confirm the verification email
3. Add `approve@yourdomain.com` — confirm the verification email

---

## 6. Obtain API Tokens

### Anthropic (Claude)
1. Go to [console.anthropic.com](https://console.anthropic.com) → **API Keys**
2. Create a new key and copy it — this is your `anthropic_api_key`

### Medium
1. Go to **medium.com/me/settings** → scroll to **Integration tokens**
2. Generate a token — this is your `medium_token`

### LinkedIn
1. Create a LinkedIn Developer App at [developer.linkedin.com](https://developer.linkedin.com)
2. In the app, go to **Auth → OAuth 2.0 tools**
3. Generate a token with scopes: `w_member_social`, `r_liteprofile`
4. Copy the access token — this is your `linkedin_token`

> **Important:** LinkedIn OAuth tokens expire every **60 days**. When a token expires, re-generate it in the Developer portal and run `terraform apply` with the updated `linkedin_token` value to update the Lambda environment variable.

### Google Drive Service Account
1. Go to **Google Cloud Console → APIs & Services → Enabled APIs**
2. Enable both **Google Drive API** and **Google Docs API**
3. Go to **IAM & Admin → Service Accounts → Create service account**
4. Give it any name (e.g. `blog-agent-drive`)
5. On the service account page, go to **Keys → Add key → Create new key → JSON**
6. Download the JSON file — this is your `drive_sa_json`
7. **Share your target Drive folder** with the service account's `client_email` (found in the JSON), with **Editor** permissions

> The service account cannot access Drive folders it hasn't been shared with. If you create a new folder, remember to share it.

---

## 7. Deploy with Terraform

```bash
# From the repo root:

# 1. Build the Google SDK Lambda layer
./build.sh

# 2. Create your tfvars file
cp infra/terraform.tfvars.example infra/terraform.tfvars
# Edit infra/terraform.tfvars and fill in all values

# 3. Deploy
cd infra
terraform init
terraform apply
```

Terraform will output the S3 bucket name, DynamoDB table, and Lambda function names.

---

## 8. Test the End-to-End Flow

1. Copy `docs/EMAIL_TEMPLATE.txt` into a new email
2. Fill in all fields (Topic, Tone, Audience, etc.)
3. Send to `blogagent@yourdomain.com`
4. Wait **60–90 seconds** — you should receive a draft email at your `approval_email` address
5. Reply to the draft with one of:
   - `Approved` — publishes to all three platforms
   - `Revise: please make the tone more casual` — Claude rewrites both pieces
   - `Rejected` — discards the draft

---

## 9. Cost Reference

| Service | Cost |
|---------|------|
| Route 53 domain | ~$13/year |
| SES inbound | $0.10 per 1,000 emails |
| SES outbound | $0.10 per 1,000 emails |
| S3 storage | < $0.01/month |
| Lambda (512 MB, 120s) | < $0.01 per 100 invocations |
| DynamoDB (on-demand) | < $0.01/month |
| Anthropic Claude API | ~$0.02–$0.10 per blog post |

Total typical cost: **$13/year + ~$0.10 per published post**.

---

## 10. Troubleshooting

### Email not arriving at Lambda
- Check Route 53 MX record is set correctly: `10 inbound-smtp.<region>.amazonaws.com`
- Verify the domain is shown as **Verified** in SES
- Confirm the SES receipt rule set is **active** (SES → Email receiving → Rule sets)

### Lambda errors
- Check CloudWatch Logs at:
  - `/aws/lambda/blog-agent-agent`
  - `/aws/lambda/blog-agent-publisher`
- Common issues: missing env vars, expired LinkedIn token, Drive API not enabled

### Still in SES sandbox
- You will see `MessageRejected` errors in CloudWatch if the recipient is not verified
- Either verify the destination email in SES or wait for production access approval

### Google Drive permission errors
- Ensure the target folder is shared with the service account `client_email` with Editor role
- Ensure Drive API and Docs API are both enabled in Google Cloud Console

### LinkedIn token expired
- Re-generate the token in the LinkedIn Developer portal
- Update `infra/terraform.tfvars` with the new token
- Run `terraform apply` — only the Publisher Lambda environment variable updates

### Double invocation (S3 trigger fires twice)
- This is handled: Lambda 2 sets status to `PROCESSING` before publishing, and checks `status == PENDING` before acting. A second invocation will see `PROCESSING` and skip.

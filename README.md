# Email-Driven Content Marketing Automation Agent

A fully serverless, email-driven system that takes a content brief, generates a blog post and LinkedIn post with Claude, routes it through an email-based approval workflow, and publishes to Medium, LinkedIn, and Google Drive — all with no UI and no manual intervention beyond sending and replying to emails.

---

## How It Works

```
You → email brief → blogagent@yourdomain.com
                           ↓
                     SES → S3 (inbound/)
                           ↓
                    Lambda 1 (Agent)
                    • Parses brief
                    • Calls Claude API
                    • Stores draft in DynamoDB
                           ↓
              Draft email → you (approval_email)
                           ↓
         You reply: Approved / Rejected / Revise: ... / Edit Blog: ... / Edit LinkedIn: ...
                           ↓
                     SES → S3 (replies/)
                    ┌──────┴──────┐
             Lambda 1          Lambda 2
             (revisions)       (publish)
                    └──────┬──────┘
                           ↓
              Medium + LinkedIn + Google Drive
                           ↓
              Confirmation email with all three links
```

---

## Project Structure

```
blog-agent/
├── lambda_agent/
│   └── handler.py          # Content generation and revision logic
├── lambda_publisher/
│   └── handler.py          # Approval, rejection, and publishing logic
├── infra/
│   ├── main.tf             # All AWS infrastructure (S3, DynamoDB, Lambda, IAM)
│   ├── ses_rules.tf        # SES receipt rules for inbound and reply emails
│   └── terraform.tfvars.example
├── docs/
│   ├── SETUP.md            # Full step-by-step deployment guide
│   └── EMAIL_TEMPLATE.txt  # Content brief template + reply command reference
├── build.sh                # Builds the Google SDK Lambda layer
└── .gitignore
```

---

## Quick Start

```bash
# 1. Build the Lambda layer (installs Google SDK deps)
./build.sh

# 2. Configure
cp infra/terraform.tfvars.example infra/terraform.tfvars
# Edit infra/terraform.tfvars with your tokens and settings

# 3. Deploy
cd infra && terraform init && terraform apply
```

See [docs/SETUP.md](docs/SETUP.md) for the full deployment guide, including domain registration, SES verification, MX records, and API token setup.

---

## Reply Commands

After receiving a draft email, reply to `approve@yourdomain.com` — always include the `Draft ID:` line from the draft.

| Command | Effect |
|---------|--------|
| `Approved` | Publishes to Medium, LinkedIn, Google Drive |
| `Rejected` | Discards the draft |
| `Revise: <feedback>` | Claude rewrites both pieces with your feedback |
| `Edit Blog: <text>` | Replaces blog exactly; LinkedIn auto-adapted |
| `Edit LinkedIn: <text>` | Replaces LinkedIn post exactly; blog unchanged |

Revisions are unlimited.

---

## Infrastructure

| Resource | Type |
|----------|------|
| Email storage | S3 (30-day lifecycle) |
| Draft storage | DynamoDB (14-day TTL) |
| Drive credentials | SSM Parameter Store (SecureString) |
| Lambda 1 (Agent) | Python 3.12, 512 MB, 120s timeout |
| Lambda 2 (Publisher) | Python 3.12, 512 MB, 120s timeout + Google SDK layer |
| Email routing | SES receipt rules |

---

## Environment Variables

**Lambda 1 — Agent**

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `DYNAMODB_TABLE` | DynamoDB table name |
| `SES_SENDER_EMAIL` | Agent from address |
| `APPROVAL_EMAIL` | Your personal email |

**Lambda 2 — Publisher**

| Variable | Description |
|----------|-------------|
| `DYNAMODB_TABLE` | DynamoDB table name |
| `SES_SENDER_EMAIL` | Agent from address |
| `APPROVAL_EMAIL` | Your personal email |
| `MEDIUM_TOKEN` | Medium integration token |
| `LINKEDIN_TOKEN` | LinkedIn OAuth token (expires every 60 days) |
| `DRIVE_SA_PARAM` | SSM parameter name for Drive service account JSON |

---

## Cost

Route 53 domain (~$13/year) + SES/Lambda/DynamoDB (near-zero) + Anthropic API (~$0.02–$0.10 per post).

# ── SES Receipt Rule Set ──────────────────────────────────────────────────────

resource "aws_ses_receipt_rule_set" "main" {
  rule_set_name = "${var.project}-rules"
}

resource "aws_ses_active_receipt_rule_set" "main" {
  rule_set_name = aws_ses_receipt_rule_set.main.rule_set_name
}

# ── Rule 1 — Inbound Briefs ───────────────────────────────────────────────────
# Receives emails sent to the agent address (e.g. blogagent@yourdomain.com)
# and stores them under s3://bucket/inbound/

resource "aws_ses_receipt_rule" "inbound_briefs" {
  name          = "inbound-briefs"
  rule_set_name = aws_ses_receipt_rule_set.main.rule_set_name
  recipients    = [var.ses_sender_email]
  enabled       = true
  scan_enabled  = true

  s3_action {
    bucket_name = aws_s3_bucket.emails.bucket
    object_key_prefix = "inbound/"
    position    = 1
  }

  depends_on = [aws_s3_bucket_policy.allow_ses]
}

# ── Rule 2 — Approval Replies ─────────────────────────────────────────────────
# Receives reply emails sent to approve@<domain>
# and stores them under s3://bucket/replies/

resource "aws_ses_receipt_rule" "approval_replies" {
  name          = "approval-replies"
  rule_set_name = aws_ses_receipt_rule_set.main.rule_set_name
  recipients    = ["approve@${var.ses_domain}"]
  enabled       = true
  scan_enabled  = true
  after         = aws_ses_receipt_rule.inbound_briefs.name

  s3_action {
    bucket_name = aws_s3_bucket.emails.bucket
    object_key_prefix = "replies/"
    position    = 1
  }

  depends_on = [aws_s3_bucket_policy.allow_ses]
}

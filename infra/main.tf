terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

# ── Variables ─────────────────────────────────────────────────────────────────

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name prefix used for all resource names"
  type        = string
  default     = "blog-agent"
}

variable "ses_domain" {
  description = "Verified SES domain (e.g. yourdomain.com)"
  type        = string
}

variable "ses_sender_email" {
  description = "Agent from/sender email address (e.g. blogagent@yourdomain.com)"
  type        = string
}

variable "approval_email" {
  description = "Personal email address to receive drafts"
  type        = string
}

variable "anthropic_api_key" {
  description = "Anthropic API key for Claude"
  type        = string
  sensitive   = true
}

variable "linkedin_token" {
  description = "LinkedIn OAuth 2.0 access token"
  type        = string
  sensitive   = true
}

variable "drive_sa_json" {
  description = "Google Drive service account credentials JSON (stored in SSM SecureString)"
  type        = string
  sensitive   = true
}

# ── S3 Bucket ─────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "emails" {
  bucket        = "${var.project}-emails-${data.aws_caller_identity.current.account_id}"
  force_destroy = true

  tags = {
    Project = var.project
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "emails" {
  bucket = aws_s3_bucket.emails.id

  rule {
    id     = "expire-emails"
    status = "Enabled"

    filter {
      prefix = ""
    }

    expiration {
      days = 30
    }
  }
}

resource "aws_s3_bucket_policy" "allow_ses" {
  bucket = aws_s3_bucket.emails.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowSESPutObject"
        Effect = "Allow"
        Principal = {
          Service = "ses.amazonaws.com"
        }
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.emails.arn}/*"
        Condition = {
          StringEquals = {
            "aws:Referer" = data.aws_caller_identity.current.account_id
          }
        }
      }
    ]
  })
}

# ── DynamoDB Table ────────────────────────────────────────────────────────────

resource "aws_dynamodb_table" "drafts" {
  name         = "${var.project}-drafts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "draft_id"

  attribute {
    name = "draft_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = {
    Project = var.project
  }
}

# ── SSM Parameter — Drive Service Account ─────────────────────────────────────

resource "aws_ssm_parameter" "drive_sa" {
  name  = "/${var.project}/drive-service-account"
  type  = "SecureString"
  value = var.drive_sa_json

  tags = {
    Project = var.project
  }
}

# ── IAM Role for Lambdas ──────────────────────────────────────────────────────

resource "aws_iam_role" "lambda_exec" {
  name = "${var.project}-lambda-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Project = var.project
  }
}

resource "aws_iam_role_policy" "lambda_inline" {
  name = "${var.project}-lambda-policy"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Logs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Sid      = "S3GetEmails"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.emails.arn}/*"
      },
      {
        Sid    = "DynamoDB"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:UpdateItem"
        ]
        Resource = aws_dynamodb_table.drafts.arn
      },
      {
        Sid    = "SES"
        Effect = "Allow"
        Action = [
          "ses:SendEmail",
          "ses:SendRawEmail"
        ]
        Resource = "*"
      },
      {
        Sid    = "SSMDriveSA"
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.drive_sa.arn
      },
      {
        Sid    = "InvokePublisher"
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.publisher.arn
      },
    ]
  })
}

# ── Lambda Layer (Publisher Google SDK deps) ──────────────────────────────────

resource "aws_lambda_layer_version" "deps" {
  layer_name          = "${var.project}-deps"
  filename            = "${path.module}/../lambda_publisher/layer.zip"
  compatible_runtimes = ["python3.12"]

  lifecycle {
    create_before_destroy = true
  }
}

# ── Lambda 1 — Agent ──────────────────────────────────────────────────────────

data "archive_file" "agent_zip" {
  type        = "zip"
  source_file = "${path.module}/../lambda_agent/handler.py"
  output_path = "${path.module}/agent_handler.zip"
}

resource "aws_lambda_function" "agent" {
  function_name    = "${var.project}-agent"
  role             = aws_iam_role.lambda_exec.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.agent_zip.output_path
  source_code_hash = data.archive_file.agent_zip.output_base64sha256
  timeout          = 120
  memory_size      = 512

  environment {
  variables = {
    ANTHROPIC_API_KEY        = var.anthropic_api_key
    DYNAMODB_TABLE           = aws_dynamodb_table.drafts.name
    SES_SENDER_EMAIL         = var.ses_sender_email
    APPROVAL_EMAIL           = var.approval_email
    PUBLISHER_FUNCTION_NAME  = aws_lambda_function.publisher.function_name
  }
}

  tags = {
    Project = var.project
  }
}

# ── Lambda 2 — Publisher ──────────────────────────────────────────────────────

data "archive_file" "publisher_zip" {
  type        = "zip"
  source_file = "${path.module}/../lambda_publisher/handler.py"
  output_path = "${path.module}/publisher_handler.zip"
}

resource "aws_lambda_function" "publisher" {
  function_name    = "${var.project}-publisher"
  role             = aws_iam_role.lambda_exec.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.publisher_zip.output_path
  source_code_hash = data.archive_file.publisher_zip.output_base64sha256
  timeout          = 120
  memory_size      = 512
  layers           = [aws_lambda_layer_version.deps.arn]

  environment {
    variables = {
      DYNAMODB_TABLE   = aws_dynamodb_table.drafts.name
      SES_SENDER_EMAIL = var.ses_sender_email
      APPROVAL_EMAIL   = var.approval_email
      LINKEDIN_TOKEN   = var.linkedin_token
      DRIVE_SA_PARAM   = aws_ssm_parameter.drive_sa.name
    }
  }

  tags = {
    Project = var.project
  }
}

# ── Lambda Permissions (S3 → Lambda invoke) ───────────────────────────────────

resource "aws_lambda_permission" "s3_agent" {
  statement_id  = "AllowS3InvokeAgent"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.agent.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.emails.arn
}

resource "aws_lambda_permission" "s3_publisher" {
  statement_id  = "AllowS3InvokePublisher"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.publisher.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.emails.arn
}

# ── S3 Bucket Notifications ───────────────────────────────────────────────────
# Three triggers with unique IDs:
#   1. inbound/   → Agent Lambda
#   2. replies/   → Agent Lambda  (handles revision intents)
#   3. replies/   → Publisher Lambda (handles approved/rejected)

resource "aws_s3_bucket_notification" "email_triggers" {
  bucket = aws_s3_bucket.emails.id

  lambda_function {
    id                  = "inbound-agent"
    lambda_function_arn = aws_lambda_function.agent.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "inbound/"
  }

  lambda_function {
    id                  = "replies-agent"
    lambda_function_arn = aws_lambda_function.agent.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "replies/"
  }

  depends_on = [
    aws_lambda_permission.s3_agent,
    aws_lambda_permission.s3_publisher,
  ]
}

# ── Outputs ───────────────────────────────────────────────────────────────────

output "email_bucket" {
  description = "S3 bucket receiving SES emails"
  value       = aws_s3_bucket.emails.bucket
}

output "dynamodb_table" {
  description = "DynamoDB drafts table name"
  value       = aws_dynamodb_table.drafts.name
}

output "agent_lambda" {
  description = "Content Agent Lambda function name"
  value       = aws_lambda_function.agent.function_name
}

output "publisher_lambda" {
  description = "Publisher Lambda function name"
  value       = aws_lambda_function.publisher.function_name
}

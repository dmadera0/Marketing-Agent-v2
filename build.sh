#!/usr/bin/env bash
# build.sh — Package the Google SDK layer for Lambda 2 (Publisher)
# Run this once before `terraform apply`.

set -euo pipefail

LAYER_DIR="lambda_publisher/layer/python"
LAYER_ZIP="lambda_publisher/layer.zip"

echo "==> Creating layer directory: ${LAYER_DIR}"
mkdir -p "${LAYER_DIR}"

echo "==> Installing Google SDK dependencies..."
pip install \
  google-auth \
  google-auth-httplib2 \
  google-api-python-client \
  --target "${LAYER_DIR}" \
  --quiet

echo "==> Zipping layer to ${LAYER_ZIP}..."
(cd lambda_publisher/layer && zip -r "../layer.zip" python/ -x "*.pyc" -x "*/__pycache__/*" > /dev/null)

SIZE=$(du -sh "${LAYER_ZIP}" | cut -f1)
echo "==> Done! ${LAYER_ZIP} — ${SIZE}"
echo ""
echo "Next steps:"
echo "  cp infra/terraform.tfvars.example infra/terraform.tfvars"
echo "  # fill in infra/terraform.tfvars"
echo "  cd infra && terraform init && terraform apply"

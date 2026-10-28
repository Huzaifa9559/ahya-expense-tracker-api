#!/usr/bin/env bash
# Runs when LocalStack is ready (mounted into /etc/localstack/init/ready.d/).
set -euo pipefail
awslocal s3 mb "s3://${S3_BUCKET:-ahya-receipts}" || true
awslocal s3api put-public-access-block \
  --bucket "${S3_BUCKET:-ahya-receipts}" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" || true
echo "localstack s3 bucket ready"

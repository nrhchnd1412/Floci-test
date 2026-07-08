#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${AWS_ENDPOINT_URL:-}" ]]; then
  echo "ERROR: AWS_ENDPOINT_URL is not set." >&2
  exit 1
fi

export AWS_PAGER=""

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
TABLE_NAME="floci-dynamodb-table"
QUEUE_NAME="floci-sqs"
BUCKET_NAME="file-upload"
FUNCTION_NAME="floci-lambda"
FUNCTION_ROLE_ARN="arn:aws:iam::000000000000:role/floci-lambda-role"
LAMBDA_ENDPOINT_FOR_CI="${LAMBDA_ENDPOINT_FOR_CI:-http://172.17.0.1:4566}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

aws_cmd() {
  aws --endpoint-url "${AWS_ENDPOINT_URL}" --region "${REGION}" "$@"
}

echo "Ensuring DynamoDB table ${TABLE_NAME} exists..."
if ! aws_cmd dynamodb describe-table --table-name "${TABLE_NAME}" >/dev/null 2>&1; then
  aws_cmd dynamodb create-table \
    --table-name "${TABLE_NAME}" \
    --attribute-definitions AttributeName=pk,AttributeType=S \
    --key-schema AttributeName=pk,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST >/dev/null
fi

echo "Ensuring SQS queue ${QUEUE_NAME} exists..."
if ! queue_url="$(aws_cmd sqs get-queue-url --queue-name "${QUEUE_NAME}" --query QueueUrl --output text 2>/dev/null)" || [[ -z "${queue_url}" || "${queue_url}" == "None" ]]; then
  aws_cmd sqs create-queue --queue-name "${QUEUE_NAME}" >/dev/null
  queue_url="$(aws_cmd sqs get-queue-url --queue-name "${QUEUE_NAME}" --query QueueUrl --output text)"
fi

queue_arn="$(aws_cmd sqs get-queue-attributes --queue-url "${queue_url}" --attribute-names QueueArn --query Attributes.QueueArn --output text)"

echo "Applying queue policy for S3 -> SQS delivery..."
policy_file="${REPO_ROOT}/sqs-policy.json"
if [[ -f "${policy_file}" ]]; then
  policy_json="$(tr -d '\n' < "${policy_file}")"
else
  policy_json="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"AllowS3ToSend\",\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"s3.amazonaws.com\"},\"Action\":\"sqs:SendMessage\",\"Resource\":\"${queue_arn}\",\"Condition\":{\"ArnEquals\":{\"aws:SourceArn\":\"arn:aws:s3:::${BUCKET_NAME}\"}}}]}"
fi
queue_attrs_file="${REPO_ROOT}/.tmp-sqs-attributes.json"
escaped_policy_json="$(printf '%s' "${policy_json}" | sed 's/\\/\\\\/g; s/"/\\"/g')"
cat > "${queue_attrs_file}" <<EOF
{
  "Policy": "${escaped_policy_json}"
}
EOF
aws_cmd sqs set-queue-attributes --queue-url "${queue_url}" --attributes "file://${queue_attrs_file}" >/dev/null
rm -f "${queue_attrs_file}"

echo "Ensuring S3 bucket ${BUCKET_NAME} exists..."
if ! aws_cmd s3api head-bucket --bucket "${BUCKET_NAME}" >/dev/null 2>&1; then
  aws_cmd s3api create-bucket --bucket "${BUCKET_NAME}" >/dev/null
fi

echo "Packaging Lambda code..."
zip_path="${REPO_ROOT}/app.zip"
rm -f "${zip_path}"
(
  cd "${REPO_ROOT}"
  zip -q "${zip_path}" app.py
)

echo "Creating or updating Lambda function ${FUNCTION_NAME}..."
if aws_cmd lambda get-function --function-name "${FUNCTION_NAME}" >/dev/null 2>&1; then
  aws_cmd lambda update-function-code --function-name "${FUNCTION_NAME}" --zip-file "fileb://${zip_path}" >/dev/null
  aws_cmd lambda update-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --handler app.handler \
    --runtime python3.11 \
    --role "${FUNCTION_ROLE_ARN}" \
    --environment "Variables={AWS_ENDPOINT_URL=${LAMBDA_ENDPOINT_FOR_CI}}" >/dev/null
else
  aws_cmd lambda create-function \
    --function-name "${FUNCTION_NAME}" \
    --runtime python3.11 \
    --role "${FUNCTION_ROLE_ARN}" \
    --handler app.handler \
    --zip-file "fileb://${zip_path}" \
    --timeout 30 \
    --environment "Variables={AWS_ENDPOINT_URL=${LAMBDA_ENDPOINT_FOR_CI}}" >/dev/null
fi

echo "Configuring S3 notifications to SQS..."
notif_config="${REPO_ROOT}/.tmp-s3-notification.json"
cat > "${notif_config}" <<EOF
{
  "QueueConfigurations": [
    {
      "QueueArn": "${queue_arn}",
      "Events": ["s3:ObjectCreated:*"]
    }
  ]
}
EOF
aws_cmd s3api put-bucket-notification-configuration \
  --bucket "${BUCKET_NAME}" \
  --notification-configuration "file://${notif_config}" >/dev/null
rm -f "${notif_config}"

echo "Ensuring Lambda event source mapping exists..."
mapping_uuid="$(aws_cmd lambda list-event-source-mappings \
  --function-name "${FUNCTION_NAME}" \
  --event-source-arn "${queue_arn}" \
  --query 'EventSourceMappings[0].UUID' \
  --output text 2>/dev/null || true)"

if [[ -z "${mapping_uuid}" || "${mapping_uuid}" == "None" ]]; then
  mapping_uuid="$(aws_cmd lambda create-event-source-mapping \
    --function-name "${FUNCTION_NAME}" \
    --event-source-arn "${queue_arn}" \
    --enabled \
    --batch-size 10 \
    --query UUID \
    --output text)"
fi

echo "Waiting for event source mapping to be Enabled..."
for _ in {1..30}; do
  state="$(aws_cmd lambda get-event-source-mapping --uuid "${mapping_uuid}" --query State --output text 2>/dev/null || true)"
  if [[ "${state}" == "Enabled" ]]; then
    echo "Event source mapping is Enabled (${mapping_uuid})."
    echo "Provisioning complete."
    exit 0
  fi
  sleep 2
done

echo "ERROR: Event source mapping ${mapping_uuid} did not reach Enabled state in time." >&2
aws_cmd lambda get-event-source-mapping --uuid "${mapping_uuid}" || true
exit 1

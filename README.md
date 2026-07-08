# Floci S3 to SQS to Lambda to DynamoDB Integration Demo

This repository demonstrates an event-driven ingestion pipeline on an AWS emulator (Floci):

1. A CSV file is uploaded to S3.
2. S3 sends an object-created notification to SQS.
3. Lambda is mapped to the SQS queue and receives the message.
4. Lambda reads the CSV from S3 and writes rows to DynamoDB.

The sample dataset in employee.csv contains 10 employees (E001 to E010), and the validation script asserts all records are written and the queue is drained.

## Architecture Flow

```text
employee.csv upload
      |
      v
S3 bucket: file-upload
      |
      v
S3 event notification
      |
      v
SQS queue: floci-sqs
      |
      v
Lambda: floci-lambda (app.handler)
      |
      v
DynamoDB table: floci-dynamodb-table (pk = employee_id)
```

## Repository Structure

- app.py: Lambda handler logic.
  - Parses SQS messages containing S3 event payloads.
  - Reads CSV object from S3.
  - Writes each row into DynamoDB using pk and employee_id.
- employee.csv: Sample input with 10 employee rows.
- compose.yaml: Starts Floci on port 4566.
- scripts/provision.sh: End-to-end resource provisioning.
  - Creates DynamoDB table, SQS queue, S3 bucket.
  - Applies SQS queue policy for S3 notifications.
  - Packages and creates/updates Lambda.
  - Configures S3 notification to SQS.
  - Creates Lambda event source mapping from SQS.
- scripts/wait_and_assert.py: Polls and validates integration output.
  - Confirms DynamoDB contains E001 to E010.
  - Confirms all items have pk.
  - Confirms SQS visible messages count is 0.
- .github/workflows/integration.yml: CI workflow for full integration test.
- s3.py: Local helper script to read employee.csv from the emulator S3 bucket.
- notifications.json, out.json, sqs-policy.json, sqs.json, sqs1.json, cmds: supporting and exploratory artifacts used during setup/testing.

## Prerequisites

- Docker with Docker Compose
- Python 3.11+ (CI uses 3.13)
- AWS CLI v2
- Python packages: boto3 (and optionally awscli via pip if AWS CLI binary is not installed)

## Local Run (Floci + AWS Env)

Start Floci:

```bash
docker compose up -d
```

Export AWS emulator environment variables:

```bash
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
```

Important local note:

- scripts/provision.sh sets Lambda environment AWS_ENDPOINT_URL using LAMBDA_ENDPOINT_FOR_CI.
- The default value is http://172.17.0.1:4566, which matches Linux CI Docker networking.
- On macOS, set this before provisioning:

```bash
export LAMBDA_ENDPOINT_FOR_CI=http://host.docker.internal:4566
```

## Manual End-to-End Run

Run the full flow manually:

```bash
# 1) Start emulator
docker compose up -d

# 2) Export env
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export LAMBDA_ENDPOINT_FOR_CI=http://host.docker.internal:4566

# 3) Provision resources and wiring
chmod +x scripts/provision.sh
scripts/provision.sh

# 4) Upload test CSV to S3
aws --endpoint-url "$AWS_ENDPOINT_URL" s3 cp employee.csv s3://file-upload/employee.csv

# 5) Wait and assert expected results
python scripts/wait_and_assert.py
```

Expected result:

- Validation successful: DynamoDB has E001..E010, each item includes pk, and SQS visible messages is 0.

Optional checks:

```bash
aws --endpoint-url "$AWS_ENDPOINT_URL" dynamodb scan --table-name floci-dynamodb-table --select COUNT
aws --endpoint-url "$AWS_ENDPOINT_URL" sqs get-queue-url --queue-name floci-sqs
```

## GitHub Actions CI

Workflow file: .github/workflows/integration.yml

Trigger behavior:

- Manual run via workflow_dispatch
- On push to main
- On pull_request

What CI does:

1. Checks out repository.
2. Sets up Python and installs awscli and boto3.
3. Starts Floci via docker compose.
4. Waits for http://localhost:4566 to become reachable.
5. Exports emulator AWS environment variables.
6. Runs scripts/provision.sh.
7. Uploads employee.csv to S3.
8. Runs scripts/wait_and_assert.py.
9. On failure, prints diagnostics (Floci logs, SQS attributes, Lambda logs, DynamoDB count).

## Troubleshooting

### 1) Endpoint connectivity problems

Symptoms:

- AWS CLI commands fail with connection errors.
- Lambda cannot read from S3 during invocation.

Checks:

```bash
docker compose ps
curl -fsS http://localhost:4566 && echo OK
```

Fixes:

- Ensure Floci container is up.
- Re-export AWS_ENDPOINT_URL and credentials.
- On macOS, set LAMBDA_ENDPOINT_FOR_CI to http://host.docker.internal:4566 before running scripts/provision.sh.

### 2) Queue not draining

Symptoms:

- scripts/wait_and_assert.py times out.
- ApproximateNumberOfMessages stays above 0.

Checks:

```bash
QUEUE_URL=$(aws --endpoint-url "$AWS_ENDPOINT_URL" sqs get-queue-url --queue-name floci-sqs --query QueueUrl --output text)
aws --endpoint-url "$AWS_ENDPOINT_URL" sqs get-queue-attributes \
  --queue-url "$QUEUE_URL" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```

Fixes:

- Confirm S3 bucket notification is configured to the queue.
- Confirm event source mapping is enabled.
- Re-run scripts/provision.sh to re-apply wiring.

### 3) Lambda event source mapping not Enabled

Symptoms:

- Provisioning fails waiting for mapping state.

Checks:

```bash
aws --endpoint-url "$AWS_ENDPOINT_URL" lambda list-event-source-mappings --function-name floci-lambda
```

Fixes:

- Re-run scripts/provision.sh.
- Verify queue ARN and function name are correct.
- Inspect Floci logs for Lambda service errors.

### 4) Missing DynamoDB writes

Symptoms:

- Less than 10 records appear.
- Missing expected employee IDs.

Checks:

```bash
aws --endpoint-url "$AWS_ENDPOINT_URL" dynamodb scan --table-name floci-dynamodb-table
```

Fixes:

- Verify employee.csv was uploaded to s3://file-upload/employee.csv.
- Verify CSV includes employee_id values.
- Confirm Lambda environment can reach emulator endpoint.
- Check Lambda logs in CI diagnostics step or emulator logs locally.

## Cleanup

Stop the local emulator:

```bash
docker compose down
```

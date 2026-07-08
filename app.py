import boto3
import csv
import io
import json
import os


AWS_ENDPOINT_URL = os.getenv('AWS_ENDPOINT_URL')
AWS_REGION = os.getenv('AWS_REGION') or os.getenv('AWS_DEFAULT_REGION') or 'us-east-1'


def _aws_common_kwargs():
    kwargs = {'region_name': AWS_REGION}

    # Emulator mode: use custom endpoint and allow test credential fallback.
    if AWS_ENDPOINT_URL:
        kwargs['endpoint_url'] = AWS_ENDPOINT_URL
        kwargs['aws_access_key_id'] = os.getenv('AWS_ACCESS_KEY_ID') or 'test'
        kwargs['aws_secret_access_key'] = os.getenv('AWS_SECRET_ACCESS_KEY') or 'test'

    # Real AWS mode: rely on default AWS credential provider chain.
    return kwargs


def _extract_s3_targets(event):
    targets = []

    # SQS trigger path: each SQS message body contains an S3 event notification.
    if 'Records' in event and event['Records'] and 'body' in event['Records'][0]:
        for sqs_record in event['Records']:
            body = sqs_record.get('body', '{}')
            body_json = json.loads(body)

            for s3_record in body_json.get('Records', []):
                bucket = s3_record.get('s3', {}).get('bucket', {}).get('name')
                key = s3_record.get('s3', {}).get('object', {}).get('key')
                if bucket and key:
                    targets.append((bucket, key))

        return targets

    # Direct S3 trigger path.
    if 'Records' in event and event['Records'] and 's3' in event['Records'][0]:
        for s3_record in event.get('Records', []):
            bucket = s3_record.get('s3', {}).get('bucket', {}).get('name')
            key = s3_record.get('s3', {}).get('object', {}).get('key')
            if bucket and key:
                targets.append((bucket, key))

        return targets

    # Manual invocation fallback.
    bucket = event.get('bucket')
    key = event.get('key')
    if bucket and key:
        targets.append((bucket, key))

    return targets


def handler(event, context):
    print("Received event:")
    aws_kwargs = _aws_common_kwargs()
    s3 = boto3.client('s3', **aws_kwargs)
    dynamodb = boto3.resource('dynamodb', **aws_kwargs)
    table_name = event.get('table_name') or "floci-dynamodb-table"

    if not table_name:
        raise ValueError("Missing DynamoDB table name. Provide event['table_name'] or set DDB_TABLE_NAME env var.")

    targets = _extract_s3_targets(event)
    if not targets:
        raise ValueError(
            "No S3 objects found in event. Provide SQS(S3-notification), direct S3 trigger, or event['bucket']/event['key']."
        )

    table = dynamodb.Table(table_name)
    total_inserted = 0
    processed_files = []
    print(f"Processing {len(targets)} S3 objects for table '{table_name}'.")
    for bucket, key in targets:
        response = s3.get_object(Bucket=bucket, Key=key)
        csv_text = response['Body'].read().decode('utf-8')
        reader = csv.DictReader(io.StringIO(csv_text))
        inserted_count = 0
        with table.batch_writer() as batch:
            for row in reader:
                employee_id = (row.get('employee_id') or '').strip()
                print(f"Processing row: {row}, extracted employee_id: '{employee_id}'")
                if not employee_id:
                    continue

                item = {'pk': employee_id, 'employee_id': employee_id}
                for column, value in row.items():
                    if column == 'employee_id':
                        continue
                    item[column] = value

                batch.put_item(Item=item)
                inserted_count += 1

        total_inserted += inserted_count
        processed_files.append(
            {
                'bucket': bucket,
                'key': key,
                'rows_inserted': inserted_count,
            }
        )

    return {
        'statusCode': 200,
        'body': {
            'message': 'CSV processed successfully from SQS/S3 notification',
            'table_name': table_name,
            'files_processed': len(processed_files),
            'rows_inserted': total_inserted,
            'details': processed_files,
        },
    }
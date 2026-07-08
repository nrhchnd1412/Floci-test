from __future__ import annotations

import os
import sys
import time
from typing import Any

import boto3


def _aws_auth_kwargs() -> dict[str, str]:
    return {
        "aws_access_key_id": os.getenv("AWS_ACCESS_KEY_ID", "test"),
        "aws_secret_access_key": os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
    }


def _scan_all_items(ddb_client: Any, table_name: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {"TableName": table_name}

    while True:
        response = ddb_client.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    return items


def _extract_str_attr(item: dict[str, Any], key: str) -> str | None:
    attr = item.get(key)
    if not isinstance(attr, dict):
        return None
    value = attr.get("S")
    return value if isinstance(value, str) else None


def main() -> int:
    endpoint_url = os.getenv("AWS_ENDPOINT_URL", "http://host.docker.internal:4566")
    region_name = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    table_name = "floci-dynamodb-table"
    queue_name = "floci-sqs"

    timeout_seconds = int(os.getenv("ASSERT_TIMEOUT_SECONDS", "180"))
    poll_interval_seconds = float(os.getenv("ASSERT_POLL_SECONDS", "3"))

    ddb_client = boto3.client(
        "dynamodb",
        endpoint_url=endpoint_url,
        region_name=region_name,
        **_aws_auth_kwargs(),
    )
    sqs_client = boto3.client(
        "sqs",
        endpoint_url=endpoint_url,
        region_name=region_name,
        **_aws_auth_kwargs(),
    )

    expected_ids = {f"E{i:03d}" for i in range(1, 11)}
    deadline = time.monotonic() + timeout_seconds

    observed_ids: set[str] = set()
    last_count = 0
    while time.monotonic() < deadline:
        items = _scan_all_items(ddb_client, table_name)
        last_count = len(items)

        observed_ids = set()
        missing_pk = 0
        for item in items:
            pk = _extract_str_attr(item, "pk")
            if not pk:
                missing_pk += 1
                continue

            employee_id = _extract_str_attr(item, "employee_id") or pk
            observed_ids.add(employee_id)

        missing_ids = sorted(expected_ids - observed_ids)
        if last_count >= 10 and not missing_ids and missing_pk == 0:
            break

        time.sleep(poll_interval_seconds)
    else:
        missing_ids = sorted(expected_ids - observed_ids)
        raise SystemExit(
            "ERROR: Timed out waiting for expected DynamoDB records. "
            f"count={last_count}, missing_ids={missing_ids}, observed_ids={sorted(observed_ids)}"
        )

    items = _scan_all_items(ddb_client, table_name)
    missing_pk = [i for i in items if not _extract_str_attr(i, "pk")]
    if missing_pk:
        raise SystemExit(
            f"ERROR: Found items without pk attribute. count={len(missing_pk)}"
        )

    queue_url = sqs_client.get_queue_url(QueueName=queue_name)["QueueUrl"]
    attrs = sqs_client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["ApproximateNumberOfMessages"],
    )["Attributes"]

    visible = int(attrs.get("ApproximateNumberOfMessages", "0"))
    if visible != 0:
        raise SystemExit(
            f"ERROR: SQS queue still has visible messages. ApproximateNumberOfMessages={visible}"
        )

    print(
        "Validation successful: DynamoDB has expected employee IDs E001..E010, "
        "all items include pk, and SQS visible messages is 0."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

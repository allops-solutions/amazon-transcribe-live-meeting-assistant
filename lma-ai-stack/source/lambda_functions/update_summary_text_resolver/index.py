# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Lambda function resolver for the updateSummaryText mutation.

Persists a manually edited summary (the "Edit Summary" UI on the meeting
panel). Writes the S3 summary object, then pushes the new text through the
same Kinesis ADD_SUMMARY event / addCallSummaryText / onUpdateCall path the
original end-of-call summary and regenerateSummary both use (see
call_event_processor.py's execute_add_call_summary_text_mutation) — the UI
only has to listen in one place regardless of which of the three wrote the
new text.

No knowledge-base action needed here: the Bedrock KB S3 data source syncs
on a fixed EventBridge schedule (see lma-bedrockkb-stack/template.yaml,
S3KBDataSourceScheduler calling bedrock:StartIngestionJob) rather than any
on-demand trigger the app can call — there is no such trigger anywhere in
this codebase. The next scheduled sync picks up this S3 write on its own.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
kinesis = boto3.client("kinesis")

EVENT_SOURCING_TABLE_NAME = os.environ["EVENT_SOURCING_TABLE_NAME"]
CALL_DATA_STREAM_NAME = os.environ["CALL_DATA_STREAM_NAME"]
S3_BUCKET_NAME = os.environ["S3_BUCKET_NAME"]
S3_PREFIX = os.environ["S3_PREFIX"]

# Fallback only for the (unexpected) case where the call record has no TTL
# of its own — editing is a deliberate, rare action, so err on the side of
# not expiring the record early rather than guessing a short window.
DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 3650  # 10 years


def posixify_filename(filename: str) -> str:
    """Mirrors bedrock_summary_lambda/index.py's posixify_filename exactly.

    The S3 key has to match what the original writer used, or this creates
    a second, orphaned object next to the real one instead of overwriting
    it.
    """
    posix_filename = re.sub(r"[^a-zA-Z0-9_.]", "_", filename)
    posix_filename = re.sub(r"^_+", "", posix_filename)
    posix_filename = re.sub(r"_+$", "", posix_filename)
    return posix_filename


def write_to_s3(call_id: str, call_item: Dict[str, Any], summary_text: str) -> None:
    filename = posixify_filename(call_id)
    summary_key = f"{S3_PREFIX}{filename}-SUMMARY.txt"
    metadata = {
        "metadataAttributes": {
            "CallId": call_id,
            "CreatedAt": call_item.get("CreatedAt", {}).get("S", ""),
            "UpdatedAt": datetime.now(timezone.utc).isoformat(),
            "Owner": call_item.get("Owner", {}).get("S", ""),
            "TotalConversationDurationMillis": int(
                call_item.get("TotalConversationDurationMillis", {}).get("N", "0")
            ),
        }
    }
    s3.put_object(Bucket=S3_BUCKET_NAME, Key=summary_key, Body=summary_text)
    s3.put_object(Bucket=S3_BUCKET_NAME, Key=f"{summary_key}.metadata.json", Body=json.dumps(metadata))
    logger.info("Wrote edited summary to S3: s3://%s/%s", S3_BUCKET_NAME, summary_key)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("updateSummaryText event: %s", json.dumps(event, default=str))

    call_id = event["arguments"]["input"]["CallId"]
    call_summary_text = event["arguments"]["input"]["CallSummaryText"]

    table = dynamodb.Table(EVENT_SOURCING_TABLE_NAME)
    pk = f"c#{call_id}"
    response = table.get_item(Key={"PK": pk, "SK": pk})
    call_item = response.get("Item")
    if not call_item:
        raise ValueError(f"No such meeting: {call_id}")

    # allOps policy: every authenticated user (Admin or User) can edit every
    # meeting's summary — no Owner/SharedWith check, matching
    # regenerateSummary and the rest of this fork's RBAC hardcode. See
    # getCall.response.vtl for the read-side rationale this mirrors.

    try:
        write_to_s3(call_id, call_item, call_summary_text)
    except Exception as exc:  # noqa: BLE001 - best-effort; the KDS write below is what the UI depends on
        logger.error("Could not write edited summary to S3 for %s: %s", call_id, exc)

    expires_after = call_item.get("ExpiresAfter", {}).get("N")
    message = {
        "CallId": call_id,
        "EventType": "ADD_SUMMARY",
        "ExpiresAfter": (
            int(expires_after)
            if expires_after
            else int(datetime.now(timezone.utc).timestamp()) + DEFAULT_TTL_SECONDS
        ),
        "CallSummaryText": call_summary_text,
    }
    kinesis.put_record(StreamName=CALL_DATA_STREAM_NAME, PartitionKey=call_id, Data=json.dumps(message))
    logger.info("Wrote ADD_SUMMARY event to KDS for edited summary: %s", call_id)

    return {"CallId": call_id, "Success": True}

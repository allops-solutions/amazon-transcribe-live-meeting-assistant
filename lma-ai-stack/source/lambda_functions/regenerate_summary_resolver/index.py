"""
Lambda function resolver for the regenerateSummary mutation.

Re-runs end-of-call summary generation for an already-ended meeting, reusing
the SummaryProfile / SummaryLanguage the meeting was originally launched
with (persisted on the Call record at start). Invokes the same
AsyncTranscriptSummaryOrchestrator the automatic end-of-call path uses, so
the new CallSummaryText arrives via the existing onUpdateCall subscription —
no separate result plumbing needed.

Copyright (c) 2025 Amazon.com
This file is licensed under the MIT License.
"""

import json
import logging
import os
from typing import Any, Dict

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")

EVENT_SOURCING_TABLE_NAME = os.environ["EVENT_SOURCING_TABLE_NAME"]
ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN = os.environ["ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN"]


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("regenerateSummary event: %s", json.dumps(event, default=str))

    call_id = event["arguments"]["input"]["CallId"]

    table = dynamodb.Table(EVENT_SOURCING_TABLE_NAME)
    pk = f"c#{call_id}"
    response = table.get_item(Key={"PK": pk, "SK": pk})
    call_item = response.get("Item")
    if not call_item:
        raise ValueError(f"No such meeting: {call_id}")

    # allOps policy: every authenticated user (Admin or User) can regenerate
    # every meeting's summary — no Owner/SharedWith check. See
    # getCall.response.vtl for the rationale.

    payload: Dict[str, Any] = {"CallId": call_id}
    if call_item.get("SummaryProfile"):
        payload["SummaryProfile"] = call_item["SummaryProfile"]
    if call_item.get("SummaryLanguage"):
        payload["SummaryLanguage"] = call_item["SummaryLanguage"]

    logger.info("Invoking summary orchestrator for regenerate: %s", json.dumps(payload))
    lambda_client.invoke(
        FunctionName=ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN,
        InvocationType="Event",
        Payload=json.dumps(payload),
    )

    return {"CallId": call_id, "Success": True}

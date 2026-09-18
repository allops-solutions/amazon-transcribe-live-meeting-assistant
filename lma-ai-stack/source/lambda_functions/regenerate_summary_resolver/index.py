"""
Lambda function resolver for the regenerateSummary mutation.

Generates (or regenerates) an ended meeting's summary on demand. With
SummaryGenerationMode=ON_DEMAND nothing is generated at meeting end, so this
is the only path that spends Bedrock tokens on a summary.

- Takes SummaryProfile / SummaryLanguage from the mutation input when given
  (empty string = stack-wide templates / template language), otherwise
  reuses what the Call record already has. Persists the choice on the Call
  so the meeting panel's form and later runs pick it up.
- Marks the Call SummaryStatus=IN_PROGRESS and refuses a second request while
  one is still fresh — a slow run cannot be doubled (and double-billed) from
  another tab or by another user. The ADD_SUMMARY event clears the status.
- Invokes the same AsyncTranscriptSummaryOrchestrator the automatic path uses,
  so the new CallSummaryText arrives via the existing onUpdateCall
  subscription.

Copyright (c) 2025 Amazon.com
This file is licensed under the MIT License.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")

EVENT_SOURCING_TABLE_NAME = os.environ["EVENT_SOURCING_TABLE_NAME"]
ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN = os.environ["ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN"]
# A run older than this is treated as dead (the orchestrator's own Timeout
# has passed) and may be replaced.
SUMMARY_IN_PROGRESS_TTL_SECONDS = int(os.environ.get("SUMMARY_IN_PROGRESS_TTL_SECONDS", "660"))

STATUS_IN_PROGRESS = "IN_PROGRESS"


def _seconds_since(iso_timestamp: str) -> float:
    started = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - started).total_seconds()


def _in_progress_age(call_item: Dict[str, Any]):
    """Seconds since a still-fresh run started, or None if none is running."""
    if call_item.get("SummaryStatus") != STATUS_IN_PROGRESS:
        return None
    requested_at = call_item.get("SummaryRequestedAt")
    if not requested_at:
        return None
    try:
        age = _seconds_since(requested_at)
    except ValueError:
        return None
    return age if age < SUMMARY_IN_PROGRESS_TTL_SECONDS else None


def _resolve_option(input_args: Dict[str, Any], call_item: Dict[str, Any], key: str):
    """Input wins when the key is present (even blank); otherwise the Call's value."""
    if key in input_args:
        return (input_args.get(key) or "").strip() or None
    return call_item.get(key) or None


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("regenerateSummary event: %s", json.dumps(event, default=str))

    input_args = event["arguments"]["input"]
    call_id = input_args["CallId"]

    table = dynamodb.Table(EVENT_SOURCING_TABLE_NAME)
    pk = f"c#{call_id}"
    response = table.get_item(Key={"PK": pk, "SK": pk})
    call_item = response.get("Item")
    if not call_item:
        raise ValueError(f"No such meeting: {call_id}")

    # allOps policy: every authenticated user (Admin or User) can generate
    # every meeting's summary — no Owner/SharedWith check. See
    # getCall.response.vtl for the rationale.

    age = _in_progress_age(call_item)
    if age is not None:
        raise ValueError(
            f"A summary is already being generated for this meeting "
            f"(started {int(age // 60)} min ago). Wait for it to finish."
        )

    summary_profile = _resolve_option(input_args, call_item, "SummaryProfile")
    summary_language = _resolve_option(input_args, call_item, "SummaryLanguage")
    requested_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Persist the choice and claim the run. Deliberately does not touch
    # UpdatedAt: the AppSync updateCall resolvers guard on it, and the
    # ADD_SUMMARY update that clears this status must still be "newer".
    set_parts = ["SummaryStatus = :status", "SummaryRequestedAt = :requested_at"]
    remove_parts = []
    values: Dict[str, Any] = {":status": STATUS_IN_PROGRESS, ":requested_at": requested_at}
    for key, value in (("SummaryProfile", summary_profile), ("SummaryLanguage", summary_language)):
        if value:
            set_parts.append(f"{key} = :{key.lower()}")
            values[f":{key.lower()}"] = value
        else:
            remove_parts.append(key)
    expression = "SET " + ", ".join(set_parts)
    if remove_parts:
        expression += " REMOVE " + ", ".join(remove_parts)
    table.update_item(
        Key={"PK": pk, "SK": pk},
        UpdateExpression=expression,
        ExpressionAttributeValues=values,
        ConditionExpression="attribute_exists(PK)",
    )

    payload: Dict[str, Any] = {"CallId": call_id}
    if summary_profile:
        payload["SummaryProfile"] = summary_profile
    if summary_language:
        payload["SummaryLanguage"] = summary_language

    logger.info("Invoking summary orchestrator: %s", json.dumps(payload))
    lambda_client.invoke(
        FunctionName=ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN,
        InvocationType="Event",
        Payload=json.dumps(payload),
    )

    return {"CallId": call_id, "Success": True}

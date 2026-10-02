#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

import json
from os import getenv
from typing import TYPE_CHECKING, Any, Dict

import boto3
from botocore.exceptions import ClientError

# third-party imports from Lambda layer
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.config import Config as BotoCoreConfig
from eventprocessor_utils import get_meeting_ttl

# pylint: enable=import-error
LOGGER = Logger(location="%(filename)s:%(lineno)d - %(funcName)s()")

if TYPE_CHECKING:
    from boto3 import Session as Boto3Session
    from mypy_boto3_kinesis.client import KinesisClient
    from mypy_boto3_lambda.client import LambdaClient
else:
    Boto3Session = object
    LambdaClient = object
    KinesisClient = object

BOTO3_SESSION: Boto3Session = boto3.Session()
# One attempt only: the summary Lambda has its own retries, and re-sending a
# synchronous invoke that is merely slow just runs (and bills) it twice.
CLIENT_CONFIG = BotoCoreConfig(
    read_timeout=int(getenv("BOTO_READ_TIMEOUT", "595")),
    retries={"mode": "standard", "max_attempts": 1},
)

LAMBDA_CLIENT: LambdaClient = BOTO3_SESSION.client(
    "lambda",
    config=CLIENT_CONFIG,
)
KINESIS_CLIENT: KinesisClient = BOTO3_SESSION.client("kinesis")
DYNAMODB_CLIENT = BOTO3_SESSION.client("dynamodb")

TRANSCRIPT_SUMMARY_FUNCTION_ARN = getenv("TRANSCRIPT_SUMMARY_FUNCTION_ARN", "")
CALL_DATA_STREAM_NAME = getenv("CALL_DATA_STREAM_NAME", "")
EVENT_SOURCING_TABLE_NAME = getenv("EVENT_SOURCING_TABLE_NAME", "")


def mark_summary_failed(message: Dict[str, Any]):
    """Release only the failed on-demand run; preserve the previous summary."""
    call_id = message.get("CallId")
    requested_at = message.get("SummaryRequestedAt")
    if not (call_id and requested_at):
        return
    try:
        DYNAMODB_CLIENT.update_item(
            TableName=EVENT_SOURCING_TABLE_NAME,
            Key={"PK": {"S": f"c#{call_id}"}, "SK": {"S": f"c#{call_id}"}},
            UpdateExpression="SET SummaryStatus = :failed",
            ConditionExpression="SummaryStatus = :running AND SummaryRequestedAt = :requested_at",
            ExpressionAttributeValues={
                ":failed": {"S": "FAILED"}, ":running": {"S": "IN_PROGRESS"},
                ":requested_at": {"S": requested_at},
            },
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            LOGGER.info("Summary claim was already completed or replaced for %s", call_id)
            return
        LOGGER.exception("Could not mark summary failed for %s", call_id)
    except Exception:
        LOGGER.exception("Could not mark summary failed for %s", call_id)


def get_call_summary(message: Dict[str, Any]):
    lambda_response = LAMBDA_CLIENT.invoke(
        FunctionName=TRANSCRIPT_SUMMARY_FUNCTION_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps(message),
    )
    try:
        message = json.loads(lambda_response.get("Payload").read().decode("utf-8"))
        if lambda_response.get("FunctionError"):
            raise RuntimeError("Summary Lambda failed; preserving existing summary.")
    except Exception as error:
        LOGGER.error(
            "Transcript summary result payload parsing exception. Lambda must return JSON object with (modified) input event fields",
            extra={"error": str(error)},
        )
        raise
    return message


def write_call_summary_to_kds(message: Dict[str, Any]):
    callId = message.get("CallId", None)
    expiresAfter = message.get("ExpiresAfter", get_meeting_ttl())

    new_message = dict(
        CallId=callId,
        EventType="ADD_SUMMARY",
        ExpiresAfter=expiresAfter,
        CallSummaryText=message["CallSummaryText"],
        # Clears the IN_PROGRESS claim regenerateSummary put on the Call.
        SummaryStatus="DONE",
    )

    if callId:
        try:
            KINESIS_CLIENT.put_record(
                StreamName=CALL_DATA_STREAM_NAME, PartitionKey=callId, Data=json.dumps(new_message)
            )
            LOGGER.info("Write ADD_SUMMARY event to KDS")
        except Exception as error:
            LOGGER.error(
                "Error writing ADD_SUMMARY event to KDS ",
                extra=error,
            )
            raise
    return


@LOGGER.inject_lambda_context
def handler(event, context: LambdaContext):
    # pylint: disable=unused-argument
    """Lambda handler"""
    LOGGER.debug("Transcript summary lambda event", extra={"event": event})

    data = json.loads(json.dumps(event))

    try:
        call_summary = get_call_summary(message=data)
    except Exception:
        LOGGER.exception("Summary Lambda invocation failed")
        mark_summary_failed(data)
        return

    LOGGER.debug("Call summary: ")
    LOGGER.debug(call_summary)
    if data.get("TranscriptOnly"):
        # End-of-call in ON_DEMAND mode: the summary Lambda only archived the
        # transcript for the Knowledge Base. No summary, nothing to publish.
        LOGGER.info("TranscriptOnly run finished for %s — no summary generated", data.get("CallId"))
        return
    if call_summary.get("error") or not call_summary.get("summary"):
        LOGGER.error("Summary generation failed; no ADD_SUMMARY event will be written.")
        mark_summary_failed(data)
        return
    data["CallSummaryText"] = call_summary["summary"]

    try:
        write_call_summary_to_kds(data)
    except Exception:
        mark_summary_failed(data)

# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Synthetic Kinesis (KDS) end-to-end pipeline probe.

Drives the *downstream* half of LMA's data flow that neither unit tests nor the
other integ-tests exercise:

    KDS CallDataStream -> CallEventProcessor Lambda -> DynamoDB (+ AppSync)

We put a realistic START (and a transcript segment + END) on the CallDataStream
using the same envelope the Virtual Participant / transcriber use — a plain JSON
record whose ``AccessToken`` is empty, so the processor falls back to using
``AgentId`` as the meeting Owner (the documented VP service-call path). Then we
poll the EventSourcing DynamoDB table for the call record the processor writes.

This is the probe that would have caught the gql 4 -> AppSync regression
(CallEventProcessor threw on every batch, so no call was ever written).

Used by test_kds_pipeline_creates_meeting in test_lma_integration.py, and can be
run standalone for debugging:

    AWS_PROFILE=default python integ-tests/kds_pipeline_probe.py lma-integtest1
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone

import boto3


def _stream_name(cfn, stack_name: str) -> str:
    outs = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["Outputs"]
    for o in outs:
        if o["OutputKey"] == "CallDataStreamName":
            return o["OutputValue"]
    raise RuntimeError("CallDataStreamName output not found on stack")


def _event_sourcing_table(cfn, ddb, stack_name: str) -> str:
    """Resolve the active AISTACK's EventSourcingTable name.

    The table isn't a top-level stack output, so find it from the AISTACK
    nested-stack resources.
    """
    # The AISTACK physical id is a resource of the parent stack.
    resources = cfn.list_stack_resources(StackName=stack_name)["StackResourceSummaries"]
    ai_stack = next(
        (r["PhysicalResourceId"] for r in resources if r["LogicalResourceId"] == "AISTACK"),
        None,
    )
    if not ai_stack:
        raise RuntimeError("AISTACK nested stack not found")
    ai_resources = cfn.list_stack_resources(StackName=ai_stack)["StackResourceSummaries"]
    for r in ai_resources:
        if r["LogicalResourceId"] == "EventSourcingTable":
            return r["PhysicalResourceId"]
    raise RuntimeError("EventSourcingTable not found in AISTACK")


def run_probe(stack_name: str, region: str | None = None, timeout_s: float = 90.0) -> dict:
    """Send a synthetic call through KDS and confirm it lands in DynamoDB.

    Returns a dict with ``call_id``, ``created`` (bool), and ``item`` (the DDB
    metadata item, if found).
    """
    session = boto3.Session(region_name=region)
    region = session.region_name
    cfn = session.client("cloudformation")
    kinesis = session.client("kinesis")
    ddb = session.client("dynamodb")

    stream = _stream_name(cfn, stack_name)
    table = _event_sourcing_table(cfn, ddb, stack_name)

    # Unique, obviously-synthetic CallId so we never collide with real meetings
    # and it's easy to spot / clean up.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    call_id = f"integ-test-kds-{stamp}"
    owner = "integ-test@lma.aws"
    now_iso = datetime.now(timezone.utc).isoformat()

    def put(record: dict) -> None:
        kinesis.put_record(
            StreamName=stream,
            Data=json.dumps(record).encode("utf-8"),
            PartitionKey=call_id,
        )

    # 1) START — empty AccessToken => processor uses AgentId as Owner.
    put({
        "EventType": "START",
        "CallId": call_id,
        "CustomerPhoneNumber": "Integ Test Caller",
        "SystemPhoneNumber": "LMA Integ Test",
        "AgentId": owner,
        "CreatedAt": now_iso,
        "AccessToken": "",
        "IdToken": "",
        "RefreshToken": "",
    })
    # NB: We send only START. START -> createCall is exactly the path the gql-4
    # regression broke (introspection failed, so no createCall, so no meeting in
    # the UI), and it flows through the same client/session/introspection and
    # DSL-mutation machinery as every other mutation. We deliberately skip
    # ADD_TRANSCRIPT_SEGMENT and END: the transcriber/VP populate sentiment and
    # summary analysis fields that a synthetic probe doesn't, so those paths
    # would log benign but noisy errors (Non-Null sentiment Float, end-of-call
    # "item put condition failure" retries) without adding meaningful coverage
    # of the regression. START-only keeps the probe deterministic and the
    # CallEventProcessor logs clean.

    # Poll DynamoDB for the call metadata item. CallEventProcessor writes the
    # call record with PK/SK = "c#<callId>".
    pk = f"c#{call_id}"
    deadline = time.monotonic() + timeout_s
    item = None
    while time.monotonic() < deadline:
        resp = ddb.get_item(
            TableName=table,
            Key={"PK": {"S": pk}, "SK": {"S": pk}},
        )
        item = resp.get("Item")
        if item:
            break
        time.sleep(3)

    return {
        "call_id": call_id,
        "owner": owner,
        "stream": stream,
        "table": table,
        "created": item is not None,
        "item": item,
    }


def cleanup(stack_name: str, call_id: str, region: str | None = None) -> None:
    """Delete only this synthetic owner's call partition and separate list rows.

    List rows use cls# date/shard partitions, not c#<callId>. Leaving them behind
    produces an Unknown meeting after the detail row is removed. Paginate both
    reads and propagate failures rather than claiming cleanup succeeded.
    """
    if not re.fullmatch(r"integ-test-kds-[0-9]{8}T[0-9]{6}", call_id):
        raise ValueError("Cleanup requires an exact synthetic KDS call ID")
    session = boto3.Session(region_name=region)
    cfn = session.client("cloudformation")
    ddb = session.client("dynamodb")
    table = _event_sourcing_table(cfn, ddb, stack_name)
    pk = f"c#{call_id}"
    owner = {"S": "integ-test@lma.aws"}
    detail = ddb.get_item(TableName=table, Key={"PK": {"S": pk}, "SK": {"S": pk}},
                          ConsistentRead=True).get("Item")
    if detail and detail.get("Owner") != owner:
        raise ValueError("Synthetic call owner does not match; refusing cleanup")
    listings = []
    scan = dict(TableName=table, ConsistentRead=True,
                FilterExpression="#call = :call AND #owner = :owner AND begins_with(PK, :prefix)",
                ExpressionAttributeNames={"#call": "CallId", "#owner": "Owner"},
                ExpressionAttributeValues={":call": {"S": call_id}, ":owner": owner,
                                           ":prefix": {"S": "cls#"}},
                ProjectionExpression="PK, SK")
    while True:
        page = ddb.scan(**scan)
        listings.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            break
        scan["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if detail or listings:
        query = dict(TableName=table, ConsistentRead=True, KeyConditionExpression="PK = :pk",
                     ExpressionAttributeValues={":pk": {"S": pk}})
        while True:
            page = ddb.query(**query)
            for item in page.get("Items", []):
                request = dict(TableName=table, Key={"PK": item["PK"], "SK": item["SK"]})
                if item["SK"] == {"S": pk}:
                    request.update(ConditionExpression="#owner = :owner",
                                   ExpressionAttributeNames={"#owner": "Owner"},
                                   ExpressionAttributeValues={":owner": owner})
                ddb.delete_item(**request)
            if not page.get("LastEvaluatedKey"):
                break
            query["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        for item in listings:
            ddb.delete_item(
                TableName=table,
                Key={"PK": item["PK"], "SK": item["SK"]},
                ConditionExpression="#call = :call AND #owner = :owner",
                ExpressionAttributeNames={"#call": "CallId", "#owner": "Owner"},
                ExpressionAttributeValues={":call": {"S": call_id}, ":owner": owner},
            )


if __name__ == "__main__":
    stack = sys.argv[1] if len(sys.argv) > 1 else "LMA"
    result = run_probe(stack)
    print(json.dumps({k: v for k, v in result.items() if k != "item"}, indent=2))
    print("MEETING CREATED" if result["created"] else "MEETING NOT CREATED (pipeline broken)")
    if result["created"]:
        cleanup(stack, result["call_id"])
        print("cleaned up synthetic call")
    sys.exit(0 if result["created"] else 1)

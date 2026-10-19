#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Real OAuth gateway lifecycle probe, restricted to a retained isolated dev stack.

Creates one temporary non-admin Cognito user with no invitation email. Password,
client secret and tokens stay in memory. Cancels its test VP and deletes the user.
This is an HTTP gateway test, not a Google Calendar or Quick UI flow test.
"""

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import boto3
import requests


def result_objects(value):
    """Unwrap AgentCore's text content and the Lambda API-style response."""
    if isinstance(value, str):
        try:
            yield from result_objects(json.loads(value))
        except (ValueError, TypeError):
            return
    elif isinstance(value, dict):
        if value.get("isError") or "error" in value:
            raise RuntimeError("Gateway tool reported an error")
        if "statusCode" in value and value["statusCode"] != 200:
            raise RuntimeError("Target rejected the tool request")
        yield value
        for key in ("result", "content", "body", "text"):
            if key in value:
                yield from result_objects(value[key])
    elif isinstance(value, list):
        for child in value:
            yield from result_objects(child)


def run(stack, owner_run, meeting_link):
    session = boto3.Session(profile_name="default", region_name="us-east-1")
    if session.client("sts").get_caller_identity()["Account"] != "135755363077":
        raise ValueError("Probe is restricted to the dev AWS account")
    if not owner_run.isdigit() or stack != "LMA-CI-" + owner_run[-6:]:
        raise ValueError(
            "Probe requires the exact isolated stack and full owning run ID"
        )
    cf = session.client("cloudformation")
    detail = cf.describe_stacks(StackName=stack)["Stacks"][0]
    tags = {tag["Key"]: tag["Value"] for tag in detail.get("Tags", [])}
    if tags.get("LmaCiManaged") != "true" or tags.get("LmaCiRun") != owner_run:
        raise ValueError("Isolated stack ownership mismatch")
    if detail["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
        raise ValueError("Wait for a healthy completed deployment")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in detail["Outputs"]}
    pool = outputs["MCPServerUserPoolId"]
    client = outputs["MCPServerClientId"]
    secret = outputs["MCPServerClientSecret"]
    endpoint = outputs["MCPServerEndpoint"]
    ai = cf.describe_stack_resource(StackName=stack, LogicalResourceId="AISTACK")[
        "StackResourceDetail"
    ]["PhysicalResourceId"]
    function = cf.describe_stack_resource(
        StackName=ai, LogicalResourceId="MCPServerAnalyticsFunction"
    )["StackResourceDetail"]["PhysicalResourceId"]
    table_name = session.client("lambda").get_function_configuration(
        FunctionName=function
    )["Environment"]["Variables"]["VP_TABLE_NAME"]
    table = session.resource("dynamodb").Table(table_name)
    cognito = session.client("cognito-idp")
    username = "integ-oauth-" + uuid.uuid4().hex + "@allops.co"
    password = secrets.token_urlsafe(36) + "Aa1!"
    uid = "integ-oauth-" + uuid.uuid4().hex + "@lma.test"
    created = False
    schedule_attempted = False
    cancelled = False
    token = None

    def call(tool, arguments):
        response = requests.post(
            endpoint,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "tools/call",
                "params": {
                    "name": "LMAAnalyticsTarget___" + tool,
                    "arguments": arguments,
                },
            },
            timeout=(10, 60),
        )
        if response.status_code != 200:
            raise RuntimeError("Gateway HTTP " + str(response.status_code))
        # Accept both JSON and standard MCP single-event SSE responses.
        payload = response.text
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            payload = "\n".join(
                line[5:].strip()
                for line in payload.splitlines()
                if line.startswith("data:")
            )
        objects = list(result_objects(json.loads(payload)))
        found = next((item for item in objects if "virtualParticipantId" in item), None)
        if not found:
            raise RuntimeError("Gateway did not return a VP tool result")
        return found

    def report(label, result):
        print(
            json.dumps(
                {
                    "test": label,
                    "passed": True,
                    **{
                        key: result.get(key)
                        for key in (
                            "virtualParticipantId",
                            "status",
                            "alreadyScheduled",
                            "notificationPending",
                        )
                    },
                }
            ),
            flush=True,
        )

    try:
        cognito.admin_create_user(
            UserPoolId=pool,
            Username=username,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": username},
                {"Name": "email_verified", "Value": "true"},
            ],
        )
        created = True
        cognito.admin_set_user_password(
            UserPoolId=pool, Username=username, Password=password, Permanent=True
        )
        groups = cognito.admin_list_groups_for_user(UserPoolId=pool, Username=username)[
            "Groups"
        ]
        if any(group["GroupName"] == "Admin" for group in groups):
            raise AssertionError(
                "Temporary OAuth test user must not be an administrator"
            )
        secret_hash = base64.b64encode(
            hmac.new(
                secret.encode(), (username + client).encode(), hashlib.sha256
            ).digest()
        ).decode()
        auth = cognito.initiate_auth(
            ClientId=client,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": username,
                "PASSWORD": password,
                "SECRET_HASH": secret_hash,
            },
        )
        token = auth["AuthenticationResult"]["AccessToken"]
        user = cognito.get_user(AccessToken=token)
        sub = next(
            item["Value"] for item in user["UserAttributes"] if item["Name"] == "sub"
        )
        start = datetime.now(UTC) + timedelta(hours=3)
        args = {
            "meetingName": "integ-oauth-gateway-lifecycle",
            "meetingPlatform": "Google Meet",
            "meetingId": meeting_link,
            "scheduledTime": start.isoformat(),
            "meetingLanguage": "Bosnian only",
            "calendarEventUid": uid,
        }
        schedule_attempted = True
        first = call("schedule_meeting", args)
        vid = first["virtualParticipantId"]
        if first["status"] != "SCHEDULED" or first["alreadyScheduled"]:
            raise AssertionError("Initial schedule was not new and pending")
        row = table.get_item(Key={"id": vid}, ConsistentRead=True)["Item"]
        if row["owner"] != sub or row["transcribeLanguageMode"] != "bs-BA":
            raise AssertionError(
                "VP does not belong to the real OAuth user or language is wrong"
            )
        report("OAuth schedule", first)
        duplicate = call("schedule_meeting", args)
        if (
            duplicate["virtualParticipantId"] != vid
            or not duplicate["alreadyScheduled"]
        ):
            raise AssertionError("Duplicate prevention failed")
        report("OAuth duplicate prevention", duplicate)
        update = call(
            "update_scheduled_meeting",
            {
                "scheduleAction": "UPDATE",
                "calendarEventUid": uid,
                "scheduledTime": (start + timedelta(minutes=30)).isoformat(),
            },
        )
        if update["virtualParticipantId"] != vid or update["status"] != "SCHEDULED":
            raise AssertionError("OAuth update failed")
        row = table.get_item(Key={"id": vid}, ConsistentRead=True)["Item"]
        if (
            row["meetingTime"] != int((start + timedelta(minutes=30)).timestamp())
            or row["transcribeLanguageMode"] != "bs-BA"
        ):
            raise AssertionError("Updated time/language was not saved correctly")
        report("OAuth update", update)
        final = call(
            "cancel_scheduled_meeting",
            {
                "scheduleAction": "CANCEL",
                "calendarEventUid": uid,
            },
        )
        if final["virtualParticipantId"] != vid or final["status"] != "CANCELLED":
            raise AssertionError("OAuth cancellation failed")
        if (
            table.get_item(Key={"id": vid}, ConsistentRead=True)["Item"]["status"]
            != "CANCELLED"
        ):
            raise AssertionError("Cancellation not saved in the database")
        cancelled = True
        report("OAuth cancellation", final)
        again = call(
            "cancel_scheduled_meeting",
            {
                "scheduleAction": "CANCEL",
                "calendarEventUid": uid,
            },
        )
        if again["virtualParticipantId"] != vid or not again["alreadyScheduled"]:
            raise AssertionError("Repeat cancellation failed")
        report("OAuth repeat cancellation", again)
    finally:
        try:
            if schedule_attempted and not cancelled and token:
                try:
                    cleanup = call(
                        "cancel_scheduled_meeting",
                        {
                            "scheduleAction": "CANCEL",
                            "calendarEventUid": uid,
                        },
                    )
                except (RuntimeError, ValueError, requests.RequestException):
                    raise RuntimeError("Could not verify test VP cleanup for " + uid) from None
                if cleanup["status"] != "CANCELLED":
                    raise RuntimeError("Test VP cleanup requires attention for " + uid)
        finally:
            if created:
                cognito.admin_delete_user(UserPoolId=pool, Username=username)
                print(
                    "Temporary OAuth test user deleted; no invitation email sent.",
                    flush=True,
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", required=True)
    parser.add_argument("--owner-run", required=True)
    parser.add_argument("--meeting-link", required=True)
    args = parser.parse_args()
    run(args.stack, args.owner_run, args.meeting_link)

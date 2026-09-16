# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the MicroVM VNC token Lambda's response shape.

Why this file exists: the VNC viewer failed with "Failed to connect:
{data: {createMicrovmVncToken: null}, errors: [...]}" even though the Lambda
logged "Minted VNC token" successfully. The mutation returned

    expiresAt: "None"

because the code did `str(response.get("expiresAt"))` — but
CreateMicrovmAuthToken returns ONLY `authToken` (verified against the service
model). The GraphQL field is `AWSDateTime!`, so AppSync could not serialize the
string "None" and nulled the ENTIRE parent object:

    Can't serialize value (/createMicrovmVncToken/expiresAt) :
    Unable to serialize `None` as a valid DateTime Object.

A valid auth token was discarded over a metadata field. These tests assert the
Lambda emits a real ISO-8601 UTC timestamp that AppSync can serialize.

No AWS calls: the MicroVM client and DynamoDB lookup are stubbed.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOKEN_DIR = REPO / "lma-ai-stack" / "source" / "lambda_functions" / "microvm_vnc_token"
SCHEMA = REPO / "lma-ai-stack" / "source" / "appsync" / "schema.graphql"
sys.path.insert(0, str(TOKEN_DIR))

# AWSDateTime requires an offset; AppSync accepts the "Z" form.
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def _code_only(path: Path) -> str:
    """Source with comments stripped.

    These assertions are about what the code DOES; the file deliberately
    documents the old broken expression in a comment, and matching that would
    make the test fail on its own explanation.
    """
    lines = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def test_schema_still_declares_expires_at_non_null() -> None:
    """The whole failure mode depends on this being non-null.

    If `expiresAt` were nullable, a bad value would null just that field. Because
    it is `AWSDateTime!`, AppSync nulls the parent object and the viewer loses
    the auth token entirely. Pinned so the reasoning above stays valid.
    """
    schema = SCHEMA.read_text()
    block = schema[schema.index("type MicrovmVncToken") :]
    block = block[: block.index("}")]
    assert "authToken: String!" in block
    assert "expiresAt: AWSDateTime!" in block


def test_source_does_not_read_expires_at_from_the_api_response() -> None:
    """CreateMicrovmAuthToken has exactly one output member: authToken.

    Reading a non-existent `expiresAt` is what produced the string "None".
    """
    src = _code_only(TOKEN_DIR / "index.py")
    assert 'response.get("expiresAt")' not in src, (
        "CreateMicrovmAuthToken returns only authToken; expiresAt must be "
        "computed from TOKEN_TTL_MINUTES"
    )
    assert "TOKEN_TTL_MINUTES" in src


def test_handler_returns_a_serializable_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end shape check through the real handler, AWS calls stubbed."""
    monkeypatch.setenv("VP_TABLE_NAME", "vp-table")
    monkeypatch.setenv("VP_TASK_REGISTRY_TABLE_NAME", "registry-table")
    # index.py creates boto3 clients at import time, and client construction
    # resolves an endpoint even though every call below is stubbed. CI has no
    # ambient AWS config, so without a region the IMPORT raises NoRegionError.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    import index as token_index

    class _FakeDynamo:
        def get_item(self, TableName, Key):  # noqa: N803 - boto3 kwarg names
            if TableName == "vp-table":
                return {"Item": {"Owner": {"S": "bob@example.com"}}}
            return {"Item": {"microvmId": {"S": "microvm-abc"}}}

    class _FakeMicrovms:
        def create_microvm_auth_token(self, **kwargs):
            # Exactly what the real API returns -- note: no expiresAt.
            assert kwargs["expiration_in_minutes"] == token_index.TOKEN_TTL_MINUTES
            return {"authToken": {"X-aws-proxy-auth": "a.token.value"}}

    monkeypatch.setattr(token_index, "dynamodb", _FakeDynamo())
    monkeypatch.setattr(token_index, "microvms", _FakeMicrovms())

    result = token_index.lambda_handler(
        {
            "arguments": {"vpId": "vp-1"},
            "identity": {"username": "bob@example.com", "claims": {}},
        },
        None,
    )

    assert result["authToken"] == "a.token.value"
    assert ISO_Z.match(result["expiresAt"]), (
        f"expiresAt must be an AppSync-serializable AWSDateTime, got "
        f"{result['expiresAt']!r}"
    )
    # Must be in the future, or the viewer would treat the token as expired.
    parsed = datetime.fromisoformat(result["expiresAt"].replace("Z", "+00:00"))
    assert parsed > datetime.now(timezone.utc)


def test_expires_at_is_never_the_string_none() -> None:
    """A regression guard on the exact observed bad value."""
    src = _code_only(TOKEN_DIR / "index.py")
    assert "str(expires_at)" not in src


def test_ecs_launch_type_mints_a_vp_scoped_hmac_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """No MicroVM registered + an ECS-shaped vncEndpoint -> HMAC token, not an error.

    Before this branch existed, this was the "may be starting, or running on
    an ECS launch type" exception path for every ECS/Fargate VP - the viewer
    fell back to the caller's own raw Cognito ID token instead (vncConnection.js),
    which is the cross-user VNC escalation this change closes. See
    LMA-HANDOFF.md's VNC section.
    """
    monkeypatch.setenv("VP_TABLE_NAME", "vp-table")
    monkeypatch.setenv("VP_TASK_REGISTRY_TABLE_NAME", "registry-table")
    monkeypatch.setenv("VNC_HMAC_SECRET", "unit-test-secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    import importlib

    import index as token_index

    importlib.reload(token_index)  # pick up VNC_HMAC_SECRET set just above

    class _FakeDynamo:
        def get_item(self, TableName, Key):  # noqa: N803 - boto3 kwarg names
            if TableName == "vp-table":
                return {
                    "Item": {
                        "Owner": {"S": "bob@example.com"},
                        "vncEndpoint": {"S": "wss://d123abc.cloudfront.net/vnc/vp-1"},
                    }
                }
            return {"Item": {}}  # no microvmId registered - ECS/Fargate case

    monkeypatch.setattr(token_index, "dynamodb", _FakeDynamo())

    result = token_index.lambda_handler(
        {
            "arguments": {"vpId": "vp-1"},
            "identity": {"username": "bob@example.com", "claims": {}},
        },
        None,
    )

    assert ISO_Z.match(result["expiresAt"])
    parts = result["authToken"].split(".")
    assert len(parts) == 3, f"expected <vpId>.<expiry>.<hmac>, got {result['authToken']!r}"
    token_vp_id, expiry_str, mac_hex = parts
    assert token_vp_id == "vp-1"
    expected_mac = hmac.new(
        b"unit-test-secret", f"{token_vp_id}.{expiry_str}".encode(), hashlib.sha256
    ).hexdigest()
    assert mac_hex == expected_mac, "token must verify against the same scheme the edge function uses"


def test_ecs_token_denied_for_non_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner/SharedWith/Admin check applies before the ECS branch is even reached."""
    monkeypatch.setenv("VP_TABLE_NAME", "vp-table")
    monkeypatch.setenv("VP_TASK_REGISTRY_TABLE_NAME", "registry-table")
    monkeypatch.setenv("VNC_HMAC_SECRET", "unit-test-secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    import importlib

    import index as token_index

    importlib.reload(token_index)

    class _FakeDynamo:
        def get_item(self, TableName, Key):  # noqa: N803 - boto3 kwarg names
            if TableName == "vp-table":
                return {
                    "Item": {
                        "Owner": {"S": "bob@example.com"},
                        "vncEndpoint": {"S": "wss://d123abc.cloudfront.net/vnc/vp-1"},
                    }
                }
            return {"Item": {}}

    monkeypatch.setattr(token_index, "dynamodb", _FakeDynamo())

    with pytest.raises(Exception, match="Not authorized"):
        token_index.lambda_handler(
            {
                "arguments": {"vpId": "vp-1"},
                "identity": {"username": "eve@example.com", "claims": {}},
            },
            None,
        )


def test_vp_not_started_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """No microvmId and no vncEndpoint yet -> still the "may be starting" error, not a token."""
    monkeypatch.setenv("VP_TABLE_NAME", "vp-table")
    monkeypatch.setenv("VP_TASK_REGISTRY_TABLE_NAME", "registry-table")
    monkeypatch.setenv("VNC_HMAC_SECRET", "unit-test-secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    import importlib

    import index as token_index

    importlib.reload(token_index)

    class _FakeDynamo:
        def get_item(self, TableName, Key):  # noqa: N803 - boto3 kwarg names
            if TableName == "vp-table":
                return {"Item": {"Owner": {"S": "bob@example.com"}}}  # no vncEndpoint yet
            return {"Item": {}}

    monkeypatch.setattr(token_index, "dynamodb", _FakeDynamo())

    with pytest.raises(Exception, match="may be starting"):
        token_index.lambda_handler(
            {
                "arguments": {"vpId": "vp-1"},
                "identity": {"username": "bob@example.com", "claims": {}},
            },
            None,
        )

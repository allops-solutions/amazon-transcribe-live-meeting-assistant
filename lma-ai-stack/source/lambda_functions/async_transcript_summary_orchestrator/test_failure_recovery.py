#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Offline failure-destination tests: no model, clock wait or AWS requests."""

import ast
from pathlib import Path
from typing import Any, Dict
from unittest.mock import Mock

import pytest
from botocore.config import Config
from botocore.exceptions import ClientError


def functions(names, **overrides):
    """Load the real handlers without importing deployment-only layer modules."""
    tree = ast.parse(Path(__file__).with_name("lambda_function.py").read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    namespace = {
        "Any": Any,
        "Dict": Dict,
        "LambdaContext": object,
        "ClientError": ClientError,
        "LOGGER": Mock(),
        "EVENT_SOURCING_TABLE_NAME": "calls",
        **overrides,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "lambda_function.py", "exec"), namespace)
    return namespace


def test_model_worker_invoke_has_no_hidden_sdk_retries():
    tree = ast.parse(Path(__file__).with_name("lambda_function.py").read_text())
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CLIENT_CONFIG" for t in node.targets)
    )
    value = eval(
        compile(ast.Expression(assignment.value), "config", "eval"),
        {"BotoCoreConfig": Config, "getenv": lambda key, default: default},
    )
    assert value.retries == {"mode": "standard", "total_max_attempts": 1}


@pytest.mark.parametrize("condition", ["RetriesExhausted", "EventAgeExceeded"])
def test_hard_timeout_or_expired_queue_destination_marks_only_original_request_failed(condition):
    ddb = Mock()
    ns = functions({"failure_handler", "mark_summary_failed"}, DYNAMODB_CLIENT=ddb)
    ns["failure_handler"](
        {
            "requestContext": {"condition": condition},
            "requestPayload": {"CallId": "meeting", "SummaryRequestedAt": "run-1"},
        },
        None,
    )
    args = ddb.update_item.call_args.kwargs
    assert args["Key"] == {"PK": {"S": "c#meeting"}, "SK": {"S": "c#meeting"}}
    assert args["ExpressionAttributeValues"][":requested_at"] == {"S": "run-1"}
    assert args["ExpressionAttributeValues"][":failed"] == {"S": "FAILED"}
    assert (
        args["ConditionExpression"]
        == "SummaryStatus = :running AND SummaryRequestedAt = :requested_at"
    )
    assert args["UpdateExpression"] == "SET SummaryStatus = :failed"


def test_delayed_or_repeated_failure_cannot_modify_newer_or_completed_request():
    ddb = Mock()
    ddb.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )
    ns = functions({"failure_handler", "mark_summary_failed"}, DYNAMODB_CLIENT=ddb)
    event = {
        "requestContext": {"condition": "RetriesExhausted"},
        "requestPayload": {"CallId": "meeting", "SummaryRequestedAt": "old-run"},
    }
    ns["failure_handler"](event, None)
    ns["failure_handler"](event, None)
    assert ddb.update_item.call_count == 2
    assert "CallSummaryText" not in ddb.update_item.call_args.kwargs["UpdateExpression"]


def test_recovery_storage_failure_is_retriable_but_never_invokes_a_model():
    ddb = Mock()
    ddb.update_item.side_effect = RuntimeError("storage unavailable")
    ns = functions({"failure_handler", "mark_summary_failed"}, DYNAMODB_CLIENT=ddb)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        ns["failure_handler"](
            {
                "requestContext": {"condition": "RetriesExhausted"},
                "requestPayload": {"CallId": "meeting", "SummaryRequestedAt": "run-1"},
            },
            None,
        )


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"CallId": "meeting"},
        {"requestPayload": {"CallId": "meeting"}},
        {"requestContext": {"condition": "RetriesExhausted"}, "requestPayload": "invalid"},
        {"requestContext": {"condition": "Success"}, "requestPayload": {"CallId": "meeting"}},
        {
            "requestContext": {"condition": "RetriesExhausted"},
            "requestPayload": {"CallId": "meeting"},
        },
    ],
)
def test_invalid_or_untracked_requests_do_not_write(event):
    ddb = Mock()
    ns = functions({"failure_handler", "mark_summary_failed"}, DYNAMODB_CLIENT=ddb)
    ns["failure_handler"](event, None)
    ddb.update_item.assert_not_called()

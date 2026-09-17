# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the Lambda@Edge VNC auth code embedded in edge_auth_deployer.

Why this file exists: EDGE_FUNCTION_CODE (source/lambda_functions/
edge_auth_deployer/index.py) is the entire authentication boundary for the
ECS/Fargate VNC path - it is the only thing standing between "anyone who can
reach the CloudFront URL" and interactive control of a Virtual Participant's
browser (the ALB behind it has no auth of its own, and x11vnc runs with
-nopw). Two real bugs were found and fixed here on 2026-09-16/17:

  1. The edge function decoded a Cognito JWT's claims but never verified the
     signature - a hand-crafted token with plausible-looking claims worked
     with no AWS account at all.
  2. Even after (1) was fixed, and after switching to VP-scoped HMAC tokens
     (this file's current scheme), vp_id was extracted with
     uri[len('/vnc/'):].split('/')[0] - so /vnc/vpA/../vpB verified fine
     against a real vpA token without the URI actually being /vnc/vpA.

Both were caught by hand before landing, using a throwaway, uncommitted
script that exec()'d EDGE_FUNCTION_CODE and asserted the same things this
file asserts - but that script never became a regression test, so nothing
would have caught either bug coming back. This file is that harness, made
permanent.

EDGE_FUNCTION_CODE is deployed as a *separate* Lambda (Lambda@Edge, in
us-east-1, built and published by a custom resource - see
create_edge_function/update_edge_function) from the one this test file's
module lives next to. It cannot be imported normally: it is a string
constant containing an entire second program, with HMAC_SECRET_PLACEHOLDER
substituted in at deploy time. So these tests extract the string via `ast`,
substitute a throwaway test secret, and exec() it into an isolated
namespace - the closest thing to importing it that its own deployment
mechanism allows.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOYER_SRC = (
    REPO / "lma-ai-stack" / "source" / "lambda_functions" / "edge_auth_deployer" / "index.py"
)

TEST_SECRET = "unit-test-hmac-secret-do-not-use-in-prod"


def _load_edge_function_code() -> str:
    """Extract the EDGE_FUNCTION_CODE string constant via ast.

    Not a regex/string search: EDGE_FUNCTION_CODE is a large triple-quoted
    literal containing its own quotes and escapes, and the outer file is
    parsed as Python here for the same reason the deployer's own
    .replace()-based placeholder substitution works - it is just a string.
    """
    tree = ast.parse(DEPLOYER_SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == "EDGE_FUNCTION_CODE" for target in node.targets
        ):
            return node.value.value
    raise AssertionError("EDGE_FUNCTION_CODE not found in edge_auth_deployer/index.py")


@pytest.fixture(scope="module")
def edge_ns() -> dict:
    """The edge function's own namespace, with a known test secret baked in.

    Mirrors what create_edge_function/update_edge_function do at deploy time
    (code.replace("HMAC_SECRET_PLACEHOLDER", hmac_secret)), just against a
    fixed test value instead of one fetched from Secrets Manager.
    """
    code = _load_edge_function_code()
    compile(code, "<edge_function>", "exec")  # fails loudly on a syntax error
    assert "HMAC_SECRET_PLACEHOLDER" in code, "placeholder substitution below would be a no-op"
    code = code.replace("HMAC_SECRET_PLACEHOLDER", TEST_SECRET)
    ns: dict = {}
    exec(code, ns)  # noqa: S102 - the "module" under test, not untrusted input
    return ns


def _make_token(vp_id: str, expiry: int, secret: str = TEST_SECRET) -> str:
    mac = hmac.new(secret.encode(), f"{vp_id}.{expiry}".encode(), hashlib.sha256).hexdigest()
    return f"{vp_id}.{expiry}.{mac}"


def _cf_event(uri: str, querystring: str = "") -> dict:
    """A minimal CloudFront viewer-request event, shaped like what
    Lambda@Edge actually delivers (event['Records'][0]['cf']['request'])."""
    return {
        "Records": [
            {
                "cf": {
                    "request": {
                        "uri": uri,
                        "querystring": querystring,
                        "method": "GET",
                        "headers": {},
                    }
                }
            }
        ]
    }


FUTURE = lambda: int(time.time()) + 3600  # noqa: E731
PAST = lambda: int(time.time()) - 10  # noqa: E731


# --------------------------------------------------------------------------
# verify_vp_scoped_token - the actual auth check
# --------------------------------------------------------------------------


def test_valid_token_for_the_right_vp_is_accepted(edge_ns: dict) -> None:
    token = _make_token("vp-abc", FUTURE())
    assert edge_ns["verify_vp_scoped_token"](token, "vp-abc") is True


def test_token_scoped_to_a_different_vp_is_rejected(edge_ns: dict) -> None:
    """The core fix: a token minted for one VP must not work for another."""
    token = _make_token("vp-OTHER", FUTURE())
    assert edge_ns["verify_vp_scoped_token"](token, "vp-abc") is False


def test_expired_token_is_rejected(edge_ns: dict) -> None:
    token = _make_token("vp-abc", PAST())
    assert edge_ns["verify_vp_scoped_token"](token, "vp-abc") is False


def test_tampered_mac_is_rejected(edge_ns: dict) -> None:
    vp_id, expiry, _mac = _make_token("vp-abc", FUTURE()).split(".")
    tampered = f"{vp_id}.{expiry}.{'0' * 64}"
    assert edge_ns["verify_vp_scoped_token"](tampered, "vp-abc") is False


def test_token_signed_with_a_different_secret_is_rejected(edge_ns: dict) -> None:
    """Simulates a forgery attempt by someone who doesn't hold VNC_HMAC_SECRET."""
    token = _make_token("vp-abc", FUTURE(), secret="attacker-does-not-have-the-real-secret")
    assert edge_ns["verify_vp_scoped_token"](token, "vp-abc") is False


@pytest.mark.parametrize("malformed", ["not-a-token", "a.b", "a.b.c.d", "", "a.notanumber.deadbeef"])
def test_malformed_tokens_are_rejected(edge_ns: dict, malformed: str) -> None:
    assert edge_ns["verify_vp_scoped_token"](malformed, "vp-abc") is False


# --------------------------------------------------------------------------
# VP_PATH_RE / lambda_handler path parsing - added after review found that
# vp_id extraction did not require the URI to be exactly /vnc/<vpId>
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        "/vnc/vpA/../vpB",  # the exact case review found: verifies as vpA, routes anywhere
        "/vnc/vpA/",  # trailing slash
        "/vnc/vpA/extra",  # extra segment
        "/vnc/",  # empty vpId
        "/vnc/vp a",  # space - not in the allowed charset
    ],
)
def test_lambda_handler_rejects_non_exact_vnc_paths(edge_ns: dict, uri: str) -> None:
    """These must 400 before the token is even looked at.

    Why this matters beyond "malformed input": the ALB's listener rules are
    exact-path matches per vpId (see status-manager.ts). A URI CloudFront
    forwards unchanged but that doesn't exactly match /vnc/<vpId> falls
    through to the shared default target group, which round-robins across
    every running VP's task - so a token holder for vpA could land on an
    arbitrary VP's container if this weren't rejected here first.
    """
    token = _make_token("vpA", FUTURE())
    result = edge_ns["lambda_handler"](_cf_event(uri, f"token={token}"), None)
    assert result["status"] == "400", f"expected 400 for {uri!r}, got {result}"


def test_lambda_handler_allows_exact_valid_path_and_token(edge_ns: dict) -> None:
    """The end-to-end happy path: passthrough (the original request dict)."""
    token = _make_token("vpA", FUTURE())
    event = _cf_event("/vnc/vpA", f"token={token}")
    result = edge_ns["lambda_handler"](event, None)
    assert result is event["Records"][0]["cf"]["request"]


def test_lambda_handler_allows_uuid_shaped_vpid(edge_ns: dict) -> None:
    """Real VP ids are $util.autoId() UUIDs (hex + hyphens)."""
    vp_id = "550e8400-e29b-41d4-a716-446655440000"
    token = _make_token(vp_id, FUTURE())
    event = _cf_event(f"/vnc/{vp_id}", f"token={token}")
    result = edge_ns["lambda_handler"](event, None)
    assert result is event["Records"][0]["cf"]["request"]


def test_lambda_handler_ignores_non_vnc_paths(edge_ns: dict) -> None:
    """Everything except /vnc/* must pass through untouched - this function
    also gates nothing else on the distribution."""
    event = _cf_event("/index.html")
    result = edge_ns["lambda_handler"](event, None)
    assert result is event["Records"][0]["cf"]["request"]


def test_lambda_handler_401s_when_token_is_missing(edge_ns: dict) -> None:
    event = _cf_event("/vnc/vpA")  # no querystring at all
    result = edge_ns["lambda_handler"](event, None)
    assert result["status"] == "401"


def test_lambda_handler_403s_for_a_valid_shape_but_wrong_vp_token(edge_ns: dict) -> None:
    """A well-formed, correctly-signed token for a DIFFERENT vp must not
    authorize this vp's path - this is the cross-user escalation itself."""
    token = _make_token("vp-someone-elses", FUTURE())
    event = _cf_event("/vnc/vpA", f"token={token}")
    result = edge_ns["lambda_handler"](event, None)
    assert result["status"] == "403"

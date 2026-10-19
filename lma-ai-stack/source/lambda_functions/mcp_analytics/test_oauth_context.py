#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Real-signature token and interceptor-contract regression tests (no AWS)."""

import copy
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import jwt
import oauth_context as auth
from cryptography.hazmat.primitives.asymmetric import rsa


class TokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "MCP_OAUTH_ISSUER": "https://example.test/pool",
                "MCP_OAUTH_CLIENT_ID": "mcp-client",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.jwks = patch.object(auth, "_keys")
        self.keys = self.jwks.start()
        self.addCleanup(self.jwks.stop)
        self.keys.return_value.get_signing_key_from_jwt.return_value = SimpleNamespace(
            key=self.key.public_key()
        )
        now = int(time.time())
        self.claims = {
            "sub": "user1",
            "iss": "https://example.test/pool",
            "exp": now + 600,
            "iat": now,
            "token_use": "access",
            "client_id": "mcp-client",
            "cognito:groups": ["Users"],
        }

    def token(self, claims=None, key=None, algorithm="RS256"):
        return jwt.encode(claims or self.claims, key or self.key, algorithm=algorithm)

    def test_valid_access_token(self):
        self.assertEqual(auth.verified_claims(self.token())["sub"], "user1")
        self.keys.assert_called_once_with("https://example.test/pool")

    def test_admin_group_only_from_signed_token(self):
        self.claims["cognito:groups"] = ["Admin"]
        self.assertEqual(auth.verified_claims(self.token())["cognito:groups"], ["Admin"])

    def test_wrong_issuer_client_type_and_expiry_are_rejected(self):
        for field, value in [
            ("iss", "https://evil.test"),
            ("client_id", "other"),
            ("token_use", "id"),
            ("exp", int(time.time()) - 60),
        ]:
            with self.subTest(field=field), self.assertRaises((ValueError, jwt.PyJWTError)):
                auth.verified_claims(self.token(dict(self.claims, **{field: value})))

    def test_required_claims_are_mandatory(self):
        for field in ("sub", "iss", "exp", "iat", "token_use", "client_id"):
            claims = dict(self.claims)
            del claims[field]
            with self.subTest(field=field), self.assertRaises(jwt.PyJWTError):
                auth.verified_claims(self.token(claims))

    def test_wrong_signature_and_algorithm_are_rejected(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        for token in (
            self.token(key=other),
            self.token(key="not-a-real-secret" * 3, algorithm="HS256"),
        ):
            with self.assertRaises(jwt.PyJWTError):
                auth.verified_claims(token)

    def test_missing_malformed_and_oversized_tokens_are_rejected(self):
        for token in (None, "", {}, "bad.token", "x" * 16385):
            with (
                self.subTest(token_type=type(token)),
                self.assertRaises((ValueError, jwt.PyJWTError)),
            ):
                auth.verified_claims(token)


class InterceptorTests(unittest.TestCase):
    def event(self, method="tools/call", headers=None, arguments=None):
        return {
            "interceptorInputVersion": "1.0",
            "mcp": {
                "gatewayRequest": {
                    "headers": headers
                    if headers is not None
                    else {"Authorization": "Bearer actual-token"},
                    "body": {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": {
                            "name": "LMAAnalyticsTarget___list_meetings",
                            "arguments": arguments or {"limit": 5},
                        },
                    },
                }
            },
        }

    def test_real_header_overwrites_all_forged_context(self):
        event = self.event(
            arguments={
                "limit": 5,
                "_lmaCallerToken": "forged",
                "requestContext": {"admin": True},
                "httpMethod": "POST",
                "body": "forged",
            }
        )
        original = copy.deepcopy(event)
        out = auth.intercept_request(event)
        args = out["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
        self.assertEqual(args, {"limit": 5, "_lmaCallerToken": "actual-token"})
        self.assertEqual(event, original)
        self.assertEqual(auth.intercept_request(event), out)

    def test_header_case_insensitive(self):
        out = auth.intercept_request(self.event(headers={"authorization": "bearer actual-token"}))
        self.assertEqual(
            out["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"][
                auth.CALLER_TOKEN
            ],
            "actual-token",
        )

    def test_missing_header_short_circuits_without_target_call(self):
        for headers in ({}, {"Authorization": "Basic something"}):
            out = auth.intercept_request(self.event(headers=headers))
            self.assertEqual(out["mcp"]["transformedGatewayResponse"]["statusCode"], 403)
            self.assertNotIn("transformedGatewayRequest", out["mcp"])

    def test_discovery_does_not_receive_token_argument(self):
        event = self.event(method="tools/list")
        out = auth.intercept_request(event)
        self.assertEqual(
            out["mcp"]["transformedGatewayRequest"]["body"], event["mcp"]["gatewayRequest"]["body"]
        )


if __name__ == "__main__":
    unittest.main()

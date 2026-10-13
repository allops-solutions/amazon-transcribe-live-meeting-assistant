#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Forward and verify the OAuth caller on AgentCore Lambda tool invocations."""

import copy
import os
from functools import lru_cache

import jwt

CALLER_TOKEN = "_lmaCallerToken"


@lru_cache(maxsize=4)
def _keys(issuer):
    return jwt.PyJWKClient(issuer + "/.well-known/jwks.json", timeout=5)


def verified_claims(token):
    """Trust only a signed, unexpired access token for this pool and MCP client."""
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise ValueError("Missing OAuth caller token")
    issuer = os.environ["MCP_OAUTH_ISSUER"]
    client = os.environ["MCP_OAUTH_CLIENT_ID"]
    key = _keys(issuer).get_signing_key_from_jwt(token).key
    claims = jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        issuer=issuer,
        options={
            "require": ["exp", "iat", "iss", "sub", "token_use", "client_id"],
            "verify_aud": False,
        },
    )
    if claims["token_use"] != "access" or claims["client_id"] != client:
        raise ValueError("Wrong OAuth token type or client")
    if not isinstance(claims["sub"], str) or not claims["sub"]:
        raise ValueError("Missing OAuth caller identity")
    return claims


def intercept_request(event):
    """Replace any supplied identity with the gateway's actual bearer token.

    AgentCore authenticates the header before invoking this interceptor. The
    target independently verifies the forwarded token; arguments alone can never
    grant identity or administrator membership. Never log headers or this body.
    """
    request = event["mcp"]["gatewayRequest"]
    body = copy.deepcopy(request["body"])
    if body.get("method") == "tools/call":
        auth = next(
            (v for k, v in request.get("headers", {}).items() if k.lower() == "authorization"), ""
        )
        if not isinstance(auth, str) or not auth.lower().startswith("bearer "):
            return {
                "interceptorOutputVersion": "1.0",
                "mcp": {
                    "transformedGatewayResponse": {
                        "statusCode": 403,
                        "body": {
                            "jsonrpc": "2.0",
                            "id": body.get("id"),
                            "error": {"code": -32001, "message": "OAuth identity required"},
                        },
                    }
                },
            }
        arguments = body["params"].setdefault("arguments", {})
        # These are integration metadata, never caller-selected tool arguments.
        for field in ("requestContext", "httpMethod", "body", CALLER_TOKEN):
            arguments.pop(field, None)
        arguments[CALLER_TOKEN] = auth.split(" ", 1)[1].strip()
    return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayRequest": {"body": body}}}

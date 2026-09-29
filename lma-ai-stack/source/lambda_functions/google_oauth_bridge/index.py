"""Adapt Google's CloudFront redirect to Cognito's OIDC flow.

Cognito validates state/nonce and Google tokens, and issues the LMA session.
This adapter neither mints tokens nor modifies users or meeting data.
"""
import base64
import hmac
import json
import os
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import boto3

ssm = boto3.client("ssm")


class NoGoogleTokenRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward client credentials to a redirected token endpoint.
        raise HTTPError(req.full_url, code, "Token endpoint redirect rejected", headers, fp)


google_http = build_opener(NoGoogleTokenRedirects())


def workspace_domain():
    domain = os.environ["ALLOWED_WORKSPACE_DOMAIN"].strip().lower()
    if not domain:
        raise ValueError("Workspace domain must be configured")
    return domain


def require_workspace_token(raw_response):
    # Inspect ONLY Google's authenticated, direct HTTPS token response, never
    # a browser-supplied JWT/hd parameter. Cognito subsequently validates the
    # unchanged token's signature, issuer, audience, expiry and flow bindings.
    token_response = json.loads(raw_response)
    if not isinstance(token_response, dict):
        raise ValueError("Invalid token response")
    token = token_response.get("id_token")
    if not isinstance(token, str):
        raise ValueError("Missing Google identity token")
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError("Invalid Google identity token")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.b64decode(payload, altchars=b"-_", validate=True).decode("utf-8"))
    if not isinstance(claims, dict) or claims.get("hd") != workspace_domain():
        raise ValueError("Company Workspace membership is required")


def response(status, body="", headers=None):
    return {
        "statusCode": status,
        "headers": {
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            **(headers or {}),
        },
        "body": body,
    }


def web_url():
    # Runtime lookup avoids CloudFront -> API -> Lambda -> CloudFront CFN cycle.
    value = ssm.get_parameter(Name=os.environ["LMA_SETTINGS_PARAMETER"])["Parameter"]["Value"]
    endpoint = json.loads(value)["CloudFrontEndpoint"].rstrip("/")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("Invalid application endpoint")
    return endpoint


def single_values(raw):
    values = parse_qs(raw, keep_blank_values=True)
    if any(len(v) != 1 for v in values.values()):
        raise ValueError("Duplicate OAuth parameters")
    return {key: value[0] for key, value in values.items()}


def lambda_handler(event, context):
    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    try:
        client_id = os.environ["GOOGLE_CLIENT_ID"]
        redirect_uri = web_url() + "/oauth2/idpresponse"
        cognito_callback = os.environ["COGNITO_LOGIN_URL"] + "/oauth2/idpresponse"
        if path == "/oauth2/google/authorize" and method == "GET":
            params = single_values(event.get("rawQueryString", ""))
            if (params.get("client_id") != client_id
                    or params.get("redirect_uri") != cognito_callback
                    or params.get("response_type") != "code"
                    or not params.get("state")):
                return response(400, "Invalid authorization request")
            # Cognito's upstream code-flow request can omit nonce. Preserve
            # state and any nonce/PKCE values it supplies; never invent them.
            params["redirect_uri"] = redirect_uri
            # Account-picker hint only; the returned token claim is enforced below.
            params["hd"] = workspace_domain()
            return response(302, headers={
                "Location": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
            })
        if path == "/oauth2/idpresponse" and method == "GET":
            params = single_values(event.get("rawQueryString", ""))
            if not params.get("state") or not (params.get("code") or params.get("error")):
                return response(400, "Invalid sign-in response")
            allowed = {k: v for k, v in params.items() if k in ("code", "state", "error")}
            # Destination is fixed; Cognito validates its own state and cookie.
            return response(302, headers={
                "Location": cognito_callback + "?" + urlencode(allowed)
            })
        if path == "/oauth2/google/token" and method == "POST":
            body = event.get("body") or ""
            if event.get("isBase64Encoded"):
                body = base64.b64decode(body, validate=True).decode("utf-8")
            params = single_values(body)
            headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
            if headers.get("authorization", "").startswith("Basic "):
                credentials = base64.b64decode(
                    headers["authorization"][6:], validate=True).decode("utf-8")
                supplied_id, supplied_secret = map(unquote, credentials.split(":", 1))
            else:
                supplied_id = params.get("client_id", "")
                supplied_secret = params.get("client_secret", "")
            if (not hmac.compare_digest(supplied_id, client_id)
                    or not hmac.compare_digest(supplied_secret, os.environ["GOOGLE_CLIENT_SECRET"])):
                return response(401, '{"error":"invalid_client"}',
                                {"Content-Type": "application/json"})
            if (params.get("grant_type") != "authorization_code" or not params.get("code")
                    or params.get("redirect_uri") != cognito_callback):
                return response(400, '{"error":"invalid_request"}',
                                {"Content-Type": "application/json"})
            params.update(client_id=client_id, client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
                          redirect_uri=redirect_uri)
            request = Request("https://oauth2.googleapis.com/token",
                              data=urlencode(params).encode("utf-8"),
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
            with google_http.open(request, timeout=15) as upstream:
                raw_response = upstream.read().decode("utf-8")
            try:
                require_workspace_token(raw_response)
            except (ValueError, KeyError, UnicodeError):
                # Do not expose rejected claims, authorization codes or tokens.
                return response(400, '{"error":"invalid_grant"}',
                                {"Content-Type": "application/json"})
            return response(200, raw_response, {"Content-Type": "application/json"})
        return response(404, "Not found")
    except HTTPError:
        return response(400, '{"error":"invalid_grant"}', {"Content-Type": "application/json"})
    except (ValueError, KeyError, UnicodeError):
        return response(400, "Invalid OAuth request")
    except Exception:
        # Never log authorization codes, secrets, upstream responses or tokens.
        return response(503, "Sign-in is temporarily unavailable")

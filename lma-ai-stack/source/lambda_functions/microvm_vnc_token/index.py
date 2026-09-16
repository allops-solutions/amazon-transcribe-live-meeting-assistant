# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Mint a short-lived MicroVM auth token for a VP's noVNC port.

Authorization: the caller must own (or have been shared) the VP, or be an
Admin. This is a DELIBERATE EXCEPTION to the allOps "every user sees every
meeting" policy (see getCall.response.vtl) — that policy is about read
access to meeting data, but a VNC session is interactive control, not a
read. The client's viewOnly toggle (VNCViewer.jsx) is a UI convenience, not
a security boundary: whoever holds a valid token can drive the browser,
which is signed into the launching user's own Zoom/Teams/Webex credentials
(profile-store.ts) or, with VPSharedGoogleMeetProfile enabled, the shared
Google Workspace account. Restored 2026-09-16 after a security review
caught that a broader "everyone sees everything" pass had removed this
check along with the (correct) read-access ones.

The VP id alone is not sufficient either — it appears in URLs and logs, so
treating it as a bearer token would let an unauthenticated request through.
"""

import hashlib
import hmac
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from microvm_client import MicrovmClient, MicrovmError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Not boto3.client("lambda-microvms"): the Lambda runtime's bundled botocore has
# no service model for it (UnknownServiceError). See microvm_client.py.
microvms = MicrovmClient()
dynamodb = boto3.client("dynamodb")

# websockify serves noVNC here inside the container. Scope the token to
# this single port so it cannot be used to reach anything else in the VM
# (the app's own hook port 9000, for instance).
NOVNC_PORT = 5901
TOKEN_TTL_MINUTES = 60

# Same host marker the frontend uses (vncConnection.js's isMicrovmEndpoint) to
# tell a MicroVM endpoint apart from the CloudFront/ALB one used by ECS.
MICROVM_HOST_MARKER = ".lambda-microvm."

# Shared with EdgeAuthDeployerFunction's Lambda@Edge code (see
# edge_auth_deployer/index.py) via VncHmacSecret in lma-ai-stack.yaml.
VNC_HMAC_SECRET = os.environ.get("VNC_HMAC_SECRET", "")


def mint_vp_scoped_token(vp_id):
    """Mint a `<vpId>.<expiry>.<hmac-hex>` token for the ECS/Fargate VNC path.

    Verified by the Lambda@Edge function gating /vnc/* on CloudFront (see
    edge_auth_deployer/index.py). Binding the vpId into the signed payload -
    not just checking "signed by us" - is what stops a token minted for one
    VP (which required passing the owner/SharedWith/Admin check above) from
    being replayed against a different VP's path.
    """
    expiry = int((datetime.now(timezone.utc) + timedelta(minutes=TOKEN_TTL_MINUTES)).timestamp())
    signing_input = f"{vp_id}.{expiry}".encode("ascii")
    mac = hmac.new(VNC_HMAC_SECRET.encode("utf-8"), signing_input, hashlib.sha256).hexdigest()
    return f"{vp_id}.{expiry}.{mac}"


def lambda_handler(event, context):
    args = event.get("arguments", {}) or {}
    vp_id = args.get("vpId")
    identity = event.get("identity", {}) or {}
    claims = identity.get("claims", {}) or {}
    # Same precedence the VP manager uses (see virtual_participant_manager), and
    # it must include identity.username: a live call failed "Unauthenticated"
    # because only the claims were checked and this deployment populates
    # identity.username instead.
    caller = claims.get("email") or claims.get("cognito:username") or identity.get("username") or ""

    if not vp_id:
        raise Exception("vpId is required")
    if not caller:
        logger.error(
            "No caller identity; claims=%s identity keys=%s",
            sorted(claims),
            sorted(identity),
        )
        raise Exception("Unauthenticated")

    vp = dynamodb.get_item(
        TableName=os.environ["VP_TABLE_NAME"],
        Key={"id": {"S": vp_id}},
    ).get("Item")
    if not vp:
        raise Exception(f"Virtual Participant {vp_id} not found")

    # Field names and semantics match the canonical subscription filter in
    # source/appsync/subscription.js: Owner (capital O) equals identity.username,
    # and SharedWith CONTAINS it. SharedWith is a comma-ish String, not a List —
    # reading it as a List (and "owner" lowercase) made every request fail
    # "Not authorized", because both lookups silently returned empty.
    owner = vp.get("Owner", {}).get("S", "") or vp.get("owner", {}).get("S", "")
    shared_raw = vp.get("SharedWith", {}).get("S", "")
    shared = {v.strip() for v in shared_raw.split(",") if v.strip()}

    groups = identity.get("groups") or claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    is_admin = "Admin" in groups

    if not is_admin and caller != owner and caller not in shared:
        logger.warning(
            "Caller %s is not authorized for VP %s (owner=%s shared=%s)",
            caller,
            vp_id,
            owner,
            sorted(shared),
        )
        raise Exception("Not authorized for this Virtual Participant")

    registry = dynamodb.get_item(
        TableName=os.environ["VP_TASK_REGISTRY_TABLE_NAME"],
        Key={"vpId": {"S": vp_id}},
    ).get("Item")
    microvm_id = (registry or {}).get("microvmId", {}).get("S", "")
    if not microvm_id:
        # No MicroVM registered: either this VP is on ECS/Fargate (the ALB
        # transport, gated by a VP-scoped HMAC token instead of a MicroVM
        # auth token) or it has not started yet. vncEndpoint is set once the
        # VP publishes its VNC readiness (see status-manager.ts:setVncReady),
        # for either launch type, so its presence/shape is what tells the two
        # apart here.
        vnc_endpoint = vp.get("vncEndpoint", {}).get("S", "")
        if vnc_endpoint and MICROVM_HOST_MARKER not in vnc_endpoint:
            if not VNC_HMAC_SECRET:
                # Deployed with VPLaunchType=MICROVM (VncHmacSecret intentionally
                # left unset there) but this VP's own endpoint looks like ECS -
                # a stack/data mismatch, not a normal "starting up" case.
                raise Exception(
                    f"VP {vp_id} has an ECS-shaped vncEndpoint but this deployment "
                    "has no VNC_HMAC_SECRET configured"
                )
            token = mint_vp_scoped_token(vp_id)
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_TTL_MINUTES)
            logger.info("Minted VP-scoped VNC token for VP %s (ECS/Fargate)", vp_id)
            return {
                "authToken": token,
                "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
            }
        raise Exception(f"No MicroVM is registered for {vp_id}; it may be starting")

    try:
        response = microvms.create_microvm_auth_token(
            microvm_identifier=microvm_id,
            expiration_in_minutes=TOKEN_TTL_MINUTES,
            allowed_ports=[{"port": NOVNC_PORT}],
        )
    except MicrovmError as exc:
        logger.error("Could not mint a token for %s: %s", microvm_id, exc)
        raise Exception(f"Could not mint a VNC token: {exc}") from exc
    token = response["authToken"]["X-aws-proxy-auth"]
    # CreateMicrovmAuthToken returns ONLY authToken -- there is no expiresAt in
    # the response (verified against the service model: output members are
    # exactly ["authToken"]). Compute it from the TTL we asked for.
    #
    # This previously did `str(response.get("expiresAt"))`, which produced the
    # literal string "None". The GraphQL field is AWSDateTime!, so AppSync failed
    # to serialize it and nulled the ENTIRE parent object -- the viewer got
    # `createMicrovmVncToken: null` and reported "Failed to connect" even though
    # a perfectly valid token had been minted.
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_TTL_MINUTES)
    logger.info("Minted VNC token for VP %s (microvm %s)", vp_id, microvm_id)
    return {
        "authToken": token,
        "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
    }

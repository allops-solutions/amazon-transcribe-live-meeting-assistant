# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Tool 6: start_meeting_now
Start a meeting immediately with virtual participant
"""

import json
import logging
import os
import re
from typing import Any, Dict, Optional

import boto3

from tools.url_helper import get_meeting_url, get_virtual_participant_url
from tools.user_helper import has_zoom_credentials, resolve_user_sub

logger = logging.getLogger()

# Natural-language aliases -> the TRANSCRIBE_LANGUAGE_CODE values scribe.ts
# already understands. Kept in sync with the UI's five-option picker
# (VirtualParticipantList.jsx) and schedule_meeting.py.
VALID_LANGUAGE_MODES = {
    "english": "en-US",
    "english only": "en-US",
    "en-us": "en-US",
    "en": "en-US",
    "bosnian": "bs-BA",
    "bosnian only": "bs-BA",
    "bs-ba": "bs-BA",
    "bs": "bs-BA",
    "croatian": "hr-HR",
    "croatian only": "hr-HR",
    "hr-hr": "hr-HR",
    "hr": "hr-HR",
    "auto-detect": "identify-language",
    "auto-detect (locks in early)": "identify-language",
    "identify-language": "identify-language",
    "auto": "identify-language",
    "auto-detect (mixed languages)": "identify-multiple-languages",
    "auto-detect, mixed languages": "identify-multiple-languages",
    "mixed": "identify-multiple-languages",
    "identify-multiple-languages": "identify-multiple-languages",
}


def _resolve_language_mode(meeting_language: Optional[str]) -> Optional[str]:
    if not meeting_language:
        return None
    normalized = meeting_language.strip().lower()
    if normalized not in VALID_LANGUAGE_MODES:
        raise ValueError(
            "Invalid meeting_language. Must be one of: English only, Bosnian only, "
            "Croatian only, Auto-detect (locks in early), Auto-detect (mixed languages)"
        )
    return VALID_LANGUAGE_MODES[normalized]



def execute(
    meeting_name: str,
    meeting_platform: str,
    meeting_id: str,
    meeting_password: Optional[str] = None,
    meeting_language: Optional[str] = None,
    user_id: str = None,
    is_admin: bool = False,
    use_stored_zoom_credentials: bool = True,
) -> Dict[str, Any]:
    """
    Start a meeting immediately with virtual participant.

    Args:
        meeting_name: Name/title of the meeting
        meeting_platform: Platform (Zoom, Teams, Chime, Webex, Google Meet)
        meeting_id: Meeting ID (numeric ID, or Google Meet code/URL)
        meeting_password: Optional meeting password
        meeting_language: Optional transcription language mode — "English
            only", "Bosnian only", "Auto-detect (locks in early)", or
            "Auto-detect (mixed languages)". Omit to use the stack default
            (auto-detect, mixed languages).
        user_id: User ID for access control
        is_admin: Whether user is admin
        use_stored_zoom_credentials: When the user has stored Zoom
            credentials in LMA, sign in to Zoom with them before joining
            the meeting (recommended; joins far more reliably and can join
            meetings that disallow guests). Set to False to force
            a guest join. Has no effect on non-Zoom platforms.

    Returns:
        Dict with virtual participant details and status
    """
    if not meeting_name or not meeting_platform or not meeting_id:
        raise ValueError("meeting_name, meeting_platform, and meeting_id are required")

    # Remove ALL whitespace from meeting_id (including internal spaces)
    meeting_id = "".join(meeting_id.split())

    # Validate and normalize platform (VP code expects uppercase)
    valid_platforms = {
        "zoom": "ZOOM",
        "teams": "TEAMS",
        "chime": "CHIME",
        "webex": "WEBEX",
        "google_meet": "GOOGLE_MEET",
        "googlemeet": "GOOGLE_MEET",
        "google meet": "GOOGLE_MEET",
        "meet": "GOOGLE_MEET",
    }

    platform_lower = meeting_platform.lower()
    if platform_lower not in valid_platforms:
        raise ValueError("Invalid platform. Must be one of: Zoom, Teams, Chime, Webex, Google Meet")

    # Convert to uppercase for VP infrastructure
    meeting_platform = valid_platforms[platform_lower]

    # Validates and raises on an unrecognized meeting_language; None (not
    # provided) passes through untouched and the resolver applies the stack
    # default.
    transcribe_language_mode = _resolve_language_mode(meeting_language)

    # Create virtual participant via GraphQL mutation
    appsync_url = os.environ.get("APPSYNC_GRAPHQL_URL")
    if not appsync_url:
        raise ValueError("APPSYNC_GRAPHQL_URL environment variable not set")

    # Prepare GraphQL mutation
    mutation = """
    mutation CreateVirtualParticipant($input: CreateVirtualParticipantInput!) {
        createVirtualParticipant(input: $input) {
            id
            meetingName
            meetingPlatform
            meetingId
            meetingPassword
            status
            owner
            createdAt
            CallId
            vncEndpoint
            vncPort
        }
    }
    """

    # Resolve the Cognito sub for the LMA user on whose behalf the MCP
    # client is calling. We use the sub to:
    #   1. Override the createVirtualParticipant Owner field (so the row
    #      isn't owned by the MCP Lambda role's session ARN).
    #   2. Pass userZoomSub/userSub to the state machine so the launched VP
    #      can use the user's stored Zoom credentials and persistent profile.
    cognito_sub = resolve_user_sub(user_id) if user_id else None
    has_zoom_creds = has_zoom_credentials(cognito_sub) if cognito_sub else False
    use_zoom_creds = bool(use_stored_zoom_credentials and has_zoom_creds)
    logger.info(
        "MCP user resolution: user_id=%s sub=%s has_zoom_creds=%s use_zoom_creds=%s",
        user_id,
        cognito_sub,
        has_zoom_creds,
        use_zoom_creds,
    )

    variables = {
        "input": {
            "meetingName": meeting_name,
            "meetingPlatform": meeting_platform,
            "meetingId": meeting_id,
            "meetingPassword": meeting_password or "",
            "status": "INITIALIZING",
            # owner override — only honored by the resolver when the caller
            # is IAM (ours is, the MCP Lambda role). Cognito-authenticated
            # callers from the React UI ignore this and use their own identity.
            **({"owner": user_id} if user_id else {}),
            # Persisted on the row for record-keeping, matching the UI. The
            # immediate-launch path below actually consumes these via the
            # Step Functions "data" input, not this row.
            **({"transcribeLanguageMode": transcribe_language_mode} if transcribe_language_mode else {}),
        }
    }

    # Execute GraphQL mutation using boto3
    try:
        # Use IAM auth to call AppSync
        import requests
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        session = boto3.Session()
        credentials = session.get_credentials()

        headers = {"Content-Type": "application/json"}

        request_body = json.dumps({"query": mutation, "variables": variables})

        request = AWSRequest(method="POST", url=appsync_url, data=request_body, headers=headers)

        SigV4Auth(credentials, "appsync", session.region_name).add_auth(request)

        response = requests.post(
            appsync_url,
            headers=dict(request.headers),
            data=request_body,
            timeout=(5, 30),
        )

        response.raise_for_status()
        result = response.json()

        if "errors" in result:
            logger.error(f"GraphQL errors: {result['errors']}")
            raise ValueError(f"Failed to start meeting: {result['errors'][0]['message']}")

        vp_data = result["data"]["createVirtualParticipant"]
        vp_id = vp_data["id"]

        # Step 2: Invoke Step Functions State Machine to launch ECS task
        # This is what the UI does after creating the VP record
        sfn = boto3.client("stepfunctions")

        # Get state machine ARN from SSM parameter
        ssm = boto3.client("ssm")
        settings_param = os.environ.get("LMA_SETTINGS_PARAMETER")
        if not settings_param:
            raise ValueError("LMA_SETTINGS_PARAMETER environment variable not set")

        settings_response = ssm.get_parameter(Name=settings_param)
        settings = json.loads(settings_response["Parameter"]["Value"])
        state_machine_arn = settings.get("LMAVirtualParticipantSchedulerStateMachine")

        if not state_machine_arn:
            raise ValueError("LMAVirtualParticipantSchedulerStateMachine not found in settings")

        # Invoke state machine (matches UI behavior)
        sfn_input = {
            "apiInfo": {"httpMethod": "POST"},
            "data": {
                "meetingPlatform": meeting_platform,
                "meetingID": meeting_id,
                "meetingPassword": meeting_password or "",
                "meetingName": meeting_name,
                "meetingTime": "",  # Empty string triggers immediate RunTask path
                "userName": user_id or "mcp-server-user",
                "virtualParticipantId": vp_id,
                # Always populate userSub when we know it so the persistent
                # Chromium profile (Phase C4) is keyed correctly. userZoomSub
                # is only set when the user has actually stored Zoom creds —
                # an empty string means the VP joins as a guest.
                "userSub": cognito_sub or "",
                "userZoomSub": cognito_sub if use_zoom_creds else "",
                "accessToken": "",
                "idToken": "",
                "refreshToken": "",
                "rereshToken": "",  # Typo in VP template (line 1168) - workaround
                # Omitted entirely when not resolved — the state machine's own
                # "Format/Empty Zoom Secret Name" defaults
                # (identify-multiple-languages / blank) apply, same as when
                # the UI's meeting-creation form is left on its defaults.
                **({"transcribeLanguageMode": transcribe_language_mode} if transcribe_language_mode else {}),
            },
        }

        logger.info(f"Invoking state machine (sync): {state_machine_arn}")
        # Express SM supports synchronous execution — use it so we can
        # surface RunTask failures (image pull, ECS capacity, IAM, etc.)
        # back to the MCP caller as an actionable error message instead
        # of leaving the VP record stuck at INITIALIZING. Falls back to
        # async on any error so a missing IAM permission doesn't break
        # the launch path entirely.
        try:
            sync_resp = sfn.start_sync_execution(
                stateMachineArn=state_machine_arn,
                input=json.dumps(sfn_input),
            )
            logger.info(
                "Sync execution finished for VP %s: status=%s",
                vp_id,
                sync_resp.get("status"),
            )
            sm_status = sync_resp.get("status", "")
            sm_error = sync_resp.get("error", "") or ""
            sm_cause = sync_resp.get("cause", "") or ""
            if sm_status not in ("SUCCEEDED",):
                # MarkVPFailed inside the state machine has already updated the
                # DDB row, but raise so the MCP caller gets a clear error and
                # doesn't think the launch silently succeeded.
                detail = f"{sm_status}: {sm_error}"
                if sm_cause:
                    detail = f"{detail} — {sm_cause}"
                raise ValueError(f"Failed to launch virtual participant ({detail})")
        except sfn.exceptions.ClientError as exc:  # type: ignore[attr-defined]
            logger.warning(
                "start_sync_execution failed (%s); falling back to async start_execution",
                exc,
            )
            sfn.start_execution(stateMachineArn=state_machine_arn, input=json.dumps(sfn_input))
            logger.info(f"Async state machine execution started for VP {vp_id}")

        # Build response
        response_data = {
            "virtualParticipantId": vp_id,
            "meetingName": vp_data["meetingName"],
            "meetingPlatform": vp_data["meetingPlatform"],
            "meetingId": vp_data["meetingId"],
            "status": vp_data["status"],
            "owner": vp_data.get("owner", user_id),
            "message": "Virtual participant is initializing and will join the meeting shortly. Check LMA UI for live status.",
        }

        # Add CallId if available
        call_id = vp_data.get("CallId")
        if call_id:
            response_data["callId"] = call_id

        # Add VNC details if available
        if vp_data.get("vncEndpoint"):
            response_data["vncEndpoint"] = vp_data["vncEndpoint"]
            response_data["vncPort"] = vp_data.get("vncPort")
            response_data["vncInfo"] = "VNC preview available - check LMA UI for live view"

        response_data["meetingUrl"] = get_meeting_url(call_id) if call_id else None
        response_data["virtualParticipantUrl"] = get_virtual_participant_url(vp_id)

        return response_data

    except Exception as e:
        logger.error(f"Error starting meeting: {e}", exc_info=True)
        raise ValueError(f"Failed to start meeting: {str(e)}")

#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
MCP Analytics Lambda Function
Implements MCP protocol tools for LMA meeting data access
Provides meeting analytics, VP scheduling, calendar reconciliation and VP status tools.
"""

import json
import logging
import os
from typing import Any, Dict

from oauth_context import CALLER_TOKEN, intercept_request, verified_claims

# Import tool implementations
from tools import (
    calendar_schedule,
    get_summary,
    get_transcript,
    get_virtual_participant_status,
    list_meetings,
    schedule_meeting,
    search_meetings,
    start_meeting_now,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")

# Invariant: every tool scopes its results to the calling user and widens that
# scope only for members of the admin group, so a request that does not resolve
# to a user cannot be answered. Both entry paths carry an identity: the REST
# endpoint through its authorizer context, the gateway through a verified token.
NO_IDENTITY_MESSAGE = (
    "Unable to determine the calling user. Connect through the per-user API key "
    "endpoint so that each request carries a user identity "
    "(see docs/mcp-api-key-auth.md)."
)


def is_admin_claim(claims: Dict[str, Any]) -> bool:
    """Whether the caller's `cognito:groups` claim contains the admin group.

    The claim arrives as a list from the Cognito user-pool authorizer and as a
    comma-joined string from some token shapes. Both are normalized to a list of
    group names so the comparison is an exact match on a whole group name.
    """
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [group.strip() for group in groups.split(",") if group.strip()]
    elif not isinstance(groups, (list, tuple)):
        groups = []
    return ADMIN_GROUP in groups


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main handler for MCP Analytics Lambda.
    Routes tool calls to appropriate implementations.
    Enforces user-based access control (UBAC).
    """
    # An event carries caller claims or authorizer context, so the event body is
    # not logged; its shape is, and the resolved caller is logged on its own below.
    logger.info(f"Event keys: {sorted(event.keys())}")

    try:
        if event.get("interceptorInputVersion") == "1.0" and "mcp" in event:
            return intercept_request(event)

        client_context = getattr(context, "client_context", None)
        gateway_context = getattr(client_context, "custom", {}) or {}
        gateway_call = CALLER_TOKEN in event or "bedrockAgentCoreToolName" in gateway_context
        # Detect API Gateway proxy integration (API key auth path)
        if not gateway_call and "httpMethod" in event and "body" in event:
            body = event.get("body", "{}")
            tool_input = json.loads(body) if isinstance(body, str) else (body or {})
            authorizer = event.get("requestContext", {}).get("authorizer", {})
            user_id = authorizer.get("userId")
            username = authorizer.get("username") or user_id
            is_admin = authorizer.get("isAdmin", "false") == "true"

            # The API key authorizer resolves the key to a user before this
            # function runs, so a request without that context has no caller.
            if not user_id:
                logger.warning("Request carries no caller identity - rejecting")
                return error_response(403, NO_IDENTITY_MESSAGE)

            logger.info(f"API Gateway path - User: {username}, Admin: {is_admin}")

            # Handle MCP JSON-RPC protocol messages
            if "jsonrpc" in tool_input and "method" in tool_input:
                return handle_mcp_jsonrpc(tool_input, user_id, username, is_admin)
        else:
            # Lambda targets receive tool arguments, not an API Gateway claims
            # envelope. Verify the actual bearer token forwarded by our REQUEST
            # interceptor; never trust a caller-supplied requestContext.
            try:
                claims = verified_claims(event.get(CALLER_TOKEN))
            except Exception:
                logger.warning("OAuth caller verification failed - rejecting")
                return error_response(403, NO_IDENTITY_MESSAGE)

            user_id = claims.get("sub")
            username = claims.get("username", claims.get("email", user_id))
            is_admin = is_admin_claim(claims)

            # Clients that reach this function without claims should use the
            # per-user API key endpoint instead -- it supplies
            # userId/username/isAdmin on every call. See docs/mcp-api-key-auth.md.
            if not user_id:
                logger.warning("Request carries no caller identity - rejecting")
                return error_response(403, NO_IDENTITY_MESSAGE)

            tool_input = {k: v for k, v in event.items()
                          if k not in {CALLER_TOKEN, "requestContext", "httpMethod", "body"}}

        logger.info(f"User: {username}, ID: {user_id}, Admin: {is_admin}")

        # Determine which tool based on input parameters
        if "scheduleAction" in tool_input:
            if tool_input["scheduleAction"] not in {"UPDATE", "CANCEL"}:
                raise ValueError("scheduleAction must be UPDATE or CANCEL")
            tool_name = ("update_scheduled_meeting" if tool_input["scheduleAction"] == "UPDATE"
                         else "cancel_scheduled_meeting")
        elif "virtualParticipantId" in tool_input and len(tool_input) <= 2:
            tool_name = "get_virtual_participant_status"
        elif "query" in tool_input and "maxResults" in tool_input:
            tool_name = "search_lma_meetings"
        elif "meetingId" in tool_input and "format" in tool_input:
            tool_name = "get_meeting_transcript"
        elif "meetingId" in tool_input and (
            "includeActionItems" in tool_input or "includeTopics" in tool_input
        ):
            tool_name = "get_meeting_summary"
        elif "meetingPlatform" in tool_input and "scheduledTime" in tool_input:
            tool_name = "schedule_meeting"
        elif (
            "meetingPlatform" in tool_input
            and "meetingName" in tool_input
            and "scheduledTime" not in tool_input
        ):
            tool_name = "start_meeting_now"
        elif "limit" in tool_input or "status" in tool_input or "participant" in tool_input:
            tool_name = "list_meetings"
        else:
            # Default to list_meetings if we can't determine
            tool_name = "list_meetings"

        # On the gateway path `tool_input` is the event itself, which carries the
        # caller's claims, so the argument names are logged rather than the values.
        logger.info(f"Inferred tool: {tool_name}, Input keys: {sorted(tool_input.keys())}")

        # Route to appropriate tool
        if tool_name in {"update_scheduled_meeting", "cancel_scheduled_meeting"}:
            result = manage_calendar_tool(tool_name, tool_input, user_id, is_admin)
        elif tool_name == "search_lma_meetings":
            result = search_meetings.execute(
                query=tool_input.get("query"),
                start_date=tool_input.get("startDate"),
                end_date=tool_input.get("endDate"),
                max_results=tool_input.get("maxResults", 10),
                user_id=user_id,
                is_admin=is_admin,
            )

        elif tool_name == "get_meeting_transcript":
            result = get_transcript.execute(
                meeting_id=tool_input.get("meetingId"),
                format=tool_input.get("format", "text"),
                user_id=user_id,
                is_admin=is_admin,
            )

        elif tool_name == "get_meeting_summary":
            result = get_summary.execute(
                meeting_id=tool_input.get("meetingId"),
                include_action_items=tool_input.get("includeActionItems", True),
                include_topics=tool_input.get("includeTopics", True),
                user_id=user_id,
                is_admin=is_admin,
            )

        elif tool_name == "list_meetings":
            result = list_meetings.execute(
                start_date=tool_input.get("startDate"),
                end_date=tool_input.get("endDate"),
                participant=tool_input.get("participant"),
                status=tool_input.get("status", "ALL"),
                limit=tool_input.get("limit", 20),
                user_id=user_id,
                is_admin=is_admin,
            )

        elif tool_name == "schedule_meeting":
            result = schedule_meeting.execute(
                meeting_name=tool_input.get("meetingName"),
                meeting_platform=tool_input.get("meetingPlatform"),
                meeting_id=tool_input.get("meetingId"),
                scheduled_time=tool_input.get("scheduledTime"),
                calendar_event_uid=tool_input.get("calendarEventUid"),
                calendar_occurrence_start=tool_input.get("calendarOccurrenceStart"),
                meeting_password=tool_input.get("meetingPassword"),
                meeting_language=tool_input.get("meetingLanguage"),
                user_id=user_id,
                is_admin=is_admin,
            )

        elif tool_name == "start_meeting_now":
            result = start_meeting_now.execute(
                meeting_name=tool_input.get("meetingName"),
                meeting_platform=tool_input.get("meetingPlatform"),
                meeting_id=tool_input.get("meetingId"),
                meeting_password=tool_input.get("meetingPassword"),
                meeting_language=tool_input.get("meetingLanguage"),
                user_id=user_id,
                is_admin=is_admin,
                # Default ON; pass useStoredZoomCredentials=false to opt-out.
                use_stored_zoom_credentials=tool_input.get("useStoredZoomCredentials", True),
            )

        elif tool_name == "get_virtual_participant_status":
            result = get_virtual_participant_status.execute(
                virtual_participant_id=tool_input.get("virtualParticipantId"),
                user_id=user_id,
                is_admin=is_admin,
            )

        else:
            return error_response(400, f"Unknown tool: {tool_name}")

        # Return MCP-formatted success response
        return success_response(result)

    except PermissionError as e:
        logger.warning(f"Permission denied: {e}")
        return error_response(403, str(e))

    except ValueError as e:
        logger.warning(f"Invalid input: {e}")
        return error_response(400, str(e))

    except Exception as e:
        logger.error(f"Error processing tool call: {e}", exc_info=True)
        return error_response(500, f"Internal error: {str(e)}")


# IMPORTANT: MCPServerGatewayTarget's ToolSchema.InlinePayload in
# lma-ai-stack.yaml is a SECOND, INDEPENDENT copy of this list, hand-typed
# in CloudFormation's own schema format. The Bedrock AgentCore Gateway
# (what Quick Desktop/Web actually connect to) serves that hardcoded
# snapshot for tool discovery - it does not introspect this file at
# request time - so a change here alone does not reach Quick no matter
# how the Lambda itself behaves. Found 2026-09-17: schedule_meeting and
# start_meeting_now had drifted (missing Google Meet, meetingLanguage,
# summaryProfile, summaryLanguage) with nothing failing loudly; Quick just
# told the user those features "aren't supported". Mirror any change to
# this list into that CFN block by hand.
MCP_TOOLS = [
    {
        "name": "search_lma_meetings",
        "description": "Search across all meeting transcripts and summaries using natural language queries",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language search query"},
                "startDate": {"type": "string", "description": "Optional ISO 8601 start date"},
                "endDate": {"type": "string", "description": "Optional ISO 8601 end date"},
                "maxResults": {"type": "number", "description": "Maximum results to return"},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_meeting_transcript",
        "description": "Retrieve the complete transcript for a specific meeting",
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingId": {"type": "string", "description": "Meeting ID (CallId)"},
                "format": {"type": "string", "description": "Output format (json, text, or srt)"},
            },
            "required": ["meetingId"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_meeting_summary",
        "description": "Get AI-generated summary and action items for a meeting",
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingId": {"type": "string", "description": "Meeting ID (CallId)"},
                "includeActionItems": {"type": "boolean", "description": "Include action items"},
                "includeTopics": {"type": "boolean", "description": "Include key topics"},
            },
            "required": ["meetingId"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_meetings",
        "description": "List meetings with optional filters",
        "inputSchema": {
            "type": "object",
            "properties": {
                "startDate": {"type": "string", "description": "ISO 8601 start date"},
                "endDate": {"type": "string", "description": "ISO 8601 end date"},
                "participant": {"type": "string", "description": "Filter by participant name"},
                "status": {"type": "string", "description": "Meeting status filter"},
                "limit": {"type": "number", "description": "Maximum number of meetings to return"},
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "schedule_meeting",
        "description": (
            "Schedule a future meeting with allOps LMA. Supported platforms: Google Meet, "
            "Zoom, Teams, Chime and Webex. Google Meet IS supported; use the meet.google.com "
            "URL or abc-defg-hij code. Host admission or bot Google sign-in may be required."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingName": {"type": "string", "description": "Name/title of the meeting"},
                "meetingPlatform": {
                    "type": "string",
                    "description": "Platform (Zoom, Teams, Chime, Webex, Google Meet)",
                },
                "meetingId": {
                    "type": "string",
                    "description": (
                        "Meeting ID: numeric ID for Zoom/Teams/Chime/Webex, or the "
                        "abc-defg-hij meeting code (or full URL) for Google Meet"
                    ),
                },
                "scheduledTime": {"type": "string", "description": "ISO 8601 datetime"},
                "calendarEventUid": {
                    "type": "string",
                    "description": "Google Calendar iCalUID (shared across attendees), NOT per-calendar event id. For recurring events also supply calendarOccurrenceStart; omit occurrence start for one-off events.",
                },
                "calendarOccurrenceStart": {
                    "type": "string",
                    "description": "Recurring events only: originalStartTime.dateTime with timezone, not the updated event start. Keep unchanged when rescheduling; omit for one-off events.",
                },
                "meetingPassword": {"type": "string", "description": "Optional meeting password"},
                "meetingLanguage": {
                    "type": "string",
                    "description": (
                        "Optional transcription language mode: 'English only', 'Bosnian only', "
                        "'Croatian only', 'Auto-detect (locks in early)', or 'Auto-detect (mixed "
                        "languages)'. Omit for the stack default (auto-detect, mixed languages) — "
                        "best for calls that mix languages within the same sentence only if you "
                        "don't know the dominant language in advance; prefer 'English only', "
                        "'Bosnian only', or 'Croatian only' when you do, for much better accuracy."
                    ),
                },
            },
            "required": ["meetingName", "meetingPlatform", "meetingId", "scheduledTime"],
        },
    },
    {
        "name": "update_scheduled_meeting",
        "description": "Update an existing pending MCP-scheduled VP when its calendar occurrence changes. Owner/admin only; use virtualParticipantId or calendarEventUid (plus original start for recurring events). Never create another VP for a reschedule. Changes must arrive at least 3 minutes before the old and new meeting starts. No running meeting is interrupted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scheduleAction": {
                    "type": "string",
                    "enum": [
                        "UPDATE"
                    ],
                    "description": "Required routing marker: UPDATE."
                },
                "virtualParticipantId": {
                    "type": "string",
                    "description": "VP id returned by schedule_meeting; omit only when calendarEventUid is supplied (plus original start for recurring events)."
                },
                "calendarEventUid": {
                    "type": "string",
                    "description": "Google Calendar iCalUID (shared across attendees), NOT per-calendar event id. For recurring events also supply calendarOccurrenceStart; omit occurrence start for one-off events."
                },
                "calendarOccurrenceStart": {
                    "type": "string",
                    "description": "Recurring events only: originalStartTime.dateTime with timezone, not the updated event start. Keep unchanged when rescheduling; omit for one-off events."
                },
                "meetingName": {
                    "type": "string",
                    "description": "Updated event title."
                },
                "meetingPlatform": {
                    "type": "string",
                    "description": "Updated platform: Google Meet, Zoom, Teams, Chime, Webex."
                },
                "meetingId": {
                    "type": "string",
                    "description": "Updated meeting code/URL."
                },
                "scheduledTime": {
                    "type": "string",
                    "description": "Updated start datetime with timezone; NOT calendarOccurrenceStart."
                },
                "meetingPassword": {
                    "type": "string",
                    "description": "Updated password; empty string clears it."
                },
                "meetingLanguage": {
                    "type": "string",
                    "description": "Updated transcription language mode (same choices as schedule_meeting)."
                }
            },
            "required": [
                "scheduleAction"
            ]
        }
    },
    {
        "name": "cancel_scheduled_meeting",
        "description": "Cancel a pending MCP-scheduled VP when its calendar occurrence is cancelled or no longer requires LMA. Owner/admin only; use virtualParticipantId or calendarEventUid (plus original start for recurring events). Retrying cancellation is safe. Must arrive at least 3 minutes before meeting start. Does not delete transcripts or stop an already-running meeting.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scheduleAction": {
                    "type": "string",
                    "enum": [
                        "CANCEL"
                    ],
                    "description": "Required routing marker: CANCEL."
                },
                "virtualParticipantId": {
                    "type": "string",
                    "description": "VP id returned by schedule_meeting; omit only when calendarEventUid is supplied (plus original start for recurring events)."
                },
                "calendarEventUid": {
                    "type": "string",
                    "description": "Google Calendar iCalUID (shared across attendees), NOT per-calendar event id. For recurring events also supply calendarOccurrenceStart; omit occurrence start for one-off events."
                },
                "calendarOccurrenceStart": {
                    "type": "string",
                    "description": "Recurring events only: originalStartTime.dateTime with timezone, not the updated event start. Keep unchanged when rescheduling; omit for one-off events."
                }
            },
            "required": [
                "scheduleAction"
            ]
        }
    },
    {
        "name": "start_meeting_now",
        "description": (
            "Start a meeting immediately with a virtual participant. "
            "Supported platforms: Google Meet, Zoom, Teams, Chime and Webex. "
            "Google Meet IS supported; host admission or bot Google sign-in may be required. "
            "When the user has stored Zoom credentials in LMA, the VP will "
            "sign in to Zoom with them by default for a more reliable "
            "join; pass useStoredZoomCredentials=false to force a guest "
            "join. Returns immediately with the virtualParticipantId; the "
            "VP may take up to 60 seconds to fully join. Use "
            "get_virtual_participant_status to poll progress, especially to "
            "detect MANUAL_ACTION_REQUIRED states (CAPTCHA, 2FA, SSO) where "
            "the human user must complete a challenge in the LMA viewer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingName": {"type": "string", "description": "Name/title of the meeting"},
                "meetingPlatform": {
                    "type": "string",
                    "description": "Platform (Zoom, Teams, Chime, Webex, Google Meet)",
                },
                "meetingId": {
                    "type": "string",
                    "description": (
                        "Meeting ID: numeric ID for Zoom/Teams/Chime/Webex, or the "
                        "abc-defg-hij meeting code (or full URL) for Google Meet"
                    ),
                },
                "meetingPassword": {"type": "string", "description": "Optional meeting password"},
                "meetingLanguage": {
                    "type": "string",
                    "description": (
                        "Optional transcription language mode: 'English only', 'Bosnian only', "
                        "'Croatian only', 'Auto-detect (locks in early)', or 'Auto-detect (mixed "
                        "languages)'. Omit for the stack default (auto-detect, mixed languages) — "
                        "best for calls that mix languages within the same sentence only if you "
                        "don't know the dominant language in advance; prefer 'English only', "
                        "'Bosnian only', or 'Croatian only' when you do, for much better accuracy."
                    ),
                },
                "useStoredZoomCredentials": {
                    "type": "boolean",
                    "description": (
                        "Default true. When the user has stored Zoom credentials, "
                        "the VP signs in to Zoom before joining the meeting. "
                        "Set to false to force a guest join."
                    ),
                },
            },
            "required": ["meetingName", "meetingPlatform", "meetingId"],
        },
    },
    {
        "name": "get_virtual_participant_status",
        "description": (
            "Get the current status of a virtual participant launched via "
            "start_meeting_now. Returns status, a human-readable summary, "
            "and — critically — manualActionRequired=true with a "
            "manualActionMessage when the VP is blocked on a CAPTCHA / 2FA "
            "/ SSO / consent challenge that the human user must complete "
            "in the LMA viewer (virtualParticipantUrl). Poll this every "
            "5–10 seconds for ~2 minutes after start_meeting_now if "
            "you need to confirm the meeting was joined or surface a "
            "manual-action prompt back to your user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "virtualParticipantId": {
                    "type": "string",
                    "description": "VP id returned by start_meeting_now",
                },
            },
            "required": ["virtualParticipantId"],
        },
        "annotations": {"readOnlyHint": True},
    },
]


def manage_calendar_tool(tool_name, arguments, user_id, is_admin):
    """Shared dispatch for AgentCore and JSON-RPC transports."""
    action = "UPDATE" if tool_name == "update_scheduled_meeting" else "CANCEL"
    if arguments.get("scheduleAction") != action:
        raise ValueError(f"scheduleAction must be {action}")
    changes = {}
    if action == "UPDATE":
        for source, target in (("meetingName", "meeting_name"),
                               ("meetingPlatform", "meeting_platform"),
                               ("meetingId", "meeting_id"), ("scheduledTime", "scheduled_time"),
                               ("meetingPassword", "meeting_password")):
            if source in arguments:
                changes[target] = arguments[source]
        if "meetingLanguage" in arguments:
            changes["language"] = schedule_meeting._resolve_language_mode(arguments["meetingLanguage"])
    return calendar_schedule.manage(
        action, user_id=user_id, is_admin=is_admin,
        virtual_participant_id=arguments.get("virtualParticipantId"),
        calendar_event_uid=arguments.get("calendarEventUid"),
        calendar_occurrence_start=arguments.get("calendarOccurrenceStart"), **changes,
    )


def handle_mcp_jsonrpc(msg, user_id, username, is_admin):
    """Handle MCP JSON-RPC 2.0 protocol messages."""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params", {})

    logger.info(f"MCP JSON-RPC method={method}, id={msg_id}")

    if method == "initialize":
        return jsonrpc_response(
            msg_id,
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "lma-mcp-server", "version": "0.2.30"},
            },
        )

    if method == "notifications/initialized":
        # Client acknowledgment — no response needed for notifications
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": ""}

    if method == "tools/list":
        return jsonrpc_response(msg_id, {"tools": MCP_TOOLS})

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        return execute_tool_call(msg_id, tool_name, arguments, user_id, username, is_admin)

    if method == "ping":
        return jsonrpc_response(msg_id, {})

    return jsonrpc_error(msg_id, -32601, f"Method not found: {method}")


def execute_tool_call(msg_id, tool_name, arguments, user_id, username, is_admin):
    """Execute a tool call and return MCP JSON-RPC response."""
    try:
        if tool_name in {"update_scheduled_meeting", "cancel_scheduled_meeting"}:
            result = manage_calendar_tool(tool_name, arguments, user_id, is_admin)
        elif tool_name == "search_lma_meetings":
            result = search_meetings.execute(
                query=arguments.get("query"),
                start_date=arguments.get("startDate"),
                end_date=arguments.get("endDate"),
                max_results=arguments.get("maxResults", 10),
                user_id=user_id,
                is_admin=is_admin,
            )
        elif tool_name == "get_meeting_transcript":
            result = get_transcript.execute(
                meeting_id=arguments.get("meetingId"),
                format=arguments.get("format", "text"),
                user_id=user_id,
                is_admin=is_admin,
            )
        elif tool_name == "get_meeting_summary":
            result = get_summary.execute(
                meeting_id=arguments.get("meetingId"),
                include_action_items=arguments.get("includeActionItems", True),
                include_topics=arguments.get("includeTopics", True),
                user_id=user_id,
                is_admin=is_admin,
            )
        elif tool_name == "list_meetings":
            result = list_meetings.execute(
                start_date=arguments.get("startDate"),
                end_date=arguments.get("endDate"),
                participant=arguments.get("participant"),
                status=arguments.get("status", "ALL"),
                limit=arguments.get("limit", 20),
                user_id=user_id,
                is_admin=is_admin,
            )
        elif tool_name == "schedule_meeting":
            result = schedule_meeting.execute(
                meeting_name=arguments.get("meetingName"),
                meeting_platform=arguments.get("meetingPlatform"),
                meeting_id=arguments.get("meetingId"),
                scheduled_time=arguments.get("scheduledTime"),
                calendar_event_uid=arguments.get("calendarEventUid"),
                calendar_occurrence_start=arguments.get("calendarOccurrenceStart"),
                meeting_password=arguments.get("meetingPassword"),
                meeting_language=arguments.get("meetingLanguage"),
                user_id=user_id,
                is_admin=is_admin,
            )
        elif tool_name == "start_meeting_now":
            result = start_meeting_now.execute(
                meeting_name=arguments.get("meetingName"),
                meeting_platform=arguments.get("meetingPlatform"),
                meeting_id=arguments.get("meetingId"),
                meeting_password=arguments.get("meetingPassword"),
                meeting_language=arguments.get("meetingLanguage"),
                user_id=user_id,
                is_admin=is_admin,
                use_stored_zoom_credentials=arguments.get("useStoredZoomCredentials", True),
            )
        elif tool_name == "get_virtual_participant_status":
            result = get_virtual_participant_status.execute(
                virtual_participant_id=arguments.get("virtualParticipantId"),
                user_id=user_id,
                is_admin=is_admin,
            )
        else:
            return jsonrpc_error(msg_id, -32602, f"Unknown tool: {tool_name}")

        return jsonrpc_response(
            msg_id,
            {
                "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}],
            },
        )
    except Exception as e:
        logger.error(f"Tool call error: {e}", exc_info=True)
        return jsonrpc_response(
            msg_id,
            {
                "content": [{"type": "text", "text": f"Error: {str(e)}"}],
                "isError": True,
            },
        )


def jsonrpc_response(msg_id, result):
    """Format a JSON-RPC 2.0 success response."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}),
    }


def jsonrpc_error(msg_id, code, message):
    """Format a JSON-RPC 2.0 error response."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
        ),
    }


def success_response(result: Any) -> Dict[str, Any]:
    """Format successful MCP response"""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {"content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]}
        ),
    }


def error_response(status_code: int, message: str) -> Dict[str, Any]:
    """Format error MCP response"""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": {"message": message, "code": status_code}}),
    }

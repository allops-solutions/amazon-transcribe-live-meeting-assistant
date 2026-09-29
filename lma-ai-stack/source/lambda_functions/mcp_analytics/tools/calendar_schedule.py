# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Atomic MCP scheduling and calendar-occurrence reconciliation.

Only pending, MCP-managed VPs can be edited. Calendar identity is independent
of the current start time; meeting-link/start-time claims also deduplicate
calls from different employees. Claims survive cancellation and rescheduling
so a stale flow cannot resurrect an old invitation.
"""

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional
from urllib.parse import urlparse

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

from tools.url_helper import get_virtual_participant_url

SERIALIZER = TypeSerializer()
DESERIALIZER = TypeDeserializer()
PREJOIN_SECONDS = 120
LOGGER = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _client() -> Any:
    return boto3.client("dynamodb", config=Config(retries={"mode": "adaptive", "max_attempts": 3}))


def _tables() -> tuple[str, str]:
    return os.environ["VP_TABLE_NAME"], os.environ["CALENDAR_SCHEDULE_TABLE_NAME"]


def _encode(value: dict[str, Any]) -> dict[str, Any]:
    return {key: SERIALIZER.serialize(item) for key, item in value.items()}


def _get(table: str, key: dict[str, Any]) -> dict[str, Any]:
    response = _client().get_item(TableName=table, Key=_encode(key), ConsistentRead=True)
    return {key: DESERIALIZER.deserialize(value) for key, value in response.get("Item", {}).items()}


def timestamp(value: str) -> int:
    """Require an explicit timezone; equivalent offsets identify the same occurrence."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("timezone is required")
        return int(parsed.timestamp())
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Use an ISO 8601 datetime with a timezone, e.g. 2026-10-01T09:00:00Z") from exc


def normalize_meeting(platform: str, meeting_id: str) -> tuple[str, str]:
    platforms = {"zoom": "ZOOM", "teams": "TEAMS", "chime": "CHIME", "webex": "WEBEX",
                 "google_meet": "GOOGLE_MEET", "googlemeet": "GOOGLE_MEET",
                 "google meet": "GOOGLE_MEET", "meet": "GOOGLE_MEET"}
    normalized = platforms.get((platform or "").strip().lower())
    value = "".join((meeting_id or "").split())
    if not normalized or not value:
        raise ValueError("A supported meetingPlatform and nonempty meetingId are required")
    parsed = urlparse(value)
    if normalized == "GOOGLE_MEET":
        if parsed.scheme:
            if parsed.scheme != "https" or parsed.hostname != "meet.google.com":
                raise ValueError("Google Meet URL must use https://meet.google.com")
            value = parsed.path.strip("/")
        value = value.lower()
        if not re.fullmatch(r"[a-z]{3}-[a-z]{4}-[a-z]{3}", value):
            raise ValueError("Use a Google Meet code such as abc-defg-hij or its full URL")
    elif normalized == "ZOOM" and parsed.scheme:
        if not parsed.hostname or not (parsed.hostname == "zoom.us" or parsed.hostname.endswith(".zoom.us")):
            raise ValueError("Zoom meeting URL must use zoom.us")
        match = re.fullmatch(r"/(?:j|wc/j)/(\d+)/?", parsed.path)
        if not match:
            raise ValueError("Use a Zoom meeting ID or /j/ meeting URL")
        value = match.group(1)
    return normalized, value


def _key(kind: str, *parts: Any) -> str:
    return kind + ":" + hashlib.sha256("\0".join(str(part) for part in parts).encode()).hexdigest()


def calendar_key(uid: Optional[str] = None, occurrence: Optional[str] = None) -> Optional[str]:
    if occurrence and not uid:
        raise ValueError("calendarOccurrenceStart requires calendarEventUid")
    if not uid:
        return None
    if not isinstance(uid, str) or not uid.strip():
        raise ValueError("calendarEventUid must be a nonempty iCalUID")
    return _key("calendar", uid.strip(), timestamp(occurrence) if occurrence else "single")


def _claim(key: str, vp_id: str, start: int) -> dict[str, Any]:
    _, table = _tables()
    return {"Put": {"TableName": table,
                    "Item": _encode({"key": key, "virtualParticipantId": vp_id,
                                     "expiresAt": start + 366 * 86400}),
                    "ConditionExpression": "attribute_not_exists(#key) OR virtualParticipantId = :vp",
                    "ExpressionAttributeNames": {"#key": "key"},
                    "ExpressionAttributeValues": _encode({":vp": vp_id})}}


def _claimed_vp(keys: list[str]) -> Optional[dict[str, Any]]:
    vp_table, claims_table = _tables()
    for key in keys:
        claim = _get(claims_table, {"key": key})
        if claim:
            row = _get(vp_table, {"id": claim["virtualParticipantId"]})
            if not row:
                raise ValueError("This occurrence's VP was deleted; refusing to recreate it from a stale flow")
            return row
    return None


def _result(row: dict[str, Any], duplicate: bool = False) -> dict[str, Any]:
    notifications_pending = not _notify(row["id"])
    return {"virtualParticipantId": row["id"], "meetingName": row["meetingName"],
            "meetingPlatform": row["meetingPlatform"], "meetingId": row["meetingId"],
            "scheduledFor": row["scheduledFor"], "status": row["status"],
            "owner": row.get("owner"), "alreadyScheduled": duplicate,
            "notificationPending": notifications_pending,
            "virtualParticipantUrl": get_virtual_participant_url(row["id"]),
            "message": "Existing VP returned; no second VP created." if duplicate else
                       "Schedule saved. The scheduler will reconcile the pending launch."}


def _notify(vp_id: str) -> bool:
    """Publish committed state, never a caller-supplied row. Retry is harmless."""
    import requests
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    try:
        session = boto3.Session()
        query = """mutation NotifyScheduledVP($id: ID!) {
          notifyScheduledVirtualParticipant(id: $id) {
            id meetingName meetingPlatform meetingId meetingTime scheduledFor
            isScheduled status owner Owner SharedWith createdAt updatedAt
          }
        }"""
        body = json.dumps({"query": query, "variables": {"id": vp_id}})
        url = os.environ["APPSYNC_GRAPHQL_URL"]
        request = AWSRequest(method="POST", url=url, data=body,
                             headers={"Content-Type": "application/json"})
        SigV4Auth(session.get_credentials().get_frozen_credentials(),
                  "appsync", session.region_name).add_auth(request)
        response = requests.post(url, data=body, headers=dict(request.headers), timeout=(5, 10))
        response.raise_for_status()
        result = response.json()
        if result.get("errors") or not result.get("data", {}).get("notifyScheduledVirtualParticipant"):
            raise ValueError("AppSync notification failed")
        return True
    except Exception:
        # The atomic schedule is already committed. A notification outage must
        # not create another VP or imply the scheduling transaction rolled back.
        LOGGER.warning("Schedule saved but UI notification failed for VP %s", vp_id)
        return False


def create(meeting_name: str, meeting_platform: str, meeting_id: str,
           scheduled_time: str, user_id: str, meeting_password: str = "",
           language: Optional[str] = None, user_sub: Optional[str] = None,
           zoom_sub: Optional[str] = None, calendar_event_uid: Optional[str] = None,
           calendar_occurrence_start: Optional[str] = None) -> dict[str, Any]:
    """Claim calendar identity AND physical meeting slot in the same transaction."""
    if not user_id or not meeting_name or not meeting_name.strip():
        raise ValueError("Authenticated user and nonempty meetingName are required")
    platform, meeting = normalize_meeting(meeting_platform, meeting_id)
    start = timestamp(scheduled_time)
    identity = calendar_key(calendar_event_uid, calendar_occurrence_start)
    slot = _key("slot", platform, meeting, start)
    keys = [identity, slot] if identity else [slot]
    existing = _claimed_vp(keys)
    if existing:
        # Different attendees can supply different calendar identifiers for the
        # same physical meeting. Bind their identity to the already claimed VP.
        if identity and not _get(_tables()[1], {"key": identity}):
            try:
                _client().transact_write_items(TransactItems=[_claim(identity, existing["id"], start)])
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
                existing = _claimed_vp([identity])
                if not existing:
                    raise ValueError("Concurrent scheduling conflict; retry the same request") from exc
        return _result(existing, duplicate=True)
    _require_pending_time(start)
    now = datetime.now(timezone.utc).isoformat()
    row = {"id": str(uuid.uuid4()), "meetingName": meeting_name.strip(),
           "meetingPlatform": platform, "meetingId": meeting,
           "meetingPassword": meeting_password or "", "meetingTime": start,
           "scheduledFor": datetime.fromtimestamp(start, timezone.utc).isoformat(),
           "isScheduled": True, "status": "SCHEDULED", "owner": user_id, "Owner": user_id,
           "SharedWith": "", "createdAt": now, "updatedAt": now,
           "calendarManaged": True, "scheduleRevision": 1, "calendarSlotKey": slot}
    for key, value in (("transcribeLanguageMode", language), ("userSub", user_sub), ("userZoomSub", zoom_sub)):
        if value:
            row[key] = value
    vp_table, _ = _tables()
    transaction = [{"Put": {"TableName": vp_table, "Item": _encode(row),
                            "ConditionExpression": "attribute_not_exists(id)"}}]
    transaction.extend(_claim(key, row["id"], start) for key in keys)
    try:
        _client().transact_write_items(TransactItems=transaction)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        existing = _claimed_vp(keys)
        if not existing:
            raise ValueError("Concurrent scheduling conflict; retry the same request") from exc
        # Reconcile an alias on the retry, without creating another VP.
        return create(meeting_name, platform, meeting, scheduled_time, user_id,
                      meeting_password, language, user_sub, zoom_sub,
                      calendar_event_uid, calendar_occurrence_start)
    return _result(row)


def _require_pending_time(start: int) -> None:
    if start <= int(datetime.now(timezone.utc).timestamp()) + PREJOIN_SECONDS + 60:
        raise ValueError("Schedule/change at least 3 minutes before the meeting; VP pre-join starts 2 minutes early")


def manage(action: str, user_id: str, is_admin: bool = False,
           virtual_participant_id: Optional[str] = None,
           calendar_event_uid: Optional[str] = None,
           calendar_occurrence_start: Optional[str] = None, **changes: Any) -> dict[str, Any]:
    """Update/cancel one occurrence, preserving owner and guarding concurrent edits."""
    if action not in {"UPDATE", "CANCEL"}:
        raise ValueError("scheduleAction must be UPDATE or CANCEL")
    if not user_id:
        raise ValueError("Authentication required")
    identity = calendar_key(calendar_event_uid, calendar_occurrence_start)
    vp_table, _ = _tables()
    row = _get(vp_table, {"id": virtual_participant_id}) if virtual_participant_id else (
        _claimed_vp([identity]) if identity else None)
    if not row:
        raise ValueError("Scheduled VP not found; provide virtualParticipantId or calendarEventUid (plus original start for recurring events)")
    if identity and virtual_participant_id:
        matched = _claimed_vp([identity])
        if not matched or matched["id"] != row["id"]:
            raise ValueError("Calendar identity does not match virtualParticipantId")
    if not is_admin and row.get("owner") != user_id and row.get("userSub") != user_id:
        raise ValueError("Only the scheduling owner or an admin can update/cancel this VP")
    if not row.get("calendarManaged"):
        raise ValueError("Only VPs scheduled through the calendar-aware MCP can be changed with this tool")
    if action == "CANCEL" and row["status"] == "CANCELLED":
        return _result(row, duplicate=True)
    if row["status"] != "SCHEDULED":
        raise ValueError("VP is no longer pending; this tool does not interrupt running meetings")
    updated = dict(row)
    if action == "UPDATE":
        if changes.get("meeting_name") is not None:
            name = changes["meeting_name"].strip()
            if not name:
                raise ValueError("meetingName cannot be empty")
            updated["meetingName"] = name
        if changes.get("meeting_platform") is not None or changes.get("meeting_id") is not None:
            updated["meetingPlatform"], updated["meetingId"] = normalize_meeting(
                changes.get("meeting_platform") or row["meetingPlatform"],
                changes.get("meeting_id") or row["meetingId"])
        if changes.get("scheduled_time") is not None:
            updated["meetingTime"] = timestamp(changes["scheduled_time"])
            updated["scheduledFor"] = datetime.fromtimestamp(updated["meetingTime"], timezone.utc).isoformat()
        if changes.get("meeting_password") is not None:
            updated["meetingPassword"] = changes["meeting_password"]
        if changes.get("language") is not None:
            updated["transcribeLanguageMode"] = changes["language"]
        if updated == row:
            return _result(row, duplicate=True)
        _require_pending_time(updated["meetingTime"])
    _require_pending_time(row["meetingTime"])
    updated["calendarSlotKey"] = _key("slot", updated["meetingPlatform"], updated["meetingId"], updated["meetingTime"])
    if action == "CANCEL":
        updated.update(status="CANCELLED", scheduleCancelled=True, endReason="Calendar event cancelled")
    updated["scheduleRevision"] = row["scheduleRevision"] + 1
    updated["updatedAt"] = datetime.now(timezone.utc).isoformat()
    # Update only scheduling fields: do not overwrite a concurrent sharing edit
    # or other runtime metadata with the snapshot we read earlier.
    edited = {key: value for key, value in updated.items() if row.get(key) != value}
    names = {"#status": "status"}
    values = {":pending": "SCHEDULED", ":revision": row["scheduleRevision"]}
    setters = []
    for index, (key, value) in enumerate(edited.items()):
        names[f"#field{index}"] = key
        values[f":value{index}"] = value
        setters.append(f"#field{index} = :value{index}")
    transaction = [{"Update": {"TableName": vp_table, "Key": _encode({"id": row["id"]}),
                               "UpdateExpression": "SET " + ", ".join(setters),
                               "ConditionExpression": "#status = :pending AND scheduleRevision = :revision",
                               "ExpressionAttributeNames": names,
                               "ExpressionAttributeValues": _encode(values)}}]
    if action == "UPDATE":
        transaction.append(_claim(updated["calendarSlotKey"], row["id"], updated["meetingTime"]))
        if identity:
            transaction.append(_claim(identity, row["id"], updated["meetingTime"]))
    try:
        _client().transact_write_items(TransactItems=transaction)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        raise ValueError("VP changed concurrently or another VP owns the new slot; re-read the calendar and retry") from exc
    return _result(updated)

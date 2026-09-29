# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Tool 5: schedule_meeting
Schedule a future meeting with virtual participant
"""

from typing import Any, Dict, Optional

from tools.user_helper import has_zoom_credentials, resolve_user_sub

# Natural-language aliases -> the TRANSCRIBE_LANGUAGE_CODE values scribe.ts
# already understands. Kept in sync with the UI's five-option picker
# (VirtualParticipantList.jsx).
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
    scheduled_time: str,
    meeting_password: Optional[str] = None,
    meeting_language: Optional[str] = None,
    user_id: str = None,
    is_admin: bool = False,
    use_stored_zoom_credentials: bool = True,
    calendar_event_uid: Optional[str] = None,
    calendar_occurrence_start: Optional[str] = None,
) -> Dict[str, Any]:
    """Schedule one occurrence atomically; repeats return the existing VP."""
    from tools import calendar_schedule

    # Reject invalid input before performing any Cognito/Secrets Manager reads.
    calendar_schedule.normalize_meeting(meeting_platform, meeting_id)
    calendar_schedule.timestamp(scheduled_time)
    calendar_schedule.calendar_key(calendar_event_uid, calendar_occurrence_start)
    language = _resolve_language_mode(meeting_language)
    cognito_sub = resolve_user_sub(user_id) if user_id else None
    use_zoom = (
        use_stored_zoom_credentials and cognito_sub
        and has_zoom_credentials(cognito_sub)
    )
    return calendar_schedule.create(
        meeting_name, meeting_platform, meeting_id, scheduled_time, user_id,
        meeting_password=meeting_password or "", language=language,
        user_sub=cognito_sub, zoom_sub=cognito_sub if use_zoom else None,
        calendar_event_uid=calendar_event_uid,
        calendar_occurrence_start=calendar_occurrence_start,
    )

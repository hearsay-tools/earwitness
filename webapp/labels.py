"""Słownik języka interfejsu.

Nazwy techniczne (`fetch_assets`, `queued`, `google_meet`) są dobre w logach
i w bazie, ale w UI to żargon. Tu jest jedno miejsce, w którym zamieniamy je
na tekst dla człowieka — dzięki temu ta sama rzecz nazywa się tak samo na
liście spotkań, w szczegółach i w kolejce.

Surowy kod zostaje dostępny w `title`/tooltipie, bo przy debugowaniu jest
potrzebny.
"""

from __future__ import annotations

from typing import Optional

# --- kolejka -------------------------------------------------------------

JOB_KINDS: dict[str, str] = {
    "process": "Download + transcription",
    "fetch_assets": "Audio download",
    "transcribe": "Transcription",
    "sync_recall": "Recall sync",
    "sync_calendar": "Calendar sync",
    "backfill_titles": "Title backfill",
    "cleanup_audio": "Audio cleanup",
    "repair_assets": "Audio recovery from disk",
    "repair_participants": "Participant repair",
}

JOB_STATUSES: dict[str, str] = {
    "queued": "Waiting",
    "running": "In progress",
    "done": "Done",
    "failed": "Failed",
    "canceled": "Canceled",
}

# --- stan spotkania ------------------------------------------------------

ASSET_STATES: dict[str, str] = {
    "none": "Not downloaded",
    "queued": "Queued",
    "fetching": "Downloading",
    "ready": "On disk",
    "expired": "Expired",
    "failed": "Failed",
}

TRANSCRIPT_STATES: dict[str, str] = {
    "none": "None",
    "queued": "Queued",
    "running": "In progress",
    "ready": "Ready",
    "failed": "Failed",
}

# Status spotkania widoczny w UI — jedna oś wyprowadzana w webapp/models.py
# (kolejność = cykl życia). Surowy stan Recalla zostaje w tooltipach
# i w „Status in Recall” na szczegółach spotkania.
USER_STATUSES: dict[str, str] = {
    "upcoming": "Upcoming",
    "in_meeting": "In meeting",
    "processing": "Processing",
    "to_process": "To process",
    "ready": "Ready",
    "failed": "Failed",
    "no_recording": "No recording",
}

PLATFORMS: dict[str, str] = {
    "google_meet": "Google Meet",
    "zoom": "Zoom",
    "microsoft_teams": "Microsoft Teams",
    "teams": "Microsoft Teams",
    "webex": "Webex",
    "slack": "Slack Huddle",
}

# Po co bot był w spotkaniu, a nie wyszło — tłumaczymy najczęstsze sub_code'y,
# bo „timeout_exceeded_waiting_room” nic nie mówi osobie nietechnicznej.
STATUS_HINTS: dict[str, str] = {
    "timeout_exceeded_waiting_room": "Nobody let the bot in from the waiting room.",
    "timeout_exceeded_everyone_left": "Everyone left, so the bot stopped recording.",
    "timeout_exceeded_only_bots": "Only bots were left in the meeting.",
    "timeout_exceeded_silence": "Silence for a long stretch — the bot disconnected.",
    "meeting_not_started": "The meeting never started.",
    "bot_kicked_from_call": "The bot was removed from the meeting.",
    "bot_kicked_from_waiting_room": "The bot was rejected in the waiting room.",
    "permission_denied": "Recording was not permitted.",
    "meeting_locked": "The meeting was locked.",
}


def _lookup(table: dict[str, str], value: Optional[str]) -> str:
    if not value:
        return "—"
    return table.get(value, value.replace("_", " ").capitalize())


def job_kind(value: Optional[str]) -> str:
    return _lookup(JOB_KINDS, value)


def job_status(value: Optional[str]) -> str:
    return _lookup(JOB_STATUSES, value)


def asset_state(value: Optional[str]) -> str:
    return _lookup(ASSET_STATES, value)


def transcript_state(value: Optional[str]) -> str:
    return _lookup(TRANSCRIPT_STATES, value)


def user_status(value: Optional[str]) -> str:
    return _lookup(USER_STATUSES, value)


def platform(value: Optional[str]) -> str:
    return _lookup(PLATFORMS, value)


def status_hint(sub_code: Optional[str]) -> Optional[str]:
    return STATUS_HINTS.get(sub_code or "")

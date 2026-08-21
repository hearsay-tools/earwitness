"""Oś statusów widocznych dla użytkownika (issue #1).

Właściwość i wyrażenie SQL muszą się zgadzać na całej macierzy stanów —
to jedna para reguł, dwa wykonania.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from webapp.models import Meeting, USER_STATUS_ORDER, utcnow, user_status_case


def _m(**kw) -> Meeting:
    base = dict(
        id="bot-1",
        status_group="done",
        transcript_state="none",
        asset_state="none",
        recording_id="rec-1",
        media_expires_at=None,
    )
    base.update(kw)
    return Meeting(**base)


# (nazwa, pola nadpisujące _m(), oczekiwany status)
CASES = [
    ("transcript ready wygrywa ze wszystkim",
     dict(status_group="failed", transcript_state="ready", asset_state="failed"), "ready"),
    ("scheduled", dict(status_group="scheduled", recording_id=None), "upcoming"),
    ("joining", dict(status_group="joining", recording_id=None), "in_meeting"),
    ("recording", dict(status_group="recording", recording_id=None), "in_meeting"),
    ("bot failed", dict(status_group="failed", recording_id=None), "failed"),
    ("transcript failed mimo assets na dysku",
     dict(transcript_state="failed", asset_state="ready"), "failed"),
    ("asset failed", dict(asset_state="failed"), "failed"),
    ("asset fetching", dict(asset_state="fetching"), "processing"),
    ("transcript queued", dict(transcript_state="queued"), "processing"),
    ("transcript running z assets na dysku",
     dict(transcript_state="running", asset_state="ready"), "processing"),
    ("done + assets na dysku", dict(asset_state="ready"), "to_process"),
    ("expired + assets na dysku (dysk jest źródłem prawdy)",
     dict(status_group="expired", asset_state="ready"), "to_process"),
    ("done + recording, media bez TTL", dict(), "to_process"),
    ("done + recording, media żyje",
     dict(media_expires_at=utcnow() + dt.timedelta(hours=2)), "to_process"),
    ("done + recording, media wygasło",
     dict(media_expires_at=utcnow() - dt.timedelta(hours=2)), "no_recording"),
    ("done bez nagrania", dict(recording_id=None), "no_recording"),
    ("expired bez assetów", dict(status_group="expired", recording_id=None), "no_recording"),
]


@pytest.mark.parametrize("name,fields,expected", CASES, ids=[c[0] for c in CASES])
def test_user_status_property(name, fields, expected):
    assert _m(**fields).user_status == expected


def test_every_status_is_covered_by_cases():
    covered = {expected for _, _, expected in CASES}
    assert covered == set(USER_STATUS_ORDER)


def test_sql_case_agrees_with_property(session):
    """To samo wyrażenie filtro/fasetowe musi dawać identyczne wyniki."""
    for i, (_, fields, _) in enumerate(CASES):
        m = _m(**fields)
        m.id = f"bot-{i}"
        session.add(m)
    session.commit()
    rows = session.execute(
        select(Meeting.id, user_status_case(utcnow())).order_by(Meeting.id)
    ).all()
    by_id = {i: expected for i, (_, _, expected) in enumerate(CASES)}
    for mid, sql_status in rows:
        assert sql_status == by_id[int(mid.rsplit("-", 1)[1])]

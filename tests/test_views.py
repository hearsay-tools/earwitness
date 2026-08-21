"""Testy warstwy prezentacji.

Nie sprawdzamy pikseli — sprawdzamy to, co psuje się przy refaktorach:
czy stany puste mają wyjście, czy błędy są brandowane, czy żargon nie
przecieka do UI i czy tabele mają etykiety potrzebne na mobile.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from webapp import labels
from webapp import tasks
from webapp.app import app
from webapp.config import settings
from webapp.models import Job, Meeting, Transcript

HTML = {"accept": "text/html,application/xhtml+xml"}


@pytest.fixture()
def client(session):
    """TestClient na czystej bazie (fixture `session` czyści schemat)."""
    return TestClient(app)


@pytest.fixture()
def meeting(session):
    m = Meeting(
        id="bot-1",
        title="Sales Review",
        platform="google_meet",
        started_at=dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.timezone.utc),
        status_code="done",
        status_group="done",
        recording_id="rec-1",
        duration_seconds=1800,
    )
    session.add(m)
    session.commit()
    return m


# --------------------------------------------------------------------------
# Stany puste — każdy musi dawać drogę dalej
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url,cta", [
    ("/meetings", "/sync"),
    ("/transcripts", "/meetings"),
    ("/jobs", "/meetings"),
])
def test_empty_states_offer_a_way_out(client, url, cta):
    r = client.get(url, headers=HTML)
    assert r.status_code == 200
    assert 'class="art"' in r.text, "stan pusty bez ilustracji"
    assert cta in r.text, "stan pusty bez akcji prowadzącej dalej"


def test_filtered_empty_state_offers_clearing_filters(client):
    r = client.get("/meetings?q=nieistniejacafraza", headers=HTML)
    assert "Nothing matches the filters" in r.text
    assert "Clear filters" in r.text


# --------------------------------------------------------------------------
# Błędy
# --------------------------------------------------------------------------

def test_html_404_is_branded_not_raw_json(client):
    r = client.get("/meetings/nie-ma-takiego", headers=HTML)
    assert r.status_code == 404
    assert "<!doctype html>" in r.text.lower()
    assert "No such page" in r.text
    assert "/meetings" in r.text, "brak drogi powrotnej"


def test_api_404_stays_json(client):
    r = client.get("/api/nie-ma", headers=HTML)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


# --------------------------------------------------------------------------
# Język interfejsu
# --------------------------------------------------------------------------

def test_job_kinds_are_translated_in_ui(client, session, meeting):
    session.add(Job(kind="fetch_assets", status="done", meeting_id=meeting.id, progress=100))
    session.add(Job(kind="process", status="queued", meeting_id=meeting.id))
    session.commit()

    r = client.get("/jobs", headers=HTML)
    assert "Audio download" in r.text
    assert "Download + transcription" in r.text
    # Surowe nazwy zostają tylko w tooltipach/atrybutach, nie jako treść.
    assert ">fetch_assets<" not in r.text
    assert ">queued<" not in r.text


def test_status_labels_cover_every_state():
    from webapp.models import ASSET_STATES, TRANSCRIPT_STATES

    assert set(ASSET_STATES) <= set(labels.ASSET_STATES)
    assert set(TRANSCRIPT_STATES) <= set(labels.TRANSCRIPT_STATES)


def test_user_status_labels_cover_every_state():
    from webapp.models import USER_STATUS_ORDER

    assert set(USER_STATUS_ORDER) == set(labels.USER_STATUSES)


def test_meeting_badge_shows_user_status_with_recall_tooltip(client, session, meeting):
    r = client.get(f"/meetings/{meeting.id}", headers=HTML)
    assert 'class="badge b-to_process"' in r.text
    assert "To process" in r.text
    assert "Recall: done" in r.text


def test_unknown_code_degrades_gracefully():
    assert labels.job_kind("nowy_typ_zadania") == "Nowy typ zadania"
    assert labels.job_status(None) == "—"
    assert labels.platform("google_meet") == "Google Meet"


def test_status_hint_explains_recall_sub_codes():
    assert "waiting room" in labels.status_hint("timeout_exceeded_waiting_room")
    assert labels.status_hint("cos_nieznanego") is None


# --------------------------------------------------------------------------
# Akcje na spotkaniu: jedna intencja = jeden przycisk
# --------------------------------------------------------------------------

def _seed_axis(session):
    when = dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.timezone.utc)
    session.add(Meeting(id="bot-up", title="Planned", status_group="scheduled",
                        transcript_state="none", asset_state="none", started_at=when))
    session.add(Meeting(id="bot-none", title="Empty", status_group="done",
                        transcript_state="none", asset_state="none", started_at=when))
    session.add(Meeting(id="bot-ok", title="Done deal", status_group="done",
                        transcript_state="ready", asset_state="ready", started_at=when))
    session.commit()


def test_meetings_default_to_finished_view(client, session):
    _seed_axis(session)
    html = client.get("/meetings", headers=HTML).text
    assert "Empty" in html and "Done deal" in html
    assert "Planned" not in html
    assert "finished" in html.lower()  # podtytuł „N finished meetings”


def test_meetings_all_view_shows_everything(client, session):
    _seed_axis(session)
    html = client.get("/meetings?view=all", headers=HTML).text
    assert "Planned" in html


def test_status_deep_link_still_filters(client, session):
    _seed_axis(session)
    html = client.get("/meetings?status=ready", headers=HTML).text
    assert "Done deal" in html and "Empty" not in html


def test_api_exposes_user_status_and_defaults_to_all(client, session, meeting):
    data = client.get("/api/meetings").json()
    item = data["items"][0]
    assert item["user_status"] == "to_process"
    assert item["status"] == "done" and item["transcript_state"] == "none"


def test_sidebar_uses_single_status_axis(client, session, meeting):
    html = client.get("/meetings", headers=HTML).text
    for label in ("Upcoming", "In meeting", "Processing", "To process", "No recording"):
        assert f">{label}</span>" in html
    assert 'name="transcript"' not in html


def test_view_toggle_switches_between_finished_and_all(client, session, meeting):
    html = client.get("/meetings", headers=HTML).text
    assert "Show all meetings" in html
    assert "finished meeting" in html  # podtytuł liczby
    html = client.get("/meetings?view=all", headers=HTML).text
    assert "Show finished only" in html


def test_finished_empty_state_explains_itself(client, session):
    session.add(Meeting(id="bot-up", title="Planned", status_group="scheduled",
                        transcript_state="none", asset_state="none"))
    session.commit()
    html = client.get("/meetings", headers=HTML).text
    assert "No finished meetings yet" in html
    assert "Show all meetings" in html


def test_transcripts_empty_state_links_to_to_process(client, session):
    html = client.get("/transcripts", headers=HTML).text
    assert 'href="/meetings?status=to_process"' in html


def test_meeting_without_transcript_offers_one_action(client, session, meeting):
    r = client.get(f"/meetings/{meeting.id}", headers=HTML)
    assert "Get transcript" in r.text
    assert 'name="force"' not in r.text, "force to decyzja taska, nie wybór użytkownika"
    assert "<dialog" not in r.text, "nie ma za co zapłacić, nie ma o co pytać"


def test_redo_forces_asr_behind_a_confirmation(client, session, meeting):
    meeting.transcript_state = "ready"
    session.commit()

    r = client.get(f"/meetings/{meeting.id}", headers=HTML)
    assert "Redo transcript" in r.text
    assert 'name="force_asr" value="true"' in r.text
    assert 'data-confirm="redo-confirm"' in r.text and 'id="redo-confirm"' in r.text
    assert "data-confirm-ok" in r.text, "bez przycisku OK dialog nie ma czym potwierdzić"
    assert "30m 00s" in r.text, "koszt ma być konkretny, nie ogólne ostrzeżenie"


def test_meeting_without_recording_offers_nothing_to_run(client, session, meeting):
    meeting.recording_id = None
    session.commit()
    r = client.get(f"/meetings/{meeting.id}", headers=HTML)
    assert "No recording" in r.text
    assert "Get transcript" not in r.text


# --------------------------------------------------------------------------
# Mobile: kolumny tabeli muszą nieść własne etykiety
# --------------------------------------------------------------------------

def test_table_cells_carry_labels_for_card_layout(client, session, meeting):
    r = client.get("/meetings", headers=HTML)
    for label in ("Meeting", "When", "Participants", "Status", "Transcript"):
        assert f'data-label="{label}"' in r.text, f"brak data-label={label}"


# --------------------------------------------------------------------------
# Postęp i polling
# --------------------------------------------------------------------------

def test_only_active_jobs_are_marked_for_polling(client, session, meeting):
    session.add(Job(kind="process", status="done", meeting_id=meeting.id, progress=100))
    session.commit()
    r = client.get("/jobs", headers=HTML)
    assert "data-job-active" not in r.text, "zakończone zadanie nie może być pollowane"

    session.add(Job(kind="transcribe", status="running", meeting_id=meeting.id, progress=40))
    session.commit()
    r = client.get("/jobs", headers=HTML)
    assert "data-job-active" in r.text


def test_progress_bar_is_accessible(client, session, meeting):
    session.add(Job(kind="process", status="running", meeting_id=meeting.id, progress=42))
    session.commit()
    r = client.get("/jobs", headers=HTML)
    assert 'role="progressbar"' in r.text
    assert 'aria-valuenow="42"' in r.text
    assert "--p: 0.42" in r.text


def test_api_jobs_expose_human_labels(client, session, meeting):
    session.add(Job(kind="process", status="running", meeting_id=meeting.id, progress=10))
    session.commit()
    data = client.get("/api/jobs").json()
    item = data["items"][0]
    assert item["status_label"] == "In progress"
    assert item["kind_label"] == "Download + transcription"


# --------------------------------------------------------------------------
# Transkrypt: pobieranie i stan „w toku”
# --------------------------------------------------------------------------

def test_transcript_offers_every_export_format(client, session, meeting, tmp_path):
    path = tmp_path / "t.txt"
    path.write_text("Jan Kowalski [00:00:01] Cześć.\n", encoding="utf-8")
    t = Transcript(
        meeting_id=meeting.id, text_path=str(path), utterance_count=1,
        word_count=1, duration_seconds=2, speakers=[{"name": "Jan Kowalski", "seconds": 2}],
    )
    session.add(t)
    session.commit()

    r = client.get(f"/transcripts/{t.id}", headers=HTML)
    for fmt in ("txt", "md", "vtt", "json", "raw"):
        assert f"fmt={fmt}" in r.text
    # Widok listy nie oferuje surowego ASR — to narzędzie debugowe.
    r = client.get("/transcripts", headers=HTML)
    assert "fmt=raw" not in r.text


def test_transcript_text_resolves_relative_path(meeting):
    path = settings.transcripts_dir / meeting.id / "t.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Jan Kowalski [00:00:01] Cześć.\n", encoding="utf-8")

    t = Transcript(meeting_id=meeting.id, text_path=f"{meeting.id}/t.txt")

    assert tasks.transcript_text(t) == "Jan Kowalski [00:00:01] Cześć.\n"


def test_running_transcription_shows_skeleton_not_blank(client, session, meeting):
    meeting.transcript_state = "running"
    session.add(Job(kind="process", status="running", meeting_id=meeting.id, progress=55))
    session.commit()
    r = client.get(f"/meetings/{meeting.id}", headers=HTML)
    assert 'aria-busy="true"' in r.text
    assert 'class="utt"' in r.text, "brak szkieletu w docelowej geometrii"


def test_expired_media_is_explained(client, session, meeting):
    meeting.media_expires_at = dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc)
    meeting.asset_state = "expired"
    session.commit()
    r = client.get(f"/meetings/{meeting.id}", headers=HTML)
    assert "The audio expired" in r.text
    assert "24 hours" in r.text


# --------------------------------------------------------------------------
# Dostępność prezentacji
# --------------------------------------------------------------------------

def test_pages_have_skip_link_and_landmark(client):
    r = client.get("/meetings", headers=HTML)
    assert 'class="skip"' in r.text
    assert 'id="main"' in r.text


def test_decorative_art_is_hidden_from_screen_readers(client):
    r = client.get("/jobs", headers=HTML)
    assert 'aria-hidden="true"' in r.text
    # Ilustracje nie mogą mieć tekstu alternatywnego — są dekoracyjne.
    assert "<title>" not in r.text.split("<body")[1]

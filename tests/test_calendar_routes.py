"""Authenticated Recall Calendar management and OAuth bridge behavior."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy import select
from transcripts.recall_client import RecallConfig
from webapp import models
from webapp.app import app
from webapp.calendar_identity import ensure_identity
from webapp.config import settings
from webapp.models import CalendarIdentity, Meeting, User, utcnow
from webapp.recall_calendar import MODE_PREFERENCES, CalendarClient

HTML = {"accept": "text/html,application/xhtml+xml"}


@pytest.fixture()
def client(session):
    return TestClient(app)


def _csrf(html: str, action: str) -> str:
    form = re.search(
        rf'<form\b[^>]*action="{re.escape(action)}"[^>]*>(.*?)</form>',
        html,
        re.DOTALL,
    )
    assert form is not None, f"form for {action} missing"
    token = re.search(r'name="csrf_token" value="([^"]+)"', form.group(1))
    assert token is not None, f"CSRF token for {action} missing"
    return token.group(1)


def _dev_user(session) -> User:
    user = session.scalar(select(User).where(User.email == "dev@localhost"))
    if user is None:
        user = User(google_sub="dev", email="dev@localhost", name="Dev")
        session.add(user)
        session.commit()
    return user


def _preferences(mode: str = "off") -> dict:
    return {
        "id": "preferences-id",
        **MODE_PREFERENCES[mode],
        "bot_name": "Earwitness",
    }


class RecallFixture:
    def __init__(self) -> None:
        self.connected = False
        self.preferences = _preferences()
        self.fail_update = False
        self.fail_authenticate = False
        self.fail_reads = False
        self.requests: list[httpx.Request] = []
        self.upcoming_meetings: list[dict] | None = None

    def user(self, external_id: str = "stable-external") -> dict:
        connections = []
        if self.connected:
            connections.append(
                {
                    "connected": True,
                    "platform": "google",
                    "email": "dev@localhost",
                    "id": "connection-id",
                }
            )
        return {
            "id": "calendar-user-id",
            "external_id": external_id,
            "connections": connections,
            "preferences": dict(self.preferences),
        }

    def meeting(
        self, *, bot_id: str | None = None, override=None, will_record: bool = True
    ) -> dict:
        return {
            "id": "calendar-meeting-id",
            "override_should_record": override,
            "title": "Customer planning",
            "description": "",
            "will_record": will_record,
            "will_record_reason": "record_external",
            "start_time": "2026-09-07T09:00:00Z",
            "end_time": "2026-09-07T09:30:00Z",
            "platform": "google_calendar",
            "platform_id": "event-id",
            "meeting_platform": "google_meet",
            "calendar_platform": "google",
            "zoom_invite": None,
            "teams_invite": None,
            "meet_invite": {"meeting_id": "abc-defg-hij"},
            "webex_invite": None,
            "goto_meeting_invite": None,
            "bot_id": bot_id,
            "is_external": True,
            "is_hosted_by_me": True,
            "is_recurring": False,
            "organizer_email": "dev@localhost",
            "attendee_emails": ["guest@customer.test"],
            "attendees": [],
            "ical_uid": "event-id@example.com",
            "visibility": "default",
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/calendar/users/"):
            return httpx.Response(200, json=[])
        if path.endswith("/calendar/authenticate/"):
            if self.fail_authenticate:
                return httpx.Response(503, text="private token failure")
            return httpx.Response(200, json={"token": "calendar-user-token"})
        if path.endswith("/calendar/user/disconnect/"):
            self.connected = False
            return httpx.Response(200, json=self.user())
        if path.endswith("/calendar/meetings/"):
            meetings = self.upcoming_meetings or [
                self.meeting(bot_id="scheduled-bot"),
                self.meeting(override=False),
                self.meeting(bot_id="kept-bot", override=True),
                self.meeting(bot_id="pending-removal-bot", override=False),
            ]
            return httpx.Response(200, json=meetings)
        if path.endswith("/calendar/user/") and request.method == "PUT":
            if self.fail_update:
                return httpx.Response(503, text="private upstream detail")
            body = json.loads(request.content)
            self.preferences.update(body["preferences"])
            return httpx.Response(200, json=self.user())
        if path.endswith("/calendar/user/"):
            if self.fail_reads:
                return httpx.Response(503, text="private read failure")
            return httpx.Response(200, json=self.user())
        raise AssertionError(f"unexpected Recall request: {request.method} {path}")


@pytest.fixture()
def recall(client, monkeypatch) -> RecallFixture:
    from webapp import calendar_routes

    fixture = RecallFixture()
    config = RecallConfig(api_key="workspace-secret", region="eu-central-1")
    transport = httpx.MockTransport(fixture.handle)

    class BoundCalendarClient(CalendarClient):
        def __init__(self, external_id: str, **kwargs) -> None:
            super().__init__(external_id, config=config, transport=transport, **kwargs)

        @classmethod
        def list_users(cls, email: str, **kwargs) -> list[dict]:
            return CalendarClient.list_users(
                email, config=config, transport=transport, **kwargs
            )

    monkeypatch.setattr(calendar_routes, "CalendarClient", BoundCalendarClient)
    monkeypatch.setattr(settings, "recall_api_key", "workspace-secret")
    monkeypatch.setattr(settings, "recall_google_client_id", "google-client-id")
    monkeypatch.setattr(settings, "base_url", "https://earwitness.example")
    monkeypatch.setattr(settings, "recall_region", "eu-central-1")
    return fixture


def test_unconfigured_calendar_page_explains_recovery(client, monkeypatch):
    monkeypatch.setattr(settings, "recall_api_key", "")
    monkeypatch.setattr(settings, "recall_google_client_id", "")

    response = client.get("/calendar", headers=HTML)

    assert response.status_code == 200
    assert "Calendar setup is unavailable" in response.text
    assert "RECALL_API_KEY" in response.text
    assert "RECALL_GOOGLE_CLIENT_ID" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_calendar_oauth_attempt_is_persisted_with_separate_consumption_fields(
    session,
):
    attempt_type = getattr(models, "CalendarOAuthAttempt")
    user = _dev_user(session)
    attempt = attempt_type(
        user_id=user.id,
        state_digest="a" * 64,
        session_digest="b" * 64,
        return_nonce_digest="c" * 64,
        requested_mode="external",
        expires_at=utcnow() + dt.timedelta(minutes=10),
    )
    session.add(attempt)
    session.commit()

    saved = session.get(attempt_type, attempt.id)
    assert saved.bridge_consumed_at is None
    assert saved.return_consumed_at is None
    assert saved.requested_mode == "external"


def test_calendar_get_does_not_create_an_identity(client, session, recall):
    page = client.get("/calendar", headers=HTML)

    assert page.status_code == 200
    assert session.scalar(select(CalendarIdentity)) is None
    assert "Connect Google Calendar" in page.text
    assert "External only" in page.text
    assert "different email domain" in page.text


def test_anonymous_calendar_management_redirects_to_login(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_disabled", False)

    response = client.get("/calendar", headers=HTML, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login?next=/calendar"


def test_connect_rejects_missing_csrf_without_creating_identity(
    client, session, recall
):
    response = client.post(
        "/calendar/connect",
        data={"mode": "external"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/calendar"
    assert session.scalar(select(CalendarIdentity)) is None


def test_connect_creates_bound_oauth_attempt_and_exact_google_state(
    client, session, recall
):
    page = client.get("/calendar", headers=HTML)
    token = _csrf(page.text, "/calendar/connect")

    response = client.post(
        "/calendar/connect",
        data={"csrf_token": token, "mode": "external"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    parsed = urlparse(response.headers["location"])
    query = parse_qs(parsed.query)
    assert parsed._replace(query="", fragment="").geturl() == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    assert query["client_id"] == ["google-client-id"]
    assert query["redirect_uri"] == [
        "https://earwitness.example/calendar/oauth/callback"
    ]
    assert query["scope"] == [
        "https://www.googleapis.com/auth/calendar.events.readonly "
        "https://www.googleapis.com/auth/userinfo.email"
    ]
    state = json.loads(query["state"][0])
    assert state["google_oauth_redirect_url"] == query["redirect_uri"][0]
    assert state["recall_calendar_auth_token"] == "calendar-user-token"
    assert state["success_url"].startswith(
        "https://earwitness.example/calendar/oauth/return?"
    )
    assert state["error_url"].startswith(
        "https://earwitness.example/calendar/oauth/return?"
    )
    assert "workspace-secret" not in response.headers["location"]

    attempt_type = getattr(models, "CalendarOAuthAttempt")
    attempt = session.scalar(select(attempt_type))
    assert attempt.requested_mode == "external"
    assert query["state"][0] not in {attempt.state_digest, attempt.session_digest}


def _begin_oauth(client: TestClient, mode: str = "external") -> tuple[str, dict]:
    page = client.get("/calendar", headers=HTML)
    token = _csrf(page.text, "/calendar/connect")
    response = client.post(
        "/calendar/connect",
        data={"csrf_token": token, "mode": mode},
        follow_redirects=False,
    )
    state_text = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    return state_text, json.loads(state_text)


def _app_path(absolute_url: str) -> str:
    parsed = urlparse(absolute_url)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _bridge(client: TestClient, state_text: str, *, denied: bool = False):
    result = {"error": "access_denied"} if denied else {"code": "google-code"}
    return client.get(
        "/calendar/oauth/callback",
        params={**result, "state": state_text},
        follow_redirects=False,
    )


def test_callback_forwards_the_original_query_once(client, recall):
    state_text, _ = _begin_oauth(client)
    original = urlencode(
        [("code", "code/value+with space"), ("scope", "one two"), ("state", state_text)]
    )

    forwarded = client.get(
        f"/calendar/oauth/callback?{original}", follow_redirects=False
    )
    replayed = client.get(
        f"/calendar/oauth/callback?{original}", follow_redirects=False
    )

    assert forwarded.status_code == 307
    assert forwarded.headers["location"] == (
        "https://eu-central-1.recall.ai/api/v1/calendar/google_oauth_callback/"
        f"?{original}"
    )
    assert replayed.status_code == 303
    assert replayed.headers["location"] == "/calendar"
    assert replayed.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("duplicate", ["state", "code"])
def test_callback_rejects_ambiguous_duplicate_oauth_parameters(
    client, recall, duplicate
):
    state_text, _ = _begin_oauth(client)
    pairs = [("code", "one-code"), ("state", state_text)]
    pairs.append((duplicate, state_text if duplicate == "state" else "other-code"))

    response = client.get(
        "/calendar/oauth/callback?" + urlencode(pairs),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/calendar"


@pytest.mark.parametrize("result_pairs", [[], [("code", "one"), ("error", "denied")]])
def test_callback_requires_exactly_one_google_result(client, recall, result_pairs):
    state_text, _ = _begin_oauth(client)
    response = client.get(
        "/calendar/oauth/callback?" + urlencode([*result_pairs, ("state", state_text)]),
        follow_redirects=False,
    )

    assert response.headers["location"] == "/calendar"


@pytest.mark.parametrize("failure", ["forged", "expired", "wrong-session"])
def test_invalid_callback_attempt_never_redirects_to_recall(
    client, session, recall, failure
):
    state_text, _ = _begin_oauth(client)
    if failure == "forged":
        state_text += " "
    elif failure == "expired":
        attempt_type = getattr(models, "CalendarOAuthAttempt")
        attempt = session.scalar(select(attempt_type))
        attempt.expires_at = utcnow() - dt.timedelta(seconds=1)
        session.commit()
    else:
        client.cookies.clear()

    response = client.get(
        "/calendar/oauth/callback",
        params={"code": "private-code", "state": state_text},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/calendar"
    assert "recall.ai" not in response.headers["location"]


def test_success_return_connects_and_applies_requested_mode(client, recall):
    state_text, state = _begin_oauth(client, "internal")
    assert _bridge(client, state_text).status_code == 307
    recall.connected = True

    returned = client.get(_app_path(state["success_url"]), follow_redirects=False)
    page = client.get(returned.headers["location"], headers=HTML)

    assert returned.status_code == 303
    assert returned.headers["location"] == "/calendar"
    assert 'value="internal" checked' in page.text
    assert recall.preferences["record_internal"] is True
    assert recall.preferences["record_external"] is False

    replayed = client.get(_app_path(state["success_url"]), follow_redirects=False)
    assert replayed.headers["location"] == "/calendar"


def test_failed_first_setup_still_requires_an_explicit_mode(client, recall):
    state_text, state = _begin_oauth(client, "internal")
    assert _bridge(client, state_text, denied=True).status_code == 307
    failed = client.get(_app_path(state["error_url"]), follow_redirects=False)
    assert failed.headers["location"] == "/calendar"
    page = client.get("/calendar", headers=HTML)
    token = _csrf(page.text, "/calendar/connect")

    retried = client.post(
        "/calendar/connect",
        data={"csrf_token": token},
        follow_redirects=False,
    )

    assert retried.headers["location"] == "/calendar"
    assert "accounts.google.com" not in retried.headers["location"]


def test_return_cannot_skip_the_bridge_even_when_google_was_already_connected(
    client, recall
):
    _, state = _begin_oauth(client, "all")
    recall.connected = True

    response = client.get(_app_path(state["success_url"]), follow_redirects=False)

    assert response.headers["location"] == "/calendar"
    page = client.get("/calendar", headers=HTML)
    assert "invalid or expired" in page.text
    assert recall.preferences["record_external"] is False


def test_token_failure_after_identity_creation_still_requires_mode_on_retry(
    client, session, recall
):
    recall.fail_authenticate = True
    page = client.get("/calendar", headers=HTML)
    token = _csrf(page.text, "/calendar/connect")
    failed = client.post(
        "/calendar/connect",
        data={"csrf_token": token, "mode": "all"},
        follow_redirects=False,
    )
    assert failed.headers["location"] == "/calendar"
    assert session.scalar(select(CalendarIdentity)) is not None
    recall.fail_authenticate = False
    page = client.get("/calendar", headers=HTML)
    token = _csrf(page.text, "/calendar/connect")

    retried = client.post(
        "/calendar/connect",
        data={"csrf_token": token},
        follow_redirects=False,
    )

    assert retried.headers["location"] == "/calendar"
    rendered = client.get("/calendar", headers=HTML)
    assert "Choose a recording option" in rendered.text


def test_abandoned_initial_choice_survives_cross_user_attempt_cleanup(
    client, session, recall
):
    from webapp.calendar_routes import _invalidate_pending_attempts

    _begin_oauth(client, "all")
    attempt_type = getattr(models, "CalendarOAuthAttempt")
    abandoned = session.scalar(select(attempt_type))
    abandoned.expires_at = utcnow() - dt.timedelta(seconds=1)
    other = User(google_sub="other", email="other@example.test", name="Other")
    session.add(other)
    session.commit()

    _invalidate_pending_attempts(session, other.id)

    page = client.get("/calendar", headers=HTML)
    token = _csrf(page.text, "/calendar/connect")
    retried = client.post(
        "/calendar/connect",
        data={"csrf_token": token, "mode": "all"},
        follow_redirects=False,
    )
    assert "accounts.google.com" in retried.headers["location"]
    retry_state = parse_qs(urlparse(retried.headers["location"]).query)["state"][0]
    retry_attempt = session.scalar(select(attempt_type))
    assert retry_attempt.requested_mode == "all"

    assert _bridge(client, retry_state).status_code == 307
    recall.connected = True
    state = json.loads(retry_state)
    completed = client.get(_app_path(state["success_url"]), follow_redirects=False)
    assert completed.headers["location"] == "/calendar"
    identity = session.scalar(select(CalendarIdentity))
    session.refresh(identity)
    assert identity.setup_pending is False
    assert recall.preferences["record_external"] is True
    assert recall.preferences["record_internal"] is True


def test_preference_failure_keeps_submitted_choice(client, session, recall):
    user = _dev_user(session)
    ensure_identity(session, user, workspace_users=[])
    recall.connected = True
    recall.fail_update = True
    page = client.get("/calendar", headers=HTML)
    token = _csrf(page.text, "/calendar/preferences")

    response = client.post(
        "/calendar/preferences",
        data={"csrf_token": token, "mode": "all"},
        follow_redirects=False,
    )
    rendered = client.get(response.headers["location"], headers=HTML)

    assert response.status_code == 303
    assert 'value="all" checked' in rendered.text
    assert "temporarily unavailable" in rendered.text
    assert "private upstream detail" not in rendered.text


def test_preference_choice_survives_a_followup_read_outage_until_saved(
    client, session, recall
):
    user = _dev_user(session)
    ensure_identity(session, user, workspace_users=[])
    recall.connected = True
    page = client.get("/calendar", headers=HTML)
    token = _csrf(page.text, "/calendar/preferences")
    recall.fail_update = True
    recall.fail_reads = True

    failed = client.post(
        "/calendar/preferences",
        data={"csrf_token": token, "mode": "internal"},
        follow_redirects=False,
    )
    outage = client.get(failed.headers["location"], headers=HTML)

    assert 'value="internal" checked' in outage.text
    assert "Connection status unavailable" in outage.text
    assert 'action="/calendar/preferences"' in outage.text
    assert 'action="/calendar/connect"' not in outage.text
    assert "Not connected" not in outage.text

    recall.fail_reads = False
    recall.fail_update = False
    recovered = client.get("/calendar", headers=HTML)
    assert 'value="internal" checked' in recovered.text
    token = _csrf(recovered.text, "/calendar/preferences")
    saved = client.post(
        "/calendar/preferences",
        data={"csrf_token": token, "mode": "internal"},
        follow_redirects=False,
    )
    reloaded = client.get(saved.headers["location"], headers=HTML)
    assert 'value="internal" checked' in reloaded.text


def test_preferences_persist_on_reload_and_page_shows_meeting_states(
    client, session, recall
):
    user = _dev_user(session)
    ensure_identity(session, user, workspace_users=[])
    recall.connected = True
    page = client.get("/calendar", headers=HTML)
    token = _csrf(page.text, "/calendar/preferences")

    saved = client.post(
        "/calendar/preferences",
        data={"csrf_token": token, "mode": "external"},
        follow_redirects=False,
    )
    reloaded = client.get(saved.headers["location"], headers=HTML)

    assert 'value="external" checked' in reloaded.text
    assert "Customer planning" in reloaded.text
    assert "Bot scheduled" in reloaded.text
    assert "Manual skip" in reloaded.text


def test_bot_schedule_and_manual_override_are_both_visible(client, session, recall):
    user = _dev_user(session)
    ensure_identity(session, user, workspace_users=[])
    recall.connected = True

    page = client.get("/calendar", headers=HTML)

    assert page.text.count("Bot scheduled") == 3
    assert page.text.count("Manual recording") == 1
    assert page.text.count("Manual skip") == 2


def _badge_class(html: str, label: str) -> str:
    match = re.search(rf'class="badge ([^"]+)">{re.escape(label)}', html)
    assert match is not None, f"badge {label!r} missing"
    return match.group(1)


def _badge_background_token(css: str, class_name: str) -> str:
    for block in css.split("}"):
        header = block.split("{")[0]
        if not re.search(rf"\.{re.escape(class_name)}(?![\w-])", header):
            continue
        body = block.split("{", 1)[1] if "{" in block else ""
        match = re.search(r"background:\s*var\((--[\w-]+)\)", body)
        if match:
            return match.group(1)
    raise AssertionError(f"no background token for .{class_name}")


def test_bot_scheduled_and_not_scheduled_use_distinct_token_colors(
    client, session, recall
):
    user = _dev_user(session)
    ensure_identity(session, user, workspace_users=[])
    recall.connected = True
    recall.upcoming_meetings = [
        recall.meeting(bot_id="scheduled-bot"),
        recall.meeting(will_record=False),
    ]

    page = client.get("/calendar", headers=HTML)
    css = Path("webapp/static/app.css").read_text()
    scheduled = _badge_class(page.text, "Bot scheduled")
    unscheduled = _badge_class(page.text, "Not scheduled")

    assert scheduled != unscheduled
    assert _badge_background_token(css, scheduled) != _badge_background_token(
        css, unscheduled
    )
    assert _badge_background_token(css, "b-upcoming") == "--bg-3"


def test_reconnect_preserves_custom_preferences(client, session, recall):
    user = _dev_user(session)
    session.add(
        CalendarIdentity(
            user_id=user.id,
            external_id="adopted-external",
            setup_pending=False,
        )
    )
    session.commit()
    recall.preferences["record_only_host"] = True
    page = client.get("/calendar", headers=HTML)
    token = _csrf(page.text, "/calendar/connect")

    response = client.post(
        "/calendar/connect",
        data={"csrf_token": token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert getattr(models, "CalendarOAuthAttempt").__table__ is not None
    attempt = session.scalar(select(getattr(models, "CalendarOAuthAttempt")))
    assert attempt.requested_mode is None
    assert recall.preferences["record_only_host"] is True


def test_completed_setup_reconnect_preserves_custom_preferences(
    client, session, recall
):
    state_text, state = _begin_oauth(client, "external")
    assert _bridge(client, state_text).status_code == 307
    recall.connected = True
    client.get(_app_path(state["success_url"]), follow_redirects=False)
    recall.connected = False
    recall.preferences["record_only_host"] = True
    page = client.get("/calendar", headers=HTML)
    token = _csrf(page.text, "/calendar/connect")

    reconnect = client.post(
        "/calendar/connect",
        data={"csrf_token": token},
        follow_redirects=False,
    )

    assert "accounts.google.com" in reconnect.headers["location"]
    attempt_type = getattr(models, "CalendarOAuthAttempt")
    attempt = session.scalar(
        select(attempt_type).order_by(attempt_type.id.desc()).limit(1)
    )
    assert attempt.requested_mode is None
    assert recall.preferences["record_only_host"] is True


def test_disconnect_preserves_local_user_identity_and_recording(
    client, session, recall
):
    user = _dev_user(session)
    identity = ensure_identity(session, user, workspace_users=[])
    session.add(Meeting(id="historic-bot", title="Recorded call", recording_id="rec"))
    session.commit()
    recall.connected = True
    page = client.get("/calendar", headers=HTML)
    token = _csrf(page.text, "/calendar/disconnect")

    response = client.post(
        "/calendar/disconnect",
        data={"csrf_token": token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert recall.connected is False
    assert session.get(User, user.id) is not None
    assert (
        session.get(CalendarIdentity, identity.user_id).external_id
        == identity.external_id
    )
    assert session.get(Meeting, "historic-bot").recording_id == "rec"
    assert client.get("/calendar", headers=HTML).status_code == 200


def test_calendar_page_ignores_forged_success_query(client, recall):
    response = client.get("/calendar?success=Google+Calendar+connected.", headers=HTML)

    assert "Google Calendar connected." not in response.text


@pytest.mark.parametrize(
    ("endpoint", "data"),
    [
        ("/calendar/connect", {"mode": "all"}),
        ("/calendar/preferences", {"mode": "all"}),
        ("/calendar/disconnect", {}),
    ],
)
def test_non_ascii_csrf_is_safely_rejected_for_every_mutation(
    client, session, recall, endpoint, data
):
    client.get("/calendar", headers=HTML)

    response = client.post(
        endpoint,
        data={"csrf_token": "zażółć", **data},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/calendar"
    page = client.get("/calendar", headers=HTML)
    assert "That form expired" in page.text
    assert session.scalar(select(CalendarIdentity)) is None


def test_oauth_access_log_filter_removes_callback_secrets():
    from webapp.calendar_routes import CallbackQueryRedactionFilter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:1",
            "GET",
            "/calendar/oauth/callback?code=private&state=private",
            "1.1",
            307,
        ),
        None,
    )

    assert CallbackQueryRedactionFilter().filter(record) is True
    assert "private" not in record.getMessage()
    assert "/calendar/oauth/callback HTTP/1.1" in record.getMessage()


@pytest.mark.parametrize(
    ("requested", "target"),
    [
        ("", "/calendar"),
        ("/jobs?status=failed", "/jobs?status=failed"),
        ("https://attacker.example/", "/calendar"),
        ("//attacker.example/", "/calendar"),
    ],
)
def test_first_login_opens_calendar_unless_a_safe_intent_was_explicit(
    client, monkeypatch, requested, target
):
    import webapp.app as app_module

    class FakeGoogle:
        async def authorize_redirect(self, request, redirect_uri, **kwargs):
            return RedirectResponse("https://accounts.google.test/consent")

        async def authorize_access_token(self, request):
            return {
                "userinfo": {
                    "sub": "new-google-user",
                    "email": "new@example.com",
                    "email_verified": True,
                    "name": "New user",
                },
                "access_token": "app-login-token",
                "scope": "openid email",
            }

    monkeypatch.setattr(app_module, "oauth", SimpleNamespace(google=FakeGoogle()))
    monkeypatch.setattr(settings, "auth_disabled", False)
    monkeypatch.setattr(settings, "google_client_id", "app-google-client")
    monkeypatch.setattr(settings, "google_client_secret", "app-google-secret")

    started = client.get(
        "/auth/google?" + urlencode({"next": requested}), follow_redirects=False
    )
    assert started.status_code == 307
    completed = client.get("/auth/callback", follow_redirects=False)

    assert completed.status_code == 302
    assert completed.headers["location"] == target

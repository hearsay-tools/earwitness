from __future__ import annotations

import datetime as dt
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from transcripts.recall_client import RecallConfig
from webapp import calendar_identity
from webapp.calendar_identity import (
    CalendarIdentityAdoptionRequired,
    ensure_identity,
    find_identity,
)
from webapp.models import Base, CalendarIdentity, User
from webapp.recall_calendar import CalendarClient, CalendarError, recording_mode

FLAGS = (
    "record_non_host",
    "record_recurring",
    "record_external",
    "record_internal",
    "record_confirmed",
    "record_only_host",
)


def preferences(**overrides: object) -> dict:
    value = {
        "id": "0f45c674-2617-4aa4-98fe-edf04a73fc79",
        "record_non_host": False,
        "record_recurring": False,
        "record_external": False,
        "record_internal": False,
        "record_confirmed": False,
        "record_only_host": False,
        "bot_name": "Notetaker",
    }
    value.update(overrides)
    return value


def calendar_user(**overrides: object) -> dict:
    value = {
        "id": "01d175be-d5b0-4589-9770-7e8e0a02bef9",
        "external_id": "local-identity",
        "connections": [
            {
                "connected": True,
                "platform": "google",
                "email": "owner@example.com",
                "id": "google-connection",
            }
        ],
        "preferences": preferences(),
    }
    value.update(overrides)
    return value


def calendar_meeting(meeting_id: str) -> dict:
    return {
        "id": meeting_id,
        "override_should_record": None,
        "title": "Planning",
        "description": "",
        "will_record": True,
        "will_record_reason": "record_external",
        "start_time": "2026-09-07T09:00:00Z",
        "end_time": "2026-09-07T09:30:00Z",
        "platform": "google_calendar",
        "platform_id": "calendar-event",
        "meeting_platform": "google_meet",
        "calendar_platform": "google",
        "zoom_invite": None,
        "teams_invite": None,
        "meet_invite": {"meeting_id": "abc-defg-hij"},
        "webex_invite": None,
        "goto_meeting_invite": None,
        "bot_id": None,
        "is_external": True,
        "is_hosted_by_me": True,
        "is_recurring": False,
        "organizer_email": "owner@example.com",
        "attendee_emails": ["guest@outside.test"],
        "attendees": [],
        "ical_uid": "calendar-event@example.com",
        "visibility": "default",
    }


@pytest.fixture()
def recall_config() -> RecallConfig:
    return RecallConfig(api_key="workspace-secret", region="eu-central-1")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "off"),
        ({"record_external": True}, "external"),
        ({"record_internal": True}, "internal"),
        ({"record_external": True, "record_internal": True}, "all"),
        ({"record_external": True, "record_only_host": True}, "custom"),
        ({"record_non_host": True}, "custom"),
        ({"record_confirmed": True}, "custom"),
    ],
)
def test_recording_mode_recognizes_supported_six_flag_combinations(
    overrides: dict, expected: str
) -> None:
    assert recording_mode(preferences(**overrides)) == expected


def test_set_mode_sends_only_literal_flags_and_reads_back_preferences(
    recall_config: RecallConfig,
) -> None:
    requests: list[httpx.Request] = []
    saved = preferences(record_external=True, record_internal=True)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/authenticate/"):
            assert request.headers["authorization"] == "Token workspace-secret"
            assert request.read() == b'{"user_id":"local-identity"}'
            return httpx.Response(200, json={"token": "calendar-token"})
        assert request.headers["x-recallcalendarauthtoken"] == "calendar-token"
        if request.method == "PUT":
            assert json.loads(request.content) == {
                "preferences": {
                    "record_non_host": False,
                    "record_recurring": False,
                    "record_external": True,
                    "record_internal": True,
                    "record_confirmed": False,
                    "record_only_host": False,
                }
            }
            return httpx.Response(200, json=calendar_user(preferences=saved))
        return httpx.Response(200, json=calendar_user(preferences=saved))

    with CalendarClient(
        "local-identity",
        config=recall_config,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.set_mode("all")

    assert result["preferences"]["bot_name"] == "Notetaker"
    assert [request.method for request in requests] == ["POST", "PUT", "GET"]


def test_set_mode_rejects_a_readback_that_did_not_apply(
    recall_config: RecallConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authenticate/"):
            return httpx.Response(200, json={"token": "calendar-token"})
        return httpx.Response(200, json=calendar_user())

    with CalendarClient(
        "local-identity",
        config=recall_config,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(CalendarError, match="could not be verified"):
            client.set_mode("external")


def test_expired_token_is_refreshed_once_for_the_same_identity(
    recall_config: RecallConfig,
) -> None:
    auth_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authenticate/"):
            auth_bodies.append(request.read())
            return httpx.Response(200, json={"token": f"token-{len(auth_bodies)}"})
        if request.headers["x-recallcalendarauthtoken"] == "token-1":
            return httpx.Response(401, json={"detail": "expired private token"})
        return httpx.Response(200, json=calendar_user())

    with CalendarClient(
        "stable-id",
        config=recall_config,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.get_user()["id"] == calendar_user()["id"]

    assert auth_bodies == [b'{"user_id":"stable-id"}', b'{"user_id":"stable-id"}']


def test_repeated_unauthorized_response_refreshes_only_once(
    recall_config: RecallConfig,
) -> None:
    authentication_count = 0
    user_request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal authentication_count, user_request_count
        if request.url.path.endswith("/authenticate/"):
            authentication_count += 1
            return httpx.Response(200, json={"token": f"token-{authentication_count}"})
        user_request_count += 1
        return httpx.Response(401, json={"detail": "still expired"})

    with CalendarClient(
        "stable-id",
        config=recall_config,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(CalendarError, match="authorization expired"):
            client.get_user()

    assert authentication_count == 2
    assert user_request_count == 2


@pytest.mark.parametrize("status_code", [401, 429, 500])
def test_http_failures_do_not_expose_response_text(
    recall_config: RecallConfig, status_code: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            text="private upstream token and personal event",
            request=request,
        )

    with CalendarClient(
        "stable-id",
        config=recall_config,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(CalendarError) as caught:
            client.authenticate()

    assert "private upstream" not in str(caught.value)
    if status_code == 429:
        assert "too many requests" in str(caught.value).lower()


def test_timeout_has_safe_actionable_error(recall_config: RecallConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private network detail", request=request)

    with CalendarClient(
        "stable-id",
        config=recall_config,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(CalendarError) as caught:
            client.authenticate()

    assert "try again" in str(caught.value).lower()
    assert "private network detail" not in str(caught.value)


def test_disconnect_targets_only_google(recall_config: RecallConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authenticate/"):
            return httpx.Response(200, json={"token": "calendar-token"})
        assert request.method == "POST"
        assert json.loads(request.content) == {"platform": "google"}
        return httpx.Response(200, json=calendar_user(connections=[]))

    with CalendarClient(
        "stable-id",
        config=recall_config,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.disconnect()["connections"] == []


def test_meetings_accepts_an_unpaginated_list_and_sends_time_window(
    recall_config: RecallConfig,
) -> None:
    meeting = calendar_meeting("f4c7ee28-c79d-4a61-9928-2c9815ce7260")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authenticate/"):
            return httpx.Response(200, json={"token": "calendar-token"})
        assert request.url.params.get("start_time_after")
        assert request.url.params.get("start_time_before")
        after = dt.datetime.fromisoformat(request.url.params["start_time_after"])
        before = dt.datetime.fromisoformat(request.url.params["start_time_before"])
        assert before - after == dt.timedelta(days=14)
        return httpx.Response(200, json=[meeting])

    with CalendarClient(
        "stable-id",
        config=recall_config,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.meetings() == [meeting]


def test_meetings_follows_same_endpoint_pagination(recall_config: RecallConfig) -> None:
    first = calendar_meeting("87b8badd-2533-4aa8-a66a-b169bcb83a24")
    second = calendar_meeting("9321c6b9-7b8d-4df4-83bb-4382a2157d56")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authenticate/"):
            return httpx.Response(200, json={"token": "calendar-token"})
        if request.url.params.get("cursor") == "second":
            return httpx.Response(200, json={"results": [second], "next": None})
        return httpx.Response(
            200,
            json={
                "results": [first],
                "next": (
                    "https://eu-central-1.recall.ai/api/v1/calendar/meetings/"
                    "?cursor=second"
                ),
            },
        )

    with CalendarClient(
        "stable-id",
        config=recall_config,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.meetings() == [first, second]


def test_meetings_resolves_query_relative_pagination_against_collection(
    recall_config: RecallConfig,
) -> None:
    first = calendar_meeting("1ca37fc9-77ac-4f44-9278-45f95af1268b")
    second = calendar_meeting("d9007004-b8ac-4609-bb0b-52f083748729")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authenticate/"):
            return httpx.Response(200, json={"token": "calendar-token"})
        if request.url.params.get("cursor") == "next":
            assert request.url.path == "/api/v1/calendar/meetings/"
            return httpx.Response(200, json={"results": [second], "next": None})
        return httpx.Response(
            200,
            json={"results": [first], "next": "?cursor=next"},
        )

    with CalendarClient(
        "stable-id",
        config=recall_config,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.meetings() == [first, second]


def test_meetings_stops_at_page_cap(recall_config: RecallConfig) -> None:
    meeting_request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal meeting_request_count
        if request.url.path.endswith("/authenticate/"):
            return httpx.Response(200, json={"token": "calendar-token"})
        meeting_request_count += 1
        return httpx.Response(
            200,
            json={
                "results": [],
                "next": (
                    "https://eu-central-1.recall.ai/api/v1/calendar/meetings/"
                    f"?cursor={meeting_request_count + 1}"
                ),
            },
        )

    with CalendarClient(
        "stable-id",
        config=recall_config,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(CalendarError, match="pagination"):
            client.meetings()

    assert meeting_request_count == 100


@pytest.mark.parametrize(
    "next_url",
    [
        "https://attacker.example/api/v1/calendar/meetings/?cursor=secret",
        "https://eu-central-1.recall.ai/api/v1/calendar/users/?cursor=secret",
    ],
)
def test_meetings_rejects_foreign_pagination_urls(
    recall_config: RecallConfig, next_url: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authenticate/"):
            return httpx.Response(200, json={"token": "calendar-token"})
        return httpx.Response(200, json={"results": [], "next": next_url})

    with CalendarClient(
        "stable-id",
        config=recall_config,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(CalendarError, match="pagination"):
            client.meetings()


def test_list_users_uses_workspace_token_and_email_filter(
    recall_config: RecallConfig,
) -> None:
    user = calendar_user()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Token workspace-secret"
        assert "x-recallcalendarauthtoken" not in request.headers
        assert dict(request.url.params) == {"email": "owner@example.com"}
        return httpx.Response(200, json=[user])

    result = CalendarClient.list_users(
        "owner@example.com",
        config=recall_config,
        transport=httpx.MockTransport(handler),
    )

    assert result == [user]


def make_user(email: str, sub: str) -> User:
    return User(google_sub=sub, email=email, name=email, is_active=True)


def test_find_identity_does_not_create_one(session: Session) -> None:
    user = make_user("new@example.com", "new-sub")
    session.add(user)
    session.commit()

    assert find_identity(session, user) is None
    assert session.scalars(select(CalendarIdentity)).all() == []


def test_ensure_identity_reuses_the_committed_mapping(session: Session) -> None:
    user = make_user("owner@example.com", "owner-sub")
    session.add(user)
    session.commit()

    first = ensure_identity(session, user, workspace_users=[])
    second = ensure_identity(session, user, workspace_users=[])

    assert first.external_id == second.external_id
    assert first.setup_pending is True
    assert session.get(CalendarIdentity, user.id).external_id == first.external_id


def test_competing_identity_creations_return_the_committed_winner(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'identity-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def set_wal(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as setup_session:
        user = make_user("race@example.com", "race-sub")
        setup_session.add(user)
        setup_session.commit()
        user_id = user.id

    allocation_barrier = threading.Barrier(2)
    real_new_external_id = calendar_identity._new_external_id

    def synchronized_external_id() -> str:
        value = real_new_external_id()
        allocation_barrier.wait(timeout=5)
        return value

    monkeypatch.setattr(calendar_identity, "_new_external_id", synchronized_external_id)

    def allocate() -> str:
        with sessions() as worker_session:
            worker_user = worker_session.get(User, user_id)
            return ensure_identity(
                worker_session, worker_user, workspace_users=[]
            ).external_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        identities = list(executor.map(lambda _index: allocate(), range(2)))

    with sessions() as verify_session:
        persisted = verify_session.scalars(select(CalendarIdentity)).all()

    assert identities[0] == identities[1]
    assert len(persisted) == 1
    assert persisted[0].external_id == identities[0]


def test_ensure_identity_assigns_distinct_users_distinct_ids(session: Session) -> None:
    first_user = make_user("first@example.com", "first-sub")
    second_user = make_user("second@example.com", "second-sub")
    session.add_all([first_user, second_user])
    session.commit()

    first = ensure_identity(session, first_user, workspace_users=[])
    second = ensure_identity(session, second_user, workspace_users=[])

    assert first.external_id != second.external_id


def test_external_identity_is_unique(session: Session) -> None:
    first_user = make_user("first@example.com", "first-sub")
    second_user = make_user("second@example.com", "second-sub")
    session.add_all([first_user, second_user])
    session.commit()
    session.add(CalendarIdentity(user_id=first_user.id, external_id="shared"))
    session.commit()

    session.add(CalendarIdentity(user_id=second_user.id, external_id="shared"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_fresh_identity_refuses_an_existing_google_owner(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = make_user("owner@example.com", "owner-sub")
    session.add(user)
    session.commit()
    looked_up: list[str] = []

    def fake_list_users(email: str, **_kwargs: object) -> list[dict]:
        looked_up.append(email)
        return [calendar_user()]

    monkeypatch.setattr(CalendarClient, "list_users", fake_list_users)

    with pytest.raises(CalendarIdentityAdoptionRequired):
        ensure_identity(session, user)

    assert looked_up == ["owner@example.com"]
    assert find_identity(session, user) is None


def test_disconnected_or_other_email_does_not_block_identity(session: Session) -> None:
    user = make_user("owner@example.com", "owner-sub")
    session.add(user)
    session.commit()
    other = calendar_user(
        connections=[
            {
                "connected": True,
                "platform": "google",
                "email": "other@example.com",
                "id": "other-google-connection",
            },
            {
                "connected": False,
                "platform": "google",
                "email": "owner@example.com",
                "id": "old-google-connection",
            },
        ]
    )

    identity = ensure_identity(session, user, workspace_users=[other])

    assert identity.user_id == user.id


def test_email_filtered_connected_google_user_without_returned_email_blocks_creation(
    session: Session,
) -> None:
    user = make_user("owner@example.com", "owner-sub")
    session.add(user)
    session.commit()
    existing = calendar_user(
        connections=[
            {
                "connected": True,
                "platform": "google",
                "id": "google-connection",
            }
        ]
    )

    with pytest.raises(CalendarIdentityAdoptionRequired):
        ensure_identity(session, user, workspace_users=[existing])


def test_existing_database_initialization_adds_calendar_identity_table(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'existing.db'}")
    User.__table__.create(engine)
    with Session(engine) as session:
        user = make_user("existing@example.com", "existing-sub")
        session.add(user)
        session.commit()

    Base.metadata.create_all(engine)

    with Session(engine) as session:
        user = session.scalar(select(User).where(User.email == "existing@example.com"))
        identity = ensure_identity(session, user, workspace_users=[])
        identity_user_id = identity.user_id
        user_id = user.id

    assert "calendar_identities" in inspect(engine).get_table_names()
    assert identity_user_id == user_id


def test_list_users_can_be_injected_into_identity_creation(
    session: Session,
) -> None:
    user = make_user("owner@example.com", "owner-sub")
    session.add(user)
    session.commit()

    with pytest.raises(CalendarIdentityAdoptionRequired):
        ensure_identity(session, user, workspace_users=[calendar_user()])


def test_all_supported_modes_define_every_documented_flag() -> None:
    expected: dict[str, dict[str, bool]] = {
        "off": {
            "record_non_host": False,
            "record_recurring": False,
            "record_external": False,
            "record_internal": False,
            "record_confirmed": False,
            "record_only_host": False,
        },
        "external": {
            "record_non_host": False,
            "record_recurring": False,
            "record_external": True,
            "record_internal": False,
            "record_confirmed": False,
            "record_only_host": False,
        },
        "internal": {
            "record_non_host": False,
            "record_recurring": False,
            "record_external": False,
            "record_internal": True,
            "record_confirmed": False,
            "record_only_host": False,
        },
        "all": {
            "record_non_host": False,
            "record_recurring": False,
            "record_external": True,
            "record_internal": True,
            "record_confirmed": False,
            "record_only_host": False,
        },
    }

    for mode, literal_flags in expected.items():
        assert set(literal_flags) == set(FLAGS)
        assert recording_mode(literal_flags) == mode

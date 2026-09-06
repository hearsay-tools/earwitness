"""Small synchronous client for Recall Calendar V1."""

from __future__ import annotations

import datetime as dt
from types import TracebackType
from typing import Any, Self

import httpx
from transcripts.recall_client import RecallConfig

from webapp.config import settings


class CalendarError(RuntimeError):
    """A safe, human-facing Calendar integration failure."""


PREFERENCE_FLAGS = (
    "record_non_host",
    "record_recurring",
    "record_external",
    "record_internal",
    "record_confirmed",
    "record_only_host",
)

MODE_PREFERENCES: dict[str, dict[str, bool]] = {
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

_MEETING_PATH = "/calendar/meetings/"
_MAX_MEETING_PAGES = 100


def recording_mode(preferences: dict) -> str:
    """Collapse Recall's six recording flags into the four supported choices."""

    actual = {flag: preferences.get(flag) is True for flag in PREFERENCE_FLAGS}
    for mode, expected in MODE_PREFERENCES.items():
        if actual == expected:
            return mode
    return "custom"


class CalendarClient:
    """Recall Calendar V1 client bound to one stable external identity.

    ``transport`` is accepted for httpx ``MockTransport`` tests. A supplied
    ``client`` remains owned by the caller; otherwise this object closes its
    client when used as a context manager.
    """

    def __init__(
        self,
        external_id: str,
        *,
        config: RecallConfig | None = None,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not external_id:
            raise ValueError("external_id is required")
        if client is not None and transport is not None:
            raise ValueError("Pass client or transport, not both.")
        self.external_id = external_id
        self.config = config or self._config_from_settings()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.config.base_url,
            headers={"Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        self._token: str | None = None

    @staticmethod
    def _config_from_settings() -> RecallConfig:
        try:
            return RecallConfig.from_env(
                api_key=settings.recall_api_key or None,
                region=settings.recall_region or None,
            )
        except ValueError as exc:
            raise CalendarError(
                "Recall Calendar is not configured. Ask an administrator to "
                "check the workspace settings."
            ) from exc

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._owns_client:
            self._client.close()

    @classmethod
    def list_users(
        cls,
        email: str,
        *,
        config: RecallConfig | None = None,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        timeout: float = 15.0,
    ) -> list[dict]:
        """List workspace Calendar users filtered by exact owner email."""

        with cls(
            "workspace-lookup",
            config=config,
            transport=transport,
            client=client,
            timeout=timeout,
        ) as calendar:
            payload = calendar._workspace_request(
                "GET", "/calendar/users/", params={"email": email}
            )
        if isinstance(payload, list) and all(
            isinstance(item, dict) for item in payload
        ):
            return payload
        if isinstance(payload, dict):
            results = payload.get("results")
            if isinstance(results, list) and all(
                isinstance(item, dict) for item in results
            ):
                return results
        raise CalendarError("Recall returned an invalid calendar-user list.")

    def authenticate(self) -> str:
        """Mint and retain a per-user token for this exact external identity."""

        payload = self._workspace_request(
            "POST",
            "/calendar/authenticate/",
            json={"user_id": self.external_id},
        )
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise CalendarError("Recall did not return a valid calendar token.")
        self._token = token
        return token

    def get_user(self) -> dict:
        payload = self._user_request("GET", "/calendar/user/")
        return self._require_object(payload, "calendar user")

    def set_mode(self, mode: str) -> dict:
        try:
            flags = MODE_PREFERENCES[mode]
        except KeyError as exc:
            raise ValueError(f"Unsupported recording mode: {mode}") from exc

        # Recall defines PUT with a patched nested preferences serializer. Sending
        # only these six fields preserves bot_name and other unrelated properties.
        self._user_request(
            "PUT",
            "/calendar/user/",
            json={"preferences": dict(flags)},
        )
        user = self.get_user()
        saved = user.get("preferences")
        if not isinstance(saved, dict) or any(
            saved.get(flag) is not expected for flag, expected in flags.items()
        ):
            raise CalendarError(
                "The recording preference could not be verified. Please try again."
            )
        return user

    def disconnect(self) -> dict:
        payload = self._user_request(
            "POST",
            "/calendar/user/disconnect/",
            json={"platform": "google"},
        )
        return self._require_object(payload, "calendar user")

    def meetings(self) -> list[dict]:
        now = dt.datetime.now(dt.timezone.utc)
        params = {
            "start_time_after": now.isoformat(),
            "start_time_before": (now + dt.timedelta(days=14)).isoformat(),
        }
        payload = self._user_request("GET", _MEETING_PATH, params=params)
        if isinstance(payload, list):
            return self._require_object_list(payload, "calendar meetings")

        meetings: list[dict] = []
        next_url: str | None = self._consume_meeting_page(payload, meetings)
        seen: set[str] = set()
        page_count = 1
        while next_url is not None:
            page_count += 1
            if page_count > _MAX_MEETING_PAGES or next_url in seen:
                raise CalendarError("Recall returned invalid calendar pagination.")
            seen.add(next_url)
            safe_url = self._safe_meeting_page_url(next_url)
            payload = self._user_request("GET", safe_url)
            next_url = self._consume_meeting_page(payload, meetings)
        return meetings

    def _workspace_request(self, method: str, url: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Token {self.config.api_key}"}
        return self._request(method, url, headers=headers, **kwargs)

    def _user_request(
        self,
        method: str,
        url: str,
        *,
        _can_refresh: bool = True,
        **kwargs: Any,
    ) -> Any:
        if self._token is None:
            self.authenticate()
        response = self._send(
            method,
            url,
            headers={"x-recallcalendarauthtoken": self._token},
            **kwargs,
        )
        if response.status_code == 401 and _can_refresh:
            self.authenticate()
            return self._user_request(method, url, _can_refresh=False, **kwargs)
        return self._decode_response(response)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        return self._decode_response(self._send(method, url, **kwargs))

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise CalendarError(
                "Recall Calendar timed out. Please try again in a moment."
            ) from exc
        except httpx.RequestError as exc:
            raise CalendarError(
                "Recall Calendar could not be reached. Please try again in a moment."
            ) from exc

    @staticmethod
    def _decode_response(response: httpx.Response) -> Any:
        if response.status_code == 429:
            raise CalendarError(
                "Recall Calendar received too many requests. Please wait and try again."
            )
        if response.status_code == 401:
            raise CalendarError(
                "Calendar authorization expired. Please reconnect your calendar."
            )
        if response.status_code >= 500:
            raise CalendarError(
                "Recall Calendar is temporarily unavailable. Please try again later."
            )
        if response.status_code >= 400:
            raise CalendarError(
                "The calendar request could not be completed. Please check the "
                "connection and try again."
            )
        try:
            return response.json()
        except ValueError as exc:
            raise CalendarError(
                "Recall returned an invalid calendar response."
            ) from exc

    @staticmethod
    def _require_object(payload: Any, name: str) -> dict:
        if not isinstance(payload, dict):
            raise CalendarError(f"Recall returned an invalid {name} response.")
        return payload

    @staticmethod
    def _require_object_list(payload: Any, name: str) -> list[dict]:
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise CalendarError(f"Recall returned an invalid {name} response.")
        return payload

    def _consume_meeting_page(self, payload: Any, meetings: list[dict]) -> str | None:
        if not isinstance(payload, dict):
            raise CalendarError("Recall returned invalid calendar pagination.")
        results = self._require_object_list(payload.get("results"), "meeting page")
        meetings.extend(results)
        next_url = payload.get("next")
        if next_url is not None and not isinstance(next_url, str):
            raise CalendarError("Recall returned invalid calendar pagination.")
        return next_url or None

    def _safe_meeting_page_url(self, candidate: str) -> str:
        base = httpx.URL(self.config.base_url)
        collection = httpx.URL(f"{self.config.base_url.rstrip('/')}{_MEETING_PATH}")
        url = collection.join(candidate)
        same_origin = (
            url.scheme == base.scheme
            and url.host == base.host
            and url.port == base.port
        )
        if (
            not same_origin
            or url.path != collection.path
            or url.username
            or url.password
            or url.fragment
        ):
            raise CalendarError("Recall returned unsafe calendar pagination.")
        return str(url)

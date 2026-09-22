"""Globalny webhook gotowego transkryptu.

Konfiguracja jest jedna na instancję i żyje w `app_settings` (UI, nie env).
Po udanym pipeline'ie kolejkowany jest job `webhook_deliver`. Dostawa nie
jest krokiem transkrypcji: porażka zostaje w kolejce i nie rusza
`transcript_state`.

Token Bearer jest czytany z bazy dopiero przy wysyłce. Nie wraca do HTML,
nie wchodzi do `job.args` / `job.result` / `job.error` / logu joba. Żądanie
idzie wprost przez transport httpx, bez klienta, który loguje pełny URL
(przy GET w query siedzi treść transkryptu) i bez podążania za przekierowaniem
(Bearer nie może wyciec na inny host).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import (
    parse_qsl,
    quote,
    quote_plus,
    unquote,
    unquote_plus,
    urlencode,
    urlsplit,
    urlunsplit,
)

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from webapp.jobs import Job, JobError, enqueue
from webapp.models import AppSetting, Meeting, Transcript, utcnow

log = logging.getLogger("webapp.webhook")

KIND = "webhook_deliver"
EVENT = "transcript.ready"
SCHEMA = "earwitness.transcript.ready.v1"
METHODS = ("POST", "GET")
MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 30.0
GET_URL_LIMIT = 8000
CONFIGURED_URL_LIMIT = 2000
TOKEN_LIMIT = 2000
USER_AGENT = "Earwitness-Webhook/1"

URL_KEY = "webhook_url"
METHOD_KEY = "webhook_method"
TOKEN_KEY = "webhook_bearer_token"

_SECRET_QUERY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "bearer",
    "api_key",
    "apikey",
    "signature",
    "credential",
    "access_key",
    "private_key",
)
_SECRET_QUERY_EXACT = frozenset({"sig", "key", "auth", "pwd"})
_RESERVED_QUERY = "payload"

CONFIG_ERRORS = {
    "invalid_url": (
        "Webhook URL must be a valid http or https URL with a host and a "
        "usable port, and without a username, password, credential query "
        "parameter, or the bearer token."
    ),
    "invalid_method": "Method must be POST or GET.",
    "invalid_token": (
        "Bearer token cannot contain whitespace, line breaks, or control characters."
    ),
}


class WebhookConfigError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(CONFIG_ERRORS.get(code, code))
        self.code = code


@dataclass(frozen=True)
class WebhookConfig:
    url: str
    method: str
    token: str

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @property
    def has_token(self) -> bool:
        return bool(self.token)


def public_settings(cfg: WebhookConfig) -> dict[str, Any]:
    """Widok do szablonu — bez tokenu, nawet pustego."""
    return {
        "url": cfg.url,
        "method": cfg.method,
        "has_token": cfg.has_token,
        "enabled": cfg.enabled,
    }


def _secret_forms(secret: str) -> tuple[str, ...]:
    forms = {secret, quote(secret, safe=""), quote_plus(secret)}
    return tuple(sorted((form for form in forms if form), key=len, reverse=True))


def redact(text: str, secret: str) -> str:
    if not text or not secret:
        return text
    redacted = text.replace(f"Bearer {secret}", "Bearer [redacted]")
    for form in _secret_forms(secret):
        redacted = redacted.replace(form, "[redacted]")
    return redacted


def _read(session: Session, key: str) -> str:
    row = session.get(AppSetting, key)
    if row is None or not row.value:
        return ""
    return row.value


def get_config(session: Session) -> WebhookConfig:
    method = _read(session, METHOD_KEY).strip().upper() or "POST"
    if method not in METHODS:
        method = "POST"
    return WebhookConfig(
        url=_read(session, URL_KEY).strip(),
        method=method,
        token=_read(session, TOKEN_KEY),
    )


def _upsert(session: Session, key: str, value: str) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def _validate_method(method: str) -> str:
    cleaned = (method or "").strip().upper()
    if cleaned not in METHODS:
        raise WebhookConfigError("invalid_method")
    return cleaned


def _secret_query_name(name: str) -> bool:
    key = name.lower().replace("-", "_")
    if key == _RESERVED_QUERY or key in _SECRET_QUERY_EXACT:
        return True
    return any(part in key for part in _SECRET_QUERY_PARTS)


def _validate_url(url: str) -> str:
    cleaned = (url or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) > CONFIGURED_URL_LIMIT or any(
        c.isspace() or ord(c) < 32 for c in cleaned
    ):
        raise WebhookConfigError("invalid_url")
    try:
        parts = urlsplit(cleaned)
        port = parts.port
    except ValueError:
        raise WebhookConfigError("invalid_url") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebhookConfigError("invalid_url")
    if port is not None and not 1 <= port <= 65535:
        raise WebhookConfigError("invalid_url")
    if parts.username or parts.password:
        raise WebhookConfigError("invalid_url")
    if any(
        _secret_query_name(name)
        for name, _ in parse_qsl(parts.query, keep_blank_values=True)
    ):
        raise WebhookConfigError("invalid_url")
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _validate_token(token: str) -> str:
    if (
        any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token)
        or len(token) > TOKEN_LIMIT
    ):
        raise WebhookConfigError("invalid_token")
    try:
        token.encode("latin-1")
    except UnicodeEncodeError:
        raise WebhookConfigError("invalid_token") from None
    return token


def _url_contains_secret(url: str, secret: str) -> bool:
    if not secret:
        return False
    haystacks = (url, unquote(url), unquote_plus(url))
    return any(
        needle in haystack for haystack in haystacks for needle in _secret_forms(secret)
    )


def save_config(
    session: Session,
    *,
    url: str,
    method: str,
    token: str,
    clear_token: bool,
) -> None:
    """Zapisz URL, metodę i token. Pusty URL wyłącza wysyłkę.

    Pusty token zostawia zapisany, chyba że `clear_token`. Nowy token
    wygrywa z kasowaniem. Token wklejony w URL — nowy albo już zapisany,
    dowolnej długości — jest odrzucany, bo URL wraca do formularza.
    """
    clean_url = _validate_url(url)
    clean_method = _validate_method(method)
    incoming = _validate_token(token) if token else ""
    current = get_config(session)
    if incoming:
        stored = incoming
    elif clear_token:
        stored = ""
    else:
        stored = current.token
    if any(
        _url_contains_secret(clean_url, secret) for secret in (stored, current.token)
    ):
        raise WebhookConfigError("invalid_url")

    def apply() -> None:
        _upsert(session, URL_KEY, clean_url)
        _upsert(session, METHOD_KEY, clean_method)
        _upsert(session, TOKEN_KEY, stored)

    try:
        apply()
        session.commit()
    except IntegrityError:
        session.rollback()
        apply()
        session.commit()


def queue_delivery(
    session: Session,
    meeting: Meeting,
    transcript_id: int,
    *,
    priority: int = 80,
    created_by: Optional[str] = None,
) -> Optional[Job]:
    """Zakolejkuj dostawę. None, gdy webhook jest wyłączony.

    Klucz zawiera id transkryptu: nowa transkrypcja to nowa wysyłka, a drugie
    kliknięcie na ten sam gotowy tekst nie odpala dwóch jobów naraz.
    """
    if not get_config(session).enabled:
        return None
    return enqueue(
        session,
        KIND,
        meeting_id=meeting.id,
        args={"transcript_id": int(transcript_id)},
        priority=priority,
        dedupe_key=f"{KIND}:{meeting.id}:{transcript_id}",
        max_attempts=MAX_ATTEMPTS,
        created_by=created_by,
    )


def _iso(value: Optional[dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


def build_payload(
    meeting: Meeting,
    transcript: Transcript,
    text: str,
    *,
    delivered_at: dt.datetime,
    job_id: Optional[int],
    attempt: int,
) -> dict[str, Any]:
    """Dokument `earwitness.transcript.ready.v1`.

    `transcript.text` to ten sam plik co pobranie `.txt`. Uczestnicy to ludzie
    (Recall + kalendarz), bez botów-notetakerów. `delivery.job_id` jest stałe
    przy retry; `delivered_at` i `attempt` opisują tę próbę.
    """
    return {
        "schema": SCHEMA,
        "event": EVENT,
        "delivered_at": _iso(delivered_at),
        "delivery": {"job_id": job_id, "attempt": attempt},
        "meeting": {
            "id": meeting.id,
            "title": meeting.title,
            "platform": meeting.platform,
            "meeting_url": meeting.meeting_url,
            "native_id": meeting.meeting_native_id,
            "occurred_at": _iso(meeting.occurred_at),
            "started_at": _iso(meeting.started_at),
            "completed_at": _iso(meeting.completed_at),
            "duration_seconds": meeting.duration_seconds,
            "status": meeting.status_group,
            "user_status": meeting.user_status,
            "organizer": meeting.calendar_organizer,
            "calendar_event_id": meeting.calendar_event_id,
            "calendar_link": meeting.calendar_html_link,
            "recording_id": meeting.recording_id,
            "participants": [
                {
                    "name": person.name,
                    "email": person.email,
                    "source": person.source,
                    "is_host": person.is_host,
                    "speaking_seconds": person.speaking_seconds,
                }
                for person in meeting.human_participants
            ],
        },
        "transcript": {
            "id": transcript.id,
            "recording_id": transcript.recording_id,
            "engine": transcript.engine,
            "language": transcript.language,
            "created_at": _iso(transcript.created_at),
            "utterance_count": transcript.utterance_count,
            "word_count": transcript.word_count,
            "duration_seconds": transcript.duration_seconds,
            "speakers": transcript.speakers or [],
            "text": text,
        },
    }


def send_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    content: Optional[bytes] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> httpx.Response:
    """Jedno żądanie, bez logowania URL i bez podążania za 3xx.

    Klient httpx na INFO zapisuje pełny URL. Przy GET jest w nim transkrypt,
    więc idziemy transportem. Timeout siedzi w extensions, tak jak ustawia go
    klient. Status jest w nagłówkach — ciała nie czytamy. Duża albo wolna
    odpowiedź nie może zjeść pamięci ani zmienić przyjętej dostawy w retry.
    Strumień zamykamy od razu, więc ewentualny echo Authorization nie zostaje
    w logu.
    """
    request = httpx.Request(
        method,
        url,
        headers=headers,
        content=content,
        extensions={"timeout": httpx.Timeout(TIMEOUT_SECONDS).as_dict()},
    )
    own = transport is None
    transport = transport or httpx.HTTPTransport()
    try:
        response = transport.handle_request(request)
        response.close()
        return response
    finally:
        if own:
            transport.close()


def _fail(message: str, *, retryable: bool, token: str) -> None:
    raise JobError(redact(message, token), retryable=retryable)


def deliver(
    meeting: Meeting,
    transcript: Transcript,
    text: str,
    cfg: WebhookConfig,
    *,
    job_id: Optional[int] = None,
    attempt: int = 1,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """Wyślij dokument. Wyłączony webhook kończy się `skipped`, bez HTTP.

    2xx = dostarczone. 408, 429 i 5xx oraz błąd sieci są do retry. Inne 4xx,
    3xx i za długi GET padają od razu. Komunikat błędu nie zawiera tokenu,
    ciała odpowiedzi ani (przy GET) URL z transkryptem.
    """
    if not cfg.enabled:
        return {"skipped": True, "reason": "webhook disabled"}

    payload = build_payload(
        meeting,
        transcript,
        text,
        delivered_at=utcnow(),
        job_id=job_id,
        attempt=attempt,
    )
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "X-Earwitness-Event": EVENT,
    }
    if job_id is not None:
        headers["X-Earwitness-Delivery"] = str(job_id)
    if cfg.has_token:
        headers["Authorization"] = f"Bearer {cfg.token}"

    shown = redact(cfg.url, cfg.token)
    target = cfg.url
    content: Optional[bytes] = None
    if cfg.method == "POST":
        headers["Content-Type"] = "application/json; charset=utf-8"
        content = body
    else:
        encoded = urlencode({"payload": body.decode("utf-8")})
        separator = "&" if "?" in target else "?"
        target = f"{target}{separator}{encoded}"
        if len(target) > GET_URL_LIMIT:
            _fail(
                f"GET webhook URL is {len(target)} characters, over the "
                f"{GET_URL_LIMIT} limit. Use POST for this transcript.",
                retryable=False,
                token=cfg.token,
            )

    try:
        response = send_request(
            cfg.method,
            target,
            headers=headers,
            content=content,
            transport=transport,
        )
    except httpx.TimeoutException:
        _fail(
            f"webhook {cfg.method} {shown} timed out", retryable=True, token=cfg.token
        )
    except httpx.RequestError as exc:
        _fail(
            f"webhook {cfg.method} {shown} failed ({type(exc).__name__})",
            retryable=True,
            token=cfg.token,
        )

    status = response.status_code
    if 200 <= status < 300:
        log.info("webhook %s %s -> %s", cfg.method, shown, status)
        return {
            "skipped": False,
            "event": EVENT,
            "method": cfg.method,
            "url": shown,
            "status_code": status,
            "transcript_id": transcript.id,
            "attempt": attempt,
        }
    if 300 <= status < 400:
        _fail(
            f"webhook {cfg.method} {shown} returned {status}; redirects are not followed",
            retryable=False,
            token=cfg.token,
        )
    retryable = status in (408, 429) or status >= 500
    _fail(
        f"webhook {cfg.method} {shown} returned {status}",
        retryable=retryable,
        token=cfg.token,
    )
    raise AssertionError("unreachable")

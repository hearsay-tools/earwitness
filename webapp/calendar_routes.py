"""Authenticated Recall Calendar page and Google OAuth bridge."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import secrets
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from webapp import labels
from webapp.calendar_identity import (
    CalendarIdentityAdoptionRequired,
    ensure_identity,
    find_identity,
)
from webapp.config import settings
from webapp.db import get_session
from webapp.models import CalendarOAuthAttempt, User, utcnow
from webapp.recall_calendar import CalendarClient, CalendarError, recording_mode

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates")
)

_ATTEMPT_TTL = dt.timedelta(minutes=10)
_CSRF_SESSION_KEY = "calendar_csrf"
_BINDING_SESSION_KEY = "calendar_session_binding"
_SUBMITTED_MODE_KEY = "calendar_submitted_mode"
_FLASH_SESSION_KEY = "calendar_flash"
_GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events.readonly "
    "https://www.googleapis.com/auth/userinfo.email"
)
_SUPPORTED_MODES = frozenset(("off", "external", "internal", "all"))


def _secured(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _redirect(request: Request, kind: str, message: str) -> Response:
    request.session[_FLASH_SESSION_KEY] = {"kind": kind, "message": message}
    return _secured(RedirectResponse("/calendar", status_code=303))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _csrf_token(request: Request) -> str:
    token = request.session.get(_CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[_CSRF_SESSION_KEY] = token
    return token


def _valid_csrf(request: Request, supplied: str) -> bool:
    expected = request.session.get(_CSRF_SESSION_KEY)
    return (
        isinstance(expected, str)
        and isinstance(supplied, str)
        and hmac.compare_digest(expected.encode(), supplied.encode())
    )


def _session_digest(request: Request, *, create: bool) -> str | None:
    binding = request.session.get(_BINDING_SESSION_KEY)
    if not isinstance(binding, str) or not binding:
        if not create:
            return None
        binding = secrets.token_urlsafe(32)
        request.session[_BINDING_SESSION_KEY] = binding
    return _digest(binding)


def _google_connection(user_data: dict) -> dict | None:
    for connection in user_data.get("connections") or []:
        if connection.get("platform") == "google" and connection.get("connected"):
            return connection
    return None


def _dt_label(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "—"
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    return parsed.astimezone().strftime("%a, %d %b · %H:%M")


templates.env.filters["calendar_dt"] = _dt_label
templates.env.filters["platform"] = labels.platform


def _page_context(
    request: Request,
    session: Session,
) -> dict:
    user: User = request.state.user
    identity = find_identity(session, user)
    user_data: dict | None = None
    meetings: list[dict] = []
    fetch_error: str | None = None
    if settings.recall_calendar_configured and identity is not None:
        try:
            with CalendarClient(identity.external_id) as calendar:
                user_data = calendar.get_user()
                if _google_connection(user_data) is not None:
                    meetings = calendar.meetings()
        except CalendarError as exc:
            fetch_error = str(exc)

    preferences = user_data.get("preferences") if user_data else {}
    upstream_mode = recording_mode(preferences or {}) if user_data else None
    submitted_mode = request.session.get(_SUBMITTED_MODE_KEY)
    selected_mode = submitted_mode if submitted_mode in _SUPPORTED_MODES else None
    if selected_mode is None and identity is not None and identity.setup_pending:
        selected_mode = identity.pending_mode
    if selected_mode is None:
        selected_mode = upstream_mode
    needs_initial_choice = identity is None or (
        identity is not None
        and _google_connection(user_data or {}) is None
        and identity.setup_pending
    )
    flash = request.session.pop(_FLASH_SESSION_KEY, {})
    success = flash.get("message") if flash.get("kind") == "success" else None
    error = flash.get("message") if flash.get("kind") == "error" else None
    return {
        "user": user,
        "queue": request.state.queue,
        "settings": settings,
        "configured": settings.recall_calendar_configured,
        "csrf_token": _csrf_token(request),
        "identity": identity,
        "calendar_user": user_data,
        "connection": _google_connection(user_data or {}),
        "mode": selected_mode,
        "upstream_mode": upstream_mode,
        "upstream_mode_label": next(
            (
                choice["label"]
                for choice in labels.CALENDAR_MODES
                if choice["value"] == upstream_mode
            ),
            "Custom" if upstream_mode == "custom" else None,
        ),
        "needs_initial_choice": needs_initial_choice,
        "mode_choices": labels.CALENDAR_MODES,
        "meetings": meetings,
        "fetch_error": fetch_error,
        "success": success,
        "error": error,
    }


@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    return _secured(
        templates.TemplateResponse(
            request,
            "calendar.html",
            _page_context(request, session),
        )
    )


def _invalidate_pending_attempts(session: Session, user_id: int) -> None:
    now = utcnow()
    session.execute(
        delete(CalendarOAuthAttempt).where(
            CalendarOAuthAttempt.expires_at < now,
        )
    )
    session.execute(
        update(CalendarOAuthAttempt)
        .where(
            CalendarOAuthAttempt.user_id == user_id,
            CalendarOAuthAttempt.return_consumed_at.is_(None),
        )
        .values(expires_at=now)
    )
    session.commit()


@router.post("/calendar/connect")
def calendar_connect(
    request: Request,
    csrf_token: str = Form(""),
    mode: str | None = Form(None),
    session: Session = Depends(get_session),
) -> Response:
    if not _valid_csrf(request, csrf_token):
        return _redirect(
            request, "error", "That form expired. Reload the page and try again."
        )
    if not settings.recall_calendar_configured:
        return _redirect(
            request,
            "error",
            "Calendar setup is unavailable. Ask an administrator for help.",
        )

    user: User = request.state.user
    identity = find_identity(session, user)
    requested_mode: str | None = None
    try:
        if identity is None:
            if mode not in _SUPPORTED_MODES:
                return _redirect(
                    request, "error", "Choose a recording option before connecting."
                )
            workspace_users = CalendarClient.list_users(user.email)
            identity = ensure_identity(session, user, workspace_users=workspace_users)
            requested_mode = mode
        else:
            with CalendarClient(identity.external_id) as existing_calendar:
                existing_calendar.get_user()
            failed_first_setup = identity.setup_pending
            if failed_first_setup:
                if mode not in _SUPPORTED_MODES:
                    return _redirect(
                        request, "error", "Choose a recording option before connecting."
                    )
                requested_mode = mode

        if requested_mode:
            identity.pending_mode = requested_mode
            session.commit()
        _invalidate_pending_attempts(session, user.id)
        with CalendarClient(identity.external_id) as calendar:
            calendar_token = calendar.authenticate()
    except CalendarIdentityAdoptionRequired as exc:
        return _redirect(request, "error", str(exc))
    except CalendarError as exc:
        if requested_mode and identity is not None:
            request.session[_SUBMITTED_MODE_KEY] = requested_mode
        return _redirect(request, "error", str(exc))

    return_nonce = secrets.token_urlsafe(32)
    base_url = settings.base_url.rstrip("/")
    return_base = f"{base_url}/calendar/oauth/return"
    success_url = (
        return_base + "?" + urlencode({"result": "success", "nonce": return_nonce})
    )
    error_url = (
        return_base + "?" + urlencode({"result": "error", "nonce": return_nonce})
    )
    callback_url = f"{base_url}/calendar/oauth/callback"
    state = json.dumps(
        {
            "recall_calendar_auth_token": calendar_token,
            "google_oauth_redirect_url": callback_url,
            "success_url": success_url,
            "error_url": error_url,
        },
        separators=(",", ":"),
    )
    attempt = CalendarOAuthAttempt(
        user_id=user.id,
        state_digest=_digest(state),
        session_digest=_session_digest(request, create=True),
        return_nonce_digest=_digest(return_nonce),
        requested_mode=requested_mode,
        expires_at=utcnow() + _ATTEMPT_TTL,
    )
    session.add(attempt)
    session.commit()

    authorization_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
        {
            "scope": _GOOGLE_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "response_type": "code",
            "state": state,
            "redirect_uri": callback_url,
            "client_id": settings.recall_google_client_id,
        }
    )
    return _secured(RedirectResponse(authorization_url, status_code=303))


def _query_values(request: Request, name: str) -> list[str]:
    raw_query = request.scope.get("query_string", b"").decode("ascii")
    return [
        value
        for key, value in parse_qsl(raw_query, keep_blank_values=True)
        if key == name
    ]


def _one_query_value(request: Request, name: str) -> str | None:
    values = _query_values(request, name)
    return values[0] if len(values) == 1 and values[0] else None


@router.get("/calendar/oauth/callback")
def calendar_oauth_callback(
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    state = _one_query_value(request, "state")
    codes = _query_values(request, "code")
    errors = _query_values(request, "error")
    valid_result = (len(codes) == 1 and bool(codes[0]) and not errors) or (
        len(errors) == 1 and bool(errors[0]) and not codes
    )
    # Multiple or missing authorization results are ambiguous even with valid state.
    if not valid_result:
        state = None
    session_digest = _session_digest(request, create=False)
    if state is None or session_digest is None:
        return _redirect(
            request, "error", "This calendar connection link is invalid or expired."
        )

    now = utcnow()
    attempt = session.scalar(
        select(CalendarOAuthAttempt).where(
            CalendarOAuthAttempt.state_digest == _digest(state),
            CalendarOAuthAttempt.user_id == request.state.user.id,
            CalendarOAuthAttempt.session_digest == session_digest,
            CalendarOAuthAttempt.expires_at > now,
            CalendarOAuthAttempt.bridge_consumed_at.is_(None),
        )
    )
    if attempt is None:
        return _redirect(
            request, "error", "This calendar connection link is invalid or expired."
        )
    consumed = session.execute(
        update(CalendarOAuthAttempt)
        .where(
            CalendarOAuthAttempt.id == attempt.id,
            CalendarOAuthAttempt.bridge_consumed_at.is_(None),
            CalendarOAuthAttempt.expires_at > now,
        )
        .values(bridge_consumed_at=now)
    )
    if consumed.rowcount != 1:
        session.rollback()
        return _redirect(
            request, "error", "This calendar connection link was already used."
        )
    session.commit()

    raw_query = request.scope.get("query_string", b"").decode("ascii")
    recall_callback = (
        f"https://{settings.recall_region}.recall.ai"
        "/api/v1/calendar/google_oauth_callback/"
    )
    return _secured(
        RedirectResponse(
            recall_callback + (f"?{raw_query}" if raw_query else ""),
            status_code=307,
        )
    )


@router.get("/calendar/oauth/return")
def calendar_oauth_return(
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    nonce = _one_query_value(request, "nonce")
    result = _one_query_value(request, "result")
    session_digest = _session_digest(request, create=False)
    if nonce is None or result not in {"success", "error"} or session_digest is None:
        return _redirect(
            request, "error", "This calendar connection result is invalid or expired."
        )

    now = utcnow()
    attempt = session.scalar(
        select(CalendarOAuthAttempt).where(
            CalendarOAuthAttempt.return_nonce_digest == _digest(nonce),
            CalendarOAuthAttempt.user_id == request.state.user.id,
            CalendarOAuthAttempt.session_digest == session_digest,
            CalendarOAuthAttempt.expires_at > now,
            CalendarOAuthAttempt.bridge_consumed_at.is_not(None),
            CalendarOAuthAttempt.return_consumed_at.is_(None),
        )
    )
    if attempt is None:
        return _redirect(
            request, "error", "This calendar connection result is invalid or expired."
        )
    consumed = session.execute(
        update(CalendarOAuthAttempt)
        .where(
            CalendarOAuthAttempt.id == attempt.id,
            CalendarOAuthAttempt.bridge_consumed_at.is_not(None),
            CalendarOAuthAttempt.return_consumed_at.is_(None),
            CalendarOAuthAttempt.expires_at > now,
        )
        .values(return_consumed_at=now)
    )
    if consumed.rowcount != 1:
        session.rollback()
        return _redirect(
            request, "error", "This calendar connection result was already used."
        )
    session.commit()

    if result == "error":
        if attempt.requested_mode:
            request.session[_SUBMITTED_MODE_KEY] = attempt.requested_mode
        return _redirect(
            request,
            "error",
            "Google Calendar was not connected. You can safely try again.",
        )

    identity = find_identity(session, request.state.user)
    if identity is None:
        return _redirect(
            request, "error", "The calendar identity could not be verified."
        )
    try:
        with CalendarClient(identity.external_id) as calendar:
            user_data = calendar.get_user()
            if _google_connection(user_data) is None:
                raise CalendarError(
                    "Google Calendar did not finish connecting. Please try again."
                )
            if attempt.requested_mode:
                user_data = calendar.set_mode(attempt.requested_mode)
                if (
                    recording_mode(user_data.get("preferences") or {})
                    != attempt.requested_mode
                ):
                    raise CalendarError(
                        "The recording preference could not be verified. Please try again."
                    )
    except CalendarError as exc:
        if attempt.requested_mode:
            request.session[_SUBMITTED_MODE_KEY] = attempt.requested_mode
        return _redirect(request, "error", str(exc))

    identity.setup_pending = False
    identity.pending_mode = None
    session.commit()
    request.session.pop(_SUBMITTED_MODE_KEY, None)
    return _redirect(request, "success", "Google Calendar connected.")


@router.post("/calendar/preferences")
def calendar_preferences(
    request: Request,
    csrf_token: str = Form(""),
    mode: str = Form(""),
    session: Session = Depends(get_session),
) -> Response:
    if not _valid_csrf(request, csrf_token):
        return _redirect(
            request, "error", "That form expired. Reload the page and try again."
        )
    if mode not in _SUPPORTED_MODES:
        return _redirect(request, "error", "Choose a supported recording option.")
    identity = find_identity(session, request.state.user)
    if identity is None:
        return _redirect(
            request, "error", "Connect Google Calendar before saving preferences."
        )

    request.session[_SUBMITTED_MODE_KEY] = mode
    try:
        with CalendarClient(identity.external_id) as calendar:
            user_data = calendar.set_mode(mode)
        if recording_mode(user_data.get("preferences") or {}) != mode:
            raise CalendarError(
                "The recording preference could not be verified. Please try again."
            )
    except CalendarError as exc:
        return _redirect(request, "error", str(exc))

    request.session.pop(_SUBMITTED_MODE_KEY, None)
    if identity.setup_pending:
        identity.setup_pending = False
        identity.pending_mode = None
        session.commit()
    return _redirect(request, "success", "Recording preference saved.")


@router.post("/calendar/disconnect")
def calendar_disconnect(
    request: Request,
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),
) -> Response:
    if not _valid_csrf(request, csrf_token):
        return _redirect(
            request, "error", "That form expired. Reload the page and try again."
        )
    identity = find_identity(session, request.state.user)
    if identity is None:
        return _redirect(request, "error", "No connected calendar was found.")

    _invalidate_pending_attempts(session, request.state.user.id)
    try:
        with CalendarClient(identity.external_id) as calendar:
            calendar.disconnect()
    except CalendarError as exc:
        return _redirect(request, "error", str(exc))
    return _redirect(request, "success", "Google Calendar disconnected.")


class CallbackQueryRedactionFilter(logging.Filter):
    """Remove OAuth callback query strings from Uvicorn access records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            target = record.args[2]
            if isinstance(target, str) and target.startswith("/calendar/oauth/"):
                args = list(record.args)
                args[2] = target.split("?", 1)[0]
                record.args = tuple(args)
        return True


def install_callback_log_redaction() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(
        isinstance(item, CallbackQueryRedactionFilter) for item in logger.filters
    ):
        logger.addFilter(CallbackQueryRedactionFilter())


install_callback_log_redaction()

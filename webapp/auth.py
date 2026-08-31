"""Logowanie Google (OIDC) + opcjonalne ograniczenie do domeny + Calendar RO.

Zakres uprawnień: `openid email profile` + `calendar.readonly`. Kalendarz jest
potrzebny do tytułów spotkań i listy zaproszonych — Recall ich nie zna.

Ograniczenie domeny działa dwustopniowo:
1. `hd=<domena>` w URL-u autoryzacji — Google od razu podpowiada właściwe konto
   (działa tylko przy jednej domenie, to hint, nie zabezpieczenie),
2. twarda weryfikacja claimu `hd` / sufiksu e-maila po powrocie — to jest
   właściwa bramka.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from webapp.config import settings
from webapp.db import get_session
from webapp.models import User, utcnow

log = logging.getLogger("webapp.auth")

SCOPES = "openid email profile https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

oauth = OAuth()
if settings.oauth_configured:
    oauth.register(
        name="google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        client_kwargs={"scope": SCOPES},
    )


class DomainNotAllowed(Exception):
    def __init__(self, email: str) -> None:
        self.email = email
        super().__init__(email)


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def is_domain_allowed(email: str, hd: Optional[str] = None) -> bool:
    if not settings.allowed_domains:
        return True
    candidates = {domain_of(email)}
    if hd:
        candidates.add(hd.lower())
    return bool(candidates & set(settings.allowed_domains))


def authorize_params() -> dict[str, str]:
    """Parametry do URL-a autoryzacji.

    `access_type=offline` + `prompt=consent` są konieczne, żeby Google w ogóle
    wydał refresh token — bez niego kalendarz da się odpytać tylko przez
    godzinę po zalogowaniu.
    """
    params = {
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if len(settings.allowed_domains) == 1:
        params["hd"] = settings.allowed_domains[0]
    return params


def upsert_user(session: Session, claims: dict, token: dict) -> User:
    email = (claims.get("email") or "").lower()
    if not email:
        raise ValueError("Google did not return an email address")
    if not claims.get("email_verified", True):
        raise DomainNotAllowed(email)
    if not is_domain_allowed(email, claims.get("hd")):
        raise DomainNotAllowed(email)

    sub = claims.get("sub") or email
    user = session.execute(
        select(User).where(User.google_sub == sub)
    ).scalar_one_or_none()
    if user is None:
        user = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
    if user is None:
        user = User(google_sub=sub, email=email)
        session.add(user)

    user.google_sub = sub
    user.email = email
    user.name = claims.get("name") or user.name
    user.picture = claims.get("picture") or user.picture
    user.domain = claims.get("hd") or domain_of(email)
    user.last_login_at = utcnow()

    if token.get("access_token"):
        user.google_access_token = token["access_token"]
        expires_in = int(token.get("expires_in") or 3600)
        user.google_token_expires_at = utcnow() + dt.timedelta(seconds=expires_in)
    # Refresh token przychodzi tylko przy pierwszej zgodzie (albo prompt=consent);
    # przy kolejnych logowaniach pola nie ma — nie kasujemy tego, co mamy.
    if token.get("refresh_token"):
        user.google_refresh_token = token["refresh_token"]
    granted = token.get("scope") or ""
    if CALENDAR_SCOPE in granted:
        user.calendar_scope_granted = True

    session.commit()
    return user


def dev_user(session: Session) -> User:
    """Konto zastępcze przy AUTH_DISABLED=1 (tylko lokalny dev)."""
    user = session.execute(
        select(User).where(User.email == "dev@localhost")
    ).scalar_one_or_none()
    if user is None:
        user = User(google_sub="dev", email="dev@localhost", name="Dev (auth disabled)")
        session.add(user)
        session.commit()
    return user


def get_current_user(
    request: Request, session: Session = Depends(get_session)
) -> Optional[User]:
    """Zalogowany użytkownik albo None. Nie rzuca — do stron publicznych."""
    if settings.auth_disabled:
        return dev_user(session)
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = session.get(User, uid)
    if user is None or not user.is_active:
        return None
    return user


def require_user(user: Optional[User] = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Wymagane logowanie"
        )
    return user


__all__ = [
    "DomainNotAllowed",
    "OAuthError",
    "SCOPES",
    "authorize_params",
    "get_current_user",
    "is_domain_allowed",
    "oauth",
    "require_user",
    "upsert_user",
]

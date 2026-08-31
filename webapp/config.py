"""Konfiguracja webappki — wszystko z env (.env ładowany przez python-dotenv).

Trzymamy to w jednym miejscu, bo webapp i worker to dwa osobne procesy, które
muszą widzieć dokładnie tę samą konfigurację (ścieżki do plików, DB, klucze).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _csv(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _path(name: str, default: str) -> Path:
    p = Path(os.environ.get(name) or default)
    return p if p.is_absolute() else (REPO_ROOT / p)


@dataclass
class Settings:
    # --- serwer ---
    base_url: str = os.environ.get("BASE_URL", "http://localhost:8000")
    secret_key: str = os.environ.get("SECRET_KEY", "")
    session_max_age: int = int(os.environ.get("SESSION_MAX_AGE", 60 * 60 * 24 * 14))

    # --- baza ---
    database_url: str = os.environ.get("DATABASE_URL", "")

    # --- Google OAuth ---
    google_client_id: str = os.environ.get("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    # Puste = każda domena Google. Lista rozdzielona przecinkami = whitelist.
    allowed_domains: list[str] = field(
        default_factory=lambda: _csv("ALLOWED_GOOGLE_DOMAINS")
    )
    # Wyłącznik auth do lokalnego devu (NIE włączać na deployu).
    auth_disabled: bool = _bool("AUTH_DISABLED", False)

    # --- Recall.ai ---
    recall_api_key: str = os.environ.get("RECALL_API_KEY", "")
    recall_region: str = os.environ.get("RECALL_REGION", "eu-central-1")

    # --- pipeline ---
    elevenlabs_api_key: str = os.environ.get("ELEVENLABS_API_KEY", "")
    asr_model: str = os.environ.get("ASR_MODEL", "scribe_v2")

    # --- storage ---
    recall_dir: Path = _path("RECALL_DIR", "output/recall")
    transcripts_dir: Path = _path("TRANSCRIPTS_DIR", "output/webapp")

    # --- worker ---
    worker_concurrency: int = int(os.environ.get("WORKER_CONCURRENCY", "2"))
    worker_poll_interval: float = float(os.environ.get("WORKER_POLL_INTERVAL", "2.0"))
    # Job bez heartbeatu dłużej niż tyle sekund uznajemy za martwy i wracamy
    # do kolejki (proces workera padł w trakcie).
    job_stale_after: int = int(os.environ.get("JOB_STALE_AFTER", "900"))
    # Automatyczny sync z Recall co N sekund (0 = wyłączone).
    autosync_interval: int = int(os.environ.get("AUTOSYNC_INTERVAL", "300"))
    # Po syncu automatycznie kolejkuj fetch+pipeline dla gotowych nagrań.
    autoprocess: bool = _bool("AUTOPROCESS", False)
    # Ile dni wstecz zaciągamy przy autosyncu.
    sync_lookback_days: int = int(os.environ.get("SYNC_LOOKBACK_DAYS", "30"))

    def __post_init__(self) -> None:
        self.recall_dir.mkdir(parents=True, exist_ok=True)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = f"sqlite:///{REPO_ROOT / 'output' / 'webapp.db'}"
        if not self.secret_key:
            # Dev fallback — stały, żeby sesja przeżyła restart uvicorna.
            # Na produkcji SECRET_KEY jest wymagany (patrz `validate`).
            self.secret_key = "dev-insecure-secret-change-me"

    @property
    def oauth_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    def validate_for_serving(self) -> list[str]:
        """Zwraca listę ostrzeżeń do wypisania przy starcie."""
        warn = []
        if not self.oauth_configured and not self.auth_disabled:
            warn.append(
                "GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET nieustawione — logowanie "
                "nie zadziała. Ustaw je albo AUTH_DISABLED=1 na czas devu."
            )
        if self.auth_disabled:
            warn.append("AUTH_DISABLED=1 — brak autoryzacji, tylko do devu.")
        if self.secret_key == "dev-insecure-secret-change-me":
            warn.append("SECRET_KEY nieustawiony — używam dev fallbacku.")
        if not self.recall_api_key:
            warn.append("RECALL_API_KEY nieustawiony — sync spotkań nie zadziała.")
        if not self.elevenlabs_api_key:
            warn.append("ELEVENLABS_API_KEY nieustawiony — pipeline nie zadziała.")
        return warn


settings = Settings()

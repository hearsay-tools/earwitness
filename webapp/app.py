"""FastAPI: logowanie, lista spotkań z filtrami, kolejka, transkrypty.

Render po stronie serwera (Jinja2) + odrobina vanilla JS do odświeżania
statusów. Zero build stepu — `uv run uvicorn webapp.app:app` i działa.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from webapp import labels, tasks
from webapp.auth import (
    DomainNotAllowed,
    OAuthError,
    authorize_params,
    get_current_user,
    oauth,
    require_user,
    upsert_user,
)
from webapp.config import settings
from webapp.db import get_session, init_db, session_scope
from webapp.jobs import cancel as cancel_job
from webapp.jobs import enqueue, queue_stats
from webapp.models import (
    ACTIVE_JOB_STATES,
    DEFAULT_VIEW_STATUSES,
    USER_STATUS_ORDER,
    Job,
    Meeting,
    Transcript,
    User,
    utcnow,
)
from webapp.queries import (
    SORTS,
    MeetingFilters,
    next_sort,
    participant_facets,
    search_meetings,
    status_facets,
    transcript_rows,
    untitled_count,
)

log = logging.getLogger("webapp")
BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    init_db()
    for w in settings.validate_for_serving():
        log.warning(w)
    yield


app = FastAPI(
    title="Earwitness", docs_url="/api/docs", redoc_url=None, lifespan=lifespan
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# --------------------------------------------------------------------------
# Filtry szablonów
# --------------------------------------------------------------------------


def _fmt_dt(value: Optional[dt.datetime], fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not value:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone().strftime(fmt)


def _fmt_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _fmt_ago(value: Optional[dt.datetime]) -> str:
    if not value:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    delta = (utcnow() - value).total_seconds()
    if delta < 0:
        return f"in {_fmt_duration(-delta)}"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    days = int(delta // 86400)
    return f"{days} day ago" if days == 1 else f"{days} days ago"


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["dur"] = _fmt_duration
templates.env.filters["ago"] = _fmt_ago
templates.env.filters["kind"] = labels.job_kind
templates.env.filters["jobstatus"] = labels.job_status
templates.env.filters["assetstate"] = labels.asset_state
templates.env.filters["tstate"] = labels.transcript_state
templates.env.filters["platform"] = labels.platform
templates.env.globals["JOB_STATUSES"] = labels.JOB_STATUSES
templates.env.globals["SORTS"] = SORTS
templates.env.globals["status_hint"] = labels.status_hint
templates.env.globals["USER_STATUSES"] = labels.USER_STATUSES
templates.env.globals["USER_STATUS_ORDER"] = USER_STATUS_ORDER


def qs(base: dict[str, Any], **overrides: Any) -> str:
    merged = dict(base)
    for k, v in overrides.items():
        if v in (None, "", []):
            merged.pop(k, None)
        else:
            merged[k] = v
    # Domyślnego sortu nie trzymamy w URL — spójnie z as_query_dict.
    if merged.get("sort") == "date_desc":
        merged.pop("sort", None)
    # Nigdy "" — puste href="" w HTML to no-op na bieżącym URL (z query),
    # więc przełączenie When z powrotem na date_desc „nie działało".
    return "?" + urlencode(merged, doseq=True) if merged else "?"


templates.env.globals["qs"] = qs
templates.env.globals["next_sort"] = next_sort
templates.env.globals["days_ago"] = lambda n: (
    dt.date.today() - dt.timedelta(days=n)
).isoformat()


def render(request: Request, name: str, ctx: dict[str, Any]) -> HTMLResponse:
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx.setdefault("settings", settings)
    ctx.setdefault("now", utcnow())
    ctx.setdefault("queue", getattr(request.state, "queue", None))
    return templates.TemplateResponse(request, name, ctx)


# --------------------------------------------------------------------------
# Start / auth guard
# --------------------------------------------------------------------------

PUBLIC_PATHS = ("/login", "/auth/", "/static/", "/healthz", "/favicon.ico")


@app.middleware("http")
async def auth_guard(request: Request, call_next):  # noqa: ANN001
    path = request.url.path
    if path.startswith(PUBLIC_PATHS):
        return await call_next(request)
    with session_scope() as session:
        user = get_current_user(request, session)
        request.state.user = user
        # Licznik w nawigacji — jedno tanie zapytanie, za to widoczne wszędzie.
        request.state.queue = queue_stats(session) if user else None
    if user is None:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Sign-in required"}, status_code=401)
        return RedirectResponse(f"/login?next={path}", status_code=302)
    return await call_next(request)


# UWAGA na kolejność: Starlette uruchamia middleware w odwrotnej kolejności
# rejestracji, więc SessionMiddleware musi być dodany PO `auth_guard`, żeby
# opakować go z zewnątrz. Inaczej guard nie widzi `request.session`.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=settings.session_max_age,
    same_site="lax",
    https_only=settings.base_url.startswith("https://"),
)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    with session_scope() as session:
        return {"ok": True, "queue": queue_stats(session)}


# Surowy JSON od FastAPI na ekranie 404 wygląda jak awaria aplikacji.
# Strony HTML dostają wersję brandowaną z drogą wyjścia; /api/ zostaje JSON-em.
_ERROR_COPY = {
    404: ("No such page", "The link may be out of date, or the meeting was deleted."),
    401: (
        "Sign-in required",
        "Sign in with your Google account to see the transcripts.",
    ),
    403: ("No access", "This account has no permission for that resource."),
    410: (
        "The file is gone",
        "The transcript is in the database, but the file is no longer on disk.",
    ),
    500: (
        "Something went wrong",
        "Server-side error. Details are in the application logs.",
    ),
}


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    wants_json = request.url.path.startswith("/api/") or "text/html" not in (
        request.headers.get("accept") or ""
    )
    if wants_json:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    title, body = _ERROR_COPY.get(exc.status_code, ("Error", str(exc.detail)))
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "code": exc.status_code,
            "title": title,
            "body": body,
            "detail": exc.detail if exc.detail != title else None,
            "user": getattr(request.state, "user", None),
            "queue": getattr(request.state, "queue", None),
            "settings": settings,
        },
        status_code=exc.status_code,
    )


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: Optional[str] = None, next: str = "/"):
    if settings.auth_disabled:
        return RedirectResponse(next or "/", status_code=302)
    if request.session.get("user_id"):
        return RedirectResponse(next or "/", status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": error,
            "next": next,
            "configured": settings.oauth_configured,
            "domains": settings.allowed_domains,
            "settings": settings,
            "user": None,
        },
    )


@app.get("/auth/google")
async def auth_google(request: Request, next: str = "/"):
    if not settings.oauth_configured:
        return RedirectResponse("/login?error=OAuth+is+not+configured", status_code=302)
    request.session["post_login_redirect"] = next or "/"
    redirect_uri = f"{settings.base_url.rstrip('/')}/auth/callback"
    return await oauth.google.authorize_redirect(
        request, redirect_uri, **authorize_params()
    )


@app.get("/auth/callback")
async def auth_callback(request: Request, session: Session = Depends(get_session)):
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as e:
        return RedirectResponse(
            f"/login?error={e.description or e.error}", status_code=302
        )

    claims = token.get("userinfo") or {}
    if not claims:
        resp = await oauth.google.get(
            "https://openidconnect.googleapis.com/v1/userinfo", token=token
        )
        claims = resp.json()

    try:
        user = upsert_user(session, claims, token)
    except DomainNotAllowed as e:
        allowed = ", ".join(settings.allowed_domains)
        return RedirectResponse(
            f"/login?error=Account+{e.email}+is+outside+the+allowed+domains+({allowed})",
            status_code=302,
        )
    except ValueError as e:
        return RedirectResponse(f"/login?error={e}", status_code=302)

    request.session["user_id"] = user.id
    target = request.session.pop("post_login_redirect", "/") or "/"
    if not user.calendar_scope_granted:
        log.warning("użytkownik %s bez scope kalendarza", user.email)
    return RedirectResponse(target, status_code=302)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# --------------------------------------------------------------------------
# Spotkania
# --------------------------------------------------------------------------


def _parse_date(value: Optional[str]) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _filters(
    q: str = "",
    status: Optional[list[str]] = None,
    participant: Optional[list[str]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    view: str = "",
    sort: str = "date_desc",
    page: int = 1,
    per_page: int = 25,
) -> MeetingFilters:
    statuses = [s for s in (status or []) if s in USER_STATUS_ORDER]
    # Bez explicitego statusu pokazujemy widok „Finished” — wszystko, co się
    # zakończyło (z transkryptem, do przetworzenia, nieudane, bez nagrania).
    # `?view=all` albo dowolny status = pełna/jawna kontrola.
    default_view = not statuses and view != "all"
    return MeetingFilters(
        q=(q or "").strip(),
        statuses=statuses or (list(DEFAULT_VIEW_STATUSES) if default_view else []),
        participants=[p for p in (participant or []) if p],
        date_from=_parse_date(date_from),
        date_to=_parse_date(date_to),
        view=view if view == "all" else "",
        default_view=default_view,
        sort=sort if sort in SORTS else "date_desc",
        page=max(1, page),
        per_page=min(100, max(5, per_page)),
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse("/meetings", status_code=302)


@app.get("/meetings", response_class=HTMLResponse)
def meetings_view(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
    q: str = "",
    status: list[str] = Query(default=[]),
    participant: list[str] = Query(default=[]),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    view: str = "",
    sort: str = "date_desc",
    page: int = 1,
):
    f = _filters(q, status, participant, date_from, date_to, view, sort, page)
    rows, total = search_meetings(session, f)
    active_jobs = _active_jobs_by_meeting(session, [m.id for m in rows])
    return render(
        request,
        "meetings.html",
        {
            "meetings": rows,
            "total": total,
            "f": f,
            "qbase": f.as_query_dict(),
            "pages": max(1, math.ceil(total / f.per_page)),
            "facets": status_facets(session, f),
            "people": participant_facets(session),
            "queue": queue_stats(session),
            "active_jobs": active_jobs,
            "last_sync": session.execute(select(func.max(Meeting.synced_at))).scalar(),
            "untitled": untitled_count(session),
        },
    )


def _active_jobs_by_meeting(session: Session, meeting_ids: list[str]) -> dict[str, Job]:
    if not meeting_ids:
        return {}
    rows = session.execute(
        select(Job)
        .where(Job.meeting_id.in_(meeting_ids), Job.status.in_(ACTIVE_JOB_STATES))
        .order_by(Job.id.desc())
    ).scalars()
    out: dict[str, Job] = {}
    for job in rows:
        out.setdefault(job.meeting_id, job)
    return out


@app.get("/meetings/{meeting_id}", response_class=HTMLResponse)
def meeting_detail(
    request: Request,
    meeting_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    meeting = session.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(404, "No such meeting")
    jobs = list(
        session.execute(
            select(Job)
            .where(Job.meeting_id == meeting_id)
            .order_by(desc(Job.id))
            .limit(20)
        ).scalars()
    )
    transcript = meeting.latest_transcript
    preview = []
    if transcript:
        try:
            preview = tasks.parse_transcript(tasks.transcript_text(transcript))[:12]
        except FileNotFoundError:
            preview = []
    return render(
        request,
        "meeting_detail.html",
        {
            "m": meeting,
            "jobs": jobs,
            "transcript": transcript,
            "preview": preview,
            "has_recording": _mixed_recording(meeting) is not None,
        },
    )


@app.post("/meetings/{meeting_id}/enqueue")
def meeting_enqueue(
    meeting_id: str,
    kind: str = Form("process"),
    force: bool = Form(False),
    force_asr: bool = Form(False),
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    meeting = session.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(404, "No such meeting")
    if kind not in ("process", "fetch_assets", "transcribe"):
        raise HTTPException(400, "Unknown job type")
    job = enqueue(
        session,
        kind,
        meeting_id=meeting_id,
        args={"force": bool(force), "force_asr": bool(force_asr)},
        priority=20,
        created_by=user.email,
    )
    if kind in ("process", "transcribe"):
        meeting.transcript_state = "queued"
    if kind in ("process", "fetch_assets") and meeting.asset_state != "ready":
        meeting.asset_state = "queued"
    session.commit()
    return RedirectResponse(f"/meetings/{meeting_id}?job={job.id}", status_code=303)


@app.post("/meetings/bulk")
def meetings_bulk(
    meeting_ids: list[str] = Form(default=[]),
    kind: str = Form("process"),
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    n = 0
    for mid in meeting_ids:
        meeting = session.get(Meeting, mid)
        if meeting is None:
            continue
        enqueue(session, kind, meeting_id=mid, priority=30, created_by=user.email)
        if kind in ("process", "transcribe"):
            meeting.transcript_state = "queued"
        n += 1
    session.commit()
    return RedirectResponse(f"/jobs?queued={n}", status_code=303)


# --------------------------------------------------------------------------
# Transkrypty
# --------------------------------------------------------------------------


@app.get("/transcripts", response_class=HTMLResponse)
def transcripts_view(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
    q: str = "",
    status: list[str] = Query(default=[]),
    participant: list[str] = Query(default=[]),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort: str = "date_desc",
    page: int = 1,
):
    f = _filters(q, status, participant, date_from, date_to, sort, page)
    rows, total = transcript_rows(session, f)
    return render(
        request,
        "transcripts.html",
        {
            "rows": rows,
            "total": total,
            "f": f,
            "qbase": f.as_query_dict(),
            "pages": max(1, math.ceil(total / f.per_page)),
            "people": participant_facets(session),
        },
    )


def _get_transcript(session: Session, transcript_id: int) -> Transcript:
    t = session.get(Transcript, transcript_id)
    if t is None:
        raise HTTPException(404, "No such transcript")
    return t


def _download_stem(meeting: Meeting) -> str:
    """Wspólny rdzeń nazwy pobieranych plików: {data}-{tytuł}."""
    slug = "".join(
        c if c.isalnum() or c in "-_" else "-" for c in (meeting.title or "meeting")
    )[:80]
    when = (
        meeting.occurred_at.strftime("%Y-%m-%d") if meeting.occurred_at else "no-date"
    )
    return f"{when}-{slug}".strip("-")


def _mixed_recording(meeting: Meeting) -> Optional[Path]:
    """Mixed MP3 spotkania — dysk jest źródłem prawdy, nie Recall ani flagi w bazie."""
    if not meeting.asset_dir:
        return None
    rec_dir = Path(meeting.asset_dir)
    for path in sorted(rec_dir.glob("audio_mixed*.mp3")):
        if path.is_file() and path.resolve().parent == rec_dir.resolve():
            return path
    return None


@app.get("/transcripts/{transcript_id}", response_class=HTMLResponse)
def transcript_view(
    request: Request,
    transcript_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    t = _get_transcript(session, transcript_id)
    try:
        utterances = tasks.parse_transcript(tasks.transcript_text(t))
    except FileNotFoundError as e:
        raise HTTPException(410, str(e)) from e
    speakers = sorted({u["speaker"] for u in utterances})
    return render(
        request,
        "transcript.html",
        {
            "t": t,
            "m": t.meeting,
            "utterances": utterances,
            "speakers": speakers,
        },
    )


@app.get("/transcripts/{transcript_id}/download")
def transcript_download(
    transcript_id: int,
    fmt: str = "txt",
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    t = _get_transcript(session, transcript_id)
    meeting = t.meeting
    try:
        text = tasks.transcript_text(t)
    except FileNotFoundError as e:
        raise HTTPException(410, str(e)) from e

    stem = _download_stem(meeting)

    if fmt == "txt":
        body, media, ext = text, "text/plain; charset=utf-8", "txt"
    elif fmt == "md":
        body = tasks.to_markdown(meeting, tasks.parse_transcript(text))
        media, ext = "text/markdown; charset=utf-8", "md"
    elif fmt == "vtt":
        body = tasks.to_vtt(tasks.parse_transcript(text), t.duration_seconds)
        media, ext = "text/vtt; charset=utf-8", "vtt"
    elif fmt == "json":
        body = json.dumps(
            {
                "meeting": {
                    "id": meeting.id,
                    "title": meeting.title,
                    "platform": meeting.platform,
                    "started_at": meeting.started_at.isoformat()
                    if meeting.started_at
                    else None,
                    "duration_seconds": meeting.duration_seconds,
                    "participants": [
                        {
                            "name": p.name,
                            "email": p.email,
                            "source": p.source,
                            "speaking_seconds": p.speaking_seconds,
                        }
                        for p in meeting.participants
                    ],
                },
                "transcript": {
                    "engine": t.engine,
                    "language": t.language,
                    "speakers": t.speakers,
                    "stats": t.stats,
                    "utterances": tasks.parse_transcript(text),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        media, ext = "application/json; charset=utf-8", "json"
    elif fmt == "raw":
        if not t.raw_path:
            raise HTTPException(404, "No raw ASR response")
        raw_path = tasks.transcript_file_path(t.raw_path)
        if not raw_path.exists():
            raise HTTPException(404, "No raw ASR response")
        body = raw_path.read_text(encoding="utf-8")
        media, ext = "application/json; charset=utf-8", "raw.json"
    else:
        raise HTTPException(400, "Unknown format (txt|md|vtt|json|raw)")

    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{stem}.{ext}"'},
    )


@app.get("/meetings/{meeting_id}/recording")
def meeting_recording_download(
    meeting_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    """Pobranie mixed MP3 — stream, bo nagranie waży setki MB."""
    meeting = session.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(404, "No such meeting")
    path = _mixed_recording(meeting)
    if path is None:
        raise HTTPException(404, "No mixed recording on disk")
    return FileResponse(
        path,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f'attachment; filename="{_download_stem(meeting)}.mp3"'
        },
    )


# --------------------------------------------------------------------------
# Kolejka
# --------------------------------------------------------------------------


@app.get("/jobs", response_class=HTMLResponse)
def jobs_view(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
    status: Optional[str] = None,
    page: int = 1,
):
    per_page = 40
    stmt = select(Job)
    if status:
        stmt = stmt.where(Job.status == status)
    total = session.execute(
        select(func.count(Job.id)).where(Job.status == status)
        if status
        else select(func.count(Job.id))
    ).scalar_one()
    rows = list(
        session.execute(
            stmt.order_by(desc(Job.id))
            .limit(per_page)
            .offset((max(1, page) - 1) * per_page)
        ).scalars()
    )
    return render(
        request,
        "jobs.html",
        {
            "jobs": rows,
            "queue": queue_stats(session),
            "status": status,
            "page": max(1, page),
            "pages": max(1, math.ceil(total / per_page)),
            "total": total,
        },
    )


@app.post("/jobs/{job_id}/retry")
def job_retry(
    job_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    clone = enqueue(
        session,
        job.kind,
        meeting_id=job.meeting_id,
        args=job.args or {},
        priority=20,
        created_by=user.email,
        dedupe_key=job.dedupe_key,
    )
    return RedirectResponse(f"/jobs?highlight={clone.id}", status_code=303)


@app.post("/jobs/{job_id}/cancel")
def job_cancel(
    job_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    cancel_job(session, job)
    return RedirectResponse("/jobs", status_code=303)


@app.post("/sync")
def trigger_sync(
    request: Request,
    with_calendar: bool = Form(True),
    autoprocess: bool = Form(False),
    lookback_days: int = Form(30),
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    job = enqueue(
        session,
        "sync_recall",
        args={
            "lookback_days": lookback_days,
            "with_calendar": bool(with_calendar),
            "autoprocess": bool(autoprocess),
            "user_id": user.id,
        },
        priority=10,
        dedupe_key="sync_recall:manual",
        created_by=user.email,
    )
    back = request.headers.get("referer") or "/meetings"
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}job={job.id}", status_code=303)


@app.post("/backfill-titles")
def trigger_backfill_titles(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    """Kalendarz dla spotkań spoza okna zwykłego syncu (patrz `backfill_titles`)."""
    job = enqueue(
        session,
        "backfill_titles",
        args={"user_id": user.id},
        priority=10,
        dedupe_key="backfill_titles:manual",
        created_by=user.email,
    )
    back = request.headers.get("referer") or "/meetings"
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}job={job.id}", status_code=303)


# --------------------------------------------------------------------------
# JSON API (odświeżanie w tle + integracje)
# --------------------------------------------------------------------------


def _meeting_json(m: Meeting) -> dict[str, Any]:
    t = m.latest_transcript
    return {
        "id": m.id,
        "title": m.title,
        "platform": m.platform,
        "occurred_at": m.occurred_at.isoformat() if m.occurred_at else None,
        "duration_seconds": m.duration_seconds,
        "status": m.status_group,
        "user_status": m.user_status,
        "status_code": m.status_code,
        "asset_state": m.asset_state,
        "transcript_state": m.transcript_state,
        "media_expires_at": m.media_expires_at.isoformat()
        if m.media_expires_at
        else None,
        "participants": [
            {"name": p.name, "email": p.email, "source": p.source, "is_bot": p.is_bot}
            for p in m.participants
        ],
        "transcript_id": t.id if t else None,
    }


@app.get("/api/meetings")
def api_meetings(
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
    q: str = "",
    status: list[str] = Query(default=[]),
    participant: list[str] = Query(default=[]),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    view: str = "all",  # integracje widzą wszystko, bez domyślnego widoku
    sort: str = "date_desc",
    page: int = 1,
    per_page: int = 25,
):
    f = _filters(q, status, participant, date_from, date_to, view, sort, page, per_page)
    rows, total = search_meetings(session, f)
    return {"total": total, "page": f.page, "items": [_meeting_json(m) for m in rows]}


@app.get("/api/jobs")
def api_jobs(
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
    ids: Optional[str] = None,
    limit: int = 50,
):
    stmt = select(Job).order_by(desc(Job.id)).limit(min(200, limit))
    if ids:
        wanted = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        stmt = select(Job).where(Job.id.in_(wanted))
    rows = list(session.execute(stmt).scalars())
    return {
        "queue": queue_stats(session),
        "items": [
            {
                "id": j.id,
                "kind": j.kind,
                "kind_label": labels.job_kind(j.kind),
                "status": j.status,
                "status_label": labels.job_status(j.status),
                "progress": j.progress,
                "step": j.step,
                "meeting_id": j.meeting_id,
                "attempts": j.attempts,
                "error": (j.error or "")[:500] or None,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            }
            for j in rows
        ],
    }


@app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
def api_job_log(
    job_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job.log or "(no logs)"

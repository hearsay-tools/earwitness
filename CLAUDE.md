Jak tworzymy nowa prezentacje to utworz branch na podstawie corporate-identity.

# Projekt: transkrypcja spotkań

PoC transkrypcji spotkań z nagrań Recall.ai. Stan i decyzje:

- **Default pipeline**: `pipeline-recall` (mixed ASR ElevenLabs + diaryzacja
  energią izolowanych kanałów `audio_separate` + odzysk słów cichszego mówcy
  przy overlapie, wyłączany `--no-overlap-recovery`). Decyzja 2026-07-10 —
  pełne uzasadnienie w README → "Decyzje stackowe".
- **Fallback** dla nagrań bez assetów Recall: `pipeline` (Replicate pyannote).
- Typowy flow: `recall-fetch <bot_id>` → `pipeline-recall <rec_dir> -o out.txt`.
- Odrzucone podejścia (nie wracać bez nowego powodu): chunked per-speaker ASR
  solo (niestabilny na krótkich chunkach), korekta transkryptu LLM-em bez
  drugiego źródła akustycznego (net harmful).

## Webapp (`webapp/`)

- FastAPI + Jinja2 (server-side render, zero build stepu) + SQLAlchemy.
  Uruchomienie: `./dev.sh` (serwer + worker) albo osobno
  `uv run uvicorn webapp.app:app --reload` i `uv run python -m webapp.worker`.
- Auth: Google OIDC (Authlib) + scope `calendar.readonly`; whitelist domen
  przez `ALLOWED_GOOGLE_DOMAINS`. `AUTH_DISABLED=1` tylko na lokalny dev.
- Kolejka zadań jest własna, oparta o bazę (nie Celery — brak Redisa w PoC).
  Kontrakt: `jobs.enqueue()` → worker `claim()` (compare-and-swap) → heartbeat
  → retry z backoffem → `reap_stale()`. Concurrency = procesy, nie wątki
  (taski przechwytują globalny `stderr`).
- Pipeline w webappce to ten sam kod co CLI (`transcripts.energy_diarization`) —
  nie duplikować logiki, zmiany robić w module, nie w tasku.
- **Warstwa wizualna**: brand Apptension (DM Sans self-hosted, czerń #111111,
  limonka #C7F24A / #EAF7C7, `_` po H1, zero gradientów). Wszystko jedzie na
  tokenach z `webapp/static/app.css` — kolory, rytm (`--s1..--s7`), motion
  (`--dur-1..5`, `--ease-*`). Nie wpisywać surowych ms ani hexów w komponentach.
  Motyw nocny to te same role tokenów z innymi wartościami, nie odwrócony dzień.
- Współdzielone komponenty UI (badge, pasek postępu, pobieranie, paginacja,
  stany puste, ilustracje) są w `templates/_ui.html` — jedna rzecz ma wyglądać
  tak samo wszędzie. Nazwy techniczne tłumaczy `webapp/labels.py`, nie szablony.
- Daty w bazie przechodzą przez `UtcDateTime` — z bazy zawsze wracają
  ze strefą. Nie porównywać `datetime` z DB bez tego typu.
- **Dysk jest źródłem prawdy o audio, nie API.** Po TTL Recall zwraca bota
  bez `recordings`, więc `recording_id` nigdy nie trafi do bazy — a komplet
  assetów ściągnięty przed wygaśnięciem leży w `output/recall/<bot>/<rec>/`.
  `local_asset_state()` skanuje wtedy dysk i oddaje znalezione `recording_id`
  (`adopt_disk_recording`), a `adopt_local_recordings()` robi to samo hurtem
  (task `repair_assets`). Status `expired` dotyczy Recalla, nie naszych plików.
- Tytuły spotkań i zaproszeni pochodzą z Google Calendar, reszta z Recall.
  Zwykły sync pyta kalendarz tylko o okno wokół „teraz" (`sync_lookback_days`
  + 14 dni w przód), więc spotkanie starsze niż lookback **nigdy** samo tytułu
  nie dostanie — kolejne synchronizacje ruszają to samo okno. Od tego jest
  `enrich_meetings(..., only_missing=True)` / task `backfill_titles`: okno
  liczy się z dat spotkań bez tytułu, nie z zegara. Dopasowanie po czasie
  bierze pod uwagę wyłącznie eventy z konferencją i odrzuca te z innym kodem
  Meet/Zoom niż `meeting_native_id` — inaczej „Lunch" o tej samej godzinie
  wygrywa z prawdziwym callem.
- Testy: `uv run pytest`.

## Operacyjne

- Uruchamianie: `uv run python main.py <subkomenda>` (nigdy pip/global).
- Klucze w `.env`: ELEVENLABS_API_KEY, REPLICATE_API_TOKEN, RECALL_API_KEY,
  ANTHROPIC_API_KEY (do ewentualnych kroków LLM — używaj SDK `anthropic`,
  nie `claude -p`).
- Nagrania Recall mają TTL (default 24h) — assety ściągać zaraz po spotkaniu.
- `output/`, `audio/` i `experiments/` są gitignored — nie commituj nagrań,
  transkryptów ani logów eksperymentów (imiona, cytaty, ID botów).

## Area labels

Issues carry exactly one area label naming the part of the system or
process they touch. The labels on GitHub are the source of truth — each
label's description states its boundary; this table is a map for picking
one:

| Label | Covers |
|---|---|
| `pipeline` | ASR, diarization, overlap recovery under `transcripts/` |
| `recall` | Recall.ai bot lifecycle, asset fetch, TTL handling |
| `webapp` | the FastAPI app, Jinja2 templates, auth, the job queue |
| `infra` | Docker, `docker-compose.yml`, GitHub Actions |
| `docs` | README, `CLAUDE.md` |
| `process` | repo development process: issue templates, triage, review automation, dev-flow |

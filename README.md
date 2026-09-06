# Earwitness

Earwitness — PoC transkrypcji spotkań. Default: ElevenLabs Scribe (`scribe_v2`) na zmiksowanym
audio + **diaryzacja energią izolowanych kanałów** z Recall `audio_separate`
(deterministyczna, bez ML) — subkomenda `pipeline-recall`.

Dwa interfejsy do tego samego pipeline'u:
- **CLI** (`main.py`) — do eksperymentów i jednorazowych przebiegów,
- **webapp** (`webapp/`) — logowanie Google, lista spotkań z Recall, kolejka
  zadań, przeglądanie i pobieranie transkryptów. Patrz [Webapp](#webapp).

## Output

```
Rozmówca 1 [00:00:03] Cześć, dzięki że jesteś.
Rozmówca 2 [00:00:05] Nie ma sprawy.
Rozmówca 1 [00:00:07] Ok, lecimy z agendą...
```

## Setup

```bash
uv sync
cp .env.example .env
# wpisz ELEVENLABS_API_KEY=<twój klucz> do .env
```

## Webapp

```bash
./dev.sh                 # serwer + worker razem, http://localhost:8000
# albo osobno:
uv run uvicorn webapp.app:app --reload
uv run python -m webapp.worker -c 2
```

Do pierwszego uruchomienia lokalnie wystarczy `AUTH_DISABLED=1` w `.env`
(pomija logowanie). Testy: `uv run pytest`.

Konfiguracja nagrywania z kalendarza: [Recall Calendar V1 — wdrożenie i migracja](docs/recall-calendar.md).

### Co umie

| Obszar | Szczegóły |
|---|---|
| Logowanie | Google OIDC, scope `calendar.readonly`. `ALLOWED_GOOGLE_DOMAINS` ogranicza dostęp do wskazanych domen (weryfikacja claimu `hd` **i** sufiksu maila; `hd` idzie też jako hint do Google). |
| Lista spotkań | Filtry: data (zakres + skróty 7/30 dni), status, uczestnicy (koniunkcja — „byli oboje”), stan transkryptu. Szukajka po tytule, osobach, mailach i ID; każde słowo musi pasować. Sortowanie, paginacja, akcje masowe. |
| Kolejka | Zadania `sync_recall`, `sync_calendar`, `fetch_assets`, `transcribe`, `process`, `cleanup_audio`. Postęp i log na żywo, retry z backoffem, anulowanie. |
| Transkrypty | Przeglądanie z wyszukiwaniem w treści i filtrem po mówcy, czas mówienia per osoba, pobieranie jako `.txt` / `.md` / `.vtt` / `.json` / surowy `.raw.json`. |
| API | `/api/meetings`, `/api/jobs`, `/api/jobs/{id}/log`, `/healthz`. Swagger: `/api/docs`. |

### Skąd się biorą dane

1. **Recall.ai** — boty, statusy, czasy, nagrania, TTL mediów oraz kto realnie
   był w callu (`participants.json`).
2. **Google Calendar (read-only)** — tytuł spotkania i lista zaproszonych.
   Recall ich nie zna, a bez tytułu wyszukiwarka jest bezużyteczna. Dopasowanie
   idzie po identyfikatorze konferencji (kod Meet / id Zooma z `conferenceData`),
   a gdy go brak — po nakładaniu się czasu (±20 min).
3. **Pipeline** — dokładnie ten sam kod co `pipeline-recall` w CLI.

Bez zalogowanego użytkownika z kalendarzem spotkania mają tytuły zastępcze
(`Google Meet — 2026-08-07 12:20`).

### Kolejka zadań — dlaczego nie Celery

Celery ciągnie Redisa albo RabbitMQ. Zadania tutaj są długie (minuty) i rzadkie
(kilka na spotkanie), więc narzut pollingu bazy jest bez znaczenia, a zysk
operacyjny duży: jeden proces workera, stan zadań w tej samej bazie co reszta
appki, historia i logi per job za darmo. Kontrakt zostaje ten sam co w Celery —
`enqueue()` wraca natychmiast, worker `claim()`uje zadanie atomowo
(compare-and-swap, działa i na SQLite, i na Postgresie), raportuje heartbeat,
a padnięty worker jest wykrywany i jego zadania wracają do kolejki.

Concurrency to **procesy**, nie wątki: taski przechwytują `stderr` bibliotek
(pipeline raportuje postęp printem), a `redirect_stderr` jest globalny dla
procesu. Do tego diaryzacja jest CPU-bound, więc GIL i tak by ją zserializował.

### Ograniczenia PoC

- Transkrypty i audio leżą na dysku lokalnym (`output/`) — przy deployu na
  więcej niż jedną maszynę potrzebny S3 albo wspólny wolumen.
- SQLite domyślnie; przy kilku workerach i większym ruchu → `DATABASE_URL`
  na Postgresa (kod jest przygotowany, kolejka nie używa niczego SQLite-only).
- **Nagrania Recall mają TTL 24h** (domyślnie). Bez `AUTOSYNC_INTERVAL` +
  `AUTOPROCESS` albo bez ręcznego klikania audio przepada bezpowrotnie.
- `AUTOPROCESS=1` wydaje pieniądze w ElevenLabs bez pytania — domyślnie
  wyłączone. Deduplikacja po `dedupe_key` chroni przed podwójnym odpaleniem
  tego samego spotkania, ale nie przed świadomym `force`.
- Brak ról i uprawnień — każdy zalogowany z dozwolonej domeny widzi wszystkie
  transkrypty.

## CLI

### `pipeline-recall` — DEFAULT

Produkcyjny pipeline dla nagrań z Recall (od 2026-07-10). Wejście: katalog
nagrania pobrany przez `recall-fetch`.

```bash
uv run python main.py recall-fetch <bot_id>   # pobiera audio_mixed + audio_separate
uv run python main.py pipeline-recall \
  output/recall/<bot_id>/<recording_id> \
  -o output/spotkanie.txt
```

Kroki: (1) ElevenLabs ASR na `audio_mixed.mp3` (no-diarize, word timestamps;
raw.json obok outputu, reused idempotentnie), (2) przypisanie słów do mówców
przez energię RMS izolowanych kanałów + Viterbi (kara za zmianę mówcy w środku
frazy) + tiebreak spornych okien mini-ASR-em kanałów kandydatów, (3) odzysk
słów cichszego mówcy przy overlapie: okna, w których kanał ma energię mowy,
ale zero przypisanych słów (mixed ASR słyszał tylko dominującego), idą do
mini-ASR izolowanego kanału, a wynik po dedupie wchodzi jako osobne wypowiedzi
(odzyskuje backchannele typu "Mhm", "Sure", "Okej"). Diaryzacja jest
deterministyczna, per-channel (nazwiska z Meet), boty-notetakery
odpadają naturalnie (zero energii).

Flagi: `--no-tiebreak` (taniej, minimalnie gorzej na overlapach),
`--no-overlap-recovery` (bez odzysku backchanneli; taniej o ~100-270 mini-ASR
wycinków na godzinę spotkania), `--language`, `--model`, `--raw-out`, `--force`.

### Pozostałe subkomendy

### `transcribe`

```bash
# Sztywna liczba rozmówców (zalecane jeśli wiesz ile osób)
uv run python main.py transcribe audio/spotkanie.mp3 -n 2 -o output/spotkanie.txt

# Albo: niech model sam zgaduje, ale ze strojonym progiem
uv run python main.py transcribe audio/spotkanie.mp3 -t 0.7

# Stdout (bez -o)
uv run python main.py transcribe audio/spotkanie.mp3 -n 3
```

`--num-speakers` i `--threshold` są wzajemnie wykluczające:

- **`-n / --num-speakers N`** — model dopasowuje audio do dokładnie N profili
  głosowych. Najskuteczniejsze gdy znamy liczbę osób.
- **`-t / --threshold 0.0–0.4`** — model sam ocenia liczbę rozmówców. Wyższy
  próg = bardziej konserwatywny (scala podobne głosy). Uwaga: API ogranicza
  zakres do 0.4 (sprawdzone empirycznie — bot ElevenLabs sugerował 0.7–0.8,
  to halucynacja).

Pozostałe: `--model` (domyślnie `scribe_v2`), `--language` (np. `pl`; domyślnie
auto), `-o/--output`.

### `compare`

Porównuje kandydatów (ElevenLabs / MacWhisper / Fireflies) z ground-truth pod
kątem turns per osoba i talk time per osoba. Mapuje speakery kandydatów na
realnych ludzi przez overlap czasowy.

```bash
uv run python main.py compare \
  --timeline "audio/speaker-timeline-XXXX.json" \
  --elevenlabs output/spotkanie.elevenlabs-n7.txt \
  --elevenlabs output/spotkanie.elevenlabs-t04.txt \
  --macwhisper output/spotkanie.macwhisper.txt \
  --fireflies output/spotkanie.fireflies.json \
  -o output/comparison.txt
```

Wyjście: tabela turns/talk-time per realna osoba × kandydat plus mapping audit
pokazujący do kogo każdy cand-speaker został przyklejony i z jakim % overlap.

### `ground-truth`

Buduje benchmark z plików `speaker-timeline-*.json` + `participants-*.json`
(format z eksportu Recall.ai / podobnych botów do Teams). Używamy go do
weryfikacji czy diaryzacja Scribe trafia w prawdziwe granice mówców.

```bash
uv run python main.py ground-truth \
  audio/speaker-timeline-XXXX.json \
  -p audio/participants-XXXX.json \
  -o output/spotkanie.ground_truth.txt
```

Ground-truth zawiera nagłówek z listą mówców (z czasem mówienia i liczbą turns)
oraz po jednej linii na wypowiedź: `<Imię> [HH:mm:ss-HH:mm:ss] (Xs)`. Brak
treści — bot eksportu nie zapisuje słów, tylko speaker timeline.

## Decyzje stackowe

### 2026-07-10: ElevenLabs mixed + diaryzacja energią kanałów (CURRENT DEFAULT)

**Pipeline produkcyjny** (`pipeline-recall`):
1. ElevenLabs Scribe v2 na `audio_mixed.mp3` → tekst + per-word timestamps
2. Diaryzacja energią izolowanych kanałów `audio_separate` (Recall) —
   RMS per 10 ms + Viterbi + tiebreak mini-ASR spornych okien
3. Odzysk słów cichszego mówcy przy overlapie — mini-ASR okien z energią
   kanału bez przypisanych słów, dedup względem mixed (od 2026-07-10)
4. Zero Replicate, zero LLM. Deterministyczne (poza mini-ASR wycinków).

**Powód**: `audio_separate` to osobne strumienie sieciowe per uczestnik (zero
przesłuchu), więc przypisanie słów po energii kanału jest niemal ground truth.
Tekst pozostaje najlepszy możliwy (mixed = pełny kontekst akustyczny).
Rozwiązuje cross-talk i misatrybucje pyannote; boty-notetakery odpadają same.

**Walidacja**: dwa wewnętrzne nagrania (krótkie PL, 4 mówców; dłuższe PL/EN,
3 mówców + bot) — sporne słowa 1.0-1.6%, znane błędy graniczne naprawione,
w strefach cross-talku lepiej niż pyannote. Odrzucone po drodze: chunked
per-speaker ASR (niestabilny na krótkich fragmentach), korekta LLM bez
drugiego źródła akustycznego (net harmful — parafrazy, inwersje znaczeń).

**Ograniczenia**: wymaga assetów Recall (`audio_separate` raw + recording.json).
Dla innych nagrań fallback: `pipeline` (Replicate pyannote, poniżej). Backchannel
wchłonięty w monolog dominującego mówcy pojawia się dwa razy: raz inline w jego
wypowiedzi (artefakt mixed ASR) i raz jako odzyskana osobna linia — kosmetyczne.

### 2026-05-06: ElevenLabs Scribe + Replicate pyannote (fallback dla nagrań bez Recall)

**Pipeline produkcyjny**:
1. ElevenLabs Scribe v2 (`transcribe --no-diarize --save-raw`) → tekst + per-word timestamps
2. Replicate `collectiveai-team/speaker-diarization-3` (`diarize-cloud`) → segmenty per speaker
3. `hybrid` merge'uje po overlapie czasowym

**Powód wyboru**: jakość tekstu ElevenLabs (interpunkcja, kapitalizacja, dosłowność, najlepsze nazwy własne) + diaryzacja Replicate pyannote 3.x (N mówców = N osób, zero fragmentacji, łapie też bardzo krótkich mówców ~12 s).

**Empirycznie na 40-min spotkaniu** (vs ground-truth z Recall.ai timeline):
| osoba | GT | hybrid | różnica |
|---|---:|---:|---:|
| A (dominujący) | 1638s | 1662 | +1.5% |
| B | 255 | 254 | −0.4% |
| C | 219 | 222 | +1.4% |
| D (krótki) | 12 | 12 | 0% |

**Alternatywy rozważane i odrzucone (na razie)**:
- **Replicate-only** (WhisperX + pyannote, jeden dostawca): jakość ~90% ale gorsze nazwy własne i halucynacje słów o podobnym brzmieniu.
- **ElevenLabs solo z diaryzacją** (`-n 7` lub `-t 0.4`): fragmentuje jednego mówcę na 3 profile, halucynacje imion przy mniejszych `n`.
- **MacWhisper lokalnie**: nie skaluje się dla aplikacji webowej.
- **Fireflies.ai**: sklejone profile (jeden człowiek ×2–3), gubi ~17% talk time u krótszego mówcy.

**Do rewizji w przyszłości**:
- **Wolumen vs koszt**: jeśli ruch przekroczy break-even, przejście na Replicate-only (jeden dostawca, pay-per-use ~$0.30/spotkanie) może mieć sens.
- **Custom vocabulary** dla domain-specific terms — wszystkie silniki je gubią. Możliwe rozwiązania: ElevenLabs `keyterms`, post-processing przez LLM, custom Whisper finetune.
- **Cross-talk** na zmiksowanym audio: nie do rozwiązania bez per-speaker streams. → ROZWIĄZANE w pipeline-recall (2026-07-10).
- Bardzo krótkie wtrącenia (~1 s): nikt nie wykrywa. Akceptowalna strata.

## Docker / GHCR

Jeden obraz, tryb przez `SERVICE=web|worker|all`. Push na `main` buduje
prywatny obraz `ghcr.io/<owner>/earwitness`.

```bash
docker pull ghcr.io/<owner>/earwitness:latest
```

Lokalnie: `docker compose up --build`. Na Komodo: ten sam obraz, wolumen
pod `/app/output`, dwa serwisy (`SERVICE=web` + `SERVICE=worker`) albo
jeden z `SERVICE=all`. Pull wymaga `read:packages` (PAT albo GitHub App).

## Struktura

```
transcripts/
├── main.py                    # entrypoint
├── transcripts/
│   ├── cli.py                 # subparsers (pipeline-recall / pipeline / ...)
│   ├── energy_diarization.py  # DEFAULT: diaryzacja energią kanałów Recall
│   ├── transcribe.py          # wrapper na ElevenLabs API
│   ├── recall_client.py       # pobieranie assetów z Recall.ai
│   ├── replicate_client.py    # fallback: pyannote/Whisper na Replicate
│   ├── hybrid.py              # fallback: merge ASR + diaryzacja po overlapie
│   ├── formatter.py           # grupowanie słów w wypowiedzi, HH:mm:ss
│   └── ground_truth.py        # benchmark z speaker-timeline.json
├── webapp/
│   ├── app.py                 # FastAPI: routing, widoki, JSON API
│   ├── auth.py                # Google OIDC + whitelist domen
│   ├── gcal.py                # Google Calendar RO: tytuły i zaproszeni
│   ├── recall_sync.py         # boty Recall → tabela meetings
│   ├── jobs.py                # kolejka (enqueue/claim/retry/reap)
│   ├── tasks.py               # fetch_assets / transcribe / process / sync
│   ├── worker.py              # proces workera (multiprocessing)
│   ├── queries.py             # filtrowanie i wyszukiwanie spotkań
│   ├── models.py, db.py, config.py
│   ├── templates/             # Jinja2 (server-side render)
│   └── static/                # app.css, app.js (bez build stepu)
├── tests/                     # pytest: kolejka, filtry, kalendarz, eksport
├── audio/                     # pliki wejściowe (gitignored)
└── output/                    # transkrypcje + baza webappki (gitignored)
```

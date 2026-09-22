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
| Kolejka | Zadania `sync_recall`, `sync_calendar`, `fetch_assets`, `transcribe`, `process`, `cleanup_audio`, `webhook_deliver`. Postęp i log na żywo, retry z backoffem, anulowanie. |
| Webhook | Po gotowym transkrypcie job wysyła info o spotkaniu i treść na URL z `/settings` (POST albo GET, opcjonalny Bearer). Porażka dostawy nie cofa transkryptu. |
| Transkrypty | Przeglądanie z wyszukiwaniem w treści i filtrem po mówcy, czas mówienia per osoba, pobieranie jako `.txt` / `.md` / `.vtt` / `.json` / surowy `.raw.json`. |
| API | `/api/meetings`, `/api/jobs`, `/api/jobs/{id}/log`, `/healthz`. Swagger: `/api/docs`. |
| Pamięć spotkań (opcjonalnie) | Gotowe transkrypty lecą do self-hostowanego [Honcho](https://github.com/plastic-labs/honcho); pytania w języku naturalnym o jedno spotkanie (panel na stronie spotkania) i o wszystkie, w których użytkownik był (`/ask`). Patrz „Pamięć spotkań (Honcho)”. |

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

### Pamięć spotkań (Honcho) — opcjonalny sidecar

Domyślnie wyłączone (`HONCHO_ENABLED` puste) — appka działa jak wcześniej.
Po włączeniu:

- każdy gotowy transkrypt dostaje job `honcho_ingest`: spotkanie = sesja
  Honcho (id = bot_id), uczestnik = peer (id z adresu e-mail, fallback nazwa),
  wypowiedź = message od peera mówcy z metadanymi (tytuł, znacznik czasu).
  Ponowna transkrypcja kasuje i odtwarza sesję — pamięć nie dubluje wypowiedzi;
- do sesji trafiają **wszyscy** ludzie ze spotkania (Recall + kalendarz),
  także ci, którzy nic nie powiedzieli — dostęp do pamięci daje obecność;
- **ask-as-self**: pytania idą z perspektywy peera zalogowanego użytkownika
  (`peer.chat`), więc to Honcho ogranicza odpowiedzi do spotkań, na których
  ten peer był. Warunek: adres z logowania Google musi być tym samym adresem,
  który mamy przy uczestniku (`MeetingParticipant.email`) — bez dopasowanego
  adresu użytkownik nie zobaczy własnych spotkań w pamięci;
- `/ask` pyta międzyspotkaniowo, panel na stronie spotkania — w zakresie tej
  jednej sesji (widoczny tylko dla obecnych). Odpowiedź przychodzi w tym
  samym żądaniu (sekundy), bez kolejki;
- `honcho_backfill` (przycisk na `/ask`) kolejkuje ingest gotowych transkryptów,
  po jednym jobie na spotkanie, **od najstarszego**. Formularz ma rozmiar paczki
  (10 / 25 / 50 / wszystkie) — deriver Honcho odpala LLM na każdej wypowiedzi,
  więc pierwsza paczka pozwala sprawdzić jakość i koszt zanim wgramy resztę.
  Wynik joba podaje `queued` i `remaining`; kartę pamięci można klikać aż
  `remaining` spadnie do zera. Bez `limit` zachowanie jest jak wcześniej
  (całe archiwum). Z listy spotkań (`/meetings/bulk?kind=honcho_ingest`,
  tylko gdy `HONCHO_ENABLED`) da się wgrać zaznaczone gotowe transkrypty;
  `queue_ingest()` deduplikuje nachodzące paczki, więc to samo spotkanie
  nie dostanie dwóch ingestów.

Uruchomienie: profil compose (cztery kontenery: pgvector, redis, api,
deriver; własny wolumen bazy) plus dwa wpisy w `.env`:

```bash
# .env: HONCHO_ENABLED=1, OPENAI_API_KEY=sk-...   (jedyny dostawca LLM Honcho)
docker compose --profile honcho up -d
```

Lokalnie (`./dev.sh`) API Honcho jest pod `127.0.0.1:${HONCHO_HOST_PORT:-8100}`
i tam idzie appka bez `HONCHO_URL` (port bierze z tej samej zmiennej);
kontenery web/worker w compose mają
na sztywno `http://honcho-api:8000` (wartość z `.env` ich nie dotyczy).
Pełna lista zmiennych w `.env.example`.

Koszty i prywatność: deriver Honcho odpala LLM na każdej wgranej wypowiedzi
(godzinne spotkanie to setki), a backfill archiwum to mnoży — stąd jedna
mini-klasa modelu dla wszystkich transportów (`HONCHO_MODEL`) i wyłączone
`HONCHO_OBSERVE_OTHERS` (reprezentacje innych uczestników mnożą koszt
obserwator × mówca; pytania o jedno spotkanie ich nie potrzebują). Transkrypty
— imiona i cytaty — trafiają do OpenAI. Honcho jest AGPL-3.0; jedzie jako
osobny, niezmodyfikowany serwis z obrazu `ghcr.io/plastic-labs/honcho:v3.2.0`
(SDK `honcho-ai` 2.4.0).

### Webhook transkryptu

Jedno miejsce docelowe na całą instancję, konfigurowane w UI (`/settings`),
nie zmiennymi env. Puste URL wyłącza wysyłkę. Zapisany Bearer token nie jest
już pokazywany — pole zostaje puste, a formularz mówi tylko, że token jest
zapisany. Żeby go usunąć, trzeba zaznaczyć „Remove saved token”. Nowy token
nadpisuje stary. Token nie trafia do HTML, logów joba, `job.error`,
`job.result` ani odpowiedzi API. URL z userinfo, sekretem w query albo z
wklejonym tokenem jest odrzucany, bo URL wraca do formularza.

Gdy pipeline (`transcribe` albo `process`) zapisze transkrypt i URL jest
ustawione, kolejkowany jest job `webhook_deliver` (do 3 prób, backoff
30 s / 2 min / 8 min — ten sam co reszta kolejki). Dostawa nie jest krokiem
pipeline'u: padnięty webhook nie zmienia `transcript_state` (zostaje `ready`)
i nie kasuje pliku. Nieudane dostawy widać w Queue (status Failed albo retry,
powód bez sekretu) i da się je ponowić. Na stronie spotkania z gotowym
transkryptem jest „Send to webhook” — ten sam job, nie osobna ścieżka.
Historyczne transkrypty same nie wychodzą przy zapisie URL.

Konfiguracja jest czytana przy wykonaniu joba, nie przy kolejkowniu.
Wyłączenie webhooka między kolejką a wysyłką kończy job jako pominięty
(`skipped`), bez żądania HTTP.

**POST** (domyślny): ciało `application/json; charset=utf-8`. W URL nie ma
treści transkryptu.

**GET**: ten sam lekki JSON w parametrze query `payload` (URL-encoded).
Nagłówek `Authorization` idzie tak samo, jeśli token jest zapisany. Gdy
wynikowy URL przekroczy 8000 znaków, job pada od razu (bez próby HTTP).

Timeout 30 s. Przekierowania nie są śledzone, więc Bearer nie wycieka na inny
host (3xx = porażka bez retry). Retry: timeout, błąd połączenia, HTTP 408,
429 i 5xx. Bez retry: pozostałe 4xx, 3xx, za długi GET, brak pliku
transkryptu. 2xx = dostarczone. Ciało odpowiedzi nie jest zapisywane (serwer
mógłby odbić token). Żądanie nie idzie przez klienta httpx, który na INFO
loguje pełny URL — także metadane spotkania są prywatne.

Nagłówki: `User-Agent: Earwitness-Webhook/1`,
`X-Earwitness-Event: transcript.ready`,
`X-Earwitness-Delivery: <job id>` (stałe przy retry tego samego joba; ręczna
ponowna wysyłka i nowy transkrypt to nowe id).

Dokument (`schema`: `earwitness.transcript.ready.v2`, maksymalnie 16 KiB UTF-8):

```json
{
  "schema": "earwitness.transcript.ready.v2",
  "event": "transcript.ready",
  "delivered_at": "2026-09-22T12:00:00+00:00",
  "delivery": {"job_id": 15, "attempt": 1},
  "meeting": {
    "id": "bot-id",
    "title": "Roadmap",
    "platform": "google_meet",
    "meeting_url": null,
    "native_id": null,
    "occurred_at": "2026-09-22T10:00:00+00:00",
    "started_at": "2026-09-22T10:00:00+00:00",
    "completed_at": null,
    "duration_seconds": 1800,
    "status": "done",
    "user_status": "ready",
    "organizer": null,
    "calendar_event_id": null,
    "calendar_link": null,
    "recording_id": "rec-id",
    "participants": [
      {
        "name": "Ala",
        "email": "ala@example.com",
        "source": "recall",
        "is_host": true,
        "speaking_seconds": 12.0
      }
    ]
  },
  "transcript": {
    "id": 3,
    "recording_id": "rec-id",
    "engine": "pipeline-recall",
    "language": "pl",
    "created_at": "2026-09-22T11:00:00+00:00",
    "utterance_count": 2,
    "word_count": 5,
    "duration_seconds": 12.0,
    "speakers": [{"name": "Ala", "seconds": 12.0}]
  },
  "truncated": {"participants": false, "speakers": false}
}
```

Webhook nie zawiera `transcript.text`. Uczestnicy to ludzie (Recall +
kalendarz), bez botów-notetakerów. Pola mogą być `null`. Długie wartości
tekstowe metadanych są skracane do 256 znaków; jeśli nadal przekraczają limit,
listy uczestników i mówców są skracane, a `truncated` wskazuje którą listę.
Pełne dane zawsze są dostępne przez pull API. Do idempotencji służą
`meeting.id` i `transcript.id`; `delivery.job_id` jest stały przy retry
tego samego joba, a `delivered_at` i `attempt` opisują próbę.

**Pull API:** `GET /api/transcripts/{transcript_id}` zwraca obiekty `meeting`
i `transcript` z pełnym `transcript.text` (ten sam plik co pobranie `.txt`,
linie `Mówca [HH:MM:SS] tekst`). Wymaga nagłówka
`Authorization: Bearer <TRANSCRIPT_API_TOKEN>`. Token ustaw w zmiennej
środowiskowej `TRANSCRIPT_API_TOKEN` na serwerze oraz w konsumencie. Jest
niezależny od opcjonalnego tokenu wysyłanego *do* webhooka. Gdy token jest
nieustawiony, endpoint zawsze zwraca 401, również przy `AUTH_DISABLED=1`.
Niepoprawny token zwraca 401, a nieznany transkrypt lub brak jego pliku 404.
Każdy nowy transkrypt ma własny plik tekstowy, więc ponowne przetworzenie
spotkania nie zmienia wyniku pobrania starszego ID. Id z webhooka jest
stabilne przy ponownych dostawach; ponowne pobranie nie tworzy transkryptu.

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
  transkrypty i może zmienić webhook.
- Token webhooka leży w `app_settings` jak inne sekrety w bazie (refresh token
  Google). Nie wraca do HTML ani logów jobów, ale zrzut bazy go zawiera.

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

Pamięć spotkań to osobny profil: `docker compose --profile honcho up -d`
dokłada `honcho-db`, `honcho-redis`, `honcho-api`, `honcho-deriver` (patrz
„Pamięć spotkań (Honcho)”). Bez profilu nic się nie zmienia.

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
│   ├── tasks.py               # fetch_assets / transcribe / process / sync / honcho_*
│   ├── memory.py              # pamięć spotkań w Honcho: peerzy, ingest, pytania
│   ├── worker.py              # proces workera (multiprocessing)
│   ├── queries.py             # filtrowanie i wyszukiwanie spotkań
│   ├── models.py, db.py, config.py
│   ├── templates/             # Jinja2 (server-side render)
│   └── static/                # app.css, app.js (bez build stepu)
├── tests/                     # pytest: kolejka, filtry, kalendarz, eksport
├── audio/                     # pliki wejściowe (gitignored)
└── output/                    # transkrypcje + baza webappki (gitignored)
```

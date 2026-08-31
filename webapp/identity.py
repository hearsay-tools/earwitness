"""Dopasowanie nazw uczestników z Recall do adresów z kalendarza.

Dlaczego piszemy to sami, mimo że Recall ma własny fuzzy matching: Recall
ocenia każdego uczestnika **niezależnie** i przy niskiej pewności zwraca
`null`. Na naszych danych daje 49 trafień na 423 wpisy i przegrywa nawet
w dwuosobowym callu, gdzie oba adresy są w zaproszeniu.

Nasze zadanie jest domknięte: w obrębie jednego spotkania mamy N nazw z callu
i M adresów z zaproszenia, a przypisanie jest jeden-do-jednego. Dzięki temu
przy dwóch osobach nawet słaby sygnał jest rozstrzygający — nie ma alternatywy.
Rozwiązujemy więc globalne przypisanie na spotkanie, nie serię niezależnych
decyzji.

Adres e-mail jest kanonicznym identyfikatorem osoby (jest stabilny i zawsze
obecny w kalendarzu). Nazwa wyświetlana z Recall to tylko etykieta — jedna
    osoba potrafi mieć ich kilka („Jan Kowalski", „Jan Kowalski (Acme)").
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from itertools import permutations
from typing import Iterable, Optional

# Poniżej tego progu wolimy brak przypisania niż zgadywanie.
MIN_SCORE = 0.62
# Gdy dwa różne adresy pasują do jednej nazwy równie dobrze — nie zgadujemy.
TIE_EPS = 0.02

# `ł` i `Ł` nie rozkładają się przez NFKD, trzeba je podmienić ręcznie.
_MANUAL_FOLD = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "đ": "d", "ß": "ss"})
_PAREN = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Tytuły i dopiski, które nie są częścią nazwiska.
_NOISE_TOKENS = {
    "dr",
    "mgr",
    "inz",
    "inż",
    "prof",
    "phd",
    "mba",
    "apptension",
    "guest",
    "gosc",
    "external",
    "ext",
    "iphone",
    "ipad",
}


def fold(text: str) -> str:
    """Bez znaków diakrytycznych, małymi literami, tylko [a-z0-9]."""
    text = (text or "").translate(_MANUAL_FOLD)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return _NON_ALNUM.sub(" ", text.lower()).strip()


def name_tokens(name: Optional[str]) -> list[str]:
    """Człony nazwy, bez nawiasów, tytułów i śmieci."""
    raw = _PAREN.sub(" ", name or "")
    toks = [t for t in fold(raw).split() if len(t) >= 2 and t not in _NOISE_TOKENS]
    return toks


def email_local(email: Optional[str]) -> str:
    return fold((email or "").split("@", 1)[0])


def email_tokens(email: Optional[str]) -> list[str]:
    return [t for t in email_local(email).split() if t]


def score(name: Optional[str], email: Optional[str]) -> float:
    """Jak dobrze nazwa pasuje do adresu. 0.0 = nijak, 1.0 = pewne.

    Obsługiwane konwencje (na naszych danych realnie występujące):
      jan.kowalski@ / j.kowalski@ / kowalski.jan@  → tokeny rozdzielone kropką
      jkowalski@ / jankowalski@ / kowalskij@       → tokeny zlepione
      kowalski@                                    → samo nazwisko
    """
    nt = name_tokens(name)
    lp = email_local(email).replace(" ", "")
    if not nt or not lp:
        return 0.0

    et = email_tokens(email)
    best = 0.0

    # --- adres z separatorami: dopasowanie tokenów ---
    if len(et) > 1:
        for order in permutations(nt, min(len(nt), len(et))):
            if len(order) != len(et):
                continue
            hits = []
            for e, n in zip(et, order):
                if e == n:
                    hits.append(1.0)
                elif len(e) == 1 and n.startswith(e):
                    hits.append(0.86)  # inicjał
                elif len(e) >= 3 and n.startswith(e):
                    hits.append(0.8)  # skrócone imię (kasia → katarzyna)
                else:
                    hits.append(0.0)
            if all(hits):
                best = max(best, sum(hits) / len(hits))

    # --- adres zlepiony ---
    for order in permutations(nt, min(len(nt), 3)):
        joined = "".join(order)
        if lp == joined:
            return 1.0
        if len(order) >= 2:
            head, tail = order[0], "".join(order[1:])
            if lp == head[0] + tail:
                best = max(best, 0.95)  # jkowalski = j + kowalski
            if lp == head + tail[0]:
                best = max(best, 0.84)  # jank
            if lp == "".join(t[0] for t in order):
                best = max(best, 0.5)  # same inicjały — słabe

    # --- zawieranie członu ---
    if best < 0.8:
        surname = nt[-1]
        if len(surname) >= 4 and surname in lp:
            # Nazwisko w adresie plus inicjał imienia gdziekolwiek indziej.
            extra = (
                0.08 if len(nt) > 1 and nt[0][0] in lp.replace(surname, "", 1) else 0.0
            )
            best = max(best, 0.74 + extra)
        for tok in nt[:-1]:
            if len(tok) >= 4 and tok in lp:
                best = max(best, 0.6)

    return round(min(best, 1.0), 4)


@dataclass
class Candidate:
    """Uczestnik z callu, dla którego szukamy adresu."""

    ref: object  # cokolwiek — zwracamy to nietknięte
    name: Optional[str]


@dataclass
class Match:
    ref: object
    email: str
    score: float


def assign(
    candidates: Iterable[Candidate],
    emails: Iterable[str],
    *,
    min_score: float = MIN_SCORE,
) -> tuple[list[Match], list[Candidate], list[str]]:
    """Przypisz adresy do uczestników jeden-do-jednego.

    Zwraca (dopasowania, nieprzypisani uczestnicy, niewykorzystane adresy).

    Zachłannie po malejącym wyniku — przy tak małych zbiorach (kilka osób)
    daje ten sam efekt co algorytm węgierski, a jest czytelne. Świadomie
    zostawiamy nieprzypisanych po obu stronach: nie każdy zaproszony przyszedł
    i nie każdy obecny był zaproszony.
    """
    cands = [c for c in candidates if name_tokens(c.name)]
    pool = sorted({e.strip().lower() for e in emails if e and "@" in e})
    if not cands or not pool:
        return [], list(candidates), pool

    grid: list[tuple[float, int, str]] = []
    for i, c in enumerate(cands):
        for e in pool:
            s = score(c.name, e)
            if s >= min_score:
                grid.append((s, i, e))
    grid.sort(key=lambda r: (-r[0], r[2]))

    # Remis: dwa różne adresy pasują do tej samej nazwy równie dobrze.
    ambiguous: set[int] = set()
    by_cand: dict[int, list[tuple[float, str]]] = {}
    for s, i, e in grid:
        by_cand.setdefault(i, []).append((s, e))
    for i, opts in by_cand.items():
        if len(opts) > 1 and opts[0][0] - opts[1][0] < TIE_EPS:
            ambiguous.add(i)

    used_c: set[int] = set()
    used_e: set[str] = set()
    matches: list[Match] = []
    for s, i, e in grid:
        if i in used_c or e in used_e or i in ambiguous:
            continue
        used_c.add(i)
        used_e.add(e)
        matches.append(Match(ref=cands[i].ref, email=e, score=s))

    unmatched_c = [c for i, c in enumerate(cands) if i not in used_c]
    unmatched_c += [c for c in candidates if c not in cands]
    return matches, unmatched_c, [e for e in pool if e not in used_e]


# --------------------------------------------------------------------------
# Podłączenie do modelu
# --------------------------------------------------------------------------


def propagate_known_emails(meetings) -> int:  # noqa: ANN001 — iterable[Meeting]
    """Przenieś powiązanie nazwa→adres, poznane w jednym spotkaniu, na pozostałe.

    Po co, skoro mamy dopasowanie w obrębie spotkania: zaproszenia bywają
    niekompletne (aliasy grupowe, przesłany link), więc ta sama osoba raz jest
    do dopasowania, a raz nie ma jej adresu w evencie. Wiedzę zdobytą tam,
    gdzie się udało, przenosimy tam, gdzie jej brakuje.

    To **nie** jest wnioskowanie przez wykluczenie („została jedna nazwa i jeden
    adres, więc to musi być ta sama osoba"). Tamto opiera się na założeniu, że
    lista obecnych równa się liście zaproszonych, a to nieprawda: ludzie
    dołączają bez zaproszenia i nie przychodzą po zaproszeniu. Tu mamy
    pozytywny dowód o osobie, nie brak alternatywy.

    Dwa zabezpieczenia: propagujemy tylko nazwy, które w całych danych wskazują
    dokładnie jeden adres, i tylko gdy ten adres nie jest już w tym spotkaniu
    przypisany komuś innemu.
    """
    from webapp.models import looks_like_bot

    meetings = list(meetings)
    seen: dict[str, set[str]] = {}
    for m in meetings:
        for p in m.participants:
            if p.name and p.email and not looks_like_bot(p.name, p.email):
                key = " ".join(p.name.lower().split())
                seen.setdefault(key, set()).add(p.email.strip().lower())
    unique = {n: next(iter(e)) for n, e in seen.items() if len(e) == 1}

    filled = 0
    for m in meetings:
        people = [
            p
            for p in m.participants
            if not p.is_bot and not looks_like_bot(p.name, p.email)
        ]
        taken = {p.email.strip().lower() for p in people if p.email}
        for p in people:
            if p.email or p.source != "recall" or not p.name:
                continue
            cand = unique.get(" ".join(p.name.lower().split()))
            if cand and cand not in taken:
                p.email = cand
                p.email_source = "propagated"
                taken.add(cand)
                filled += 1
    return filled


# Dopasowanie globalne traci ograniczenie zamkniętego zbioru, więc próg jest
# wyraźnie wyższy niż w obrębie spotkania, a do tego wymagamy przewagi nad
# drugim kandydatem i wzajemności.
GLOBAL_MIN = 0.9
GLOBAL_MARGIN = 0.1


def match_globally(meetings) -> int:  # noqa: ANN001 — iterable[Meeting]
    """Dopasuj pozostałe nazwy do adresów znanych z **innych** spotkań.

    Po co, skoro mamy już dopasowanie w spotkaniu i propagację: człowiek może
    przyjść na spotkanie, na które nie był zaproszony. Wtedy jego adresu nie ma
    w tym evencie (dopasowanie nie ma czego szukać), a nazwa nigdy nie
    współwystąpiła z adresem (propagacja nie ma czego przenieść) — mimo że
    adres siedzi w zaproszeniu na inne spotkanie — np. zaproszony na jedno
    wewnętrzne sync, a wszedł na inne.

    To nadal dowód pozytywny — podobieństwo nazwy do adresu — a nie
    wnioskowanie przez wykluczenie. Trzy zabezpieczenia zamiast zamkniętego
    zbioru: wysoki próg, przewaga nad drugim kandydatem i wzajemność
    (żadna inna znana nazwa nie pasuje do tego adresu lepiej).
    """
    from webapp.models import looks_like_bot

    meetings = list(meetings)
    pool: set[str] = set()
    all_names: set[str] = set()
    for m in meetings:
        for p in m.participants:
            if looks_like_bot(p.name, p.email):
                continue
            if p.email:
                pool.add(p.email.strip().lower())
            if p.name:
                all_names.add(" ".join(p.name.split()))
    if not pool:
        return 0

    filled = 0
    for m in meetings:
        people = [
            p
            for p in m.participants
            if not p.is_bot and not looks_like_bot(p.name, p.email)
        ]
        taken = {p.email.strip().lower() for p in people if p.email}
        for p in people:
            if p.email or p.source != "recall" or not p.name:
                continue
            ranked = sorted(
                ((score(p.name, e), e) for e in pool if e not in taken), reverse=True
            )
            if not ranked or ranked[0][0] < GLOBAL_MIN:
                continue
            top_score, top_email = ranked[0]
            runner = ranked[1][0] if len(ranked) > 1 else 0.0
            if top_score - runner < GLOBAL_MARGIN:
                continue
            # Wzajemność: nikt inny nie pasuje do tego adresu lepiej.
            best_rival = max(score(n, top_email) for n in all_names)
            if top_score < best_rival:
                continue
            p.email = top_email
            p.email_source = "global"
            p.match_score = top_score
            taken.add(top_email)
            filled += 1
    return filled


def resolve_meeting(meeting) -> dict[str, int]:  # noqa: ANN001 — webapp.models.Meeting
    """Uzupełnij adresy uczestnikom z Recall na podstawie zaproszenia.

    Ruszamy tylko wiersze bez adresu — to, co Recall dopasował sam, jest
    źródłem prawdy i pozostaje nietknięte. Adresy już przypisane innym
    uczestnikom tego spotkania wypadają z puli, żeby utrzymać jeden-do-jednego.
    """
    # `is_bot` w bazie bywa nieaktualne (wiersze zapisane przed rozpoznawaniem
    # botów po adresie), a notetaker w puli adresów potrafi zostać przypisany
    # człowiekowi. Sprawdzamy więc na żywo, nie tylko flagę.
    from webapp.models import looks_like_bot

    people = [
        p
        for p in meeting.participants
        if not p.is_bot and not looks_like_bot(p.name, p.email)
    ]
    recall_rows = [p for p in people if p.source == "recall"]
    taken = {p.email.strip().lower() for p in recall_rows if p.email}
    pool = [
        p.email
        for p in people
        if p.source == "calendar" and p.email and p.email.strip().lower() not in taken
    ]
    todo = [Candidate(ref=p, name=p.name) for p in recall_rows if not p.email]
    if not todo or not pool:
        return {"matched": 0, "left": len(todo)}

    matches, unmatched, _unused = assign(todo, pool)
    for m in matches:
        row = m.ref
        row.email = m.email
        row.email_source = "matched"
        row.match_score = m.score
    return {"matched": len(matches), "left": len(unmatched)}

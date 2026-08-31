"""Dopasowanie nazwa ↔ adres. Fixtures są syntetyczne."""

from __future__ import annotations

import pytest
from webapp.identity import Candidate, assign, fold, name_tokens, score


def test_fold_removes_polish_diacritics():
    assert fold("Paweł Świątek") == "pawel swiatek"
    assert fold("Marek Słoń") == "marek slon"
    assert fold("Rafał Górski") == "rafal gorski"


def test_name_tokens_drops_parenthetical_suffix():
    assert name_tokens("Jan Kowalski (Guest)") == ["jan", "kowalski"]
    assert name_tokens("Jan Kowalski") == ["jan", "kowalski"]


@pytest.mark.parametrize(
    "name,email",
    [
        ("Jan Kowalski", "jkowalski@acme.com"),
        ("Paweł Świątek", "pswiatek@acme.com"),
        ("Michał Nowak", "mnowak@acme.com"),
        ("Tomasz Wójcik", "twojcik@acme.com"),
        ("Marek Zieliński", "mzielinski@acme.com"),
        ("Anna Nowak", "anowak@acme.com"),
        ("Łukasz Dąbrowski", "ldabrowski@acme.com"),
        ("Ewa Szymańska", "eszymanska@acme.com"),
        ("Nowak Kamil", "k.nowak@client.pl"),
        ("Kowalska Jolanta", "j.kowalska@client.pl"),
        ("Costa Maria", "maria.costa@partner.gr"),
        ("Ewa Nowicka", "ewa.nowicka@gmail.com"),
        ("Jan Kowalski (Guest)", "jkowalski@acme.com"),
    ],
)
def test_real_pairs_score_high(name, email):
    assert score(name, email) >= 0.8, f"{name} ↔ {email} = {score(name, email)}"


@pytest.mark.parametrize(
    "name,email",
    [
        ("Jan Kowalski", "anowak@acme.com"),
        ("Paweł Świątek", "mnowak@acme.com"),
        ("Marek Zieliński", "jkowalski@acme.com"),
        ("Anna Nowak", "fred@fireflies.ai"),
        ("Ewa Nowicka", "eszymanska@acme.com"),
    ],
)
def test_wrong_pairs_score_low(name, email):
    assert score(name, email) < 0.62, f"{name} ↔ {email} = {score(name, email)}"


def test_two_person_call_is_decisive():
    """Dwuosobowy call, w którym oba adresy są w zaproszeniu."""
    matches, unmatched, unused = assign(
        [Candidate("w", "Jan Kowalski"), Candidate("z", "Marek Zieliński")],
        ["jkowalski@acme.com", "mzielinski@acme.com"],
    )
    got = {m.ref: m.email for m in matches}
    assert got == {"w": "jkowalski@acme.com", "z": "mzielinski@acme.com"}
    assert not unmatched and not unused


def test_assignment_is_one_to_one():
    """Dwie osoby o podobnych nazwiskach nie mogą dostać tego samego adresu."""
    matches, _, _ = assign(
        [Candidate("a", "Ewa Nowicka"), Candidate("b", "Ewa Szymańska")],
        ["enowicka@acme.com", "eszymanska@acme.com"],
    )
    got = {m.ref: m.email for m in matches}
    assert got == {"a": "enowicka@acme.com", "b": "eszymanska@acme.com"}
    assert len({m.email for m in matches}) == len(matches)


def test_extra_invitees_stay_unused():
    """Nie każdy zaproszony przyszedł — reszta adresów zostaje wolna."""
    matches, unmatched, unused = assign(
        [Candidate("w", "Jan Kowalski")],
        ["jkowalski@acme.com", "anowak@acme.com", "mnowak@acme.com"],
    )
    assert [m.email for m in matches] == ["jkowalski@acme.com"]
    assert not unmatched
    assert set(unused) == {"anowak@acme.com", "mnowak@acme.com"}


def test_guest_without_invite_stays_unmatched():
    """Ktoś dołączył bez zaproszenia — nie wciskamy mu cudzego adresu."""
    matches, unmatched, _ = assign(
        [Candidate("w", "Jan Kowalski"), Candidate("x", "Omar Hassan")],
        ["jkowalski@acme.com"],
    )
    assert [m.ref for m in matches] == ["w"]
    assert [c.ref for c in unmatched] == ["x"]


def test_ambiguity_is_left_unmatched():
    """Dwa adresy pasują identycznie — wolimy nic niż losowo."""
    matches, unmatched, _ = assign(
        [Candidate("a", "Jan Kowalski")],
        ["jkowalski@firma.pl", "j.kowalski@firma.com"],
    )
    assert matches == []
    assert [c.ref for c in unmatched] == ["a"]


def test_empty_inputs_are_safe():
    assert assign([], ["a@b.pl"]) == ([], [], ["a@b.pl"])
    m, un, uu = assign([Candidate("a", "Ktoś Tam")], [])
    assert m == [] and len(un) == 1 and uu == []


def test_nameless_participant_is_skipped():
    m, un, _ = assign(
        [Candidate("a", None), Candidate("b", "Jan Kowalski")], ["jkowalski@acme.com"]
    )
    assert [x.ref for x in m] == ["b"]
    assert [c.ref for c in un] == ["a"]


def test_bot_email_does_not_win_a_human():
    matches, _, _ = assign(
        [Candidate("w", "Jan Kowalski")],
        ["fred@fireflies.ai", "jkowalski@acme.com"],
    )
    assert [m.email for m in matches] == ["jkowalski@acme.com"]


# --------------------------------------------------------------------------
# Propagacja między spotkaniami — i dlaczego NIE robimy wnioskowania
# przez wykluczenie.
# --------------------------------------------------------------------------


class _P:
    """Atrapa MeetingParticipant — wystarczy do logiki propagacji."""

    def __init__(self, source, name=None, email=None, is_bot=False):
        self.source, self.name, self.email = source, name, email
        self.is_bot, self.email_source, self.match_score = is_bot, None, None


class _M:
    def __init__(self, *participants):
        self.participants = list(participants)


def test_propagation_fills_gap_from_another_meeting():
    """Zaproszenie w drugim spotkaniu jest niekompletne — bierzemy z pierwszego."""
    from webapp.identity import propagate_known_emails

    a = _M(_P("recall", "Paweł Lewandowski", "plewandowski@acme.com"))
    b = _M(_P("recall", "Paweł Lewandowski"))
    assert propagate_known_emails([a, b]) == 1
    assert b.participants[0].email == "plewandowski@acme.com"
    assert b.participants[0].email_source == "propagated"


def test_propagation_refuses_when_name_maps_to_two_addresses():
    """Ta sama osoba ma dwa adresy — nie zgadujemy który."""
    from webapp.identity import propagate_known_emails

    a = _M(_P("recall", "Ewa Nowicka", "ewa.nowicka@gmail.com"))
    b = _M(_P("calendar", "Ewa Nowicka", "enowicka@acme.com"))
    c = _M(_P("recall", "Ewa Nowicka"))
    assert propagate_known_emails([a, b, c]) == 0
    assert c.participants[0].email is None


def test_propagation_refuses_address_already_taken_in_that_meeting():
    from webapp.identity import propagate_known_emails

    a = _M(_P("recall", "Jan Nowak", "jnowak@firma.pl"))
    b = _M(_P("recall", "Jan Nowak"), _P("calendar", "Ktoś Inny", "jnowak@firma.pl"))
    assert propagate_known_emails([a, b]) == 0


def test_propagation_ignores_bots():
    from webapp.identity import propagate_known_emails

    a = _M(_P("recall", "Fireflies.ai Notetaker", "fred@fireflies.ai"))
    b = _M(_P("recall", "Fireflies.ai Notetaker"))
    assert propagate_known_emails([a, b]) == 0


def test_leftover_name_and_leftover_email_are_not_paired():
    """Zasada wykluczenia jest niebezpieczna i celowo jej nie stosujemy.

    Konto firmowe w callu i adres notetakera w zaproszeniu: jedna nazwa,
    jeden adres, zero wspólnego — sparowanie byłoby cichym błędem.
    """
    matches, unmatched, unused = assign(
        [Candidate("firma", "Acme Publishing")],
        ["fred@fireflies.ai"],
    )
    assert matches == []
    assert [c.ref for c in unmatched] == ["firma"]
    assert unused == ["fred@fireflies.ai"]


def test_global_match_finds_address_from_another_meeting():
    """Zaproszony na jedno spotkanie, przyszedł na inne."""
    from webapp.identity import match_globally

    frontend = _M(
        _P("calendar", None, "kwrobel@acme.com"),
        _P("calendar", None, "ldabrowski@acme.com"),
    )
    backend = _M(
        _P("recall", "Karol Wróbel"),
        _P("recall", "Łukasz Dąbrowski", "ldabrowski@acme.com"),
    )
    assert match_globally([frontend, backend]) == 1
    p = backend.participants[0]
    assert p.email == "kwrobel@acme.com"
    assert p.email_source == "global"
    assert p.match_score >= 0.9


def test_global_match_refuses_company_account():
    """Konto firmowe nie pasuje do niczego — zostaje bez adresu."""
    from webapp.identity import match_globally

    a = _M(
        _P("calendar", None, "ela.nowak-kowalska@vendor.com"),
        _P("calendar", None, "mzielinski@acme.com"),
    )
    b = _M(_P("recall", "Acme Publishing"))
    assert match_globally([a, b]) == 0
    assert b.participants[0].email is None


def test_global_match_requires_mutual_best():
    """Adres pasuje lepiej komuś innemu — nie przypisujemy go słabszemu."""
    from webapp.identity import match_globally

    known = _M(_P("calendar", "Jan Kowalski", "jkowalski@firma.pl"))
    other = _M(_P("recall", "Jan Kowalewski"))
    assert match_globally([known, other]) == 0


def test_global_match_skips_address_taken_in_that_meeting():
    from webapp.identity import match_globally

    a = _M(_P("calendar", None, "kwrobel@acme.com"))
    b = _M(
        _P("recall", "Karol Wróbel"), _P("calendar", "Ktoś Inny", "kwrobel@acme.com")
    )
    assert match_globally([a, b]) == 0

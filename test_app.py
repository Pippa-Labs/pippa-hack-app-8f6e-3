"""Tests for Clean Plate. The build runs them with `pytest`.

Publishing runs them, and a repository whose tests fail does not deploy.
No test touches the network (conftest.py refuses every socket): each one
replaces the app's one HTTP seam, `app._fetch`, with `FakeFetch`, which answers
from the real API responses saved in tests/fixtures/ (tests/fixtures/README.md
lists the exact URL behind each file) and fails the test on any other URL, on
a fourth request, or on a timeout over 5 seconds.

Each example below is what a person would type, next to the inputs Pippa's
model would pull out of it. The expected answers were read off the fixtures.
The two tests marked `network` call the real APIs; the build leaves them out.
"""

from __future__ import annotations

import json
import re
import socket
import unittest
import urllib.error
from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

try:                       # the build has pytest; nothing else here needs it
    import pytest
except ImportError:
    pytest = None

from pippa_agentapp_sdk import (
    AgentAppError,
    InputShapeViolation,
    LocalRunner,
    load_manifest_file,
    validate_manifest,
)

from app import App


def _network(test):
    test.network = True
    return test


#: Marks a test that calls the real APIs: deselected in the build (pytest.ini),
#: run by hand only, with `pytest -m network`.
network = pytest.mark.network if pytest else _network

FIXED_TIME = datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)
HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "tests" / "fixtures"
OUTCOME = "hygiene.rating"
FSA_HOST = "api.ratings.food.gov.uk"
POSTCODES_HOST = "api.postcodes.io"
MAX_FETCHES = 3
MAX_TIMEOUT = 5.0
MAX_NARRATIVE = 500
UNEXPECTED = "Something went wrong looking up that hygiene rating, so please try again."

SERVICE_DOWN = ("The Food Standards Agency's ratings service isn't answering right now, "
                "so I can't check that rating; please try again in a minute.")
BUSY = "The food hygiene ratings service is busy right now, so please try again shortly."
NO_BUSINESS = "Tell me the name of the restaurant, cafe or takeaway you want the hygiene rating for."

EMPTY_DATA = {"business_name": "", "address": "", "postcode": "", "rating": "",
              "rating_date": "", "other_matches": []}
OUTPUT = {   # name: (type, required)
    "business_name": ("string", True), "address": ("string", True), "postcode": ("string", True),
    "rating": ("string", True), "rating_date": ("string", True),
    "hygiene_score": ("integer", False), "structural_score": ("integer", False),
    "management_score": ("integer", False), "other_matches": ("array", True),
}
SCORES = ("hygiene_score", "structural_score", "management_score")
RATINGS = {"0", "1", "2", "3", "4", "5", "Pass", "Improvement Required", "Pass and Eat Safe",
           "Exempt", "Awaiting inspection", "Awaiting publication", ""}
#: Words other Pippa apps own: a summary or phrase using one competes with them.
RESERVED_WORDS = ("calendar", "diary", "meeting", "schedule", "today", "tomorrow", "remind",
                  "alarm", "remember", "forget", "search", "google", "news")


# ── The fixtures, and a fake network that serves them ────────────────────────

def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture(name: str):
    return json.loads(fixture_bytes(name))


def _place_point(name: str) -> tuple[float, float]:
    """The London result in a saved postcodes.io /places answer."""
    results = fixture(name)["result"]
    best = next((r for r in results if r.get("county_unitary") == "Greater London"), results[0])
    return best["latitude"], best["longitude"]


def _postcode_point(name: str) -> tuple[float, float]:
    result = fixture(name)["result"]
    return result["latitude"], result["longitude"]


KINGS_CROSS = _place_point("postcodes_places_kings_cross.json")
SOHO = _place_point("postcodes_places_soho.json")
BRIXTON = _place_point("postcodes_places_brixton.json")
SW7_2AZ = _postcode_point("postcodes_postcode_sw72az.json")
SW7 = _postcode_point("postcodes_outcode_sw7.json")
EH1_1BB = _postcode_point("postcodes_postcode_eh11bb.json")

#: FSA searches by name only, keyed on the lower-cased `name` sent.
FSA_BY_NAME = {
    "dishoom": "fsa_dishoom_name_only.json",
    "chicken": "fsa_chicken_name_only.json",
    "zzqxnotarealplace": "fsa_no_results.json",
    "10 to 10 in delhi": "fsa_improvement_required.json",
    "parea hospitality": "fsa_awaiting_inspection.json",
    "science museum shop": "fsa_exempt.json",
    # "shop" is a generic word the app drops; this listing is the one under test.
    "science museum": "fsa_exempt.json",
}
#: FSA searches by name and address (as the person wrote the place).
FSA_BY_ADDRESS = {
    ("dishoom", "king's cross"): "fsa_dishoom_address_kings_cross.json",
    ("bao", "soho"): "fsa_bao_address_soho.json",
    ("little bao", "soho"): "fsa_bao_address_soho.json",   # a real empty answer: FSA
    ("bon bao", "soho"): "fsa_bao_address_soho.json",      # addresses never say "Soho"
    ("franco manca", "brixton"): "fsa_franco_manca_address_brixton.json",
    ("chicken", "exhibition road"): "fsa_chicken_address_exhibition_road.json",
}
#: FSA searches near a point: (name, point, fixture). The point must be within
#: about a kilometre of the one postcodes.io gave for the place.
FSA_NEAR = [
    ("dishoom", KINGS_CROSS, "fsa_dishoom_geo_kings_cross.json"),
    ("bao", SOHO, "fsa_bao_geo_soho.json"),
    # The "Bao" answer near Soho holds both of these listings; ranking is under test.
    ("little bao", SOHO, "fsa_bao_geo_soho.json"),
    ("bon bao", SOHO, "fsa_bao_geo_soho.json"),
    ("franco manca", BRIXTON, "fsa_franco_manca_geo_brixton.json"),
    ("pret", SW7_2AZ, "fsa_pret_geo_sw72az.json"),
    ("pret", SW7, "fsa_pret_geo_sw72az.json"),    # SW7's centre is 400 m from SW7 2AZ
    ("pret", EH1_1BB, "fsa_pret_geo_eh11bb.json"),
]
POSTCODES = {"SW72AZ": "postcodes_postcode_sw72az.json", "EH11BB": "postcodes_postcode_eh11bb.json"}
POSTCODES_NOT_FOUND = {"ZZ11ZZ"}
OUTCODES = {"SW7": "postcodes_outcode_sw7.json"}
PLACES = {
    "king's cross": "postcodes_places_kings_cross.json",
    "soho": "postcodes_places_soho.json",
    "brixton": "postcodes_places_brixton.json",
    "exhibition road": "postcodes_places_exhibition_road.json",
}


def http_error(url: str, code: int, body: bytes = b"", headers: dict | None = None):
    message = Message()
    for name, value in (headers or {}).items():
        message[name] = value
    return urllib.error.HTTPError(url, code, f"HTTP {code}", message, BytesIO(body))


class FakeFetch:
    """Replaces `App._fetch`: answers from tests/fixtures and records each call.

    `fail(url, parts)` may return an exception to raise or bytes to return
    instead of the fixture (None: serve the fixture). `extra_address` adds
    FSA name+address routes. With `fallback=True`, an FSA or postcodes.io URL
    with no fixture gets an empty (real) answer instead of failing the test.
    """

    def __init__(self, fail=None, extra_address=None, fallback=False):
        self.fail, self.fallback = fail, fallback
        self.by_address = {**FSA_BY_ADDRESS, **(extra_address or {})}
        self.calls: list[tuple[str, float]] = []
        self.problems: list[str] = []
        self.served: list = []        # the JSON served, for the narrative's numbers

    def __call__(self, url, timeout=5.0):
        self.calls.append((url, timeout))
        if len(self.calls) > MAX_FETCHES:
            self._unexpected(url, f"request number {len(self.calls)}; the limit is {MAX_FETCHES}")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT:
            self._unexpected(url, f"timeout {timeout!r}; it must be over 0 and at most {MAX_TIMEOUT}")
        parts = urlsplit(url)
        if self.fail is not None:
            outcome = self.fail(url, parts)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
        body = self._route(url, parts)
        self.served.append(json.loads(body))
        return body

    def _unexpected(self, url: str, why: str):
        self.problems.append(f"{why}: {url}")
        raise AssertionError(f"FakeFetch: {why}: {url}")

    def _route(self, url: str, parts) -> bytes:
        if parts.scheme != "https" or "api.tfl.gov.uk" in url:
            self._unexpected(url, "not an https URL of the FSA or postcodes.io")
        query = {key: values[0] for key, values in parse_qs(parts.query, keep_blank_values=True).items()}
        name = query.get("name", "").strip().lower()
        found = None
        if parts.hostname == FSA_HOST and parts.path.lower() == "/establishments":
            if "latitude" in query or "longitude" in query:
                lat, lon = float(query.get("latitude", "nan")), float(query.get("longitude", "nan"))
                found = next((file for n, (plat, plon), file in FSA_NEAR
                              if n == name and abs(lat - plat) < 0.01 and abs(lon - plon) < 0.01), None)
            elif "address" in query:
                found = self.by_address.get((name, query["address"].strip().lower()))
            else:
                found = FSA_BY_NAME.get(name)
            if found is None and self.fallback:
                found = "fsa_no_results.json"
        elif parts.hostname == POSTCODES_HOST:
            path = unquote(parts.path)
            if path.startswith("/postcodes/"):
                code = path[len("/postcodes/"):].replace(" ", "").upper()
                if code in POSTCODES_NOT_FOUND or (self.fallback and code not in POSTCODES):
                    raise http_error(url, 404, fixture_bytes("postcodes_postcode_not_found.json"))
                found = POSTCODES.get(code)
            elif path.startswith("/outcodes/"):
                found = OUTCODES.get(path[len("/outcodes/"):].strip().upper())
            elif path == "/places":
                found = PLACES.get(query.get("q", "").strip().lower())
                if found is None and self.fallback:
                    found = "postcodes_places_exhibition_road.json"     # {"result": []}
        if found is None:
            self._unexpected(url, "no fixture for this URL")
        return fixture_bytes(found)

    def check(self) -> None:
        assert not self.problems, "\n".join(self.problems)
        assert len(self.calls) <= MAX_FETCHES, self.calls
        assert all(0 < timeout <= MAX_TIMEOUT for _, timeout in self.calls), self.calls


# ── Asking, and what every answer must be ────────────────────────────────────

ABSENT = object()


def ask(business, place=ABSENT, fetch: FakeFetch | None = None, explicit: bool = False):
    """Call the app through LocalRunner, the way Pippa does, on fixtures only."""
    fetch = FakeFetch() if fetch is None else fetch
    app = App()
    app._fetch = fetch
    inputs = {"business": business}
    if place is not ABSENT:
        inputs["place"] = place
    try:
        result = LocalRunner(app).call(OUTCOME, inputs, granted_secrets={}, now=FIXED_TIME,
                                       request_id="test-1", explicit=explicit).result
    finally:
        fetch.check()
    check_answer(result, fetch, inputs)
    return result


def numbers(text: str) -> set[str]:
    return {str(int(n)) for n in re.findall(r"\d+", text)}


def assert_one_sentence(narrative) -> None:
    assert isinstance(narrative, str) and narrative.strip(), narrative
    assert narrative == narrative.strip(), repr(narrative)
    assert narrative.endswith(".") and not narrative.endswith(".."), narrative
    assert "\n" not in narrative and "\r" not in narrative and ". " not in narrative, narrative
    assert len(narrative) <= MAX_NARRATIVE, (len(narrative), narrative)
    assert "None" not in narrative and "null" not in narrative, narrative


def check_answer(result, fetch: FakeFetch, inputs: dict) -> None:
    """What every answer must be, whatever was asked."""
    data, narrative = result.data, result.narrative
    assert_one_sentence(narrative)
    # Every number in the narrative comes from the data, the inputs, the
    # "out of 5" scale or the count of listings FSA reported.
    allowed = numbers(json.dumps(dict(data), ensure_ascii=False)) | {"5"}
    allowed |= numbers(" ".join(str(v) for v in inputs.values() if isinstance(v, str)))
    for body in fetch.served:
        if isinstance(body, dict) and isinstance(body.get("establishments"), list):
            allowed |= {str(len(body["establishments"])), str((body.get("meta") or {}).get("totalCount"))}
    assert numbers(narrative) <= allowed, (narrative, numbers(narrative) - allowed)
    # The declared shape, checked here as well as by the runner.
    assert {k for k, (_, required) in OUTPUT.items() if required} <= set(data) <= set(OUTPUT), data
    for key in SCORES:
        assert key not in data or (type(data[key]) is int and data[key] >= 0), (key, data.get(key))
    assert isinstance(data["other_matches"], list) and len(data["other_matches"]) <= 3, data
    assert all(isinstance(m, str) and m.strip() for m in data["other_matches"]), data
    assert data["rating"] in RATINGS, data["rating"]
    assert data["rating_date"] == "" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["rating_date"]), data
    if data["business_name"] == "":
        assert {k: data[k] for k in ("address", "postcode", "rating", "rating_date")} == \
            {"address": "", "postcode": "", "rating": "", "rating_date": ""}, data
        assert not set(SCORES) & set(data), data
    for url, _ in fetch.calls:
        assert urlsplit(url).hostname in (FSA_HOST, POSTCODES_HOST), url
        assert "api.tfl.gov.uk" not in url
        assert " " not in url and "'" not in url, f"user input not URL-encoded: {url}"


def without_matches(data: dict) -> dict:
    return {k: v for k, v in data.items() if k != "other_matches"}


def fsa_queries(fetch: FakeFetch) -> list[dict]:
    return [{k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            for url, _ in fetch.calls if urlsplit(url).hostname == FSA_HOST]


# ── The manifest ─────────────────────────────────────────────────────────────

def test_the_manifest_is_valid():
    manifest = App().manifest
    assert manifest.app_id == "hack.app_8f6e_3"
    assert [o.id for o in manifest.outcomes] == [OUTCOME]
    again = validate_manifest(load_manifest_file(HERE / "manifest.yaml"))
    assert again.app_id == manifest.app_id and again.version == manifest.version


def test_the_manifest_declares_the_contract():
    manifest = load_manifest_file(HERE / "manifest.yaml")
    assert manifest["app"]["name"] == "Clean Plate"
    assert manifest["persistence"] == "none"
    assert not manifest.get("connectors") and not manifest.get("secrets")
    (outcome,) = manifest["outcomes"]
    assert outcome["id"] == OUTCOME
    assert [(i["name"], i["type"], i.get("required", True)) for i in outcome["inputs"]] == [
        ("business", "string", True), ("place", "string", False),
    ]
    output = {f["name"]: f for f in outcome["output"]}
    assert {name: (f["type"], f.get("required", True)) for name, f in output.items()} == OUTPUT
    assert output["other_matches"]["item_type"] == "string"
    assert len(outcome["example_phrases"]) >= 3


def test_the_summary_and_phrases_avoid_by_and_words_other_apps_own():
    manifest = load_manifest_file(HERE / "manifest.yaml")
    summary = manifest["app"]["summary"]
    assert not re.search(r"\bby\b", summary, re.IGNORECASE), summary
    (outcome,) = manifest["outcomes"]
    for text in [summary, outcome.get("summary", ""), *outcome["example_phrases"]]:
        for word in RESERVED_WORDS:
            assert not re.search(rf"\b{word}\b", text, re.IGNORECASE), (word, text)


# ── The five demo questions, as people type them ─────────────────────────────

def test_dishoom_in_kings_cross():
    # "what's the hygiene rating for Dishoom King's Cross"
    result = ask("Dishoom", "King's Cross")
    assert without_matches(result.data) == {
        "business_name": "Dishoom", "address": "5 Stable Street", "postcode": "N1C 4AB",
        "rating": "5", "rating_date": "2026-01-29",
        "hygiene_score": 5, "structural_score": 5, "management_score": 5,
    }
    assert result.narrative == "Dishoom (N1C 4AB) scored 5 out of 5 for food hygiene, inspected 29 Jan 2026."
    # The other Dishoom within a kilometre, in Roman Way.
    (other,) = result.data["other_matches"]
    assert other.startswith("Dishoom, ") and "N7 8XH" in other and other.endswith("(rating 5)")


def test_bao_in_soho_is_the_exact_name_not_the_closer_bon_bao():
    # "Bao in Soho hygiene rating": BON BAO is nearer the middle of Soho.
    result = ask("Bao", "Soho")
    assert without_matches(result.data) == {
        "business_name": "Bao", "address": "53 LEXINGTON STREET, LONDON", "postcode": "W1F 9AS",
        "rating": "3", "rating_date": "2026-01-08",
        "hygiene_score": 10, "structural_score": 10, "management_score": 5,
    }
    assert result.narrative == "Bao (W1F 9AS) scored 3 out of 5 for food hygiene, inspected 8 Jan 2026."
    assert "need improvement" not in result.narrative
    # Next best: names that start with "Bao", nearest first.
    assert len(result.data["other_matches"]) == 3
    assert result.data["other_matches"][0].startswith("Bao Spot, ")
    assert "WC2H 7JP" in result.data["other_matches"][0]
    assert not any(m.startswith("BON BAO") for m in result.data["other_matches"])


def test_franco_manca_in_brixton():
    # "is Franco Manca in Brixton clean": there is a Brixton in Devon too.
    result = ask("Franco Manca", "Brixton")
    assert result.data == {
        "business_name": "Franco Manca", "address": "20 Atlantic Road, London", "postcode": "SW9 8JA",
        "rating": "5", "rating_date": "2025-12-19",
        "hygiene_score": 0, "structural_score": 0, "management_score": 0, "other_matches": [],
    }
    assert result.narrative == "Franco Manca (SW9 8JA) scored 5 out of 5 for food hygiene, inspected 19 Dec 2025."


def test_pret_near_sw7_2az_skips_the_prep_school():
    # "food hygiene rating for the Pret near SW7 2AZ"
    result = ask("Pret", "SW7 2AZ")
    assert result.data["business_name"] in ("Pret a Manger", "Pret A Manger")
    assert without_matches(result.data) == {
        "business_name": "Pret a Manger", "address": "99 Gloucester Road, LONDON", "postcode": "SW7 4SS",
        "rating": "5", "rating_date": "2025-05-29",
        "hygiene_score": 0, "structural_score": 0, "management_score": 0,
    }
    assert result.narrative == "Pret a Manger (SW7 4SS) scored 5 out of 5 for food hygiene, inspected 29 May 2025."
    others = result.data["other_matches"]
    assert len(others) == 3 and all(m.lower().startswith("pret a manger, ") for m in others)
    assert "Prep" not in result.narrative + " ".join(others)
    assert "Brighton College Prep Kensington" not in json.dumps(result.data)


def test_chicken_shop_on_exhibition_road_is_a_friendly_no_match():
    # "hygiene rating of the chicken shop on Exhibition Road"
    result = ask("chicken shop", "Exhibition Road")
    assert result.data == EMPTY_DATA       # name-only results are all over the UK: none listed
    assert result.narrative == ("I couldn't find a food hygiene listing for chicken shop "
                                "in or near Exhibition Road.")


# ── Other answers ────────────────────────────────────────────────────────────

def test_no_place_says_how_many_listings_share_the_name():
    expected = ("Dishoom (B3 3AX) scored 5 out of 5 for food hygiene, inspected 17 Jun 2025, one of 38 listings "
                "with that name, so add an area or postcode to narrow it down.")
    for place in ("", None, "   ", ABSENT):
        fetch = FakeFetch()
        result = ask("Dishoom", place, fetch=fetch)
        assert result.narrative == expected, (place, result.narrative)
        assert without_matches(result.data) == {
            "business_name": "Dishoom", "address": "1 Chamberlain Square, Birmingham", "postcode": "B3 3AX",
            "rating": "5", "rating_date": "2025-06-17",
            "hygiene_score": 0, "structural_score": 5, "management_score": 5,
        }
        assert len(fetch.calls) == 1
        assert not {"address", "latitude", "longitude"} & set(fsa_queries(fetch)[0])


def test_scotland_pass_has_no_scores():
    result = ask("Pret", "EH1 1BB")
    assert without_matches(result.data) == {
        "business_name": "Pret a Manger", "address": "Unit 2b Waverley Station, Edinburgh",
        "postcode": "EH1 1BB", "rating": "Pass", "rating_date": "2025-09-10",
    }
    assert result.narrative == ("Pret a Manger (EH1 1BB) got a Pass from Food Standards Scotland, "
                                "inspected 10 Sep 2025.")


def test_scotland_improvement_required():
    result = ask("10 to 10 in Delhi", "")
    assert result.data == {
        "business_name": "10 to 10 in Delhi", "address": "67 Nicolson Street, Edinburgh",
        "postcode": "EH8 9BZ", "rating": "Improvement Required", "rating_date": "2026-04-15",
        "other_matches": [],
    }
    assert result.narrative == ("10 to 10 in Delhi (EH8 9BZ) was rated Improvement Required in "
                                "Scotland, inspected 15 Apr 2026.")


def test_awaiting_inspection_has_no_date():
    result = ask("Parea hospitality", "")
    assert result.data["business_name"] == "Parea hospitality ltd"
    assert result.data["postcode"] == "SE22 9EP"
    assert result.data["rating"] == "Awaiting inspection"
    assert result.data["rating_date"] == ""          # FSA says 1901-01-01
    assert not set(SCORES) & set(result.data)
    assert result.narrative == "Parea hospitality ltd (SE22 9EP) is registered but has not been inspected yet."


def test_exempt_has_no_score():
    result = ask("Science Museum Shop", "")
    assert result.data["business_name"] == "Science Museum Shop"
    assert " ".join(result.data["address"].split()) == "Museum Science Museum, Exhibition Road, LONDON"
    assert (result.data["postcode"], result.data["rating"], result.data["rating_date"]) == \
        ("SW7 2DD", "Exempt", "")
    assert not set(SCORES) & set(result.data)
    assert result.narrative == ("Science Museum Shop (SW7 2DD) is exempt from food hygiene ratings, "
                                "so it has no score.")


def test_a_placeholder_or_missing_date_is_left_out():
    # FSA writes 1901-01-01 (sometimes 1900-01-01, or null) for "never inspected".
    for placeholder in ("1900-01-01T00:00:00", "1901-01-01T00:00:00", None):
        body = fixture("fsa_exempt.json")
        body["establishments"][0]["RatingDate"] = placeholder
        fetch = FakeFetch(fail=_fail_fsa(lambda url, body=body: json.dumps(body).encode()))
        result = ask("Science Museum Shop", "", fetch=fetch)
        assert (result.data["rating"], result.data["rating_date"]) == ("Exempt", ""), placeholder
        assert "1900" not in result.narrative and "1901" not in result.narrative


def test_a_score_of_15_or_more_says_what_needs_improvement():
    bon_bao = ask("Bon Bao", "Soho")                 # structural 15
    assert (bon_bao.data["postcode"], bon_bao.data["structural_score"]) == ("W1D 6BG", 15)
    assert bon_bao.narrative == ("BON BAO (W1D 6BG) scored 2 out of 5 for food hygiene, inspected 8 Oct 2026, "
                                 "and its premises need improvement.")
    little_bao = ask("Little Bao", "Soho")           # hygiene 15, management 20
    assert (little_bao.data["hygiene_score"], little_bao.data["management_score"]) == (15, 20)
    assert little_bao.narrative == ("Little Bao (W1D 5PE) scored 1 out of 5 for food hygiene, inspected 7 May 2026, "
                                    "and its food hygiene and food safety management need improvement.")


def test_an_outward_code_alone_is_found():
    fetch = FakeFetch()
    result = ask("Pret", "SW7", fetch=fetch)
    assert any(urlsplit(url).path.upper() == "/OUTCODES/SW7" for url, _ in fetch.calls), fetch.calls
    assert result.data["business_name"].lower() == "pret a manger"
    assert result.data["postcode"].startswith("SW7")


def test_the_and_generic_words_are_dropped_from_the_name():
    fetch = FakeFetch()
    result = ask("The Dishoom restaurant", "King's Cross", fetch=fetch)
    assert {q["name"].lower() for q in fsa_queries(fetch)} == {"dishoom"}
    assert result.narrative == "Dishoom (N1C 4AB) scored 5 out of 5 for food hygiene, inspected 29 Jan 2026."


def test_a_name_with_no_listing_is_a_friendly_no_match():
    result = ask("zzqxnotarealplace", "")
    assert result.data == EMPTY_DATA
    assert result.narrative == "I couldn't find a food hygiene listing for zzqxnotarealplace."


def test_an_unknown_postcode_is_not_an_outage():
    fetch = FakeFetch(extra_address={("dishoom", "zz1 1zz"): "fsa_no_results.json"})
    result = ask("Dishoom", "ZZ1 1ZZ", fetch=fetch)        # postcodes.io answers 404
    assert result.narrative == ("I couldn't find a food hygiene listing for Dishoom near ZZ1 1ZZ, "
                                "and that postcode doesn't seem to exist.")
    assert without_matches(result.data) == without_matches(EMPTY_DATA)


# ── When the services fail ───────────────────────────────────────────────────

def _fail_fsa(make):
    return lambda url, parts: make(url) if parts.hostname == FSA_HOST else None


def _fail_every_host(make):
    return lambda url, parts: make(url)


def assert_service_down(result) -> None:
    assert result.narrative == SERVICE_DOWN
    assert result.data == EMPTY_DATA


def test_fsa_down_is_a_result_not_a_crash():
    for code in (500, 502, 503):
        for place in ("King's Cross", "", "SW7 2AZ"):
            fetch = FakeFetch(fail=_fail_fsa(lambda url, code=code: http_error(url, code, b"Service Unavailable")))
            assert_service_down(ask("Dishoom" if place != "SW7 2AZ" else "Pret", place, fetch=fetch))
    # postcodes.io failing (not a 404) is an outage too.
    fetch = FakeFetch(fail=lambda url, parts: http_error(url, 503) if parts.hostname == POSTCODES_HOST else None)
    assert_service_down(ask("Pret", "SW7 2AZ", fetch=fetch))


def test_a_timeout_is_a_result_not_a_crash():
    for error in (TimeoutError, socket.timeout):
        for place in ("King's Cross", "", "SW7 2AZ"):
            fetch = FakeFetch(fail=_fail_every_host(lambda url, error=error: error("timed out")))
            assert_service_down(ask("Pret", place, fetch=fetch))


def test_an_unreachable_host_is_a_result_not_a_crash():
    for place in ("King's Cross", "", "SW7 2AZ"):
        fetch = FakeFetch(fail=_fail_every_host(
            lambda url: urllib.error.URLError("[Errno 8] nodename nor servname provided")))
        assert_service_down(ask("Pret", place, fetch=fetch))


def test_a_busy_service_says_try_again_shortly():
    for place in ("King's Cross", ""):
        fetch = FakeFetch(fail=_fail_fsa(lambda url: http_error(url, 429, b"", {"Retry-After": "30"})))
        result = ask("Dishoom", place, fetch=fetch)
        assert result.narrative == BUSY
        assert result.data == EMPTY_DATA


def test_a_garbled_reply_is_treated_as_down():
    for body in (b"<html><body>Service Unavailable</body></html>", b"", b'{"establishments": [',
                 b"\xff\xfe not json"):
        fetch = FakeFetch(fail=_fail_fsa(lambda url, body=body: body))
        assert_service_down(ask("Dishoom", "", fetch=fetch))


# ── Whoever asks, and however ────────────────────────────────────────────────

def test_no_business_name_asks_for_one():
    for business in ("   ", "\t\n", ""):
        for place in ("Soho", "", ABSENT):
            fetch = FakeFetch()
            try:
                result = ask(business, place, fetch=fetch)
            except InputShapeViolation:
                result = None   # the runtime may refuse a blank required input itself
            if result is not None:
                assert result.narrative == NO_BUSINESS, result.narrative
                assert result.data == EMPTY_DATA, result.data
            assert fetch.calls == []
            # And the app itself, whatever the runtime lets through.
            app = App()
            app._fetch = fetch
            call = SimpleNamespace(outcome_id=OUTCOME, inputs={"business": business}, secrets={},
                                   context=SimpleNamespace(now=FIXED_TIME, request_id="t", explicit=False))
            result = app.handle(call)
            assert result.narrative == NO_BUSINESS and result.data == EMPTY_DATA, result
            assert fetch.calls == []


def test_a_bug_is_a_plain_sentence_not_a_stack_trace():
    fetch = FakeFetch(fail=lambda url, parts: RuntimeError("an unforeseen bug"))
    result = ask("Dishoom", "King's Cross", fetch=fetch)
    assert result.narrative == UNEXPECTED, result.narrative
    assert result.data == EMPTY_DATA, result.data


def test_a_huge_question_still_gets_one_short_paragraph():
    business = "Zzq\nGrill " * 300           # about 3,000 characters, with line breaks
    place = "Nowhere\tLane " * 150
    result = ask(business, place, fetch=FakeFetch(fallback=True))
    assert result.narrative.startswith("I couldn't find a food hygiene listing for Zzq Grill Zzq"), \
        result.narrative
    assert "\u2026" in result.narrative and len(result.narrative) <= MAX_NARRATIVE


def test_a_very_long_listed_name_is_shortened_in_the_sentence():
    long_name = "Grand " + "Very " * 150 + "Long Kitchen"
    payload = fixture("fsa_dishoom_geo_kings_cross.json")
    listing = dict(payload["establishments"][0], BusinessName=long_name)
    body = json.dumps({**payload, "establishments": [listing], "meta": {**payload["meta"], "totalCount": 1}})
    fetch = FakeFetch(fail=lambda url, parts: body.encode("utf-8"))
    result = ask(long_name, "", fetch=fetch)
    assert result.data["business_name"] == long_name       # the data keeps the whole name
    assert result.narrative.startswith("Grand Very Very")
    assert "(N1C 4AB) scored 5 out of 5 for food hygiene, inspected 29 Jan 2026." in result.narrative
    assert len(result.narrative) <= MAX_NARRATIVE


def test_the_narrative_cap_itself():
    import app as app_module
    assert app_module.MAX_NARRATIVE == MAX_NARRATIVE
    capped = app_module._paragraph("word " * 400)
    assert len(capped) <= MAX_NARRATIVE and capped.endswith("\u2026") and "\n" not in capped
    assert app_module._paragraph("one\n\ntwo\r\nthree") == "one two three"
    assert app_module._paragraph("") == UNEXPECTED and app_module._paragraph(None) == UNEXPECTED


def test_picked_by_name_gives_the_same_answer():
    by_name = ask("Dishoom", "King's Cross", explicit=True)
    chosen = ask("Dishoom", "King's Cross")
    assert by_name.data == chosen.data
    assert by_name.narrative == chosen.narrative


SCENARIOS = [
    ("Dishoom", "King's Cross"), ("Bao", "Soho"), ("Franco Manca", "Brixton"), ("Pret", "SW7 2AZ"),
    ("chicken shop", "Exhibition Road"), ("Dishoom", ""), ("Pret", "EH1 1BB"), ("Pret", "SW7"),
    ("10 to 10 in Delhi", ""), ("Parea hospitality", ""), ("Science Museum Shop", ""),
    ("Bon Bao", "Soho"), ("Little Bao", "Soho"), ("zzqxnotarealplace", ""),
]


def test_every_answer_is_one_sentence_with_numbers_from_the_data():
    for business, place in SCENARIOS:
        result = ask(business, place)       # ask() checks the sentence, numbers and shape
        assert_one_sentence(result.narrative)
        if result.data["postcode"]:
            assert f"({result.data['postcode']})" in result.narrative
        else:
            assert "()" not in result.narrative


def test_never_more_than_three_requests_each_with_a_short_timeout():
    for business, place in SCENARIOS:
        fetch = FakeFetch()
        ask(business, place, fetch=fetch)
        assert 1 <= len(fetch.calls) <= MAX_FETCHES, (business, place, fetch.calls)
        assert all(0 < timeout <= MAX_TIMEOUT for _, timeout in fetch.calls), fetch.calls


def test_user_input_is_url_encoded_and_tfl_is_never_called():
    fetch = FakeFetch()
    ask("Dishoom", "King's Cross", fetch=fetch)
    for url, _ in fetch.calls:
        assert " " not in url and "'" not in url and "api.tfl.gov.uk" not in url, url
    assert fsa_queries(fetch)[0]["address"] == "King's Cross"
    places = [url for url, _ in fetch.calls if urlsplit(url).path == "/places"]
    assert [parse_qs(urlsplit(url).query)["q"] for url in places] == [["King's Cross"]]

    # Characters that would break a URL built by concatenation.
    fetch = FakeFetch(fallback=True)
    business, place = "Tom's Bar & Grill #1", "Exhibition Road?x=1&y=2"
    result = ask(business, place, fetch=fetch)
    assert result.data["business_name"] == ""
    assert result.narrative.startswith(f"I couldn't find a food hygiene listing for {business} ")
    for query in fsa_queries(fetch):
        assert query["name"] == business and not {"x", "y"} & set(query), query
        assert query.get("address", place) == place
    for url, _ in fetch.calls:
        assert "#" not in url and "api.tfl.gov.uk" not in url, url


# ── Against the real APIs (deselected in the build; `pytest -m network`) ────

def _skip(reason: str):
    if pytest:
        pytest.skip(reason)
    raise unittest.SkipTest(reason)


def live_ask(business: str, place: str):
    try:
        socket.create_connection((FSA_HOST, 443), timeout=3).close()
    except OSError as exc:
        _skip(f"{FSA_HOST} is not reachable: {exc}")
    result = LocalRunner(App()).call(OUTCOME, {"business": business, "place": place}, granted_secrets={},
                                     now=datetime.now(timezone.utc), request_id="live-1").result
    if result.narrative in (SERVICE_DOWN, BUSY):
        _skip(f"the FSA did not answer: {result.narrative}")
    assert_one_sentence(result.narrative)
    return result


@network
def test_live_dishoom_in_kings_cross():
    result = live_ask("Dishoom", "King's Cross")
    assert result.data["postcode"] == "N1C 4AB"
    assert result.data["rating"]


@network
def test_live_a_name_with_no_place():
    result = live_ask("Dishoom", "")
    assert result.data["business_name"]

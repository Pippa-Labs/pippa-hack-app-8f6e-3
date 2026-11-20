"""Clean Plate: what food hygiene rating did this place get, and when?

One job: find a restaurant, cafe or takeaway in the Food Standards Agency's
public register and say what it scored and when it was last inspected.

Pippa's model fills two string inputs from the person's message, as they
wrote them (`business`, `place`); this file does the messy part: "chicken
shop", "King's Cross" (an area name almost no FSA address contains), "SW7
2AZ", a chain with forty branches, and a name half the country's cafes share.

It calls two free, keyless APIs, and never more than three requests a call:

  - postcodes.io, to turn a postcode or an area name into a point;
  - the FSA ratings API, for listings with that name near the point.

It keeps nothing between calls and writes no files.
"""

from __future__ import annotations

import datetime
import http.client
import json
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    Result,
    load_manifest_file,
    validate_manifest,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")
#: London area names postcodes.io's place list lacks ("Covent Garden"), with a
#: point for each, from OpenStreetMap. Read once, beside this file.
AREAS_PATH = Path(__file__).with_name("london_areas.json")

OUTCOME = "hygiene.rating"

#: Every request carries this. The FSA API also wants `x-api-version: 2`, or
#: it answers in an older shape; postcodes.io ignores the header.
USER_AGENT = (
    "CleanPlate-PippaAgentApp/0.2 (food hygiene rating lookup; "
    "https://github.com/Pippa-Labs)"
)

#: Fixed hosts. User input only ever reaches them as a query value through
#: `urlencode`, or as one path segment through `quote(..., safe="")`.
FSA_URL = "https://api.ratings.food.gov.uk/Establishments?"
POSTCODE_URL = "https://api.postcodes.io/postcodes/"
OUTCODE_URL = "https://api.postcodes.io/outcodes/"
PLACES_URL = "https://api.postcodes.io/places?"

#: The budget. Pippa waits 20 seconds; this app stops asking at 14, never
#: waits more than 5 for one answer, and never sends a fourth request.
MAX_REQUESTS = 3
BUDGET_SECONDS = 14.0
REQUEST_TIMEOUT = 5.0
MIN_TIMEOUT = 1.0

NEAR_PAGE = 20          # listings asked for near a point, or at an address
WIDE_PAGE = 50          # listings asked for with the name alone (a fallback)
NEAR_MILES = 1          # the FSA's maxDistanceLimit is in miles

#: Every narrative is one paragraph of 1 to 500 characters. Words echoed from
#: the question or from a listing are shortened first, so the cap is a last
#: resort that a real answer never reaches.
MAX_NARRATIVE = 500
ECHO_INPUT = 80         # the business or place as the person typed it
ECHO_NAME = 120         # a listed business name

NO_BUSINESS = (
    "Tell me the name of the restaurant, cafe or takeaway you want the "
    "hygiene rating for."
)
UNEXPECTED = "Something went wrong looking up that hygiene rating, so please try again."
SERVICE_DOWN = (
    "The Food Standards Agency's ratings service isn't answering right now, so "
    "I can't check that rating; please try again in a minute."
)
SERVICE_BUSY = "The food hygiene ratings service is busy right now, so please try again shortly."

#: Words dropped from the end of a description before it is sent as a name:
#: "chicken shop" is looked up as "chicken". Never the only word left.
GENERIC_WORDS = frozenset({"shop", "restaurant", "cafe", "café", "takeaway", "place", "bar"})

#: The FSA's spellings, folded (lower case, letters only), to the one returned.
#: "0".."5" pass through untouched; anything unknown is returned as given.
RATING_NAMES = {
    "awaitinginspection": "Awaiting inspection",
    "awaitingpublication": "Awaiting publication",
    "pass": "Pass",
    "improvementrequired": "Improvement Required",
    "passandeatsafe": "Pass and Eat Safe",
    "exempt": "Exempt",
}
NUMERIC_RATINGS = frozenset("012345")

#: Output field, FSA `scores` key, and what a score of 15 or more is about.
SCORES = (
    ("hygiene_score", "Hygiene", "food hygiene"),
    ("structural_score", "Structural", "premises"),
    ("management_score", "ConfidenceInManagement", "food safety management"),
)
NEEDS_IMPROVEMENT = 15

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: A full UK postcode anywhere in the place, and an outward code on its own.
POSTCODE = re.compile(r"(?<![A-Z0-9])([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})(?![A-Z0-9])",
                      re.IGNORECASE)
OUTCODE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$", re.IGNORECASE)


class LookupFailed(AgentAppError):
    user_message = UNEXPECTED
    retryable = True


# Internal signals, turned into a Result in `handle`. Never shown to anybody.

class _ServiceDown(Exception):
    """A 5xx, a timeout, no connection, an unreadable body, or no time left."""


class _ServiceBusy(Exception):
    """A 429."""


class _NotFound(Exception):
    """postcodes.io answered 404: the postcode or place is not one it knows."""


class App:
    """Look up a food business's hygiene rating and last inspection date."""

    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    def handle(self, call: Call) -> Result:
        if call.outcome_id != OUTCOME:
            raise LookupFailed(f"this app cannot do {call.outcome_id!r}")
        # Every way out of here is a Result with a one-paragraph narrative of
        # 1 to 500 characters, so Pippa always has a true sentence to give,
        # and never answers in the app's place with a rating it made up.
        inputs = call.inputs or {}
        business = _clean(inputs.get("business"))
        place = _clean(inputs.get("place"))
        if not business:
            return _result(_empty_data(), NO_BUSINESS)
        try:
            return _Lookup(self._fetch).run(business, place)
        except _ServiceBusy:
            return _result(_empty_data(), SERVICE_BUSY)
        except _ServiceDown:
            return _result(_empty_data(), SERVICE_DOWN)
        except Exception as exc:  # a bug: logged, never a stack trace to the person
            request_id = getattr(getattr(call, "context", None), "request_id", "")
            print(f"clean-plate: unexpected {type(exc).__name__} in request {request_id}",
                  file=sys.stderr)
            return _result(_empty_data(), UNEXPECTED)

    def _fetch(self, url: str, timeout: float = 5.0) -> bytes:
        """Every outbound request goes through here; tests replace it."""
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "x-api-version": "2",
        })
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()


# ── One call's lookup: at most three requests ────────────────────────────────

class _Lookup:
    """The requests for one call, counted and timed against the budget.

    A fresh one per call, so an App reused across calls (as tests do) never
    carries a count from one call into the next.
    """

    def __init__(self, fetch) -> None:
        self._fetch = fetch
        self._started = time.monotonic()
        self.requests = 0

    def run(self, business: str, place: str) -> Result:
        query = query_name(business)
        if not place:
            return self._name_only(business, query)
        postcode = POSTCODE.search(place)
        if postcode:
            outward = postcode.group(1).upper()
            whole = outward + postcode.group(2).upper()
            return self._postcode(business, query, place,
                                  POSTCODE_URL + quote(whole, safe=""), outward)
        if OUTCODE.match(place):
            outward = place.upper()
            return self._postcode(business, query, place,
                                  OUTCODE_URL + quote(outward, safe=""), outward)
        return self._area(business, query, place)

    # No place: [1] the name alone.
    def _name_only(self, business: str, query: str) -> Result:
        payload, listings = self._fsa({"name": query, "pageSize": NEAR_PAGE})
        ranked = rank(listings, query, business)
        if not ranked:
            return _no_match(business, "", "")
        return _answer(ranked[0], ranked[1:], listings=_total(payload, listings))

    # A postcode or outward code: [1] postcodes.io, [2] near the point,
    # [3] the name alone, kept to listings in that outward code.
    def _postcode(self, business: str, query: str, place: str, url: str,
                  outward: str) -> Result:
        try:
            payload = self._get_json(url, missing_ok=True)
            if not isinstance(payload, dict):
                raise _ServiceDown("postcodes.io answered without an object")
            point = _point(payload.get("result"))
        except _NotFound:
            point = None
        if point is None:
            return self._address_then_words(business, query, place, unknown_postcode=True)
        ranked = rank(self._fsa_near(query, point), query, business)
        if ranked:
            return _answer(ranked[0], ranked[1:])
        _, listings = self._fsa({"name": query, "pageSize": WIDE_PAGE})
        ranked = rank(listings, query, business)
        inside = [item for item in ranked if _in_outward(item, outward)]
        if inside:
            return _answer(inside[0], inside[1:])
        return _no_match(business, "near", place)

    # Any other place: [1] the FSA's own address match, [2] postcodes.io's
    # place names, [3] near that point, or the name alone with every place
    # word in the address.
    def _area(self, business: str, query: str, place: str) -> Result:
        ranked = rank(self._fsa_at(query, place), query, business)
        if ranked:
            return _answer(ranked[0], ranked[1:])
        point = self._place_point(place)
        if point is None:
            return self._words(business, query, place)
        ranked = rank(self._fsa_near(query, point), query, business)
        if ranked:
            return _answer(ranked[0], ranked[1:])
        return _no_match(business, "near", place)

    # A postcode postcodes.io does not know: [2] the FSA's address match,
    # [3] the name alone with every place word in the address.
    def _address_then_words(self, business: str, query: str, place: str,
                            unknown_postcode: bool = False) -> Result:
        ranked = rank(self._fsa_at(query, place), query, business)
        if ranked:
            return _answer(ranked[0], ranked[1:])
        return self._words(business, query, place, unknown_postcode)

    def _words(self, business: str, query: str, place: str,
               unknown_postcode: bool = False) -> Result:
        _, listings = self._fsa({"name": query, "pageSize": WIDE_PAGE})
        ranked = rank(listings, query, business)
        wanted = _norm(place).split()
        inside = [item for item in ranked
                  if wanted and all(word in _address_words(item) for word in wanted)]
        if inside:
            return _answer(inside[0], inside[1:])
        if unknown_postcode:
            return _no_match(business, "near", place, unknown_postcode=True)
        return _no_match(business, "in or near", place)

    # ── The requests ─────────────────────────────────────────────────────────

    def _fsa_at(self, query: str, place: str) -> list:
        _, listings = self._fsa({"name": query, "address": place, "pageSize": NEAR_PAGE})
        return listings

    def _fsa_near(self, query: str, point: tuple) -> list:
        latitude, longitude = point
        _, listings = self._fsa({
            "name": query,
            "latitude": latitude,
            "longitude": longitude,
            "maxDistanceLimit": NEAR_MILES,
            "sortOptionKey": "distance",
            "pageSize": NEAR_PAGE,
        })
        return listings

    def _fsa(self, params: dict) -> tuple:
        """(the whole answer, its listings). Anything else is an outage."""
        payload = self._get_json(FSA_URL + urlencode(params))
        if not isinstance(payload, dict) or not isinstance(payload.get("establishments"), list):
            raise _ServiceDown("the FSA answered without a list of establishments")
        return payload, payload["establishments"]

    def _place_point(self, place: str):
        """The point postcodes.io gives a place name, or None.

        The candidate whose name is the place (else the first); among several
        with that name, the one in London, because "Soho" and "Brixton" are
        also villages elsewhere and this was built for London first.
        """
        name = _without_london(place)
        try:
            payload = self._get_json(PLACES_URL + urlencode({"q": name, "limit": 10}),
                                     missing_ok=True)
        except _NotFound:
            return None
        if not isinstance(payload, dict):
            raise _ServiceDown("postcodes.io answered without an object")
        found = payload.get("result") or []
        if not isinstance(found, list):
            return None
        candidates = [c for c in found if isinstance(c, dict) and _point(c) is not None]
        wanted = _norm(name)
        same = [c for c in candidates if _norm(c.get("name_1")) == wanted]
        if same:
            in_london = [c for c in same if _in_london(c)]
            return _point((in_london or same)[0])
        # Not a name postcodes.io knows exactly: a London area from the bundled
        # list ("Covent Garden" is not in its list at all), else its best guess.
        known = LONDON_AREAS.get(wanted)
        if known is not None:
            return known
        return _point(candidates[0]) if candidates else None

    def _get_json(self, url: str, missing_ok: bool = False):
        """One request, parsed. Raises one of the three signals above."""
        if self.requests >= MAX_REQUESTS:
            raise _ServiceDown(f"a request beyond the {MAX_REQUESTS} allowed: {_host(url)}")
        left = BUDGET_SECONDS - (time.monotonic() - self._started)
        if left < MIN_TIMEOUT:
            raise _ServiceDown(f"only {left:.1f}s of the budget left for {_host(url)}")
        self.requests += 1
        try:
            return json.loads(self._fetch(url, timeout=min(REQUEST_TIMEOUT, left)))
        except urllib.error.HTTPError as exc:       # MUST come first: it IS a URLError
            if exc.code == 404 and missing_ok:
                raise _NotFound(url) from exc
            if exc.code == 429:
                raise _ServiceBusy(f"{_host(url)} answered 429") from exc
            raise _ServiceDown(f"{_host(url)} answered {exc.code}") from exc
        except TimeoutError as exc:                 # NOT a URLError: its own clause
            raise _ServiceDown(f"{_host(url)} did not answer in time") from exc
        except urllib.error.URLError as exc:        # unreachable, DNS, TLS
            raise _ServiceDown(f"could not reach {_host(url)}: {exc.reason}") from exc
        except ValueError as exc:                   # empty or not JSON
            raise _ServiceDown(f"{_host(url)} sent a body that is not JSON") from exc
        except (http.client.HTTPException, OSError) as exc:  # cut off mid-answer
            raise _ServiceDown(f"{_host(url)} broke off: {type(exc).__name__}") from exc


# ── Names: what is asked for, and how well a listing matches it ──────────────

def query_name(business: str) -> str:
    """The name sent to the FSA: no leading "the", no trailing "shop".

    "The Ivy" -> "Ivy", "chicken shop" -> "chicken", "Joe's Cafe" -> "Joe's".
    A one-word name is never emptied.
    """
    words = business.split()
    if len(words) > 1 and words[0].lower() == "the":
        words = words[1:]
    while len(words) > 1 and _fold(words[-1]) in GENERIC_WORDS:
        words.pop()
    return " ".join(words)


def name_score(listing: str, query: str) -> int:
    """How well a listed name matches the query, 0 (not at all) to 100."""
    have, want = _name_key(listing), _name_key(query)
    if not have or not want:
        return 0
    if have == want:
        return 100
    if have.startswith(want + " ") or want.startswith(have + " "):
        return 80
    have_words = set(have.split())
    want_words = want.split()
    if all(word in have_words for word in want_words):
        return 60
    if want in have:
        return 30
    if any(len(word) >= 3 and word in have_words for word in want_words):
        return 15
    return 0


def rank(listings: list, query: str, full: str = "") -> list:
    """The listings that match at all: best name first, then nearest, then
    the order the FSA gave.

    `full` is the name as the person gave it, before "shop" or "the" were
    dropped: a listing that is exactly that ("Science Museum Shop") beats
    one that only matches the shortened query ("Science Museum Cafe").
    """
    scored = []
    for index, item in enumerate(listings):
        if not isinstance(item, dict):
            continue
        listed = _text(item.get("BusinessName"))
        score = name_score(listed, query)
        if full and score and _name_key(listed) == _name_key(full):
            score = 101
        if score > 0:
            scored.append(((-score, _distance(item), index), item))
    scored.sort(key=lambda pair: pair[0])
    return [item for _, item in scored]


def _norm(text) -> str:
    """Lower case, & as "and", no accents or apostrophes, words only."""
    t = str(text or "").lower().replace("&", " and ")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"['‘’`]", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _name_key(text) -> str:
    """`_norm`, without a leading "the", on both sides of a comparison, so
    "The Ivy" asked for as "Ivy" is still an exact match."""
    key = _norm(text)
    return key[4:] if key.startswith("the ") else key


def _fold(word: str) -> str:
    return _norm(word).replace(" ", "")


def _distance(item: dict) -> float:
    try:
        value = float(item.get("Distance"))
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _address_words(item: dict) -> set:
    return set(_norm(f"{_address(item)} {_text(item.get('PostCode'))}").split())


def _in_outward(item: dict, outward: str) -> bool:
    """Whether a listing's postcode (or address) is in this outward code."""
    compact = re.sub(r"\s+", "", _text(item.get("PostCode"))).upper()
    if len(compact) > 3 and compact[:-3] == outward:
        return True
    return outward.lower() in _address_words(item)


# ── Places ───────────────────────────────────────────────────────────────────

def _point(record):
    """(latitude, longitude) from a postcodes.io result, or None."""
    if not isinstance(record, dict):
        return None
    latitude, longitude = record.get("latitude"), record.get("longitude")
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return None
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def _load_areas(path: Path = AREAS_PATH) -> dict:
    """{normalised area name: (latitude, longitude)}; empty if the file is
    missing or unreadable, so the app still answers without it."""
    try:
        areas = json.loads(path.read_text("utf-8"))["areas"]
        return {_norm(name): (float(lat), float(lon)) for name, (lat, lon) in areas.items()}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


LONDON_AREAS = _load_areas()


def _in_london(candidate: dict) -> bool:
    return (_norm(candidate.get("region")) == "london"
            or _norm(candidate.get("county_unitary")) in ("greater london", "london"))


def _without_london(place: str) -> str:
    """ "Soho, London" -> "Soho" for postcodes.io, which knows no such name.
    "London" on its own is left alone."""
    trimmed = re.sub(r"[\s,]+(?:greater\s+)?london\s*$", "", place, flags=re.IGNORECASE)
    return trimmed.strip(" ,") or place


# ── The answer ───────────────────────────────────────────────────────────────

def _answer(best: dict, others: list, listings: int = 0) -> Result:
    """The best listing as `data`, the next three as other_matches.

    `listings` is how many the FSA has with the name when no place was given;
    more than one adds the "narrow it down" clause.
    """
    data = _listing_data(best)
    data["other_matches"] = [_one_line(item) for item in others[:3]]
    return _result(data, _sentence(data, listings))


def _no_match(business: str, preposition: str, place: str,
              unknown_postcode: bool = False) -> Result:
    """Nothing matched. other_matches stays empty: the only listings left at
    this point are the name-only ones, scattered across the UK, and offering
    a Brighton chicken shop for Exhibition Road would mislead."""
    where = f" {preposition} {_echo(place, ECHO_INPUT)}" if place else ""
    tail = ", and that postcode doesn't seem to exist" if unknown_postcode else ""
    return _result(_empty_data(), f"I couldn't find a food hygiene listing for "
                                  f"{_echo(business, ECHO_INPUT)}{where}{tail}.")


def _result(data: dict, narrative: str) -> Result:
    """The only place a Result is made: its narrative always goes through
    `_paragraph`."""
    return Result(data=data, narrative=_paragraph(narrative))


def _paragraph(text) -> str:
    """One paragraph (no line breaks, single spaces), 1 to 500 characters.

    Never empty. Over the cap it is cut at a word and ends with an ellipsis;
    the shortened echoes in `_sentence` and `_no_match` keep real answers far
    below it.
    """
    flat = " ".join(str(text or "").split()) or UNEXPECTED
    if len(flat) <= MAX_NARRATIVE:
        return flat
    cut = flat[:MAX_NARRATIVE - 1]
    if " " in cut[-60:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.") + "\u2026"


def _echo(text, limit: int) -> str:
    """Words to repeat in a narrative: one line, at most `limit` characters."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit - 1].rstrip(" ,;:.") + "\u2026"


def _empty_data() -> dict:
    return {"business_name": "", "address": "", "postcode": "", "rating": "",
            "rating_date": "", "other_matches": []}


def _listing_data(item: dict) -> dict:
    data = {
        "business_name": _text(item.get("BusinessName")),
        "address": _address(item),
        "postcode": _text(item.get("PostCode")),
        "rating": _rating(item.get("RatingValue")),
        "rating_date": _date(item.get("RatingDate")),
    }
    scores = item.get("scores")
    if not isinstance(scores, dict):
        scores = {}
    for field, key, _ in SCORES:
        value = _score(scores.get(key))
        if value is not None:
            data[field] = value
    return data


def _sentence(data: dict, listings: int) -> str:
    """One sentence, every figure in it from `data`."""
    who = _echo(data["business_name"], ECHO_NAME) or "That business"
    if data["postcode"]:
        who += f" ({_echo(data['postcode'], 12)})"
    rating = data["rating"]
    when = _say_date(data["rating_date"])
    inspected = f", inspected {when}" if when else ""

    if rating in NUMERIC_RATINGS:
        text = f"{who} scored {rating} out of 5 for food hygiene{inspected}"
        weak = [label for field, _, label in SCORES
                if data.get(field) is not None and data[field] >= NEEDS_IMPROVEMENT]
        if weak:
            plural = len(weak) > 1 or weak[0] == "premises"
            listed = weak[0] if len(weak) == 1 else ", ".join(weak[:-1]) + " and " + weak[-1]
            text += f", and its {listed} {'need' if plural else 'needs'} improvement"
    elif rating == "Pass":
        text = f"{who} got a Pass from Food Standards Scotland{inspected}"
    elif rating == "Pass and Eat Safe":
        text = f"{who} got a Pass and Eat Safe award from Food Standards Scotland{inspected}"
    elif rating == "Improvement Required":
        text = f"{who} was rated Improvement Required in Scotland{inspected}"
    elif rating == "Exempt":
        text = f"{who} is exempt from food hygiene ratings, so it has no score"
    elif rating == "Awaiting inspection":
        text = f"{who} is registered but has not been inspected yet"
    elif rating == "Awaiting publication":
        text = f"{who} has been inspected but its rating has not been published yet"
    elif rating:
        text = f"{who} is listed with the food hygiene rating \"{_echo(rating, 40)}\"{inspected}"
    else:
        text = f"{who} is in the food hygiene register, but no rating is shown{inspected}"

    if listings > 1:
        text += (f", one of {listings} listings with that name, so add an area "
                 f"or postcode to narrow it down")
    return text + "."


def _one_line(item: dict) -> str:
    """ "Name, address, POSTCODE (rating R)" for other_matches."""
    name = _text(item.get("BusinessName"))
    lines = [_clean(item.get(f"AddressLine{n}")) for n in range(1, 5)]
    # Some listings repeat the business name as the first address line.
    lines = [line for line in lines if line and _norm(line) != _norm(name)]
    parts = [name, *lines, _text(item.get("PostCode"))]
    line = ", ".join(part for part in parts if part)
    rating = _rating(item.get("RatingValue"))
    return f"{line} (rating {rating})" if rating else line


def _total(payload: dict, listings: list) -> int:
    """How many listings the FSA has for the name: meta.totalCount, else
    however many came back."""
    meta = payload.get("meta")
    if isinstance(meta, dict) and not isinstance(meta.get("totalCount"), bool):
        try:
            return int(meta.get("totalCount"))
        except (TypeError, ValueError):
            pass
    return len(listings)


# ── Small helpers ────────────────────────────────────────────────────────────

def _clean(value) -> str:
    """An input as text: None (absent) and blanks become ""."""
    if value is None or isinstance(value, bool):
        return ""
    return " ".join(str(value).split())


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _address(item: dict) -> str:
    """The non-empty address lines, each with its runs of spaces collapsed."""
    lines = (_clean(item.get(f"AddressLine{n}")) for n in range(1, 5))
    return ", ".join(line for line in lines if line)


def _rating(value) -> str:
    text = _text(value)
    if text in NUMERIC_RATINGS and text:
        return text
    return RATING_NAMES.get(re.sub(r"[^a-z]", "", text.lower()), text)


def _date(value) -> str:
    """ "YYYY-MM-DD", or "" for none. The FSA writes 1901-01-01 for never."""
    if not isinstance(value, str):
        return ""
    try:
        day = datetime.date.fromisoformat(value.strip()[:10])
    except ValueError:
        return ""
    return "" if day.year <= 1901 else day.isoformat()


def _say_date(iso: str) -> str:
    """ "29 Jan 2026", built by hand: `%-d` is not portable."""
    if not iso:
        return ""
    day = datetime.date.fromisoformat(iso)
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def _score(value):
    """A score as an int, or None when there is none."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    return int(round(number)) if math.isfinite(number) else None


def _host(url: str) -> str:
    return urlsplit(url).netloc

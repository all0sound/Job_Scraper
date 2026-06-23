"""
Environmental / Toxicology Job Scraper — tailored for Dr. Scott Coffin
(environmental toxicology, risk assessment, exposure science, water quality,
microplastics/PFAS, and supporting data science), California-wide.

Pipelines (see __main__) include LinkedIn's guest endpoint, JobSpy-backed
Indeed/Glassdoor, public-sector boards, and a priority-employer sweep
(allowlist-filtered LinkedIn + optional direct Greenhouse/Workday probes). Each
writes {basename}.{json,md,html} digests and accumulates into all_jobs.json for
the dashboard and triage agent.

Tune the search in config.json: title keywords, board-specific search terms,
priority employers, locations, and LinkedIn geoIds / JobSpy locations.
"""

from __future__ import annotations

import http.cookiejar
import base64
import html as html_lib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# Config — ALL of a user's search settings live in config.json (edit it by hand
# or generate it from a CV; see docs/cv-to-config-prompt.md). Everything below
# falls back to a sensible value if config.json or a key is missing, so the
# scraper always runs.
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    for name in ("config.json", "config.example.json"):
        path = os.path.join(SCRIPT_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if name != "config.json":
                print(f"  ℹ️  config.json not found; using {name} (copy it to config.json and customize)")
            return data
        except FileNotFoundError:
            continue
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ⚠️  {name} not loaded ({e}); trying next")
    print("  ⚠️  No config file found; using built-in defaults")
    return {}


CONFIG = _load_config()


def _cfg(path: str, default):
    """Nested config lookup by dotted path; returns default if absent/empty."""
    cur = CONFIG
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur if cur not in (None, "", [], {}) else default


# Short field label + geo subtitle for the digest titles, from config.profile.
# "Environmental / Toxicology Job Tracker" → "Environmental / Toxicology".
PROFILE_LABEL = re.sub(
    r'\s*(job\s*tracker|tracker|jobs?)\s*$', '',
    str(_cfg("profile.title", "Job")), flags=re.I).strip() or "Job"
PROFILE_SUBTITLE = str(_cfg("profile.subtitle", "All locations"))

# Title keywords for Dr. Scott Coffin — environmental/regulatory toxicology,
# risk assessment, exposure science, water quality, and the data-science work
# that supports it. A title matches if it contains any of these (case-
# insensitive). Multi-word phrases match as substrings ("risk assess" hits
# "Risk Assessor" and "Risk Assessment Scientist"); single tokens are word-
# bounded, so list FULL words ("toxicologist", not the stem "toxicolog").
_KEYWORDS_DEFAULT = [
    # ---- Toxicology (core) ----  (fallback; real list is config.json → keywords.include)
    "toxicologist", "toxicology", "toxicological",
    "ecotoxicologist", "ecotoxicology", "ecotoxicolog",
    "environmental toxicolog", "regulatory toxicolog",
    "computational toxicolog", "predictive toxicolog",
    "aquatic toxicolog", "wildlife toxicolog", "research toxicolog",
    # ---- Risk / exposure / hazard assessment ----
    "risk assess", "risk assessor", "human health risk",
    "ecological risk", "exposure scien", "exposure assess",
    "exposure modeling", "exposure modelling", "hazard assess",
    "hazard identification", "dose-response", "dose response",
    "pharmacokinetic", "toxicokinetic", "pbpk",
    # ---- Environmental science / health / chemistry ----
    "environmental scien", "environmental health",
    "environmental chemist", "environmental chemistry",
    "environmental specialist", "environmental analyst",
    "environmental protection", "environmental epidemiolog",
    "environmental data", "environmental monitoring",
    "environmental assessment", "exposure epidemiolog",
    # ---- Water / contaminants ----
    "water quality", "drinking water", "watershed", "water resources",
    "aquatic scien", "limnolog",
    "microplastic", "microplastics", "nanoplastic", "nanomaterial",
    "pfas", "per- and polyfluoro", "emerging contaminant",
    "contaminant", "pollutant", "air pollution", "water pollution",
    "air quality", "remediation", "environmental remediation",
    # ---- Chemical safety / product stewardship / regulatory ----
    "chemical safety", "chemical risk", "chemical assess",
    "chemical regulatory", "product steward", "regulatory toxicolog",
    "hazardous materials", "hazardous waste", "environmental compliance",
    # ---- Ecotoxicology-adjacent ecology / sustainability ----
    "ecotoxicolog", "conservation toxicolog",
]
KEYWORDS = _cfg("keywords.include", _KEYWORDS_DEFAULT)
# NOTE: deliberately tight. Generic titles ("Research Scientist", "Senior
# Scientist", "Data Scientist", "Professor", "Regulatory Affairs") were removed
# because they pull in pharma/biotech/tech bench roles. Environmental academic,
# data, and policy roles are still caught via their qualified forms
# ("Environmental Data Scientist" → "environmental data", "Assistant Professor
# of Environmental Health" → "environmental health", etc.).

# Seconds to wait between API probes — keeps us polite
REQUEST_DELAY = 0.3
# LinkedIn needs a longer inter-request gap; jitter is added at call sites
LINKEDIN_REQUEST_DELAY = 3.0

# Biotech digest should only contain reliably fresh roles.
FRESH_JOB_LOOKBACK = timedelta(hours=24)

# Titles containing any excluded term are dropped (config.json → keywords.exclude).
# Single tokens are word-bounded; multi-word phrases match as substrings.
def _build_title_re(terms: list) -> re.Pattern:
    return re.compile(
        "|".join(re.escape(t) if (" " in t or "&" in t) else rf"\b{re.escape(t)}\b" for t in terms),
        re.IGNORECASE,
    )


_EXCLUDE_DEFAULT = [
    "intern", "interns", "internship", "co-op", "coop", "trainee", "apprentice", "technician",
    "research assistant", "lab assistant", "teaching assistant", "undergraduate", "postdoc",
    "postdoctoral", "work-study", "volunteer", "fellowship", "engineer", "engineering",
    "ehs", "occupational safety", "occupational health", "health and safety", "health & safety",
]
EXCLUDED_SENIORITY_RE = _build_title_re(_cfg("keywords.exclude", _EXCLUDE_DEFAULT))

# Multi-word phrases keep substring semantics; single-word keywords ("mle",
# "devops") are word-bounded so they can't match inside a word ("Hamlet").
_KEYWORD_RE = re.compile(
    "|".join(
        re.escape(k) if " " in k else rf"\b{re.escape(k)}\b"
        for k in KEYWORDS
    ),
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch(url, *, retries=1, _base_wait=35.0):
    """Fetch URL, retrying once on 429 with a randomised backoff."""
    req = Request(url, headers=HEADERS)
    for attempt in range(retries + 1):
        try:
            with urlopen(req, timeout=15) as r:
                return r.read().decode("utf-8", errors="ignore")
        except HTTPError as e:
            if e.code == 429 and attempt < retries:
                wait = _base_wait + random.uniform(0, 15)
                print(f"  ⏳ Rate-limited (429); waiting {wait:.0f}s then retrying…")
                time.sleep(wait)
                continue
            print(f"  WARNING: Could not fetch {url}: {e}")
            return ""
        except (URLError, TimeoutError, OSError) as e:
            print(f"  WARNING: Could not fetch {url}: {e}")
            return ""
    return ""


def is_mle_role(title: str) -> bool:
    """True if a job title is on-target for Dr. Coffin (env/tox/risk/etc.) and
    not a junior/student posting. (Name kept for compatibility with the
    original pipeline; it now gates environmental-toxicology titles.)"""
    if EXCLUDED_SENIORITY_RE.search(title):
        return False
    return bool(_KEYWORD_RE.search(title))


def is_mle_role_text(title: str, *parts: str) -> bool:
    """Like is_mle_role, but allows source-specific summary text to carry the signal."""
    if EXCLUDED_SENIORITY_RE.search(title or ""):
        return False
    text = " ".join([title or "", *(p or "" for p in parts)])
    return bool(_KEYWORD_RE.search(text))


def _matches_current_config(job: dict) -> bool:
    """Keep only jobs that still match the active config.json keywords."""
    return is_mle_role_text(
        str(job.get("title", "") or ""),
        str(job.get("description", "") or ""),
        str(job.get("summary", "") or ""),
    )


def _filter_current_config_jobs(jobs: list[dict], *, label: str = "jobs") -> list[dict]:
    filtered = [j for j in jobs if _matches_current_config(j)]
    dropped = len(jobs) - len(filtered)
    if dropped:
        print(f"  🧹 Dropped {dropped} stale {label} no longer matching config.json")
    return filtered


# Optional config-driven guardrail: filter out part-time roles that are too far
# from a configured home base. Remote roles are kept; unknown cities can still be
# dropped when their state falls outside filters.part_time.near_state_codes.
PART_TIME_DISTANCE_FILTER = _cfg("filters.part_time", {})
if not isinstance(PART_TIME_DISTANCE_FILTER, dict):
    PART_TIME_DISTANCE_FILTER = {}
PART_TIME_HOME_ZIP = str(PART_TIME_DISTANCE_FILTER.get("home_zip", "") or "").strip()
try:
    PART_TIME_MAX_MILES = float(PART_TIME_DISTANCE_FILTER.get("max_miles", 0) or 0)
except (TypeError, ValueError):
    PART_TIME_MAX_MILES = 0.0
_near_states_raw = PART_TIME_DISTANCE_FILTER.get("near_state_codes", [])
if isinstance(_near_states_raw, str):
    _near_states_raw = [_near_states_raw]
PART_TIME_NEAR_STATES = {
    str(s).strip().lower()
    for s in _near_states_raw
    if str(s).strip()
}

_ZIP_COORDS = {
    # Common Inland Empire center used by this fork. Keep the exact requested
    # 92534 key configurable because public ZIP data does not resolve it cleanly.
    "92534": (34.0528, -117.2513),
    "92354": (34.0528, -117.2513),  # Loma Linda, CA
}

_STATE_ABBR = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy",
}

_CITY_COORDS = {
    "loma linda, ca": (34.0528, -117.2513),
    "riverside, ca": (33.9806, -117.3755),
    "san bernardino, ca": (34.1083, -117.2898),
    "redlands, ca": (34.0556, -117.1825),
    "inland empire, ca": (34.0633, -117.6509),
    "los angeles, ca": (34.0522, -118.2437),
    "long beach, ca": (33.7701, -118.1937),
    "pasadena, ca": (34.1478, -118.1445),
    "pomona, ca": (34.0551, -117.7490),
    "claremont, ca": (34.0967, -117.7198),
    "la mirada, ca": (33.9172, -118.0120),
    "northridge, ca": (34.2381, -118.5301),
    "orange county, ca": (33.7175, -117.8311),
    "irvine, ca": (33.6846, -117.8265),
    "fullerton, ca": (33.8704, -117.9242),
    "anaheim, ca": (33.8366, -117.9143),
    "costa mesa, ca": (33.6411, -117.9187),
    "san diego, ca": (32.7157, -117.1611),
    "santa barbara, ca": (34.4208, -119.6982),
    "san luis obispo, ca": (35.2828, -120.6596),
    "fresno, ca": (36.7378, -119.7871),
    "monterey, ca": (36.6002, -121.8947),
    "sacramento, ca": (38.5816, -121.4944),
    "davis, ca": (38.5449, -121.7405),
    "san francisco, ca": (37.7749, -122.4194),
    "oakland, ca": (37.8044, -122.2712),
    "berkeley, ca": (37.8715, -122.2730),
    "palo alto, ca": (37.4419, -122.1430),
    "san jose, ca": (37.3382, -121.8863),
    "santa clara, ca": (37.3541, -121.9552),
    "portland, or": (45.5152, -122.6784),
    "seattle, wa": (47.6062, -122.3321),
    "phoenix, az": (33.4484, -112.0740),
    "las vegas, nv": (36.1716, -115.1391),
    "salt lake city, ut": (40.7608, -111.8910),
    "denver, co": (39.7392, -104.9903),
    "austin, tx": (30.2672, -97.7431),
    "dallas, tx": (32.7767, -96.7970),
    "denton, tx": (33.2148, -97.1331),
    "tulsa, ok": (36.1540, -95.9928),
    "tuscaloosa, al": (33.2098, -87.5692),
    "kennesaw, ga": (34.0234, -84.6155),
    "highland, il": (38.7395, -89.6712),
    "chicago, il": (41.8781, -87.6298),
    "ann arbor, mi": (42.2808, -83.7430),
    "detroit, mi": (42.3314, -83.0458),
    "indiana, pa": (40.6215, -79.1525),
    "new york, ny": (40.7128, -74.0060),
    "boston, ma": (42.3601, -71.0589),
    "nashville, tn": (36.1627, -86.7816),
    "miami, fl": (25.7617, -80.1918),
    "tampa, fl": (27.9506, -82.4572),
}


def _configured_part_time_center() -> tuple[float, float] | None:
    center = PART_TIME_DISTANCE_FILTER.get("center")
    if isinstance(center, dict):
        lat = center.get("latitude", center.get("lat"))
        lon = center.get("longitude", center.get("lng", center.get("lon")))
        try:
            return float(lat), float(lon)
        except (TypeError, ValueError):
            pass
    if PART_TIME_HOME_ZIP:
        return _ZIP_COORDS.get(PART_TIME_HOME_ZIP)
    return None


PART_TIME_HOME_COORDS = _configured_part_time_center()
PART_TIME_DISTANCE_ACTIVE = bool(PART_TIME_DISTANCE_FILTER and PART_TIME_MAX_MILES > 0 and PART_TIME_HOME_COORDS)
if PART_TIME_DISTANCE_FILTER and not PART_TIME_HOME_COORDS:
    print(f"  ⚠️  Part-time distance filter configured, but no coordinates found for ZIP {PART_TIME_HOME_ZIP!r}")


def _is_remote_location(*parts: str) -> bool:
    text = " ".join(str(p or "") for p in parts).lower()
    return bool(re.search(r"\b(remote|work from home|anywhere|online)\b", text))


def _is_part_time_job(job: dict) -> bool:
    text = " ".join(str(job.get(k, "") or "") for k in (
        "title", "job_type", "employment_type", "description", "summary",
    ))
    return bool(re.search(
        r"\b(part[-\s]?time|pt|adjunct|hourly|pool|temporary)\b",
        text,
        re.IGNORECASE,
    ))


def _normalize_location_text(location: str) -> str:
    loc = re.sub(r"\([^)]*\)", " ", str(location or "").lower())
    loc = loc.replace("&nbsp;", " ")
    loc = re.sub(r"\b(united states|usa|u\.s\.a\.|us)\b", " ", loc)
    loc = re.sub(r"\s+", " ", loc).strip(" ,;-")
    for state, abbr in _STATE_ABBR.items():
        loc = re.sub(rf"\b{re.escape(state)}\b", abbr, loc)
    return loc


def _coords_for_location(location: str) -> tuple[float, float] | None:
    loc = _normalize_location_text(location)
    if not loc or _is_remote_location(loc):
        return None
    for key, coords in _CITY_COORDS.items():
        if key in loc:
            return coords
    tokens = [p.strip() for p in re.split(r"[,;/|]", loc) if p.strip()]
    for i in range(len(tokens) - 1):
        state = tokens[i + 1].split()[0]
        key = f"{tokens[i]}, {state}"
        if key in _CITY_COORDS:
            return _CITY_COORDS[key]
    return None


def _state_code_for_location(location: str) -> str:
    loc = _normalize_location_text(location)
    for token in re.split(r"[,;/|]", loc):
        first = token.strip().split()
        if first and first[0] in _STATE_ABBR.values():
            return first[0]
    return ""


def _haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def _passes_part_time_distance_filter(job: dict) -> bool:
    if not PART_TIME_DISTANCE_ACTIVE or not _is_part_time_job(job):
        return True
    if _is_remote_location(job.get("location", ""), job.get("work_arrangement", "")):
        return True
    coords = _coords_for_location(job.get("location", ""))
    if coords is None:
        state = _state_code_for_location(job.get("location", ""))
        if PART_TIME_NEAR_STATES and state and state not in PART_TIME_NEAR_STATES:
            return False
        return True
    miles = _haversine_miles(PART_TIME_HOME_COORDS, coords)  # type: ignore[arg-type]
    job["distance_miles"] = round(miles, 1)
    return miles <= PART_TIME_MAX_MILES


def _filter_part_time_distance_jobs(jobs: list[dict], *, label: str = "jobs") -> list[dict]:
    filtered = [j for j in jobs if _passes_part_time_distance_filter(j)]
    dropped = len(jobs) - len(filtered)
    if dropped:
        print(
            f"  📍 Dropped {dropped} part-time {label} more than "
            f"{PART_TIME_MAX_MILES:g} miles from {PART_TIME_HOME_ZIP or 'configured home base'}"
        )
    return filtered


# Geographic scope for the curated/legacy ATS path. Primary base is California,
# extended to Portland & Bend OR and Australia. (The LinkedIn and Indeed watchers
# geo-filter at the API level — see LINKEDIN_GEOS / INDEED_GEOS.)
TARGET_LOCATIONS = [
    "california", ", ca", "remote", "hybrid",
    # Sacramento region (home base)
    "sacramento", "davis", "west sacramento", "rancho cordova", "elk grove",
    "roseville", "folsom", "woodland",
    # SF Bay Area
    "bay area", "san francisco", "south san francisco", "oakland", "berkeley",
    "emeryville", "richmond", "palo alto", "mountain view", "menlo park",
    "sunnyvale", "santa clara", "san jose", "san mateo", "redwood city",
    "fremont", "hayward", "concord", "walnut creek", "pleasanton", "livermore",
    "novato", "san rafael", "vacaville",
    # Southern California (his PhD / collaborator base)
    "los angeles", "long beach", "irvine", "costa mesa", "san diego",
    "riverside", "pasadena", "santa monica", "torrance", "fountain valley",
    # Central Coast / Valley
    "santa barbara", "san luis obispo", "fresno", "monterey",
    # Oregon
    "portland, or", "portland, oregon", "bend, or", "bend, oregon", "oregon",
    "beaverton", "hillsboro", "eugene", "salem, or", "corvallis", "redmond, or",
    # Australia
    "australia", "sydney", "melbourne", "brisbane", "perth", "adelaide",
    "canberra", "new south wales", "victoria", "queensland",
    "western australia", "tasmania",
]


def is_target_location(location: str) -> bool:
    if not location:
        return False
    loc = location.lower()
    return any(place in loc for place in TARGET_LOCATIONS)


def extract_location(job: dict) -> str:
    loc = job.get("jobLocation", {})
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = loc.get("address", {})
    if isinstance(addr, dict):
        city = addr.get("addressLocality", "")
        state = addr.get("addressRegion", "")
        return f"{city}, {state}".strip(", ")
    return str(addr)


def _parse_posted_at(value: str, *, now: datetime | None = None) -> datetime | None:
    """
    Parse ATS posting dates into UTC datetimes.

    Some ATS APIs return exact ISO dates/datetimes, while Workday often returns
    relative strings like "Posted Today" or "Posted 3 hours ago".
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    raw = (value or "").strip()
    if not raw:
        return None

    text = re.sub(r'\s+', ' ', raw).strip().lower()
    text = text.removeprefix("posted ").strip()

    if text in {"today", "just posted", "just now"}:
        return now

    relative_m = re.search(
        r'(\d+)\s*(minutes?|mins?|hours?|hrs?)\b(?:\s*ago)?',
        text,
    )
    if relative_m:
        amount = int(relative_m.group(1))
        unit = relative_m.group(2)
        if unit.startswith(("minute", "min")):
            return now - timedelta(minutes=amount)
        return now - timedelta(hours=amount)

    iso_value = raw.replace("Z", "+00:00")
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', iso_value):
            parsed = datetime.strptime(iso_value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(iso_value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
        return parsed
    except ValueError:
        return None


def is_recent_posting(job: dict, *, now: datetime | None = None) -> bool:
    posted_at = _parse_posted_at(job.get("date_posted", ""), now=now)
    if posted_at is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return timedelta(0) <= now - posted_at <= FRESH_JOB_LOOKBACK


# ---------------------------------------------------------------------------
# Curated Bay Area biotechs — direct ATS probes (Greenhouse / Workday)
# ---------------------------------------------------------------------------

# Each entry must include: name, ats, fallback_location, and the ATS-specific id
# - greenhouse: "slug" (used in boards-api.greenhouse.io/v1/boards/{slug}/jobs)
# - workday:    "url"  (full /wday/cxs/{tenant}/{site}/jobs endpoint)
#
# NOTE: The original biotech employers were on public Greenhouse/Workday boards.
# Environmental / toxicology employers (Ramboll, Exponent, ToxStrategies, Tetra
# Tech, ICF, NGOs, etc.) overwhelmingly use iCIMS / Taleo / SuccessFactors,
# which have no clean public JSON endpoint — so this direct-ATS path is left
# EMPTY and the LinkedIn + JobSpy keyword watchers (which need no slug) are the
# primary sources. To add a verified board here, confirm it returns JSON first:
#   curl https://boards-api.greenhouse.io/v1/boards/<slug>/jobs   # Greenhouse
# then add e.g.:
#   {"name": "Example Env Co", "ats": "greenhouse", "slug": "examplenv",
#    "fallback_location": "Sacramento, CA"},
CURATED_BIOTECHS: list[dict] = []


def probe_curated_greenhouse(entry: dict) -> list:
    time.sleep(REQUEST_DELAY)
    url = f"https://boards-api.greenhouse.io/v1/boards/{entry['slug']}/jobs?content=true"
    raw = fetch(url)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    jobs = []
    for job in data.get("jobs", []):
        title = job.get("title", "")
        if not is_mle_role(title):
            continue
        loc = (job.get("location") or {}).get("name", "") or entry["fallback_location"]
        jobs.append({
            "company": entry["name"],
            "title": title,
            "location": loc,
            "url": job.get("absolute_url", f"https://boards.greenhouse.io/{entry['slug']}"),
            "date_posted": (job.get("updated_at") or "")[:10],
            "ats": "Greenhouse",
        })
    return jobs


WORKDAY_SEARCH_TERMS = [
    "toxicologist",
    "environmental scientist",
    "risk assessment",
    "exposure scientist",
    "environmental health",
    "water quality",
]


def probe_curated_workday(entry: dict) -> list:
    """
    Workday's /jobs endpoint sometimes 400s on empty searchText, so we hit it
    once per term and dedupe by externalPath.
    """
    domain_m = re.match(r'https://([^/]+)', entry["url"])
    domain = domain_m.group(1) if domain_m else ""
    site_m = re.search(r'/wday/cxs/[^/]+/([^/]+)/jobs', entry["url"])
    site = site_m.group(1) if site_m else ""

    seen: dict[str, dict] = {}
    for term in WORKDAY_SEARCH_TERMS:
        time.sleep(REQUEST_DELAY)
        body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term}).encode()
        try:
            req = Request(
                entry["url"],
                data=body,
                headers={**HEADERS, "Content-Type": "application/json", "Accept": "application/json"},
            )
            with urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8", errors="ignore"))
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            print(f"  ⚠️  Workday {entry['name']} ({term!r}): {e}")
            continue

        for posting in data.get("jobPostings", []):
            ext_path = posting.get("externalPath", "")
            if ext_path in seen:
                continue
            title = posting.get("title", "")
            if not is_mle_role(title):
                continue
            public_url = f"https://{domain}/{site}{ext_path}" if ext_path else entry["url"]
            loc = posting.get("locationsText", "") or entry["fallback_location"]
            # Workday summarizes multi-location roles as "N Locations" — assume HQ
            if re.match(r'^\d+ Locations?$', loc):
                loc = entry["fallback_location"]
            seen[ext_path] = {
                "company": entry["name"],
                "title": title,
                "location": loc,
                "url": public_url,
                "date_posted": posting.get("postedOn") or "",
                "ats": "Workday",
            }
    return list(seen.values())


def scrape_curated_biotechs() -> list:
    if not CURATED_BIOTECHS:
        return []
    print(f"🔬 Scraping {len(CURATED_BIOTECHS)} curated organizations (direct ATS)...")
    all_jobs: list = []
    for entry in CURATED_BIOTECHS:
        if entry["ats"] == "greenhouse":
            jobs = probe_curated_greenhouse(entry)
        elif entry["ats"] == "workday":
            jobs = probe_curated_workday(entry)
        else:
            print(f"  ⚠️  Unknown ATS for {entry['name']}: {entry['ats']}")
            continue
        if jobs:
            print(f"  ✅ {entry['name']}: {len(jobs)} role(s)")
            all_jobs.extend(jobs)
    return all_jobs


# ---------------------------------------------------------------------------
# Genentech — custom Phenom ATS, kept as standalone
# ---------------------------------------------------------------------------

def scrape_genentech():
    print("🔍 Scraping Genentech...")
    url = (
        "https://careers.gene.com/us/en/search-results"
        "?keywords=machine+learning+engineer&category=Data+Science+%26+AI%2FML"
    )
    html = fetch(url)
    jobs = []

    matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    for match in matches:
        try:
            data = json.loads(match)
            items = (
                data if isinstance(data, list)
                else data.get("itemListElement", []) if data.get("@type") == "ItemList"
                else [data]
            )
            for item in items:
                job = item.get("item", item)
                title = job.get("title", job.get("name", ""))
                if title and is_mle_role(title):
                    jobs.append({
                        "company": "Genentech",
                        "title": title,
                        "location": extract_location(job),
                        "url": job.get("url", "https://careers.gene.com/us/en/c/data-science-ai-ml-jobs"),
                        "date_posted": job.get("datePosted", ""),
                        "ats": "Phenom",
                    })
        except json.JSONDecodeError:
            continue

    if not jobs:
        title_matches = re.findall(r'data-ph-at-job-title-text="([^"]+)"', html)
        link_matches = re.findall(r'href="(/us/en/job/[^"]+)"', html)
        for i, title in enumerate(title_matches):
            if is_mle_role(title):
                link = link_matches[i] if i < len(link_matches) else ""
                jobs.append({
                    "company": "Genentech",
                    "title": title,
                    "location": "South San Francisco, CA",
                    "url": f"https://careers.gene.com{link}" if link else "https://careers.gene.com/us/en/c/data-science-ai-ml-jobs",
                    "date_posted": "",
                    "ats": "Phenom",
                })

    print(f"  ✅ Found {len(jobs)} MLE role(s) at Genentech")
    return jobs


# ---------------------------------------------------------------------------
# LinkedIn — public guest endpoint, bucketed by recency (broad US-wide net)
# ---------------------------------------------------------------------------

LINKEDIN_SEARCH_TERMS = _cfg("search_terms.linkedin", [
    # Toxicology (core)
    "toxicologist",
    "environmental toxicologist",
    "ecotoxicologist",
    "regulatory toxicologist",
    "computational toxicology",
    # Risk / exposure
    "human health risk assessment",
    "ecological risk assessment",
    "exposure scientist",
    "exposure assessment",
    # Environmental science / health / chemistry
    "environmental scientist",
    "environmental health scientist",
    "environmental chemist",
    "environmental epidemiologist",
    # Water / contaminants
    "water quality scientist",
    "microplastics",
    "PFAS",
    "emerging contaminants",
    "air quality scientist",
    # Chemical safety / stewardship / regulatory
    "product stewardship",
    "chemical safety",
    "environmental data scientist",
])

LINKEDIN_LOOKBACK_SECONDS = 3600          # 1h — every-2h watcher only surfaces the freshest hour
LINKEDIN_BIOTECH_LOOKBACK_SECONDS = 86400 # 24h — biotech is a daily 8pm PT digest

# Geographies to search. geoId is LinkedIn's authoritative region filter; an
# empty geoId lets LinkedIn resolve the location text (verified to work for
# Bend). All confirmed by probing the guest endpoint. Add a region by finding
# its geoId (or leaving it blank for a city LinkedIn can resolve).
LINKEDIN_GEOS = _cfg("locations.linkedin", [
    {"name": "California",  "location": "California, United States",          "geoId": "102095887"},
    {"name": "Portland OR", "location": "Portland, Oregon Metropolitan Area", "geoId": "90000079"},
    {"name": "Bend OR",     "location": "Bend, Oregon, United States",        "geoId": ""},
    {"name": "Australia",   "location": "Australia",                          "geoId": "101452733"},
])

# Priority-employer allowlist used by the LinkedIn-side filter to build the
# daily "Priority Employers" digest (jobs.json). These are organizations whose
# postings are worth surfacing on their own even on a quiet day: environmental
# consulting, toxicology/risk firms, research institutes, NGOs, agencies, water
# utilities, universities, and product-safety teams in industry. Match is case-
# insensitive on alphanum-stripped names with bidirectional substring matching,
# so "Ramboll" matches "Ramboll US Corporation". Keep names ~6+ chars to limit
# incidental substring collisions (avoid bare acronyms like EPA/EWG/ERG/CARB).
BIOTECH_COMPANY_NAMES = _cfg("employers.priority", [
    # ---- Environmental / toxicology / risk consulting ----  (fallback; real list in config.json)
    "Ramboll", "Exponent", "Gradient", "ToxStrategies", "Cardno",
    "Stantec", "Tetra Tech", "Tetratech", "ICF International",
    "Abt Associates", "Abt Global", "Eastern Research Group",
    "Integral Consulting", "Geosyntec", "Arcadis", "AECOM",
    "Montrose Environmental", "Trinity Consultants", "GHD Group",
    "Environmental Resources Management", "SLR Consulting",
    "Wood Environment", "Sciome", "Cardno ChemRisk", "ChemRisk",
    # ---- Research institutes / nonprofits / NGOs ----
    "Southern California Coastal Water Research Project", "SCCWRP",
    "San Francisco Estuary Institute", "Silent Spring Institute",
    "Environmental Defense Fund", "Natural Resources Defense Council",
    "Environmental Working Group", "Ocean Conservancy", "Pew Charitable Trusts",
    "Health Effects Institute", "RTI International", "Battelle",
    "Green Science Policy Institute", "Defend Our Health",
    "Moore Institute", "Plastic Pollution Coalition", "5 Gyres",
    "ChemForward", "Cadmus Group",
    # ---- Government / agencies (as they appear on LinkedIn) ----
    "Environmental Protection Agency", "California Environmental Protection",
    "Office of Environmental Health Hazard", "State Water Resources Control",
    "California Air Resources Board", "Department of Toxic Substances Control",
    "National Institute of Environmental Health", "Geological Survey",
    "Centers for Disease Control", "Food and Drug Administration",
    "National Oceanic and Atmospheric",
    # ---- Water utilities / districts ----
    "East Bay Municipal Utility", "Metropolitan Water District",
    "Orange County Water District", "San Francisco Public Utilities",
    "Santa Clara Valley Water",
    # ---- Universities (research-scientist / faculty) ----
    "University of California", "Stanford University", "Oregon State University",
    "Duke University", "San Diego State University", "Arizona State University",
    # ---- Industry product-safety / stewardship / consumer & chemical ----
    "Procter & Gamble", "Unilever", "Colgate-Palmolive", "Johnson & Johnson",
    "Clorox", "Seventh Generation", "Patagonia",
    "Corteva", "Syngenta", "Dow Chemical", "BASF Corporation",
])

BIOTECH_COMPANY_ALLOWLIST = frozenset(
    re.sub(r'[^a-z0-9]', '', n.lower()) for n in BIOTECH_COMPANY_NAMES
)


def _is_biotech_company(name: str) -> bool:
    norm = re.sub(r'[^a-z0-9]', '', (name or "").lower())
    if not norm:
        return False
    return any(b in norm or norm in b for b in BIOTECH_COMPANY_ALLOWLIST)


# Pharma / drug-development companies. Dr. Coffin works in ENVIRONMENTAL
# toxicology, never pharmaceutical / preclinical drug-safety tox, so these are
# dropped everywhere even when the title (e.g. "Toxicologist", "Toxicology
# Director") would otherwise match. Agrochemical and chemical manufacturers
# (Corteva, Syngenta, Dow, BASF) are intentionally NOT here — their product-
# stewardship / chemical-risk roles are in-scope.
PHARMA_COMPANY_RE = re.compile(
    # ---- generic pharma / biotech / drug-development name signals (substring) ----
    r'pharmaceutic|pharma\b|therapeutic|biopharm|biotech|biologic|bioscience|'
    r'biosystem|genomics|gene therap|cell therap|immunotherap|\bvaccine|'
    r'\bmedicines\b|drug discovery|oncolog|biomedicine|nanomedicine'
    # ---- explicit pharma / biotech companies (word-bounded, length >= 5) ----
    r'|\b(?:'
    r'pfizer|merck|novartis|roche|abbvie|bristol[ -]?myers|sanofi|astrazeneca|'
    r'glaxosmithkline|takeda|boehringer|amgen|gilead|genentech|biogen|regeneron|'
    r'moderna|vertex|novo nordisk|viatris|bausch|alkermes|halozyme|galapagos|'
    r'insitro|recursion|cytokinetics|arcus|gritstone|sutro|nurix|rigel|corcept|'
    r'annexon|kodiak|coherus|vaxcyte|allakos|protagonist|kyverna|septerna|'
    r'sangamo|atara|allogene|intellia|editas|poseida|nkarta|tenaya|pliant|'
    r'rezolute|aldeyra|arcturus|caribou|chemocentryx|dynavax|geron|iovance|'
    r'karuna|mersana|mirati|nektar|prothena|revance|seagen|ultragenyx|zentalis|'
    r'exelixis|biomarin|alnylam|incyte|neurocrine|ionis|denali|acadia|adarx|'
    r'genmab|nuvation|exact sciences|revolution medicines|structure therapeutics|'
    r'relay therapeutics|beam therapeutics|sana biotechnology|fate therapeutics'
    r')\b',
    re.IGNORECASE,
)


# config.json → employers.exclude: drop roles from any company whose name
# contains one of these (case-insensitive substring). When set, it overrides the
# built-in PHARMA_COMPANY_RE; leave it [] to disable company exclusion entirely.
_EXCLUDE_COMPANY_TERMS = [str(t).lower() for t in _cfg("employers.exclude", [])]


def _is_pharma_company(name: str) -> bool:
    if _EXCLUDE_COMPANY_TERMS:
        low = (name or "").lower()
        return any(t in low for t in _EXCLUDE_COMPANY_TERMS)
    return bool(PHARMA_COMPANY_RE.search(name or ""))


def _parse_linkedin_cards(html: str) -> tuple[list[dict], int]:
    """Returns (keyword-matched cards, raw card count on the page). The raw
    count lets callers distinguish 'page full of non-matching roles' (keep
    paginating) from 'no results at all' (stop)."""
    import html as html_mod
    cards = re.split(r'<li[^>]*>', html)[1:]
    parsed = []
    raw_count = 0
    for card in cards:
        urn = re.search(r'data-entity-urn="urn:li:jobPosting:(\d+)"', card)
        if not urn:
            continue
        raw_count += 1
        title_m = re.search(r'base-search-card__title[^>]*>\s*([^<]+)', card)
        company_m = re.search(
            r'base-search-card__subtitle[^>]*>.*?<a[^>]*>\s*([^<]+)\s*</a>',
            card, re.DOTALL,
        ) or re.search(r'base-search-card__subtitle[^>]*>\s*([^<]+)', card)
        location_m = re.search(r'job-search-card__location[^>]*>\s*([^<]+)', card)
        time_m = re.search(r'<time[^>]*datetime="([^"]+)"', card)
        # LinkedIn shows pay on the card when the poster provides it.
        salary_m = re.search(r'job-search-card__salary-info[^>]*>\s*([^<]+)', card)

        title = html_mod.unescape(title_m.group(1).strip()) if title_m else ""
        if not title or not is_mle_role(title):
            continue
        company = (
            html_mod.unescape(re.sub(r'\s+', ' ', company_m.group(1).strip()))
            if company_m else "Unknown"
        )
        location = html_mod.unescape(
            (location_m.group(1).strip() if location_m else "")
        ).replace("\n", " ")
        salary = (
            re.sub(r'\s+', ' ', html_mod.unescape(salary_m.group(1).strip()))
            if salary_m else ""
        )
        parsed.append({
            "id": urn.group(1),
            "company": company,
            "title": title,
            "location": location,
            "date_posted": time_m.group(1) if time_m else "",
            "salary": salary,
        })
    return parsed, raw_count


def _linkedin_search(terms: list[str], lookback_seconds: int,
                     geos: list[dict] | None = None) -> tuple[list[dict], int]:
    """
    Per-geo, per-term, paginated LinkedIn guest-endpoint search. Dedupes by job
    ID across every geography and sorts by recency. Used by both the general
    watcher and the priority-employer scrape.

    Returns (jobs, total_raw_cards). total_raw_cards == 0 across everything means
    LinkedIn gave us no data at all — the callers' block guard.
    """
    if geos is None:
        geos = LINKEDIN_GEOS
    jobs_by_id: dict[str, dict] = {}
    total_raw_cards = 0
    for geo in geos:
        geo_param = f"&geoId={geo['geoId']}" if geo.get("geoId") else ""
        for term in terms:
            for start in range(0, 75, 25):
                time.sleep(LINKEDIN_REQUEST_DELAY + random.uniform(0, 2))
                url = (
                    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                    f"?keywords={urllib.parse.quote(term)}"
                    f"&location={urllib.parse.quote(geo['location'])}"
                    f"{geo_param}"
                    f"&f_TPR=r{lookback_seconds}"
                    f"&start={start}"
                )
                html = fetch(url)
                if not html.strip():
                    break
                parsed, raw_count = _parse_linkedin_cards(html)
                total_raw_cards += raw_count
                # Break on a truly empty page, NOT on "no keyword matches" — a page
                # of 25 off-target roles must not end pagination for the term.
                if not raw_count:
                    break
                for p in parsed:
                    if p["id"] in jobs_by_id:
                        continue
                    jobs_by_id[p["id"]] = {
                        "company": p["company"],
                        "title": p["title"],
                        "location": p["location"],
                        "url": f"https://www.linkedin.com/jobs/view/{p['id']}/",
                        "date_posted": p["date_posted"],
                        "salary": p.get("salary", ""),
                        "ats": "LinkedIn",
                    }

    jobs = list(jobs_by_id.values())
    jobs.sort(key=lambda j: -_iso_to_ts(j.get("date_posted", "")))
    return jobs, total_raw_cards


# LinkedIn search-result cards omit the full description and often omit pay, but
# the public guest *posting* page includes both. Fetch it only for jobs that need
# enrichment, capped per run to bound runtime.
LINKEDIN_SALARY_FETCH_CAP = 120
LINKEDIN_DESCRIPTION_MAX_CHARS = 12000


def _linkedin_description_from_page(page: str) -> str:
    import html as html_mod
    if not page:
        return ""
    block = ""
    m = re.search(
        r'<div[^>]+show-more-less-html__markup[^>]*>(.*?)</div>\s*</section>',
        page,
        re.I | re.S,
    )
    if m:
        block = m.group(1)
    else:
        m = re.search(r'<div[^>]+description__text[^>]*>(.*?)</section>', page, re.I | re.S)
        if m:
            block = m.group(1)
    if not block:
        return ""
    text = re.sub(r'(?i)<\s*br\s*/?\s*>', "\n", block)
    text = re.sub(r'(?i)</\s*(p|li|ul|ol|section|div|strong|h\d)\s*>', "\n", text)
    text = re.sub(r'<[^>]+>', " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    text = re.sub(r' *\n *', '\n', text).strip()
    return text[:LINKEDIN_DESCRIPTION_MAX_CHARS]


def _linkedin_posting_details(job_id: str) -> tuple[str, str]:
    import html as html_mod
    page = fetch(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}")
    if not page:
        return "", ""
    description = _linkedin_description_from_page(page)
    # Primary: structured compensation block (employer-declared LinkedIn field).
    anchor = re.search(r'compensation__salary', page)
    if anchor:
        window = page[anchor.start():anchor.start() + 400]
        amt = re.search(r'\$[\d][^<]{0,60}', window)
        if amt:
            return re.sub(r'\s+', ' ', html_mod.unescape(amt.group(0))).strip(), description
    # Fallback: salary range embedded in description text.
    text = re.sub(r'\s+', ' ', html_mod.unescape(re.sub(r'<[^>]+>', ' ', page)))

    # Pattern 1: two-dollar-sign range with optional USD codes and en/em dashes.
    # Handles: "$130k to $176k", "$7,820 – $10,732", "$75,000 USD - $85,000 USD",
    #          "USD $200,000 - USD $300,000"
    sal_m = re.search(
        r'(?:USD\s*)?\$\s*[\d,]+(?:\.\d{2})?(?:\s*[kK])?(?:\s*USD)?\s*(?:to|[–—-])\s*(?:USD\s*)?\$\s*[\d,]+(?:\.\d{2})?(?:\s*[kK])?(?:\s*USD)?'
        r'(?:\s*(?:per\s+\w+|annually|hourly|monthly|/\w+))?',
        text, re.I,
    )
    if sal_m:
        return re.sub(r'\s+', ' ', sal_m.group(0)).strip(), description

    # Pattern 2: single leading $ with bare second number: "$110,000-130,000/year".
    # Require second number to be comma-formatted (NNN,NNN) to avoid false positives.
    sal_m = re.search(
        r'\$\s*[\d,]+(?:\.\d{2})?(?:\s*[kK])?\s*-\s*\d{2,3},\d{3}(?:\.\d{2})?(?:\s*[kK])?'
        r'(?:\s*(?:per\s+\w+|annually|hourly|monthly|/\w+))?',
        text, re.I,
    )
    if sal_m:
        return re.sub(r'\s+', ' ', sal_m.group(0)).strip(), description

    # Pattern 3: keyword-anchored plain number range (no $ sign).
    # Handles: "Compensation Range 199,000.00 - 243,000.00 | Compensation Type Annual Salary"
    kw_m = re.search(
        r'(?:compensation|salary)\s+(?:range|amount)[:\s]+([\d,]+(?:\.\d{2})?)\s*(?:to|-)\s*([\d,]+(?:\.\d{2})?)',
        text, re.I,
    )
    if kw_m:
        return f"${kw_m.group(1)} - ${kw_m.group(2)}", description

    # Pattern 4: "Minimum Salary: $156,115/year / Maximum Salary: $218,560/year"
    min_m = re.search(r'[Mm]inimum\s+[Ss]alary[:\s]+\$\s*([\d,]+(?:\.\d{2})?(?:\s*[kK])?)', text)
    max_m = re.search(r'[Mm]aximum\s+[Ss]alary[:\s]+\$\s*([\d,]+(?:\.\d{2})?(?:\s*[kK])?)', text)
    if min_m and max_m:
        interval_m = re.search(
            r'(?:per\s+\w+|annually|hourly|monthly|/(?:year|yr|hr|mo))',
            text[min_m.start():min_m.start() + 60], re.I,
        )
        interval = f"/{interval_m.group(0).lstrip('/')}" if interval_m else ""
        return f"${min_m.group(1)}{interval} - ${max_m.group(1)}{interval}", description

    return "", description


def _linkedin_posting_salary(job_id: str) -> str:
    salary, _ = _linkedin_posting_details(job_id)
    return salary


def _enrich_linkedin_postings(jobs: list) -> tuple[int, int]:
    """Backfill salary and description on LinkedIn jobs from posting pages.
    Returns (salary_filled, description_filled). Bounded and never raises."""
    salary_filled = desc_filled = fetched = 0
    for job in jobs:
        if fetched >= LINKEDIN_SALARY_FETCH_CAP:
            break
        if job.get("ats") != "LinkedIn":
            continue
        if job.get("salary") and job.get("description"):
            continue
        m = re.search(r'/jobs/view/(\d+)', job.get("url", ""))
        if not m:
            continue
        time.sleep(LINKEDIN_REQUEST_DELAY + random.uniform(0, 2))
        fetched += 1
        try:
            sal, desc = _linkedin_posting_details(m.group(1))
        except (URLError, TimeoutError, OSError):
            continue
        if sal:
            job["salary"] = sal
            salary_filled += 1
        if desc:
            job["description"] = desc
            desc_filled += 1
    if fetched:
        print(
            "  LinkedIn posting backfill: "
            f"{salary_filled}/{fetched} had pay; {desc_filled}/{fetched} had descriptions"
        )
    return salary_filled, desc_filled

def scrape_linkedin_recent() -> list:
    print(f"🔎 Scraping LinkedIn (last {LINKEDIN_LOOKBACK_SECONDS // 3600}h)...")
    jobs, raw_cards = _linkedin_search(LINKEDIN_SEARCH_TERMS, LINKEDIN_LOOKBACK_SECONDS)
    # Block guard (mirrors Indeed's): zero raw cards across every term means
    # LinkedIn gave us nothing — rate-limited or blocked, not a quiet hour.
    # Reuse the previous results so we don't clobber the dedupe baseline.
    if raw_cards == 0:
        prev = _load_prev_jobs(os.path.join(OUTPUT_DIR, "linkedin_jobs.json"))
        print(f"  ⛔ LinkedIn returned 0 cards across all terms (likely blocked); "
              f"preserving previous {len(prev)} result(s)")
        return prev
    print(f"  ✅ LinkedIn: {len(jobs)} role(s)")
    _enrich_linkedin_postings(jobs)
    return jobs


def scrape_linkedin_biotech() -> list:
    """
    Last 24h on LinkedIn, filtered to the priority-employer allowlist (env/tox
    consulting, research institutes, agencies, NGOs, universities, product
    safety). LinkedIn's f_I industry filter is silently ignored on the public
    guest endpoint, so we use the env/tox keyword terms + a company allowlist.
    """
    print(f"🏛  Scraping LinkedIn priority employers (last {LINKEDIN_BIOTECH_LOOKBACK_SECONDS // 3600}h)...")
    raw, raw_cards = _linkedin_search(LINKEDIN_SEARCH_TERMS, LINKEDIN_BIOTECH_LOOKBACK_SECONDS)
    if raw_cards == 0:
        # Blocked run: contribute nothing rather than nuke the digest baseline.
        print("  ⛔ LinkedIn returned 0 cards across all terms (likely blocked); "
              "skipping LinkedIn for this digest")
        return []
    jobs = [j for j in raw if _is_biotech_company(j["company"])]
    print(f"  ✅ Priority employers: {len(jobs)} role(s) (from {len(raw)} total)")
    _enrich_linkedin_postings(jobs)
    return jobs


# ---------------------------------------------------------------------------
# JobSpy-backed broad boards. Indeed is the primary existing source; Glassdoor
# is optional extra coverage inspired by JobOps' multi-board extractor model.
# Both reuse python-jobspy so the repo keeps its single optional dependency.
# ---------------------------------------------------------------------------

INDEED_LOOKBACK_HOURS = 24  # Indeed posting dates are ~day-resolution, so a 1h window
# returns almost nothing; the hourly watcher's cross-run dedupe trims the overlap.
INDEED_BACKFILL_DAYS = 50  # one-time historical backfill window

# Indeed geographies. country sets the Indeed domain (USA → indeed.com,
# Australia → au.indeed.com). Searched per term, so we use a tighter term list
# than LinkedIn to keep the call count sane (terms × geos jobspy calls).
INDEED_GEOS = _cfg("locations.indeed", [
    {"location": "California",   "country": "USA"},
    {"location": "Portland, OR", "country": "USA"},
    {"location": "Bend, OR",     "country": "USA"},
    {"location": "Australia",    "country": "Australia"},
])
INDEED_SEARCH_TERMS = _cfg("search_terms.indeed", [
    "toxicologist", "environmental scientist", "risk assessment",
    "exposure scientist", "ecotoxicologist", "microplastics",
    "water quality scientist", "environmental health scientist",
])
GLASSDOOR_LOOKBACK_HOURS = 24
GLASSDOOR_BACKFILL_DAYS = 30
GLASSDOOR_GEOS = _cfg("locations.glassdoor", INDEED_GEOS)
GLASSDOOR_SEARCH_TERMS = _cfg("search_terms.glassdoor", INDEED_SEARCH_TERMS)
ZIPRECRUITER_LOOKBACK_HOURS = 24
ZIPRECRUITER_BACKFILL_DAYS = 30
ZIPRECRUITER_GEOS = _cfg("locations.ziprecruiter", [
    geo for geo in INDEED_GEOS
    if str(geo.get("country", "")).lower() in {"usa", "us", "united states", "canada"}
])
ZIPRECRUITER_SEARCH_TERMS = _cfg("search_terms.ziprecruiter", INDEED_SEARCH_TERMS)
GOOGLE_JOBS_LOOKBACK_HOURS = 24
GOOGLE_JOBS_BACKFILL_DAYS = 30
GOOGLE_JOBS_GEOS = _cfg("locations.google_jobs", INDEED_GEOS)
GOOGLE_JOBS_SEARCH_TERMS = _cfg("search_terms.google_jobs", INDEED_SEARCH_TERMS)
GOOGLE_JOBS_QUERIES = _cfg("google_jobs.queries", [])


def _jobspy_proxies():
    raw = _cfg("jobspy.proxies", None)
    if raw in (None, "", []):
        raw = os.environ.get("JOBSPY_PROXIES", "")
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",") if p.strip()]
    if isinstance(raw, list):
        return [str(p).strip() for p in raw if str(p).strip()]
    return None


def _jobspy_user_agent():
    raw = _cfg("jobspy.user_agent", None)
    if raw in (None, "", []):
        raw = os.environ.get("JOBSPY_USER_AGENT", "")
    return str(raw or "").strip() or None

# jobspy returns the full JD (markdown) for many boards. We keep a trimmed copy
# in source JSONs and all_jobs.json so the dashboard, deterministic scorer, and
# optional triage agent can judge roles from the actual description instead of
# title alone.
JOBSPY_JD_MAX_CHARS = 6000


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if value in ("", None):
        return None
    return str(value).strip().lower() in {"1", "true", "yes", "remote"}


WORK_ARRANGEMENTS = {
    "onsite": "On-site",
    "remote_in_state": "Remote in-state eligible",
    "remote_out_of_state": "Remote out-of-state eligible",
    "telecommute": "Telecommute eligible",
}


def classify_work_arrangement(*parts, is_remote=None) -> str:
    """Normalize board-specific remote/telework labels for dashboard filtering."""
    text = " ".join(str(p or "") for p in parts)
    text = re.sub(r"[\s\-_]+", " ", text).strip().lower()
    if not text and is_remote is None:
        return ""
    if re.search(r"\b(out of state|out state|out of state eligible|remote out of state)\b", text):
        return WORK_ARRANGEMENTS["remote_out_of_state"]
    if re.search(r"\b(in state|instate|in site|remote in state|remote in site)\b", text):
        return WORK_ARRANGEMENTS["remote_in_state"]
    if re.search(r"\b(telecommut\w*|telework|hybrid)\b", text):
        return WORK_ARRANGEMENTS["telecommute"]
    if re.search(r"\b(work from home|remote|long distance)\b", text) or is_remote is True:
        return WORK_ARRANGEMENTS["remote_in_state"]
    if re.search(r"\b(on site|onsite|in office|office centered|in person|business location|work in person)\b", text) or is_remote is False:
        return WORK_ARRANGEMENTS["onsite"]
    return ""


def _ensure_work_arrangement(job: dict) -> dict:
    label = classify_work_arrangement(
        job.get("work_arrangement", ""),
        job.get("telework", ""),
        job.get("job_type", ""),
        job.get("location", ""),
        is_remote=job.get("is_remote"),
    )
    if label:
        job["work_arrangement"] = label
    return job


def _ingest_jobspy_df(df, *, label: str, jobs_by_id: dict[str, dict]) -> int:
    """Normalize a JobSpy dataframe into this repo's jobs_by_id dict. Returns raw row count."""
    if df is None or df.empty:
        return 0
    raw_rows = len(df)
    df.columns = [c.lower() for c in df.columns]
    df = df.fillna("")
    for _, row in df.iterrows():
        title = str(row.get("title", "") or "")
        if not is_mle_role(title):
            continue
        url = str(row.get("job_url", "") or "")
        if not url:
            continue
        ident = _job_identity(url)
        if ident in jobs_by_id:
            continue
        loc = str(row.get("location", "") or "")
        if not loc:
            city = str(row.get("city", "") or "")
            state = str(row.get("state", "") or "")
            loc = ", ".join(p for p in [city, state] if p)
        is_remote = _coerce_bool(row.get("is_remote"))
        job_type = str(row.get("job_type", "") or "")
        jobs_by_id[ident] = _ensure_work_arrangement({
            "company": str(row.get("company", "") or "Unknown"),
            "title": title,
            "location": loc,
            "url": url,
            "direct_url": str(row.get("job_url_direct", "") or ""),
            "company_url": str(row.get("company_url", "") or ""),
            "date_posted": str(row.get("date_posted", "") or ""),
            "description": str(row.get("description", "") or "")[:JOBSPY_JD_MAX_CHARS],
            "salary": format_salary(
                row.get("min_amount", ""),
                row.get("max_amount", ""),
                row.get("interval", ""),
            ),
            "salary_source": str(row.get("salary_source", "") or ""),
            "salary_currency": str(row.get("currency", "") or ""),
            "job_type": job_type,
            "is_remote": is_remote,
            "work_arrangement": classify_work_arrangement(loc, job_type, is_remote=is_remote),
            "emails": str(row.get("emails", "") or ""),
            "ats": label,
        })
    return raw_rows


def _scrape_jobspy_board(*, label: str, site_name: str, geos: list, terms: list,
                         hours_old: int, prev_basename: str,
                         results_wanted: int = 50) -> list:
    """Scrape one JobSpy-supported board and normalize rows into this repo's schema."""
    print(f"🟦 Scraping {label} (last {hours_old}h)...")
    try:
        from jobspy import scrape_jobs as jobspy_scrape
    except ImportError:
        print(f"  ⚠️  python-jobspy not installed; skipping {label}")
        return []

    jobs_by_id: dict[str, dict] = {}
    ok_terms = 0
    errored_terms = 0
    raw_rows = 0
    for geo in geos:
      for term in terms:
        time.sleep(REQUEST_DELAY)  # throttle: back-to-back calls invite blocking on CI IPs
        try:
            # JobSpy gotcha: hours_old / is_remote / job_type / easy_apply
            # are mutually exclusive — only one may be set, or the time filter
            # silently breaks. Keep hours_old; do not add the others.
            df = jobspy_scrape(
                site_name=[site_name],
                search_term=term,
                location=geo.get("location", ""),
                results_wanted=results_wanted,
                hours_old=hours_old,
                country_indeed=geo.get("country", "USA"),
                enforce_annual_salary=False,
                proxies=_jobspy_proxies(),
                user_agent=_jobspy_user_agent(),
                verbose=0,
            )
        except Exception as e:
            errored_terms += 1
            print(f"  ⚠️  {label} ({geo['location']} · {term!r}): {e}")
            continue
        ok_terms += 1
        raw_rows += _ingest_jobspy_df(df, label=label, jobs_by_id=jobs_by_id)
    jobs = list(jobs_by_id.values())
    print(
        f"  📊 {label}: {len(geos)}×{len(terms)} queries → "
        f"{ok_terms} ok / {errored_terms} errored · {raw_rows} raw, {len(jobs)} matched"
    )

    # Block guard: zero rows pulled across every term means the board gave us no data
    # — a hard block (calls raised) or a soft block (empty frames). This is NOT the
    # same as "rows returned but none matched our keywords" (raw_rows > 0, jobs == []),
    # which is a legitimate empty result. On a no-data run, reuse the previous results
    # so we don't clobber the dedupe baseline (and the dashboard's source column) with
    # an empty file; the saver then reports 0 new (all already seen).
    if raw_rows == 0:
        prev = _load_prev_jobs(os.path.join(OUTPUT_DIR, f"{prev_basename}.json"))
        print(
            f"  ⛔ {label} returned 0 rows across all terms (likely blocked); "
            f"preserving previous {len(prev)} result(s)"
        )
        return prev

    return jobs


def scrape_indeed_recent(hours_old: int | None = None) -> list:
    """Indeed roles posted in the last hours_old hours (default INDEED_LOOKBACK_HOURS)."""
    h = hours_old if hours_old is not None else INDEED_LOOKBACK_HOURS
    return _scrape_jobspy_board(
        label="Indeed",
        site_name="indeed",
        geos=INDEED_GEOS,
        terms=INDEED_SEARCH_TERMS,
        hours_old=h,
        prev_basename="indeed_jobs",
    )


def scrape_glassdoor_recent(hours_old: int | None = None) -> list:
    """Glassdoor roles posted in the last hours_old hours (default GLASSDOOR_LOOKBACK_HOURS)."""
    h = hours_old if hours_old is not None else GLASSDOOR_LOOKBACK_HOURS
    return _scrape_jobspy_board(
        label="Glassdoor",
        site_name="glassdoor",
        geos=GLASSDOOR_GEOS,
        terms=GLASSDOOR_SEARCH_TERMS,
        hours_old=h,
        prev_basename="glassdoor_jobs",
    )


def scrape_ziprecruiter_recent(hours_old: int | None = None) -> list:
    """ZipRecruiter roles posted in the last hours_old hours."""
    h = hours_old if hours_old is not None else ZIPRECRUITER_LOOKBACK_HOURS
    return _scrape_jobspy_board(
        label="ZipRecruiter",
        site_name="zip_recruiter",
        geos=ZIPRECRUITER_GEOS,
        terms=ZIPRECRUITER_SEARCH_TERMS,
        hours_old=h,
        prev_basename="ziprecruiter_jobs",
        results_wanted=30,
    )


def _google_jobs_time_phrase(hours_old: int) -> str:
    """Natural-language recency phrase expected by Google Jobs search."""
    if hours_old <= 24:
        return "since yesterday"
    if hours_old <= 72:
        return "in the last 3 days"
    if hours_old <= 168:
        return "in the last week"
    return "in the last month"


def _google_jobs_query(term: str, geo: dict, hours_old: int) -> str:
    """Build the full google_search_term string JobSpy's Google adapter requires."""
    q = str(term or "").strip()
    if re.search(r"\bjobs?\b", q, re.I) and re.search(r"\b(near|remote|since|last)\b", q, re.I):
        return q
    loc = str(geo.get("location", "") or "").strip()
    recency = _google_jobs_time_phrase(hours_old)
    if loc and loc.lower() not in {"remote", "anywhere"}:
        return f"{q} jobs near {loc} {recency}"
    return f"remote {q} jobs {recency}"


def _google_jobs_query_contexts(hours_old: int) -> list[tuple[str, dict]]:
    if GOOGLE_JOBS_QUERIES:
        return [(str(q).strip(), {}) for q in GOOGLE_JOBS_QUERIES if str(q).strip()]
    return [
        (_google_jobs_query(term, geo, hours_old), geo)
        for geo in GOOGLE_JOBS_GEOS
        for term in GOOGLE_JOBS_SEARCH_TERMS
    ]


def _google_jobs_secret(name: str, env_name: str) -> str:
    return str(_cfg(f"google_jobs.{name}", os.environ.get(env_name, "")) or "").strip()


def _google_jobs_gl(geo: dict) -> str:
    country = str(geo.get("country", "") or "").strip().lower()
    return {
        "usa": "us",
        "us": "us",
        "united states": "us",
        "america": "us",
        "australia": "au",
        "canada": "ca",
        "gb": "gb",
        "uk": "gb",
        "united kingdom": "gb",
    }.get(country, country[:2] or "us")


def _posted_text_to_iso(text: str) -> str:
    s = str(text or "").strip().lower()
    if not s:
        return ""
    now = datetime.now(timezone.utc)
    if any(token in s for token in ("just", "today", "hour", "minute", "moment")):
        return now.date().isoformat()
    if "yesterday" in s:
        return (now - timedelta(days=1)).date().isoformat()
    m = re.search(r"(\d+)\s*(day|week|month)", s)
    if not m:
        return text
    n = int(m.group(1))
    unit = m.group(2)
    days = n if unit == "day" else n * 7 if unit == "week" else n * 30
    return (now - timedelta(days=days)).date().isoformat()


def _first_apply_link(raw: dict) -> str:
    apply_options = raw.get("apply_options")
    if isinstance(apply_options, list):
        for option in apply_options:
            if isinstance(option, dict) and option.get("link"):
                return str(option["link"])
    related = raw.get("related_links")
    if isinstance(related, list):
        for option in related:
            if isinstance(option, dict) and option.get("link"):
                return str(option["link"])
    return ""


def _google_jobs_description(raw: dict) -> str:
    parts = [str(raw.get("description", "") or "")]
    highlights = raw.get("job_highlights")
    if isinstance(highlights, list):
        for group in highlights:
            if not isinstance(group, dict):
                continue
            title = str(group.get("title", "") or "").strip()
            items = group.get("items")
            if title:
                parts.append(title)
            if isinstance(items, list):
                parts.extend(str(item) for item in items if item)
    return "\n".join(p for p in parts if p).strip()[:JOBSPY_JD_MAX_CHARS]


def _normalize_serpapi_google_job(raw: dict) -> dict | None:
    title = str(raw.get("title", "") or "")
    if not title or not is_mle_role(title):
        return None
    detected = raw.get("detected_extensions") if isinstance(raw.get("detected_extensions"), dict) else {}
    extensions = raw.get("extensions") if isinstance(raw.get("extensions"), list) else []
    posted = detected.get("posted_at") or next(
        (str(x) for x in extensions if re.search(r"\b(?:hour|day|week|month|yesterday|today)\b", str(x), re.I)),
        "",
    )
    salary = str(detected.get("salary", "") or "")
    if not salary:
        salary = next((str(x) for x in extensions if "$" in str(x)), "")
    direct_url = _first_apply_link(raw)
    url = direct_url or str(raw.get("share_link") or raw.get("link") or raw.get("serpapi_link") or "")
    if not url:
        return None
    job_type = str(detected.get("schedule_type", "") or "")
    remote_flag = _coerce_bool(detected.get("work_from_home"))
    remote_in_location = bool(re.search(r"\bremote\b", str(raw.get("location", "")), re.I))
    is_remote = True if remote_flag is True or remote_in_location else (False if remote_flag is False else None)
    return {
        "company": str(raw.get("company_name", "") or "Unknown"),
        "title": title,
        "location": str(raw.get("location", "") or ""),
        "url": url,
        "direct_url": direct_url,
        "date_posted": _posted_text_to_iso(posted),
        "description": _google_jobs_description(raw),
        "salary": salary,
        "job_type": job_type,
        "is_remote": is_remote,
        "work_arrangement": classify_work_arrangement(raw.get("location", ""), job_type, is_remote=is_remote),
        "ats": "GoogleJobs",
    }


def _normalize_oxylabs_google_job(raw: dict) -> dict | None:
    title = str(raw.get("job_title") or raw.get("title") or "")
    if not title or not is_mle_role(title):
        return None
    url = str(raw.get("URL") or raw.get("url") or raw.get("share_url") or "")
    if not url:
        return None
    location = str(raw.get("location", "") or "")
    is_remote = True if re.search(r"\bremote\b", location, re.I) else None
    return {
        "company": str(raw.get("company_name") or raw.get("company") or "Unknown"),
        "title": title,
        "location": location,
        "url": url,
        "direct_url": "",
        "date_posted": _posted_text_to_iso(str(raw.get("date") or raw.get("posted_at") or "")),
        "description": str(raw.get("description", "") or "")[:JOBSPY_JD_MAX_CHARS],
        "salary": str(raw.get("salary", "") or ""),
        "job_type": "",
        "is_remote": is_remote,
        "work_arrangement": classify_work_arrangement(location, is_remote=is_remote),
        "ats": "GoogleJobs",
    }


def _http_json(url: str, *, payload: dict | None = None, headers: dict | None = None,
               basic_auth: tuple[str, str] | None = None, timeout: int = 45) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = {"Accept": "application/json"}
    if body is not None:
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    if basic_auth:
        token = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode("utf-8")).decode("ascii")
        req_headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e


def _scrape_google_jobs_serpapi(contexts: list[tuple[str, dict]]) -> tuple[int, list[dict]]:
    api_key = _google_jobs_secret("serpapi_api_key", "SERPAPI_API_KEY")
    if not api_key:
        return 0, []
    jobs_by_id: dict[str, dict] = {}
    raw_rows = 0
    for query, geo in contexts:
        time.sleep(REQUEST_DELAY)
        params = {
            "engine": "google_jobs",
            "q": query,
            "api_key": api_key,
            "hl": "en",
            "gl": _google_jobs_gl(geo),
        }
        loc = str(geo.get("location", "") or "").strip()
        if loc:
            params["location"] = loc
        try:
            data = _http_json("https://serpapi.com/search.json?" + urllib.parse.urlencode(params))
        except Exception as e:
            print(f"  ⚠️  GoogleJobs SerpApi ({query!r}): {e}")
            continue
        if data.get("error"):
            print(f"  ⚠️  GoogleJobs SerpApi ({query!r}): {data.get('error')}")
            continue
        rows = data.get("jobs_results") or []
        if not isinstance(rows, list):
            rows = []
        raw_rows += len(rows)
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            job = _normalize_serpapi_google_job(raw)
            if not job:
                continue
            ident = _job_identity(job.get("url", ""))
            if ident and ident not in jobs_by_id:
                jobs_by_id[ident] = job
    return raw_rows, list(jobs_by_id.values())


OXYLABS_GOOGLE_JOBS_PARSE = {
    "jobs": {
        "_fns": [{"_fn": "xpath", "_args": ["//div[@class='nJXhWc']//ul/li"]}],
        "_items": {
            "job_title": {"_fns": [{"_fn": "xpath_one", "_args": [".//div[@class='BjJfJf PUpOsf']/text()"]}]},
            "company_name": {"_fns": [{"_fn": "xpath_one", "_args": [".//div[@class='vNEEBe']/text()"]}]},
            "location": {"_fns": [{"_fn": "xpath_one", "_args": [".//div[@class='Qk80Jf'][1]/text()"]}]},
            "date": {"_fns": [{"_fn": "xpath_one", "_args": [".//div[@class='PuiEXc']//span[@class='LL4CDc' and contains(@aria-label, 'Posted')]/span/text()"]}]},
            "salary": {"_fns": [{"_fn": "xpath_one", "_args": [".//div[@class='PuiEXc']//div[@class='I2Cbhb bSuYSc']//span[@aria-hidden='true']/text()"]}]},
            "posted_via": {"_fns": [{"_fn": "xpath_one", "_args": [".//div[@class='Qk80Jf'][2]/text()"]}]},
            "URL": {"_fns": [{"_fn": "xpath_one", "_args": [".//div[@data-share-url]/@data-share-url"]}]},
        },
    }
}


def _scrape_google_jobs_oxylabs(contexts: list[tuple[str, dict]]) -> tuple[int, list[dict]]:
    username = _google_jobs_secret("oxylabs_username", "OXYLABS_USERNAME")
    password = _google_jobs_secret("oxylabs_password", "OXYLABS_PASSWORD")
    if not username or not password:
        return 0, []
    jobs_by_id: dict[str, dict] = {}
    raw_rows = 0
    for query, geo in contexts:
        time.sleep(REQUEST_DELAY)
        gl = _google_jobs_gl(geo)
        url = "https://www.google.com/search?" + urllib.parse.urlencode({
            "q": query,
            "ibp": "htl;jobs",
            "hl": "en",
            "gl": gl,
        })
        payload = {
            "source": "google",
            "url": url,
            "geo_location": str(geo.get("location", "") or "United States"),
            "user_agent_type": "desktop",
            "render": "html",
            "parse": True,
            "parsing_instructions": OXYLABS_GOOGLE_JOBS_PARSE,
        }
        try:
            data = _http_json(
                "https://realtime.oxylabs.io/v1/queries",
                payload=payload,
                basic_auth=(username, password),
                timeout=90,
            )
        except Exception as e:
            print(f"  ⚠️  GoogleJobs Oxylabs ({query!r}): {e}")
            continue
        rows = data.get("results", [])
        if isinstance(rows, list) and rows:
            content = rows[0].get("content") if isinstance(rows[0], dict) else None
            rows = content.get("jobs", []) if isinstance(content, dict) else []
        if not isinstance(rows, list):
            rows = []
        raw_rows += len(rows)
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            job = _normalize_oxylabs_google_job(raw)
            if not job:
                continue
            ident = _job_identity(job.get("url", ""))
            if ident and ident not in jobs_by_id:
                jobs_by_id[ident] = job
    return raw_rows, list(jobs_by_id.values())


def _scrape_google_jobs_api_fallback(contexts: list[tuple[str, dict]]) -> tuple[str, int, list[dict]]:
    providers = [
        ("SerpApi", _scrape_google_jobs_serpapi),
        ("Oxylabs", _scrape_google_jobs_oxylabs),
    ]
    any_configured = False
    for label, fn in providers:
        raw_rows, jobs = fn(contexts)
        if raw_rows:
            return label, raw_rows, jobs
        if label == "SerpApi" and _google_jobs_secret("serpapi_api_key", "SERPAPI_API_KEY"):
            any_configured = True
        if label == "Oxylabs" and _google_jobs_secret("oxylabs_username", "OXYLABS_USERNAME") and _google_jobs_secret("oxylabs_password", "OXYLABS_PASSWORD"):
            any_configured = True
    if not any_configured:
        print("  ℹ️  No Google Jobs API fallback configured (set SERPAPI_API_KEY or OXYLABS_USERNAME/OXYLABS_PASSWORD).")
    return "", 0, []


def scrape_google_jobs_recent(hours_old: int | None = None) -> list:
    """Google Jobs roles posted in the last hours_old hours via JobSpy."""
    h = hours_old if hours_old is not None else GOOGLE_JOBS_LOOKBACK_HOURS
    print(f"🔎 Scraping GoogleJobs (last {h}h)...")
    try:
        from jobspy import scrape_jobs as jobspy_scrape
    except ImportError:
        print("  ⚠️  python-jobspy not installed; skipping GoogleJobs")
        return []

    contexts = _google_jobs_query_contexts(h)

    jobs_by_id: dict[str, dict] = {}
    ok_terms = 0
    errored_terms = 0
    raw_rows = 0
    for query, _geo in contexts:
        time.sleep(REQUEST_DELAY)
        try:
            # Google Jobs is the JobSpy exception: it ignores search_term,
            # location, hours_old, and country_indeed. The full role, location,
            # and recency filter must be embedded in google_search_term.
            df = jobspy_scrape(
                site_name="google",
                google_search_term=query,
                results_wanted=30,
                enforce_annual_salary=False,
                proxies=_jobspy_proxies(),
                user_agent=_jobspy_user_agent(),
                verbose=0,
            )
        except Exception as e:
            errored_terms += 1
            print(f"  ⚠️  GoogleJobs ({query!r}): {e}")
            continue
        ok_terms += 1
        raw_rows += _ingest_jobspy_df(df, label="GoogleJobs", jobs_by_id=jobs_by_id)

    jobs = list(jobs_by_id.values())
    print(
        f"  📊 GoogleJobs: {len(contexts)} queries → "
        f"{ok_terms} ok / {errored_terms} errored · {raw_rows} raw, {len(jobs)} matched"
    )
    if raw_rows == 0:
        label, fallback_raw, fallback_jobs = _scrape_google_jobs_api_fallback(contexts)
        if fallback_raw:
            print(
                f"  ✅ GoogleJobs {label} fallback: "
                f"{fallback_raw} raw, {len(fallback_jobs)} matched"
            )
            return fallback_jobs
        prev = _load_prev_jobs(os.path.join(OUTPUT_DIR, "google_jobs.json"))
        print(
            f"  ⛔ GoogleJobs returned 0 rows across all queries; "
            f"preserving previous {len(prev)} result(s)"
        )
        return prev
    return jobs


# ---------------------------------------------------------------------------
# HiringCafe — public SSR search pages (direct-from-employer listings)
# ---------------------------------------------------------------------------

HIRINGCAFE_LOOKBACK_DAYS = 30
HIRINGCAFE_BACKFILL_DAYS = 61
HIRINGCAFE_MAX_PAGES = max(1, int(_cfg("hiring_cafe.max_pages", 3)))
HIRINGCAFE_SEARCH_TERMS = _cfg("search_terms.hiring_cafe", INDEED_SEARCH_TERMS)

HIRINGCAFE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://hiring.cafe/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}


def _deep_first(obj, keys: tuple[str, ...]):
    if isinstance(obj, dict):
        for key in keys:
            val = obj.get(key)
            if val not in (None, "", []):
                return val
        for val in obj.values():
            found = _deep_first(val, keys)
            if found not in (None, "", []):
                return found
    elif isinstance(obj, list):
        for val in obj:
            found = _deep_first(val, keys)
            if found not in (None, "", []):
                return found
    return None


def _hiringcafe_salary(raw: dict) -> str:
    salary = _deep_first(raw, ("salary", "compensation", "salaryRange", "compensationRange"))
    if isinstance(salary, str):
        return re.sub(r"\s+", " ", salary).strip()
    if isinstance(salary, dict):
        return format_salary(
            salary.get("min") or salary.get("minAmount") or salary.get("min_amount") or salary.get("lowEnd"),
            salary.get("max") or salary.get("maxAmount") or salary.get("max_amount") or salary.get("highEnd"),
            salary.get("frequency") or salary.get("interval") or salary.get("period"),
        )
    for prefix, interval in (
        ("yearly", "yearly"),
        ("monthly", "monthly"),
        ("weekly", "weekly"),
        ("hourly", "hourly"),
        ("daily", "daily"),
    ):
        lo = _deep_first(raw, (f"{prefix}_min_compensation", f"{prefix}_min_amount"))
        hi = _deep_first(raw, (f"{prefix}_max_compensation", f"{prefix}_max_amount"))
        if lo or hi:
            return format_salary(lo, hi, interval)
    return ""


def _normalize_hiringcafe_job(raw: dict) -> dict | None:
    title = str(_deep_first(raw, ("title", "jobTitle", "name")) or "")
    if not title or not is_mle_role(title):
        return None
    url = str(_deep_first(raw, ("apply_url", "applyUrl", "url", "jobUrl", "job_url")) or "")
    if not url:
        job_id = str(_deep_first(raw, ("id", "jobId", "uuid")) or "")
        if job_id:
            url = "https://hiring.cafe/job/" + urllib.parse.quote(job_id)
    if not url:
        return None
    company = _deep_first(raw, ("company_name", "companyName", "employer_name", "organization_name"))
    if not company:
        enriched_company = raw.get("enriched_company_data")
        if isinstance(enriched_company, dict):
            company = enriched_company.get("name")
    if not company:
        company = _deep_first(raw, ("company", "employer", "organization", "source"))
    company = str(company or "Unknown")
    location = _deep_first(raw, (
        "location", "formatted_address", "formattedAddress", "formatted_workplace_location",
        "workplace_cities", "workplace_states", "workplace_countries", "city", "region",
    ))
    if isinstance(location, dict):
        location = location.get("formatted_address") or location.get("name") or location.get("city")
    if isinstance(location, list):
        location = ", ".join(str(x.get("formatted_address", x.get("name", x)) if isinstance(x, dict) else x) for x in location[:2])
    desc = _deep_first(raw, ("description_clean", "description", "description_raw", "jobDescription", "requirements_summary"))
    if isinstance(desc, dict):
        desc = json.dumps(desc, ensure_ascii=False)
    if not desc:
        desc_parts = []
        for key in ("requirements_summary", "company_tagline", "role_activities", "technical_tools"):
            val = _deep_first(raw, (key,))
            if isinstance(val, list):
                val = ", ".join(str(x) for x in val)
            if val:
                desc_parts.append(str(val))
        desc = "\n".join(desc_parts)
    job_type = _deep_first(raw, ("commitmentType", "commitment", "jobType", "employmentType"))
    if isinstance(job_type, list):
        job_type = ", ".join(str(x) for x in job_type)
    workplace_type = str(_deep_first(raw, ("workplace_type", "workplaceType")) or "")
    is_remote = _coerce_bool(_deep_first(raw, ("isRemote", "remote"))) or workplace_type.lower() == "remote"
    job = {
        "company": company,
        "title": title,
        "location": str(location or ""),
        "url": url,
        "direct_url": str(_deep_first(raw, ("apply_url", "applyUrl")) or ""),
        "date_posted": str(_deep_first(raw, (
            "date_posted", "datePosted", "created_at", "createdAt", "dateFetched",
            "estimated_publish_date",
        )) or ""),
        "description": re.sub(r"<[^>]+>", " ", str(desc or ""))[:JOBSPY_JD_MAX_CHARS],
        "salary": _hiringcafe_salary(raw),
        "job_type": str(job_type or ""),
        "is_remote": is_remote,
        "work_arrangement": classify_work_arrangement(location, job_type, workplace_type, is_remote=is_remote),
        "ats": "HiringCafe",
    }
    return job


def _hiringcafe_search_slug(term: str) -> str:
    return urllib.parse.quote(re.sub(r"\s+", "-", str(term).strip()))


def _hiringcafe_ssr_hits(term: str, page: int = 0) -> tuple[list[dict], bool]:
    url = "https://hiring.cafe/jobs/" + _hiringcafe_search_slug(term)
    if page > 0:
        url += "?" + urllib.parse.urlencode({"page": page})
    req = urllib.request.Request(url, headers=HIRINGCAFE_HEADERS)
    with urllib.request.urlopen(req, timeout=35) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
    if not m:
        return [], True
    data = json.loads(m.group(1))
    page_props = data.get("props", {}).get("pageProps", {})
    hits = page_props.get("ssrHits", [])
    is_last = bool(page_props.get("ssrIsLastPage", True))
    return (hits if isinstance(hits, list) else []), is_last


def _hiringcafe_recent_enough(job: dict, days: int) -> bool:
    d = str(job.get("date_posted") or "")
    if not d:
        return True
    t = None
    try:
        t = datetime.fromisoformat(d.replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            t = datetime.strptime(d[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return True
    return t >= (datetime.now(timezone.utc).timestamp() - days * 86400)


def scrape_hiringcafe_recent(days: int | None = None) -> list:
    d = days if days is not None else HIRINGCAFE_LOOKBACK_DAYS
    print(f"☕ Scraping HiringCafe (last {d}d)...")
    jobs_by_id: dict[str, dict] = {}
    ok_pages = errored_pages = raw_rows = 0
    for term in HIRINGCAFE_SEARCH_TERMS:
        for page in range(HIRINGCAFE_MAX_PAGES):
            time.sleep(REQUEST_DELAY)
            try:
                batch, is_last = _hiringcafe_ssr_hits(term, page=page)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
                errored_pages += 1
                print(f"  ⚠️  HiringCafe ({term!r} page {page}): {e}")
                break
            ok_pages += 1
            raw_rows += len(batch)
            for raw in batch:
                if not isinstance(raw, dict):
                    continue
                job = _normalize_hiringcafe_job(raw)
                if not job or not _hiringcafe_recent_enough(job, d):
                    continue
                ident = _job_identity(job.get("url", ""))
                if ident and ident not in jobs_by_id:
                    jobs_by_id[ident] = job
            if is_last or not batch:
                break
    jobs = list(jobs_by_id.values())
    print(
        f"  📊 HiringCafe: {ok_pages} page(s) ok / {errored_pages} errored · "
        f"{raw_rows} raw, {len(jobs)} matched"
    )
    if raw_rows == 0:
        prev = _load_prev_jobs(os.path.join(OUTPUT_DIR, "hiringcafe_jobs.json"))
        print(
            f"  ⛔ HiringCafe returned 0 rows across all terms; preserving previous "
            f"{len(prev)} result(s)"
        )
        return prev
    return jobs


# ---------------------------------------------------------------------------
# Academic boards — ScholarshipDB and HigherEdJobs
# ---------------------------------------------------------------------------

SCHOLARSHIPDB_LOOKBACK_DAYS = 30
SCHOLARSHIPDB_BACKFILL_DAYS = 120
SCHOLARSHIPDB_MAX_PAGES = max(1, int(_cfg("scholarshipdb.max_pages", 2)))
SCHOLARSHIPDB_SEARCH_TERMS = _cfg("search_terms.scholarshipdb", INDEED_SEARCH_TERMS)
SCHOLARSHIPDB_LOCATIONS = _cfg("locations.scholarshipdb", [{"location": "United States"}])

HIGHEREDJOBS_LOOKBACK_DAYS = 30
HIGHEREDJOBS_BACKFILL_DAYS = 120
HIGHEREDJOBS_MAX_PAGES = max(1, int(_cfg("higheredjobs.max_pages", 1)))
HIGHEREDJOBS_SEARCH_TERMS = _cfg("search_terms.higheredjobs", INDEED_SEARCH_TERMS)

ACADEMIC_BOARD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _academic_fetch_html(url: str, *, referer: str = "") -> str:
    headers = dict(ACADEMIC_BOARD_HEADERS)
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=35) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _html_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(fragment or ""))
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _html_lines(fragment: str) -> list[str]:
    text = re.sub(r"</?(?:br|p|div|li|tr|td|th|span|h[1-6])\b[^>]*>", "\n", str(fragment or ""), flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = [re.sub(r"\s+", " ", html_lib.unescape(line)).strip() for line in text.splitlines()]
    return [line for line in lines if line]


def _academic_date_to_iso(value: str) -> str:
    raw = re.sub(r"\s+", " ", str(value or "")).strip()
    if not raw:
        return ""
    rel = _posted_text_to_iso(raw)
    if rel != raw:
        return rel
    clean = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw, flags=re.I)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(clean[:30], fmt).date().isoformat()
        except ValueError:
            continue
    return raw


def _academic_recent_enough(date_posted: str, days: int) -> bool:
    if not date_posted:
        return True
    parsed = _parse_posted_at(date_posted)
    if parsed is None:
        return True
    return parsed >= datetime.now(timezone.utc) - timedelta(days=days)


def _infer_academic_job_type(*parts: str) -> str:
    text = " ".join(str(p or "") for p in parts).lower()
    if re.search(r"\b(part[-\s]?time|adjunct|hourly|pool|temporary)\b", text):
        return "Part-time"
    if re.search(r"\b(full[-\s]?time|tenure[-\s]?track|tenured)\b", text):
        return "Full-time"
    return ""


def _location_value(item) -> str:
    if isinstance(item, dict):
        return str(item.get("location") or item.get("name") or "").strip()
    return str(item or "").strip()


def _scholarshipdb_url(term: str, location: str, page: int) -> str:
    params = {"q": term, "l": location}
    if page > 1:
        params["page"] = str(page)
    return "https://scholarshipdb.net/scholarships?" + urllib.parse.urlencode(params)


def _parse_scholarshipdb_results(html: str) -> tuple[list[dict], int]:
    jobs: list[dict] = []
    raw_rows = 0
    for match in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html, flags=re.I | re.S):
        attrs, anchor_html = match.groups()
        if 'data-ev-cat="Search"' not in attrs or "Posts::Page" not in attrs:
            continue
        href_m = re.search(r'href=["\']([^"\']+)["\']', attrs, flags=re.I)
        if not href_m:
            continue
        raw_rows += 1
        title = _html_text(anchor_html)
        if not title:
            continue
        start = html.rfind("<li", 0, match.start())
        end = html.find("</li>", match.end())
        block = html[start:end + 5] if start >= 0 and end >= 0 else html[match.start():match.end() + 1200]
        tail = block.split("</h4>", 1)[1] if "</h4>" in block else block
        company_m = re.search(r"<a\b[^>]*>(.*?)</a>", tail, flags=re.I | re.S)
        location_m = re.search(r'<span\b[^>]*class=["\']text-success["\'][^>]*>(.*?)</span>', tail, flags=re.I | re.S)
        posted_m = re.search(r'<span\b[^>]*class=["\']text-muted["\'][^>]*>(.*?)</span>', tail, flags=re.I | re.S)
        desc_m = re.search(r"<p\b[^>]*>(.*?)</p>", block, flags=re.I | re.S)
        company = _html_text(company_m.group(1)) if company_m else "Unknown"
        location = _html_text(location_m.group(1)) if location_m else ""
        posted = _academic_date_to_iso(_html_text(posted_m.group(1)) if posted_m else "")
        description = _html_text(desc_m.group(1)) if desc_m else _html_text(tail)
        if not is_mle_role_text(title, description):
            continue
        url = urllib.parse.urljoin("https://scholarshipdb.net", html_lib.unescape(href_m.group(1)))
        job_type = _infer_academic_job_type(title, description)
        is_remote = True if _is_remote_location(title, location, description) else None
        jobs.append({
            "company": company,
            "title": title,
            "location": location,
            "url": url,
            "direct_url": "",
            "date_posted": posted,
            "description": description[:JOBSPY_JD_MAX_CHARS],
            "salary": "",
            "job_type": job_type,
            "is_remote": is_remote,
            "work_arrangement": classify_work_arrangement(location, job_type, description, is_remote=is_remote),
            "ats": "ScholarshipDB",
        })
    return jobs, raw_rows


def scrape_scholarshipdb_recent(days: int | None = None) -> list:
    d = days if days is not None else SCHOLARSHIPDB_LOOKBACK_DAYS
    print(f"🎓 Scraping ScholarshipDB (last {d}d)...")
    jobs_by_id: dict[str, dict] = {}
    ok_pages = errored_pages = raw_rows = 0
    locations = [_location_value(x) for x in SCHOLARSHIPDB_LOCATIONS]
    locations = [x for x in locations if x] or ["United States"]
    for term in SCHOLARSHIPDB_SEARCH_TERMS:
        for location in locations:
            for page in range(1, SCHOLARSHIPDB_MAX_PAGES + 1):
                time.sleep(REQUEST_DELAY)
                url = _scholarshipdb_url(str(term), location, page)
                try:
                    html = _academic_fetch_html(url, referer="https://scholarshipdb.net/")
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
                    errored_pages += 1
                    print(f"  ⚠️  ScholarshipDB ({term!r}, {location}, page {page}): {e}")
                    break
                ok_pages += 1
                parsed, count = _parse_scholarshipdb_results(html)
                raw_rows += count
                if count == 0:
                    break
                for job in parsed:
                    if not _academic_recent_enough(job.get("date_posted", ""), d):
                        continue
                    ident = _job_identity(job.get("url", ""))
                    if ident and ident not in jobs_by_id:
                        jobs_by_id[ident] = job
    jobs = list(jobs_by_id.values())
    print(
        f"  📊 ScholarshipDB: {ok_pages} page(s) ok / {errored_pages} errored · "
        f"{raw_rows} raw, {len(jobs)} matched"
    )
    if raw_rows == 0:
        prev = _load_prev_jobs(os.path.join(OUTPUT_DIR, "scholarshipdb_jobs.json"))
        print(
            f"  ⛔ ScholarshipDB returned 0 rows across all terms; preserving previous "
            f"{len(prev)} result(s)"
        )
        return prev
    return jobs


def _higheredjobs_url(term: str, page: int) -> str:
    params = {"Keyword": term}
    if page > 1:
        params["StartRow"] = str((page - 1) * 25 + 1)
    return "https://www.higheredjobs.com/search/advanced_action.cfm?" + urllib.parse.urlencode(params)


def _higheredjobs_blocked(html: str) -> bool:
    text = html.lower()
    return any(token in text for token in (
        "_incapsula_resource",
        "incapsula incident",
        "request unsuccessful",
        "request blocked",
    ))


def _extract_labeled_value(text: str, labels: tuple[str, ...]) -> str:
    stop = (
        "Institution|Company|Employer|Location|Posted|Date Posted|Category|Type|"
        "Job Type|Employment Type|Salary|Application Due|Job Code"
    )
    for label in labels:
        m = re.search(rf"\b{re.escape(label)}\s*:\s*(.*?)(?=\s+(?:{stop})\s*:|$)", text, flags=re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip(" -")
    return ""


def _parse_higheredjobs_results(html: str) -> tuple[list[dict], int]:
    jobs: list[dict] = []
    raw_rows = 0
    seen_urls: set[str] = set()
    for match in re.finditer(r"<a\b([^>]*href=[\"'][^\"']*details\.cfm\?JobCode=[^\"']+[\"'][^>]*)>(.*?)</a>", html, flags=re.I | re.S):
        attrs, anchor_html = match.groups()
        href_m = re.search(r'href=["\']([^"\']+)["\']', attrs, flags=re.I)
        if not href_m:
            continue
        title = _html_text(anchor_html)
        if not title:
            continue
        raw_rows += 1
        url = urllib.parse.urljoin("https://www.higheredjobs.com/search/", html_lib.unescape(href_m.group(1)))
        if url in seen_urls:
            continue
        seen_urls.add(url)
        start = max(0, html.rfind("<div", 0, match.start()))
        if start == 0:
            start = max(0, match.start() - 1800)
        end = html.find("</div>", match.end())
        if end < 0:
            end = min(len(html), match.end() + 2600)
        block = html[start:end + 6]
        block_text = _html_text(block)
        lines = _html_lines(block)
        company = _extract_labeled_value(block_text, ("Institution", "Company", "Employer"))
        location = _extract_labeled_value(block_text, ("Location",))
        posted = _academic_date_to_iso(_extract_labeled_value(block_text, ("Date Posted", "Posted")))
        job_type = _extract_labeled_value(block_text, ("Employment Type", "Job Type", "Type"))
        if not company and title in lines:
            idx = lines.index(title)
            for line in lines[idx + 1:idx + 5]:
                if not re.search(r"\b(location|posted|category|type|salary)\b", line, re.I):
                    company = line
                    break
        if not job_type:
            job_type = _infer_academic_job_type(title, block_text)
        if not is_mle_role_text(title, block_text):
            continue
        is_remote = True if _is_remote_location(title, location, block_text) else None
        jobs.append({
            "company": company or "Unknown",
            "title": title,
            "location": location,
            "url": url,
            "direct_url": "",
            "date_posted": posted,
            "description": block_text[:JOBSPY_JD_MAX_CHARS],
            "salary": _extract_labeled_value(block_text, ("Salary",)),
            "job_type": job_type,
            "is_remote": is_remote,
            "work_arrangement": classify_work_arrangement(location, job_type, block_text, is_remote=is_remote),
            "ats": "HigherEdJobs",
        })
    return jobs, raw_rows


def scrape_higheredjobs_recent(days: int | None = None) -> list:
    d = days if days is not None else HIGHEREDJOBS_LOOKBACK_DAYS
    print(f"🎓 Scraping HigherEdJobs (last {d}d)...")
    jobs_by_id: dict[str, dict] = {}
    ok_pages = errored_pages = raw_rows = 0
    blocked = False
    for term in HIGHEREDJOBS_SEARCH_TERMS:
        for page in range(1, HIGHEREDJOBS_MAX_PAGES + 1):
            time.sleep(REQUEST_DELAY)
            url = _higheredjobs_url(str(term), page)
            try:
                html = _academic_fetch_html(url, referer="https://www.higheredjobs.com/")
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
                errored_pages += 1
                print(f"  ⚠️  HigherEdJobs ({term!r}, page {page}): {e}")
                break
            if _higheredjobs_blocked(html):
                blocked = True
                print(f"  ⛔ HigherEdJobs blocked automated access for {term!r}")
                break
            ok_pages += 1
            parsed, count = _parse_higheredjobs_results(html)
            raw_rows += count
            if count == 0:
                break
            for job in parsed:
                if not _academic_recent_enough(job.get("date_posted", ""), d):
                    continue
                ident = _job_identity(job.get("url", ""))
                if ident and ident not in jobs_by_id:
                    jobs_by_id[ident] = job
        if blocked:
            break
    jobs = list(jobs_by_id.values())
    print(
        f"  📊 HigherEdJobs: {ok_pages} page(s) ok / {errored_pages} errored · "
        f"{raw_rows} raw, {len(jobs)} matched"
    )
    if raw_rows == 0:
        prev = _load_prev_jobs(os.path.join(OUTPUT_DIR, "higheredjobs_jobs.json"))
        print(
            f"  ⛔ HigherEdJobs returned 0 rows across all terms; preserving previous "
            f"{len(prev)} result(s)"
        )
        return prev
    return jobs


# ---------------------------------------------------------------------------
# CalCareers (California state civil-service jobs) — calcareers.ca.gov
#
# CalCareers is an ASP.NET WebForms portal (DevExpress) with NO public JSON
# API: search state lives in a server-side session keyed by ASP.NET_SessionId.
# So we (1) GET the results page to seed a session + capture the hidden
# __VIEWSTATE/__EVENTVALIDATION fields, (2) auto-discover the keyword text box
# and the submit control from the live HTML (the ctl00$... names aren't stable
# or documented), (3) POST the search, and (4) parse JobPosting links from the
# returned HTML. Everything is wrapped so any failure is non-fatal.
#
# NOTE: this path could NOT be verified from the dev network (the site sits
# behind a WAF that times out there); it is written to run on GitHub Actions'
# clean egress. If the first GH run logs 0 rows, the result-card parsing in
# _parse_calcareers_results() likely needs a selector tweak. CA state
# departments (OEHHA, DTSC, CARB, Caltrans, Water Boards) also surface via the
# LinkedIn priority-employer allowlist as a backstop.
# ---------------------------------------------------------------------------

CALCAREERS_BASE = "https://www.calcareers.ca.gov"
# Apex domain for the search postback (per the OpenPostings calcareers module).
CALCAREERS_SEARCH_URL = "https://calcareers.ca.gov/CalHRPublic/Search/JobSearchResults.aspx"
CALCAREERS_TIMEOUT = 30

# Broad CalCareers queries; titles are still gated by is_mle_role() afterward.
CALCAREERS_TERMS = [
    "toxicologist", "environmental scientist", "risk assessment",
    "exposure", "water quality", "microplastics", "hazard",
    "environmental health", "ecologist",
]


def _calcareers_opener():
    """A urllib opener with its own cookie jar so the ASP.NET session set on the
    seeding GET is sent back on the search POST."""
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _hidden_inputs(html: str) -> dict:
    """All <input type=hidden> name→value pairs (the ASP.NET viewstate set)."""
    fields = {}
    for tag in re.findall(r'<input\b[^>]*type=["\']hidden["\'][^>]*>', html, re.I):
        n = re.search(r'\bname=["\']([^"\']+)["\']', tag)
        v = re.search(r'\bvalue=["\']([^"\']*)["\']', tag)
        if n:
            fields[n.group(1)] = (v.group(1) if v else "")
    return fields


# CalCareers renders each result as labeled "col-xs-6 job-details" divs
# (Working Title / Job Control / Department / Location / Publish Date) followed
# by the posting link. Pattern adapted from the OpenPostings calcareers module.
CALCAREERS_CARD_RE = re.compile(
    r'Working Title:\s*</div>\s*<div class="col-xs-6 job-details">\s*<span[^>]*>(.*?)</span>'
    r'[\s\S]*?Job Control:\s*</div>\s*<div class="col-xs-6 job-details">\s*(\d+)\s*</div>'
    r'[\s\S]*?Department:\s*</div>\s*<div class="col-xs-6 job-details">\s*(.*?)\s*</div>'
    r'[\s\S]*?Location:\s*</div>\s*<div class="col-xs-6 job-details">\s*(.*?)\s*</div>'
    r'[\s\S]*?Publish Date:\s*</div>\s*<div class="col-xs-6 job-details">\s*<time[^>]*>\s*([^<]+)\s*</time>'
    r'[\s\S]*?href="(https://www\.calcareers\.ca\.gov/CalHrPublic/Jobs/JobPosting\.aspx\?JobControlId=\d+)"',
    re.I,
)


def _parse_calcareers_results(html: str) -> list[dict]:
    import html as html_mod

    def _clean(s):
        return re.sub(r'\s+', ' ', html_mod.unescape(re.sub(r'<[^>]+>', ' ', s or ''))).strip()

    jobs: list[dict] = []
    for m in CALCAREERS_CARD_RE.finditer(html):
        title, _jc, dept, location, pub_date, url = m.groups()
        date = ""
        dm = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', pub_date or "")
        if dm:
            date = f"{dm.group(3)}-{int(dm.group(1)):02d}-{int(dm.group(2)):02d}"
        # The card carries a "Salary Range:" field (e.g. "$4418.00 - $9321.00",
        # usually monthly for CA state). Pull it from the matched card span.
        card = html[m.start():m.end()]
        sal_m = re.search(
            r'Salary Range:\s*</div>\s*<div[^>]*>([\s\S]*?)</div>', card, re.I)
        salary = ""
        if sal_m:
            sm = re.search(
                r'\$[\d,]+(?:\.\d{2})?\s*-\s*\$[\d,]+(?:\.\d{2})?(?:\s*(?:per|/)\s*\w+)?',
                _clean(sal_m.group(1)))
            salary = sm.group(0).strip() if sm else ""
        jobs.append({
            "company": _clean(dept) or "State of California",
            "title": _clean(title),
            "location": _clean(location) or "California",
            "url": _clean(url),
            "date_posted": date,
            "salary": salary,
            "ats": "CalCareers",
        })
    return jobs


def _calcareers_detail_value(html: str, label: str) -> str:
    import html as html_mod

    m = re.search(
        rf'<strong>\s*{re.escape(label)}:\s*</strong>\s*</div>\s*'
        r'<div[^>]*>\s*<span[^>]*>([\s\S]*?)</span>',
        html,
        re.I,
    )
    if not m:
        return ""
    return re.sub(r'\s+', ' ', html_mod.unescape(re.sub(r'<[^>]+>', ' ', m.group(1)))).strip()


def _calcareers_posting_details(url: str) -> dict:
    try:
        html = fetch(url)
    except Exception:
        return {}
    if not html:
        return {}
    return {
        "work_location": _calcareers_detail_value(html, "Work Location"),
        "telework": _calcareers_detail_value(html, "Telework"),
        "job_type": _calcareers_detail_value(html, "Job Type"),
    }


def _calcareers_payload(hidden: dict, event_target: str, keyword: str) -> dict:
    """ASP.NET postback body that actually fires the search (the missing piece
    was __EVENTTARGET=btnSearch + the real keyword field name)."""
    payload = dict(hidden)
    payload["__EVENTTARGET"] = event_target
    payload["__EVENTARGUMENT"] = ""
    payload["ctl00$cphMainContent$txtKeyword"] = keyword
    payload["ctl00$cphMainContent$hdnInit"] = "true"
    payload.setdefault("ctl00$cphMainContent$chkExactWordMatch", "")
    payload.setdefault("ctl00$hdnShowHeaderPadding", "1")
    payload.setdefault("ctl00$ucSessionTimeoutDialog$tmrCountdown", "1200")
    return payload


def scrape_calcareers_recent() -> list:
    """CalCareers env/tox roles via the ASP.NET search postback (method proven by
    the OpenPostings project). Fully guarded — returns previous results on any
    failure so a flaky run never nukes the dashboard's CalCareers column."""
    print("🏛  Scraping CalCareers (California state jobs)...")
    headers = {
        **HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": CALCAREERS_SEARCH_URL,
    }
    jobs_by_url: dict[str, dict] = {}
    parsed_total = 0
    reached = False
    for term in CALCAREERS_TERMS:
        time.sleep(REQUEST_DELAY)
        try:
            opener = _calcareers_opener()  # fresh session/viewstate per keyword
            seed = opener.open(Request(CALCAREERS_SEARCH_URL, headers=HEADERS),
                               timeout=CALCAREERS_TIMEOUT).read().decode("utf-8", "ignore")
            reached = True
            hidden = _hidden_inputs(seed)
            if not hidden:
                continue
            data = urllib.parse.urlencode(
                _calcareers_payload(hidden, "ctl00$cphMainContent$btnSearch", term)).encode()
            res_html = opener.open(Request(CALCAREERS_SEARCH_URL, data=data, headers=headers),
                                   timeout=CALCAREERS_TIMEOUT).read().decode("utf-8", "ignore")
        except (URLError, TimeoutError, OSError) as e:
            print(f"  ⚠️  CalCareers ({term!r}): {e}")
            continue
        for job in _parse_calcareers_results(res_html):
            parsed_total += 1
            if is_mle_role(job["title"]) and job["url"] not in jobs_by_url:
                time.sleep(REQUEST_DELAY)
                details = _calcareers_posting_details(job["url"])
                if details.get("work_location"):
                    job["location"] = details["work_location"]
                if details.get("telework"):
                    job["telework"] = details["telework"]
                if details.get("job_type"):
                    job["job_type"] = details["job_type"]
                _ensure_work_arrangement(job)
                jobs_by_url[job["url"]] = job

    jobs = list(jobs_by_url.values())
    print(f"  ✅ CalCareers: {len(jobs)} on-target role(s) (from {parsed_total} parsed)")
    if not jobs and (parsed_total == 0 or not reached):
        # No data — site unreachable or parser/search mismatch. Preserve the
        # previous column rather than blanking it.
        return _load_prev_jobs(os.path.join(OUTPUT_DIR, "calcareers_jobs.json"))
    return jobs


def save_calcareers_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="calcareers_jobs",
        title=f"🏛 CalCareers — California State {PROFILE_LABEL} Roles",
        subtitle="calcareers.ca.gov · California state civil service",
        accent="#b45309",
        empty_message="No new CalCareers roles since the last run.",
        window_label="current CalCareers postings",
    )


# ---------------------------------------------------------------------------
# USAJOBS — federal jobs (EPA, NOAA, USGS, FDA, NIEHS, CDC, DOI, ...)
#
# Uses the public usajobs.gov website search (NO API key): GET the Results page
# to seed a session cookie, then POST /Search/ExecuteSearch per keyword. Returns
# federal env/tox roles WITH salary (SalaryDisplay). Verified working from a
# plain client. Source surfaced via the OpenPostings ATS catalog
# (https://github.com/Masterjx9/OpenPostings), which lists usajobs among 80+
# providers; we query the official public endpoint directly.
# ---------------------------------------------------------------------------

USAJOBS_RESULTS_URL = "https://www.usajobs.gov/Search/Results?hp=public&s=startdate&sd=desc&p=1"
USAJOBS_SEARCH_URL = "https://www.usajobs.gov/Search/ExecuteSearch"
USAJOBS_TERMS = [
    "toxicologist", "environmental protection specialist", "environmental health",
    "ecotoxicology", "exposure science", "risk assessment",
    "environmental scientist", "microplastics", "ecologist",
]
USAJOBS_RESULTS_PER_PAGE = 50


def _usajobs_date(date_display: str) -> str:
    """"Open 06/13/2026 to 06/27/2026" → "2026-06-13" (the open date)."""
    m = re.search(r'(\d{2})/(\d{2})/(\d{4})', date_display or "")
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else ""


def scrape_usajobs_recent() -> list:
    """Federal env/tox roles from usajobs.gov (no API key). Guarded — returns the
    previous results on any failure so a flaky run never blanks the column."""
    print("🇺🇸 Scraping USAJOBS (federal env/tox roles)...")
    jobs_by_url: dict[str, dict] = {}
    headers = {
        **HEADERS,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.usajobs.gov",
        "Referer": USAJOBS_RESULTS_URL,
    }
    try:
        opener = _calcareers_opener()  # cookie jar — the POST needs the session
        opener.open(Request(USAJOBS_RESULTS_URL, headers=HEADERS), timeout=25).read()
        for term in USAJOBS_TERMS:
            time.sleep(REQUEST_DELAY)
            body = json.dumps({
                "Keyword": term, "HiringPath": ["public"],
                "SortField": "startdate", "SortDirection": "desc",
                "Page": "1", "ResultsPerPage": USAJOBS_RESULTS_PER_PAGE,
            }).encode()
            try:
                raw = opener.open(Request(USAJOBS_SEARCH_URL, data=body, headers=headers),
                                  timeout=25).read().decode("utf-8", "ignore")
                payload = json.loads(raw)
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                print(f"  ⚠️  USAJOBS ({term!r}): {e}")
                continue
            for job in payload.get("Jobs", []):
                title = (job.get("Title") or "").strip()
                if not is_mle_role(title):
                    continue
                uri = (job.get("PositionURI") or "").replace(":443", "")
                if not uri and job.get("DocumentID"):
                    uri = f"https://www.usajobs.gov/job/{job['DocumentID']}"
                if not uri or uri in jobs_by_url:
                    continue
                jobs_by_url[uri] = {
                    "company": (job.get("Agency") or job.get("Department") or "Federal Government").strip(),
                    "title": title,
                    "location": (job.get("LocationName") or "").strip(),
                    "url": uri,
                    "date_posted": _usajobs_date(job.get("DateDisplay", "")),
                    "salary": (job.get("SalaryDisplay") or "").strip(),
                    "ats": "USAJOBS",
                }
    except (URLError, TimeoutError, OSError, ValueError) as e:
        print(f"  ⛔ USAJOBS unreachable ({e}); preserving previous results")
        return _load_prev_jobs(os.path.join(OUTPUT_DIR, "usajobs_jobs.json"))

    jobs = list(jobs_by_url.values())
    print(f"  ✅ USAJOBS: {len(jobs)} federal role(s)")
    if not jobs:
        return _load_prev_jobs(os.path.join(OUTPUT_DIR, "usajobs_jobs.json"))
    return jobs


def save_usajobs_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="usajobs_jobs",
        title=f"🇺🇸 USAJOBS — Federal {PROFILE_LABEL} Roles",
        subtitle="usajobs.gov · federal agencies",
        accent="#1d4ed8",
        empty_message="No new federal roles since the last run.",
        window_label="current USAJOBS postings",
    )


# ---------------------------------------------------------------------------
# GovernmentJobs.com / NEOGOV — state, county & city agencies (air & water
# districts, county environmental health, etc.). HTML search; keyword-filterable.
# Post-filtered to CA/OR (the board is nationwide). Source from the OpenPostings
# ATS catalog (https://github.com/Masterjx9/OpenPostings).
# ---------------------------------------------------------------------------

GOVERNMENTJOBS_BASE = "https://www.governmentjobs.com"
GOVERNMENTJOBS_TERMS = [
    "toxicologist", "environmental scientist", "environmental health",
    "risk assessment", "water quality", "air quality",
    "hazardous materials", "environmental specialist",
]
GOVERNMENTJOBS_DAYS = 21
GOVERNMENTJOBS_BACKFILL_DAYS = 60  # one-time historical backfill window
GOVERNMENTJOBS_PAGES = 2


def scrape_governmentjobs_recent(days: int | None = None) -> list:
    """State/local-gov env roles via governmentjobs.com, filtered to CA/OR."""
    d = days if days is not None else GOVERNMENTJOBS_DAYS
    print(f"🏛  Scraping GovernmentJobs/NEOGOV (last {d} days)...")
    item_re = re.compile(r'<li[^>]*class=["\'][^"\']*\bjob-item\b[^"\']*["\'][^>]*>([\s\S]*?)</li>', re.I)
    link_re = re.compile(r'<a[^>]*class=["\'][^"\']*\bjob-details-link\b[^"\']*["\'][^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', re.I)
    org_re = re.compile(r'<div[^>]*class=["\'][^"\']*\bjob-organization\b[^"\']*["\'][^>]*>([\s\S]*?)</div>', re.I)
    loc_re = re.compile(r'<span[^>]*class=["\'][^"\']*\bjob-location\b[^"\']*["\'][^>]*>([\s\S]*?)</span>', re.I)
    import html as html_mod

    def _clean(s):
        return re.sub(r'\s+', ' ', html_mod.unescape(re.sub(r'<[^>]+>', ' ', s or ''))).strip()

    jobs_by_url: dict[str, dict] = {}
    raw_items = 0
    for term in GOVERNMENTJOBS_TERMS:
        for page in range(1, GOVERNMENTJOBS_PAGES + 1):
            time.sleep(REQUEST_DELAY)
            url = (f"{GOVERNMENTJOBS_BASE}/jobs?keyword={urllib.parse.quote(term)}"
                   f"&daysposted={d}&isFiltered=true&page={page}")
            page_html = fetch(url)
            items = item_re.findall(page_html)
            raw_items += len(items)
            if not items:
                break
            for it in items:
                lk = link_re.search(it)
                if not lk:
                    continue
                title = _clean(lk.group(2))
                if not is_mle_role(title):
                    continue
                loc_m = loc_re.search(it)
                location = _clean(loc_m.group(1)) if loc_m else ""
                if not is_target_location(location):
                    continue   # board is nationwide — keep CA/OR only
                href = re.sub(r'\s+', '', lk.group(1))
                job_url = href if href.startswith("http") else GOVERNMENTJOBS_BASE + "/" + href.lstrip("/")
                if job_url in jobs_by_url:
                    continue
                org_m = org_re.search(it)
                # NEOGOV cards carry pay inline, e.g. "$67,296.24 - $100,098.72
                # Annually" (or Monthly/Hourly) — the dashboard annualizes it.
                sal_m = re.search(
                    r'\$[\d,]+(?:\.\d{2})?\s*-\s*\$[\d,]+(?:\.\d{2})?'
                    r'\s*(?:Annually|Monthly|Hourly|Biweekly|Bi-Weekly|Weekly|Daily)?',
                    _clean(it), re.I)
                jobs_by_url[job_url] = {
                    "company": _clean(org_m.group(1)) if org_m else "Government Agency",
                    "title": title,
                    "location": location,
                    "url": job_url,
                    "date_posted": "",
                    "salary": sal_m.group(0).strip() if sal_m else "",
                    "ats": "NEOGOV",
                }
    jobs = list(jobs_by_url.values())
    print(f"  ✅ NEOGOV: {len(jobs)} CA/OR role(s) (from {raw_items} scanned)")
    if not jobs and raw_items == 0:
        return _load_prev_jobs(os.path.join(OUTPUT_DIR, "governmentjobs_jobs.json"))
    return jobs


def save_governmentjobs_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="governmentjobs_jobs",
        title=f"🏛 NEOGOV — State & Local Government {PROFILE_LABEL} Roles",
        subtitle="governmentjobs.com · state & local agencies",
        accent="#0e7490",
        empty_message="No new state/local-gov roles since the last run.",
        window_label="recent GovernmentJobs postings",
    )


# ---------------------------------------------------------------------------
# CalOpps — California local-agency jobs (cities, counties, special districts,
# water associations). HTML list; CA-only, so no geo filter — just title filter.
# Source from the OpenPostings ATS catalog.
# ---------------------------------------------------------------------------

CALOPPS_LIST_URL = "https://www.calopps.org/job-search-list"
CALOPPS_MAX_PAGES = 10


def _calopps_company(href: str) -> str:
    m = re.match(r'/?([^/]+)/', href or "")
    if not m:
        return "California Agency"
    return m.group(1).replace('-', ' ').title()


def scrape_calopps_recent() -> list:
    """California local-agency env/tox roles from calopps.org (CA-only board)."""
    print("🏛  Scraping CalOpps (California local agencies)...")
    import html as html_mod
    row_re = re.compile(r'<tr[^>]*>([\s\S]*?)</tr>', re.I)
    cell_re = re.compile(r'<td[^>]*>([\s\S]*?)</td>', re.I)
    link_re = re.compile(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', re.I)

    def _clean(s):
        return re.sub(r'\s+', ' ', html_mod.unescape(re.sub(r'<[^>]+>', ' ', s or ''))).strip()

    jobs_by_url: dict[str, dict] = {}
    scanned = 0
    for page in range(CALOPPS_MAX_PAGES):
        time.sleep(REQUEST_DELAY)
        url = CALOPPS_LIST_URL + (f"?page={page}" if page else "")
        page_html = fetch(url)
        rows = [r for r in row_re.findall(page_html) if "views-field-label" in r.lower()]
        if not rows:
            break
        for r in rows:
            cells = cell_re.findall(r)
            if len(cells) < 5:
                continue
            lk = link_re.search(cells[0])
            if not lk:
                continue
            scanned += 1
            title = _clean(lk.group(2))
            if not is_mle_role(title):
                continue
            href = html_mod.unescape(lk.group(1).strip())
            job_url = href if href.startswith("http") else "https://www.calopps.org" + ("" if href.startswith("/") else "/") + href
            if job_url in jobs_by_url:
                continue
            jobs_by_url[job_url] = {
                "company": _calopps_company(href),
                "title": title,
                "location": _clean(cells[1]) or "California",
                "url": job_url,
                "date_posted": "",
                "salary": "",
                "ats": "CalOpps",
            }
    jobs = list(jobs_by_url.values())
    # Salary is on the posting page (e.g. "Salary $9,272.00-$11,275.00 Monthly"),
    # not the list — backfill it (few matches, so one fetch each is cheap).
    for job in jobs:
        time.sleep(REQUEST_DELAY)
        try:
            ph = fetch(job["url"])
        except (URLError, TimeoutError, OSError):
            continue
        sm = re.search(
            r'Salary\s*(\$[\d,]+(?:\.\d{2})?\s*-\s*\$[\d,]+(?:\.\d{2})?'
            r'\s*(?:Monthly|Annually|Hourly|Biweekly|Bi-Weekly|Weekly|Daily)?)',
            re.sub(r'<[^>]+>', ' ', ph), re.I)
        if sm:
            job["salary"] = re.sub(r'\s+', ' ', sm.group(1)).strip()
    print(f"  ✅ CalOpps: {len(jobs)} env/tox role(s) (from {scanned} scanned)")
    if not jobs and scanned == 0:
        return _load_prev_jobs(os.path.join(OUTPUT_DIR, "calopps_jobs.json"))
    return jobs


def save_calopps_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="calopps_jobs",
        title=f"🏛 CalOpps — California Local-Agency {PROFILE_LABEL} Roles",
        subtitle="calopps.org · CA cities, counties, special & water districts",
        accent="#15803d",
        empty_message="No new CalOpps roles since the last run.",
        window_label="recent CalOpps postings",
    )


# ---------------------------------------------------------------------------
# CSU Careers — California State University jobs (PageUp listing)
# ---------------------------------------------------------------------------

CSUCAREERS_BASE = "https://csucareers.calstate.edu"
CSUCAREERS_LISTING_URL = CSUCAREERS_BASE + "/en-us/listing/"
CSUCAREERS_PAGE_ITEMS = 100
CSUCAREERS_MAX_PAGES = int(_cfg("csucareers.max_pages", 30))


def _fetch_csucareers(url: str) -> str:
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if curl:
        try:
            got = subprocess.run(
                [
                    curl,
                    "-4",
                    "-L",
                    "--retry", "3",
                    "--retry-delay", "2",
                    "--retry-all-errors",
                    "--http1.1",
                    "--max-time", "60",
                    "--tlsv1.2",
                    "-A", HEADERS["User-Agent"],
                    url,
                ],
                check=False,
                capture_output=True,
                timeout=70,
            )
            if got.stdout:
                html = got.stdout.decode("utf-8", errors="replace")
                if 'class="job-link"' in html or "search-results-content" in html:
                    return html
        except (OSError, subprocess.SubprocessError):
            pass
    return fetch(url, retries=1, _base_wait=8.0)


def _parse_csucareers_listing(html: str) -> list[dict]:
    import html as html_mod

    def _clean(s):
        return re.sub(r'\s+', ' ', html_mod.unescape(re.sub(r'<[^>]+>', ' ', s or ''))).strip()

    rows = re.findall(r'<tr[^>]*>([\s\S]*?)</tr>', html, re.I)
    jobs: list[dict] = []
    i = 0
    while i < len(rows):
        row = rows[i]
        link = re.search(
            r'<a[^>]*class=["\'][^"\']*\bjob-link\b[^"\']*["\'][^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
            row,
            re.I,
        )
        if not link:
            i += 1
            continue
        href = html_mod.unescape(link.group(1).strip())
        url = href if href.startswith("http") else CSUCAREERS_BASE + href
        title = _clean(link.group(2))
        loc_m = re.search(r'<span[^>]*class=["\'][^"\']*\blocation\b[^"\']*["\'][^>]*>([\s\S]*?)</span>', row, re.I)
        close_m = re.search(r'<time[^>]*datetime=["\']([^"\']+)["\']', row, re.I)
        description = ""
        if i + 1 < len(rows) and re.search(r'\bclass=["\'][^"\']*\bsummary\b', rows[i + 1], re.I):
            description = _clean(rows[i + 1])
            i += 1
        jobs.append({
            "company": "California State University",
            "title": title,
            "location": _clean(loc_m.group(1)) if loc_m else "California",
            "url": url,
            "direct_url": url,
            "date_posted": "",
            "closing_date": (close_m.group(1)[:10] if close_m else ""),
            "description": description[:JOBSPY_JD_MAX_CHARS],
            "salary": "",
            "ats": "CSUCareers",
        })
        i += 1
    return jobs


def scrape_csucareers_recent() -> list:
    """CSU PageUp listing, title-filtered to configured target roles."""
    print("🎓 Scraping CSU Careers (csucareers.calstate.edu)...")
    jobs_by_url: dict[str, dict] = {}
    raw_rows = 0
    reached = False
    consecutive_failures = 0
    complete = True
    for page in range(1, CSUCAREERS_MAX_PAGES + 1):
        html = ""
        url = (
            f"{CSUCAREERS_LISTING_URL}?page={page}"
            f"&page-items={CSUCAREERS_PAGE_ITEMS}"
        )
        for attempt in range(3):
            time.sleep(REQUEST_DELAY)
            html = _fetch_csucareers(url)
            if html:
                break
            time.sleep(1 + attempt)
        if not html:
            print(f"  ⚠️  CSU Careers page {page}: no response")
            consecutive_failures += 1
            if consecutive_failures >= 3:
                complete = False
                break
            continue
        consecutive_failures = 0
        reached = True
        batch = _parse_csucareers_listing(html)
        raw_rows += len(batch)
        if not batch:
            break
        for job in batch:
            if not is_mle_role_text(job.get("title", ""), job.get("description", "")):
                continue
            if job["url"] not in jobs_by_url:
                jobs_by_url[job["url"]] = _ensure_work_arrangement(job)
        if not re.search(r'class=["\'][^"\']*\bmore-link\b', html, re.I):
            break
    jobs = list(jobs_by_url.values())
    print(f"  ✅ CSU Careers: {len(jobs)} on-target role(s) (from {raw_rows} listed)")
    if not complete:
        prev = _load_prev_jobs(os.path.join(OUTPUT_DIR, "csucareers_jobs.json"))
        print(
            f"  ⛔ CSU Careers scan incomplete; preserving previous "
            f"{len(prev)} result(s)"
        )
        return prev
    if not jobs and (raw_rows == 0 or not reached):
        return _load_prev_jobs(os.path.join(OUTPUT_DIR, "csucareers_jobs.json"))
    return jobs


def save_csucareers_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="csucareers_jobs",
        title=f"🎓 CSU Careers — California State University {PROFILE_LABEL} Roles",
        subtitle="csucareers.calstate.edu · CSU systemwide PageUp listing",
        accent="#1e40af",
        empty_message="No new CSU Careers roles since the last run.",
        window_label="current CSU Careers postings",
    )


def format_salary(min_amount, max_amount, interval) -> str:
    """
    Display string for jobspy's Indeed pay fields, e.g. "$150k–$190k/yr" or
    "$62.50/hr". Returns "" when neither bound is present.
    """
    def _num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None

    def _fmt(n):
        if n >= 10000:
            return f"${round(n / 1000)}k"
        if n == int(n):
            return f"${int(n)}"
        return f"${n:.2f}"

    lo, hi = _num(min_amount), _num(max_amount)
    if lo is None and hi is None:
        return ""
    suffix = {"yearly": "/yr", "hourly": "/hr", "monthly": "/mo",
              "weekly": "/wk", "daily": "/day"}.get(str(interval or "").lower(), "")
    if lo is not None and hi is not None and lo != hi:
        return f"{_fmt(lo)}–{_fmt(hi)}{suffix}"
    return f"{_fmt(lo if lo is not None else hi)}{suffix}"


def _iso_to_ts(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def _job_identity(url: str) -> str:
    """
    Stable identity string for a posting URL, used to dedupe across runs.

    LinkedIn → numeric posting ID (LinkedIn appends tracking params that vary
    run-to-run). Indeed → the `jk=` token (Indeed appends `indpubnum` and other
    tracking that varies). Other ATS (Greenhouse, Workday, Phenom) → URL with
    query string and trailing slash stripped.
    """
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower().removeprefix("www.")
        path = urllib.parse.unquote(parsed.path or "").rstrip("/").lower()
        qs = urllib.parse.parse_qs(parsed.query)

        def q(name: str) -> str:
            vals = qs.get(name) or []
            return vals[0] if vals else ""

        m = re.search(r"/jobs/view/(\d+)", path)
        if m:
            return f"linkedin:{m.group(1)}"
        if q("jk"):
            return f"indeed:{q('jk')}"
        if q("lvk"):
            return f"ziprecruiter:{q('lvk')}"
        if q("jid"):
            return f"ziprecruiter:{q('jid')}"
        if q("gh_jid"):
            return f"greenhouse:{q('gh_jid')}"
        m = re.search(r"/jobs/(\d+)", path)
        if m and re.search(r"greenhouse|silkroad", host):
            return f"{host}:{m.group(1)}"
        m = re.search(r"/requisitions/job/([a-z0-9_-]+)", path)
        if m:
            return f"{host}:{m.group(1)}"
        m = re.search(r"/jobs/(\d+)", path)
        if m and "governmentjobs" in host:
            return f"{host}:{m.group(1)}"
        if q("id") and host.endswith("talent.com"):
            return f"talent:{q('id')}"
        if q("jl") and "glassdoor" in host:
            return f"glassdoor:{q('jl')}"
        if host:
            return f"url:{host}{path}"
    except Exception:
        pass
    m = re.search(r'/jobs/view/(\d+)', url)
    if m:
        return f"linkedin:{m.group(1)}"
    m = re.search(r'[?&]jk=([a-zA-Z0-9]+)', url)
    if m:
        return f"indeed:{m.group(1)}"
    m = re.search(r'[?&]lvk=([a-zA-Z0-9._-]+)', url)
    if m:
        return f"ziprecruiter:{m.group(1)}"
    return url.split("?")[0].rstrip("/").lower()


_JOB_TITLE_STOP = set(
    "a an and at by for in of on the to with job jobs opening openings local recruitment".split()
)
_JOB_TITLE_LEVEL = set(
    "i ii iii iv v vi 1 2 3 4 5 one two three four five phd ph d".split()
)
_JOB_LOC_STOP = set("united states usa us california ca greater metropolitan metro area other".split())
_JOB_TEXT_STOP = set(
    "a an and are as at be by for from in into is it of on or our the this to we with you your".split()
)


def _dedupe_tokens(text: str) -> list[str]:
    text = (text or "").lower()
    text = re.sub(r"\bsr\.?\b", " senior ", text)
    text = re.sub(r"\bjr\.?\b", " junior ", text)
    text = text.replace("&", " and ")
    text = re.sub(r"\bph\.?\s*d\.?\b", " phd ", text)
    return [t for t in re.sub(r"[^a-z0-9]+", " ", text).split() if t]


def _title_tokens(title: str) -> list[str]:
    return [t for t in _dedupe_tokens(title) if t not in _JOB_TITLE_STOP]


def _location_tokens(location: str) -> list[str]:
    return [
        t for t in _dedupe_tokens(location)
        if t not in _JOB_LOC_STOP and not t.isdigit()
    ]


def _content_tokens(text: str) -> list[str]:
    return [
        t for t in _dedupe_tokens(text)
        if len(t) > 2 and t not in _JOB_TEXT_STOP
    ]


def _jaccard(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)


def _norm_company(company: str) -> str:
    company = (company or "").lower()
    company = re.sub(
        r"\b(inc|llc|llp|ltd|corp|corporation|company|companies|co|group|the|of|and|department|dept|agency|division)\b",
        " ",
        company,
    )
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", company)).strip()


def _company_match(a: str, b: str) -> bool:
    a, b = _norm_company(a), _norm_company(b)
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 5 and len(b) >= 5 and (a in b or b in a)


def _location_overlap(a: str, b: str) -> bool:
    at, bt = _location_tokens(a), _location_tokens(b)
    if not at or not bt:
        return False
    an, bn = " ".join(at), " ".join(bt)
    if an == bn:
        return True
    if len(an) >= 4 and len(bn) >= 4 and (an in bn or bn in an):
        return True
    bset = set(bt)
    return any(len(t) >= 4 and t in bset for t in at)


def _title_match(a: str, b: str) -> bool:
    at, bt = _title_tokens(a), _title_tokens(b)
    if not at or not bt:
        return False
    if at == bt:
        return True
    score = _jaccard(at, bt)
    if score >= 0.86 and len(set(at) & set(bt)) >= 2:
        return True
    aset, bset = set(at), set(bt)
    a_extra = [t for t in at if t not in bset]
    b_extra = [t for t in bt if t not in aset]
    return min(len(at), len(bt)) >= 2 and (
        bool(a_extra) and not b_extra and all(t in _JOB_TITLE_LEVEL for t in a_extra)
        or bool(b_extra) and not a_extra and all(t in _JOB_TITLE_LEVEL for t in b_extra)
    )


def _description_overlap(a: dict, b: dict) -> bool:
    ad = (a.get("description") or "")[:3000]
    bd = (b.get("description") or "")[:3000]
    if len(ad) < 220 or len(bd) < 220:
        return False
    return _jaccard(_content_tokens(ad), _content_tokens(bd)) >= 0.82


def _job_urls(job: dict) -> list[str]:
    urls = []
    if job.get("url"):
        urls.append(job["url"])
    urls.extend(job.get("duplicate_urls") or [])
    return list(dict.fromkeys(u for u in urls if u))


def _same_job(a: dict, b: dict) -> bool:
    a_ids = {_job_identity(u) for u in _job_urls(a)}
    b_ids = {_job_identity(u) for u in _job_urls(b)}
    if a_ids & b_ids:
        return True
    if not _company_match(a.get("company", ""), b.get("company", "")):
        return False
    if not _title_match(a.get("title", ""), b.get("title", "")):
        return False
    if _location_overlap(a.get("location", ""), b.get("location", "")):
        return True
    a_loc_missing = not (a.get("location") or "").strip()
    b_loc_missing = not (b.get("location") or "").strip()
    return (a_loc_missing or b_loc_missing) and _description_overlap(a, b)


def _merge_duplicate_job(existing: dict, incoming: dict) -> int:
    enriched = 0
    for key in ("description", "salary"):
        if incoming.get(key) and not existing.get(key):
            existing[key] = incoming[key]
            enriched += 1
    for key in ("direct_url", "date_posted", "job_type", "is_remote", "telework", "work_arrangement"):
        if incoming.get(key) and not existing.get(key):
            existing[key] = incoming[key]
    dupes = set(existing.get("duplicate_urls") or [])
    for url in _job_urls(incoming):
        if url and url != existing.get("url"):
            dupes.add(url)
    if dupes:
        existing["duplicate_urls"] = sorted(dupes)
    return enriched


def _dedupe_master_jobs(jobs: list[dict]) -> tuple[list[dict], int, int]:
    kept: list[dict] = []
    url_index: dict[str, dict] = {}
    id_index: dict[str, dict] = {}
    merged = enriched = 0

    def index_job(job: dict):
        for url in _job_urls(job):
            url_index[url] = job
            ident = _job_identity(url)
            if ident:
                id_index[ident] = job

    for job in jobs:
        url = job.get("url")
        ident = _job_identity(url or "")
        existing = (url_index.get(url) if url else None) or (id_index.get(ident) if ident else None)
        if existing is None:
            existing = next((candidate for candidate in kept if _same_job(candidate, job)), None)
        if existing is None:
            kept.append(job)
            index_job(job)
            continue
        enriched += _merge_duplicate_job(existing, job)
        index_job(existing)
        merged += 1
    return kept, merged, enriched


def _load_prev_jobs(json_path: str) -> list[dict]:
    """Read the `jobs` list from a previously-saved jobs JSON (empty if missing)."""
    try:
        with open(json_path, encoding="utf-8") as f:
            jobs = json.load(f).get("jobs", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return _filter_current_config_jobs(jobs, label=os.path.basename(json_path))


def _load_prev_ids(json_path: str) -> set[str]:
    """Read previously-saved jobs JSON and return the set of job identities."""
    ids = set()
    for j in _load_prev_jobs(json_path):
        i = _job_identity(j.get("url", ""))
        if i:
            ids.add(i)
    return ids


ALL_JOBS_PRUNE_DAYS = 50
# LinkedIn's guest API reliably supports ~30 days via f_TPR; use this for the
# one-time historical backfill (--linkedin-backfill) so new users get a full
# picture without running hourly for weeks.
LINKEDIN_BACKFILL_DAYS = 30


def _merge_into_all_jobs(new_jobs: list) -> int:
    """
    Maintain all_jobs.json — a cumulative, URL/content-deduped master of every role the
    scrapers surface, each stamped with first_seen. The per-source JSONs are
    rolling windows that overwrite every run (LinkedIn keeps only ~1h), so this
    master is what the triage agent and the dashboard's Rank tab read to see
    everything from the last ALL_JOBS_PRUNE_DAYS days. Returns count added.
    """
    path = os.path.join(OUTPUT_DIR, "all_jobs.json")
    try:
        with open(path, encoding="utf-8") as f:
            master = json.load(f).get("jobs", [])
    except (FileNotFoundError, json.JSONDecodeError):
        master = []
    master = _filter_current_config_jobs(master, label="all_jobs.json entries")
    master = _filter_part_time_distance_jobs(master, label="all_jobs.json entries")
    new_jobs = _filter_current_config_jobs(new_jobs, label="incoming jobs")
    new_jobs = _filter_part_time_distance_jobs(new_jobs, label="incoming jobs")

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    entries, merged_existing, enriched_existing = _dedupe_master_jobs(master)
    url_index: dict[str, dict] = {}
    id_index: dict[str, dict] = {}

    def index_entry(entry: dict):
        for u in _job_urls(entry):
            url_index[u] = entry
            ident = _job_identity(u)
            if ident:
                id_index[ident] = entry

    for entry in entries:
        index_entry(entry)

    added = 0
    enriched = enriched_existing
    merged_new = 0
    for j in new_jobs:
        url = j.get("url")
        ident = _job_identity(url or "")
        existing = (url_index.get(url) if url else None) or (id_index.get(ident) if ident else None)
        if existing is None:
            existing = next((entry for entry in entries if _same_job(entry, j)), None)
        if existing is None and url:
            entry = dict(j)
            entry["first_seen"] = stamp
            entries.append(entry)
            index_entry(entry)
            added += 1
        elif existing is not None:
            enriched += _merge_duplicate_job(existing, j)
            index_entry(existing)
            merged_new += 1

    cutoff = (now - timedelta(days=ALL_JOBS_PRUNE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kept = [j for j in entries if j.get("first_seen", stamp) >= cutoff]
    kept.sort(key=lambda j: j.get("first_seen", ""), reverse=True)

    with open(path, "w", encoding="utf-8") as f:
        # Compact separators: the dashboard downloads this file on every load.
        json.dump({"updated_at": now.strftime("%Y-%m-%d %H:%M UTC"), "jobs": kept},
                  f, separators=(",", ":"), ensure_ascii=False)
    print(
        f"all_jobs.json: +{added} new, {enriched} enriched, "
        f"{merged_existing + merged_new} duplicate(s) merged, "
        f"{len(kept)} total (last {ALL_JOBS_PRUNE_DAYS}d)"
    )
    return added


def save_jobs_output(jobs: list, *, basename: str, title: str, subtitle: str,
                     accent: str, empty_message: str, window_label: str):
    """
    Save jobs to {basename}.{json,md,html}. Dedupes against the previous JSON at
    the same path so each email surfaces only postings new to this run.
    """
    # Single chokepoint for the pharma exclusion: every source (LinkedIn,
    # Indeed, priority, CalCareers) funnels through here, so dropping pharma
    # companies once keeps all digests AND all_jobs.json clean.
    before = len(jobs)
    jobs = [j for j in jobs if not _is_pharma_company(j.get("company", ""))]
    if len(jobs) < before:
        print(f"  🚫 Dropped {before - len(jobs)} pharma role(s)")
    jobs = _filter_part_time_distance_jobs(jobs, label="role(s)")
    for job in jobs:
        _ensure_work_arrangement(job)

    json_path = os.path.join(OUTPUT_DIR, f"{basename}.json")
    md_path = os.path.join(OUTPUT_DIR, f"{basename}.md")
    html_path = os.path.join(OUTPUT_DIR, f"{basename}.html")

    prev_ids = _load_prev_ids(json_path)
    new_jobs = [j for j in jobs if _job_identity(j.get("url", "")) not in prev_ids]

    # Accumulate into the cumulative master. Guarded: a bug here must never
    # break the scrape/commit path that the digests and dashboard depend on.
    try:
        # Merge the full current source window, not only brand-new notifications:
        # existing sparse LinkedIn records can gain salary/description later.
        _merge_into_all_jobs(jobs)
    except Exception as e:
        print(f"  ⚠️  all_jobs.json accumulator failed (non-fatal): {e}")

    # Push the highly-relevant new roles to Pushover (no-op without creds).
    try:
        import notify
        notify.notify_new_jobs(new_jobs, basename)
    except Exception as e:
        print(f"  ⚠️  Pushover notify failed (non-fatal): {e}")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    output = {
        "scraped_at": timestamp,
        "total": len(jobs),
        "new_count": len(new_jobs),
        "jobs": jobs,
        "new_jobs": new_jobs,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    lines = [
        f"# {title}",
        f"*Last updated: {timestamp}*\n",
        f"**{len(new_jobs)} new role(s)** since last run · {len(jobs)} total in {window_label}\n",
    ]
    if not new_jobs:
        lines.append(empty_message)
    else:
        for job in new_jobs:
            lines.append(f"### [{job['title']}]({job['url']}) — {job['company']}")
            lines.append(f"- 📍 **Location:** {job['location'] or 'Not specified'}")
            if job.get("salary"):
                lines.append(f"- 💰 **Salary:** {job['salary']}")
            if job.get("work_arrangement"):
                lines.append(f"- **Work mode:** {job['work_arrangement']}")
            if job.get("job_type"):
                lines.append(f"- **Job type:** {job['job_type']}")
            if job.get("date_posted"):
                lines.append(f"- 🕒 **Posted:** {job['date_posted']}")
            lines.append("")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(_render_jobs_html(
            title=title,
            subtitle=subtitle,
            timestamp=timestamp,
            jobs=new_jobs,
            empty_message=empty_message,
            accent=accent,
        ))
    print(f"📄 Saved {basename}.json/.md/.html ({len(new_jobs)} new of {len(jobs)} total)")


def save_linkedin_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="linkedin_jobs",
        title=f"🔥 LinkedIn — {PROFILE_LABEL} Roles",
        subtitle=f"{PROFILE_SUBTITLE} · last {LINKEDIN_LOOKBACK_SECONDS // 3600}h",
        accent="#3b82f6",
        empty_message="No new roles since the last run.",
        window_label=f"last {LINKEDIN_LOOKBACK_SECONDS // 3600}h",
    )


def save_indeed_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="indeed_jobs",
        title=f"🟦 Indeed — {PROFILE_LABEL} Roles",
        subtitle=f"{PROFILE_SUBTITLE} · last {INDEED_LOOKBACK_HOURS}h",
        accent="#2557a7",
        empty_message="No new roles since the last run.",
        window_label=f"last {INDEED_LOOKBACK_HOURS}h",
    )


def save_glassdoor_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="glassdoor_jobs",
        title=f"🟩 Glassdoor — {PROFILE_LABEL} Roles",
        subtitle=f"{PROFILE_SUBTITLE} · last {GLASSDOOR_LOOKBACK_HOURS}h",
        accent="#0caa41",
        empty_message="No new roles since the last run.",
        window_label=f"last {GLASSDOOR_LOOKBACK_HOURS}h",
    )


def save_ziprecruiter_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="ziprecruiter_jobs",
        title=f"🟧 ZipRecruiter — {PROFILE_LABEL} Roles",
        subtitle=f"{PROFILE_SUBTITLE} · last {ZIPRECRUITER_LOOKBACK_HOURS}h",
        accent="#f97316",
        empty_message="No new roles since the last run.",
        window_label=f"last {ZIPRECRUITER_LOOKBACK_HOURS}h",
    )


def save_google_jobs_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="google_jobs",
        title=f"🔎 Google Jobs — {PROFILE_LABEL} Roles",
        subtitle=f"{PROFILE_SUBTITLE} · last {GOOGLE_JOBS_LOOKBACK_HOURS}h",
        accent="#4285f4",
        empty_message="No new roles since the last run.",
        window_label=f"last {GOOGLE_JOBS_LOOKBACK_HOURS}h",
    )


def save_hiringcafe_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="hiringcafe_jobs",
        title=f"☕ HiringCafe — {PROFILE_LABEL} Roles",
        subtitle=f"{PROFILE_SUBTITLE} · last {HIRINGCAFE_LOOKBACK_DAYS}d",
        accent="#a16207",
        empty_message="No new roles since the last run.",
        window_label=f"last {HIRINGCAFE_LOOKBACK_DAYS}d",
    )


def save_scholarshipdb_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="scholarshipdb_jobs",
        title=f"🎓 ScholarshipDB — {PROFILE_LABEL} Roles",
        subtitle=f"{PROFILE_SUBTITLE} · last {SCHOLARSHIPDB_LOOKBACK_DAYS}d",
        accent="#7c3aed",
        empty_message="No new ScholarshipDB roles since the last run.",
        window_label=f"last {SCHOLARSHIPDB_LOOKBACK_DAYS}d",
    )


def save_higheredjobs_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="higheredjobs_jobs",
        title=f"🎓 HigherEdJobs — {PROFILE_LABEL} Roles",
        subtitle=f"{PROFILE_SUBTITLE} · last {HIGHEREDJOBS_LOOKBACK_DAYS}d",
        accent="#0f766e",
        empty_message="No new HigherEdJobs roles since the last run.",
        window_label=f"last {HIGHEREDJOBS_LOOKBACK_DAYS}d",
    )


def save_biotech_linkedin_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="jobs",
        title=f"🏛 Priority Employers — {PROFILE_LABEL} Roles",
        subtitle=f"{PROFILE_SUBTITLE} · priority-employer allowlist · last {LINKEDIN_BIOTECH_LOOKBACK_SECONDS // 3600}h",
        accent="#2ea04f",
        empty_message="No new priority-employer roles since the last run.",
        window_label=f"last {LINKEDIN_BIOTECH_LOOKBACK_SECONDS // 3600}h",
    )


def _render_jobs_html(*, title: str, subtitle: str, timestamp: str,
                      jobs: list, empty_message: str, accent: str) -> str:
    import html as html_mod

    if not jobs:
        body = f'<div class="empty">{html_mod.escape(empty_message)}</div>'
    else:
        cards = []
        for j in jobs:
            salary = (
                f'<span class="meta-item">💰 {html_mod.escape(j["salary"])}</span>'
                if j.get("salary") else ""
            )
            posted = (
                f'<span class="meta-item">🕒 Posted {html_mod.escape(j["date_posted"])}</span>'
                if j.get("date_posted") else ""
            )
            work = (
                f'<span class="meta-item">{html_mod.escape(j["work_arrangement"])}</span>'
                if j.get("work_arrangement") else ""
            )
            job_type = (
                f'<span class="meta-item">{html_mod.escape(j["job_type"])}</span>'
                if j.get("job_type") else ""
            )
            ats_tag = (
                f'<span class="ats">{html_mod.escape(j["ats"])}</span>'
                if j.get("ats") else ""
            )
            cards.append(
                f'<div class="job">'
                f'<div class="title"><a href="{html_mod.escape(j["url"])}">'
                f'{html_mod.escape(j["title"])}</a></div>'
                f'<div class="company">{html_mod.escape(j["company"])} {ats_tag}</div>'
                f'<div class="meta">'
                f'<span class="meta-item">📍 {html_mod.escape(j["location"] or "Not specified")}</span>'
                f'{salary}'
                f'{work}'
                f'{job_type}'
                f'{posted}'
                f'</div></div>'
            )
        body = "\n".join(cards)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  max-width: 720px; margin: 24px auto; padding: 0 16px; color: #1a1a1a; background: #fff; line-height: 1.5; }}
h1 {{ font-size: 22px; margin: 0 0 4px 0; }}
.subtitle {{ color: #666; font-size: 14px; margin-bottom: 16px; }}
.summary {{ background: #f4f6fb; padding: 12px 16px; border-left: 4px solid {accent};
  margin: 16px 0; border-radius: 4px; font-size: 14px; }}
.summary strong {{ font-size: 18px; color: {accent}; }}
.job {{ background: #fafafa; border: 1px solid #e8e8e8; border-radius: 8px;
  padding: 14px 18px; margin-bottom: 10px; }}
.title {{ font-size: 16px; font-weight: 600; margin-bottom: 4px; }}
.title a {{ color: #0a66c2; text-decoration: none; }}
.title a:hover {{ text-decoration: underline; }}
.company {{ color: #444; font-weight: 500; margin-bottom: 8px; font-size: 14px; }}
.ats {{ display: inline-block; background: #eaf3fb; color: #0a66c2; font-size: 11px;
  padding: 1px 8px; border-radius: 10px; font-weight: 500; margin-left: 6px; vertical-align: middle; }}
.meta {{ font-size: 13px; color: #666; }}
.meta-item {{ margin-right: 14px; }}
.empty {{ color: #999; font-style: italic; padding: 28px; text-align: center;
  background: #fafafa; border-radius: 8px; border: 1px dashed #ddd; }}
.foot {{ margin-top: 28px; padding-top: 12px; border-top: 1px solid #eee;
  color: #888; font-size: 12px; text-align: center; }}
.foot a {{ color: #0a66c2; }}
</style></head>
<body>
<h1>{title}</h1>
<div class="subtitle">{subtitle}</div>
<div class="summary"><strong>{len(jobs)}</strong> role(s) &nbsp;·&nbsp; scraped {timestamp}</div>
{body}
<div class="foot">Auto-generated by <a href="https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'ScottCoffin/Job_Scraper')}">Job_Scraper</a></div>
</body></html>"""


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_results(jobs: list):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    output = {"scraped_at": timestamp, "total": len(jobs), "jobs": jobs}
    with open(os.path.join(OUTPUT_DIR, "jobs.json"), "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    lines = [
        "# 🏛 Fresh Environmental / Toxicology Job Listings (California)",
        f"*Last updated: {timestamp}*\n",
        f"**{len(jobs)} role(s) posted in the last 24 hours**\n",
    ]

    for company in sorted(set(j["company"] for j in jobs)):
        company_jobs = [j for j in jobs if j["company"] == company]
        lines.append(f"## {company} ({len(company_jobs)} role(s))\n")
        for job in company_jobs:
            lines.append(f"### [{job['title']}]({job['url']})")
            lines.append(f"- 📍 **Location:** {job['location'] or 'Not specified'}")
            if job.get("date_posted"):
                lines.append(f"- 📅 **Posted:** {job['date_posted']}")
            lines.append("")

    with open(os.path.join(OUTPUT_DIR, "jobs.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    with open(os.path.join(OUTPUT_DIR, "jobs.html"), "w", encoding="utf-8") as f:
        f.write(_render_jobs_html(
            title=f"🏛 Fresh {PROFILE_LABEL} Job Listings",
            subtitle=f"{PROFILE_SUBTITLE} · posted in the last 24 hours",
            timestamp=timestamp,
            jobs=jobs,
            empty_message="No environmental/toxicology roles posted in the last 24 hours.",
            accent="#2ea04f",
        ))

    print(f"\n📄 Saved jobs.json/.md/.html ({len(jobs)} total roles)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--indeed-only" in sys.argv:
        save_indeed_results(scrape_indeed_recent())
        sys.exit(0)

    if "--indeed-backfill" in sys.argv:
        print(f"🔁 Indeed backfill (last {INDEED_BACKFILL_DAYS} days)…")
        save_indeed_results(scrape_indeed_recent(hours_old=INDEED_BACKFILL_DAYS * 24))
        sys.exit(0)

    if "--glassdoor-only" in sys.argv:
        save_glassdoor_results(scrape_glassdoor_recent())
        sys.exit(0)

    if "--glassdoor-backfill" in sys.argv:
        print(f"🔁 Glassdoor backfill (last {GLASSDOOR_BACKFILL_DAYS} days)…")
        save_glassdoor_results(scrape_glassdoor_recent(hours_old=GLASSDOOR_BACKFILL_DAYS * 24))
        sys.exit(0)

    if "--ziprecruiter-only" in sys.argv:
        save_ziprecruiter_results(scrape_ziprecruiter_recent())
        sys.exit(0)

    if "--ziprecruiter-backfill" in sys.argv:
        print(f"🔁 ZipRecruiter backfill (last {ZIPRECRUITER_BACKFILL_DAYS} days)…")
        save_ziprecruiter_results(scrape_ziprecruiter_recent(hours_old=ZIPRECRUITER_BACKFILL_DAYS * 24))
        sys.exit(0)

    if "--google-jobs-only" in sys.argv:
        save_google_jobs_results(scrape_google_jobs_recent())
        sys.exit(0)

    if "--google-jobs-backfill" in sys.argv:
        print(f"🔁 Google Jobs backfill (last {GOOGLE_JOBS_BACKFILL_DAYS} days)…")
        save_google_jobs_results(scrape_google_jobs_recent(hours_old=GOOGLE_JOBS_BACKFILL_DAYS * 24))
        sys.exit(0)

    if "--hiringcafe-only" in sys.argv:
        save_hiringcafe_results(scrape_hiringcafe_recent())
        sys.exit(0)

    if "--hiringcafe-backfill" in sys.argv:
        print(f"🔁 HiringCafe backfill (last {HIRINGCAFE_BACKFILL_DAYS} days)…")
        save_hiringcafe_results(scrape_hiringcafe_recent(days=HIRINGCAFE_BACKFILL_DAYS))
        sys.exit(0)

    if "--scholarshipdb-only" in sys.argv:
        save_scholarshipdb_results(scrape_scholarshipdb_recent())
        sys.exit(0)

    if "--scholarshipdb-backfill" in sys.argv:
        print(f"🔁 ScholarshipDB backfill (last {SCHOLARSHIPDB_BACKFILL_DAYS} days)…")
        save_scholarshipdb_results(scrape_scholarshipdb_recent(days=SCHOLARSHIPDB_BACKFILL_DAYS))
        sys.exit(0)

    if "--higheredjobs-only" in sys.argv:
        save_higheredjobs_results(scrape_higheredjobs_recent())
        sys.exit(0)

    if "--higheredjobs-backfill" in sys.argv:
        print(f"🔁 HigherEdJobs backfill (last {HIGHEREDJOBS_BACKFILL_DAYS} days)…")
        save_higheredjobs_results(scrape_higheredjobs_recent(days=HIGHEREDJOBS_BACKFILL_DAYS))
        sys.exit(0)

    if "--linkedin-only" in sys.argv:
        save_linkedin_results(scrape_linkedin_recent())
        sys.exit(0)

    if "--linkedin-backfill" in sys.argv:
        # One-time historical backfill. Queries the last LINKEDIN_BACKFILL_DAYS of
        # LinkedIn postings so new users get a full picture on first run. Run once
        # via Actions → LinkedIn watcher → Run workflow → backfill=true.
        backfill_s = LINKEDIN_BACKFILL_DAYS * 24 * 3600
        print(f"🔁 LinkedIn backfill (last {LINKEDIN_BACKFILL_DAYS} days)…")
        jobs, _ = _linkedin_search(list(LINKEDIN_SEARCH_TERMS), backfill_s)
        if jobs:
            _enrich_linkedin_postings(jobs)
        print(f"  ✅ Backfill: {len(jobs)} role(s) found")
        save_linkedin_results(jobs)
        sys.exit(0)

    if "--calcareers-only" in sys.argv:
        save_calcareers_results(scrape_calcareers_recent())
        sys.exit(0)

    if "--usajobs-only" in sys.argv:
        save_usajobs_results(scrape_usajobs_recent())
        sys.exit(0)

    if "--governmentjobs-only" in sys.argv:
        save_governmentjobs_results(scrape_governmentjobs_recent())
        sys.exit(0)

    if "--governmentjobs-backfill" in sys.argv:
        print(f"🔁 GovernmentJobs/NEOGOV backfill (last {GOVERNMENTJOBS_BACKFILL_DAYS} days)…")
        save_governmentjobs_results(scrape_governmentjobs_recent(days=GOVERNMENTJOBS_BACKFILL_DAYS))
        sys.exit(0)

    if "--calopps-only" in sys.argv:
        save_calopps_results(scrape_calopps_recent())
        sys.exit(0)

    if "--csucareers-only" in sys.argv:
        save_csucareers_results(scrape_csucareers_recent())
        sys.exit(0)

    if "--biotech-only" in sys.argv:
        # "Priority Employers" digest (flag name kept so the GitHub workflow
        # doesn't change). Source = the LinkedIn priority-employer allowlist,
        # plus any verified direct-ATS boards added to CURATED_BIOTECHS (empty
        # by default for env/tox employers — see that list's note). Cross-run
        # dedupe via _load_prev_ids → save_biotech_linkedin_results gives
        # "new since last digest" semantics.
        jobs = list(scrape_curated_biotechs())
        jobs = [j for j in jobs if is_target_location(j.get("location", ""))]
        jobs.extend(scrape_linkedin_biotech())

        seen: set[tuple[str, str]] = set()
        deduped: list[dict] = []
        for j in jobs:
            key = (j["company"].strip().lower(), j["title"].strip().lower())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(j)
        print(f"\n🏛  Combined priority-employer total: {len(deduped)} unique role(s) "
              f"(from {len(jobs)} across sources)")

        save_biotech_linkedin_results(deduped)
        sys.exit(0)

    if "--priority-backfill" in sys.argv:
        # One-time backfill for priority employers: uses the same 30-day LinkedIn
        # window as --linkedin-backfill but filtered to the priority-employer allowlist.
        backfill_s = LINKEDIN_BACKFILL_DAYS * 24 * 3600
        print(f"🔁 Priority Employer backfill (last {LINKEDIN_BACKFILL_DAYS} days)…")
        raw, _ = _linkedin_search(list(LINKEDIN_SEARCH_TERMS), backfill_s)
        jobs = [j for j in raw if _is_biotech_company(j["company"])]
        if jobs:
            _enrich_linkedin_postings(jobs)
        jobs = list(scrape_curated_biotechs()) + jobs
        jobs = [j for j in jobs if is_target_location(j.get("location", ""))]
        seen: set[tuple[str, str]] = set()
        deduped_p: list[dict] = []
        for j in jobs:
            key = (j["company"].strip().lower(), j["title"].strip().lower())
            if key in seen:
                continue
            seen.add(key)
            deduped_p.append(j)
        print(f"  ✅ Backfill: {len(deduped_p)} unique priority-employer role(s)")
        save_biotech_linkedin_results(deduped_p)
        sys.exit(0)

    # Legacy default: direct-ATS sweep (CURATED_BIOTECHS). Empty by default for
    # env/tox employers, so this prints 0; CI uses the three --*-only flags.
    all_jobs = list(scrape_curated_biotechs())

    before = len(all_jobs)
    all_jobs = [j for j in all_jobs if is_target_location(j.get("location", ""))]
    print(f"\n📍 Location filter (California): {before} → {len(all_jobs)} roles")

    before = len(all_jobs)
    all_jobs = [j for j in all_jobs if is_recent_posting(j)]
    print(f"🕒 Freshness filter (last 24h): {before} → {len(all_jobs)} roles")

    save_results(all_jobs)

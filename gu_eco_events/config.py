"""Static configuration: source URLs, filter rules, and safety thresholds."""

from __future__ import annotations

from zoneinfo import ZoneInfo

TZID = "Europe/Stockholm"
TZ = ZoneInfo(TZID)

GU_BASE = "https://www.gu.se"

# The human-facing search the Roundtable table approved. It is a JavaScript
# shell; the page itself calls the JSON searcher below with the same
# parameters, so that is what we fetch (see README "Source").
SEARCH_PAGE_URL = (
    "https://www.gu.se/sok?date_from=2026-09-24"
    "&event_area_facet=H%C3%A5llbarhet%20%26%20milj%C3%B6&hits=25&searcher=event"
)
SEARCH_API_URL = "https://www.gu.se/api/search/rest/apps/external_web/searchers/event_sv"
CATEGORY = "Hållbarhet & miljö"
HITS_PER_PAGE = 25
MAX_PAGES = 20  # hard stop so a pagination bug cannot hammer gu.se

USER_AGENT = (
    "gu-eco-events/0.1 (+https://github.com/moonleaf-earth/gu-eco-events; "
    "weekly calendar feed, one request at a time)"
)
REQUEST_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 30

# Deterministic default when GU gives a start time but no end time.
DEFAULT_DURATION_MINUTES = 60

# --- Location rule -------------------------------------------------------
# Matching is done on a casefolded, diacritic-stripped string.
GOTEBORG_MARKERS = ("goteborg", "gothenburg")
ONLINE_MARKERS = (
    "online",
    "digital",  # also matches "digitalt"
    "webinar",
    "webbinar",  # webbinarium
    "zoom",
    "teams",
    "distans",
    "virtuell",
    "livestream",
    "livesand",  # livesänd / livesändning
)
# GU/Göteborg venues that GU often lists without the city name.
GOTEBORG_VENUES = (
    "annedalsseminariet",
    "artisten",
    "botaniska tradgarden",
    "campus linne",
    "carlanderska",
    "chalmers",
    "geovetarcentrum",
    "handelshogskolan",
    "humanisten",
    "konstepidemin",
    "lindholmen",
    "medicinareberget",
    "natrium",
    "nackrosen",
    "pedagogen",
    "sahlgrenska",
    "samhallsvetarhuset",
    "studenternas hus",
    "universitetsplatsen",
    "vasaparken",
    "wallenberg",  # Wallenbergsalen / Wallenberglaboratoriet, Medicinareberget
)

# --- Safety guards ---------------------------------------------------------
# Abort if the parsed occurrence count falls below this share of the last
# successful run (only once the previous run had at least SEVERE_DROP_MIN_PREVIOUS).
SEVERE_DROP_RATIO = 0.5
SEVERE_DROP_MIN_PREVIOUS = 4
# Abort if more than this share of parsed occurrences miss a required field.
MAX_MISSING_REQUIRED_RATIO = 0.2

# State entries whose event ended this many days ago are pruned.
STATE_RETENTION_DAYS = 180

UID_DOMAIN = "gu-eco-events.moonleaf-earth.github.io"

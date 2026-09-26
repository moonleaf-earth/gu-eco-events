"""Where listing JSON and detail HTML come from: gu.se (live) or fixtures.

Both transports expose the same two methods, so the pipeline and tests run
the exact same parsing/normalization code.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from pathlib import Path

from .. import config


class SourceError(RuntimeError):
    """The source could not be read completely; never treat as 'no events'."""


class LiveTransport:
    """Polite, sequential HTTP client for gu.se."""

    def __init__(self, date_from: str, delay: float = config.REQUEST_DELAY_SECONDS):
        self.date_from = date_from
        self.delay = delay
        self._last = 0.0
        self._robots: urllib.robotparser.RobotFileParser | None = None

    # -- helpers ------------------------------------------------------------
    def _allowed(self, url: str) -> bool:
        if self._robots is None:
            rp = urllib.robotparser.RobotFileParser()
            body = self._get(config.GU_BASE + "/robots.txt", check_robots=False)
            rp.parse(body.decode("utf-8", "replace").splitlines())
            self._robots = rp
        return self._robots.can_fetch(config.USER_AGENT, url)

    def _get(self, url: str, check_robots: bool = True, accept: str = "*/*") -> bytes:
        if check_robots and not self._allowed(url):
            raise SourceError(f"robots.txt disallows {url}")
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            url, headers={"User-Agent": config.USER_AGENT, "Accept": accept}
        )
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT_SECONDS) as r:
                    self._last = time.monotonic()
                    return r.read()
            except urllib.error.HTTPError as e:
                self._last = time.monotonic()
                last_err = e
                if e.code < 500 and e.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError) as e:
                self._last = time.monotonic()
                last_err = e
            time.sleep(self.delay * (attempt + 2))
        raise SourceError(f"GET {url} failed: {last_err}")

    # -- transport API --------------------------------------------------------
    def search_page(self, offset: int) -> dict:
        params = {
            "q": "*",
            "sort": "date_asc",
            "date_from": self.date_from,
            "event_area_facet": config.CATEGORY,
            "hits": str(config.HITS_PER_PAGE),
            "offset": str(offset),
        }
        url = config.SEARCH_API_URL + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        print(f"fetch listing offset={offset}", file=sys.stderr)
        try:
            return json.loads(self._get(url, accept="application/json"))
        except json.JSONDecodeError as e:
            raise SourceError(f"listing offset={offset} is not JSON: {e}") from e

    def detail_html(self, url: str) -> str:
        print(f"fetch detail {url}", file=sys.stderr)
        return self._get(url, accept="text/html").decode("utf-8", "replace")


class FixtureTransport:
    """Reads `<dir>/search/page-*.json` and `<dir>/detail/<slug>.html`."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        pages = sorted((self.root / "search").glob("page-*.json"))
        if not pages:
            raise SourceError(f"no search pages under {self.root}/search")
        self._pages = [json.loads(p.read_text(encoding="utf-8")) for p in pages]

    def search_page(self, offset: int) -> dict:
        for p in self._pages:
            if int(((p.get("documentList") or {}).get("pagination") or {}).get("offset") or 0) == offset:
                return p
        raise SourceError(f"fixture has no search page for offset={offset}")

    def detail_html(self, url: str) -> str:
        slug = urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        path = self.root / "detail" / f"{slug}.html"
        if not path.exists():
            raise SourceError(f"fixture detail page missing: {path}")
        return path.read_text(encoding="utf-8")

"""
Verification data validator.

Two modes:
  1. TEST MODE (default when no sheet is configured):
     Hardcoded entries pass validation so the flow can be tested end-to-end.

  2. GOOGLE SHEET MODE:
     Fetches a view-only Google Sheet as CSV (no API key required).
     The sheet must be either "Published to the web" or shared as
     "Anyone with the link can view".

     Expected CSV columns (header row required):
       team_name, uid, server

     Column names are case-insensitive and leading/trailing whitespace is trimmed.
     The validator caches the sheet in memory and refreshes every CACHE_TTL seconds.
"""

import asyncio
import csv
import io
import time
import re
import aiohttp

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CACHE_TTL = 300  # seconds (5 minutes)

# Hardcoded test entries (used when no sheet is configured)
TEST_ENTRIES = [
    {"team_name": "Test Team", "uid": "123456789", "server": "SEA"},
    {"team_name": "Test Team", "uid": "987654321", "server": "NA"},
    {"team_name": "Test Team", "uid": "111111111", "server": "EU"},
]


def _extract_sheet_id(url_or_id: str) -> str:
    """Accept a full Google Sheets URL or a bare sheet ID."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    # Assume it's already a bare ID
    return url_or_id.strip()


def _build_csv_url(sheet_id: str, gid: str = "0") -> str:
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/export?format=csv&gid={gid}"
    )


class SheetValidator:
    """
    Thread-safe, async-friendly validator that checks (team, uid, server)
    against a cached Google Sheet CSV or test data.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._cache: list[dict] | None = None
        self._cache_ts: float = 0
        self._sheet_id: str | None = None
        self._gid: str = "0"
        self._test_mode: bool = True  # starts in test mode

    # ----- Public API -------------------------------------------------------

    def configure_sheet(self, url_or_id: str, gid: str = "0") -> str:
        """Set the Google Sheet to validate against. Returns the sheet ID."""
        self._sheet_id = _extract_sheet_id(url_or_id)
        self._gid = gid
        self._test_mode = False
        self._cache = None  # force refresh on next check
        self._cache_ts = 0
        return self._sheet_id

    def enable_test_mode(self):
        self._test_mode = True
        self._cache = None

    def disable_test_mode(self):
        self._test_mode = False
        self._cache = None
        self._cache_ts = 0

    @property
    def is_test_mode(self) -> bool:
        return self._test_mode

    @property
    def is_configured(self) -> bool:
        return self._sheet_id is not None

    async def validate(self, team_name: str, uid: str, server: str) -> bool:
        """
        Return True if the (team_name, uid, server) triple matches an entry
        in the data source. Comparison is case-insensitive and stripped.
        """
        entries = await self._get_entries()
        t = team_name.strip().lower()
        u = uid.strip().lower()
        s = server.strip().lower()
        for entry in entries:
            if (
                entry.get("team_name", "").strip().lower() == t
                and entry.get("uid", "").strip().lower() == u
                and entry.get("server", "").strip().lower() == s
            ):
                return True
        return False

    async def get_teams(self) -> list[str]:
        """Return a sorted, deduplicated list of team names from the data."""
        entries = await self._get_entries()
        seen = set()
        teams = []
        for e in entries:
            name = e.get("team_name", "").strip()
            if name and name not in seen:
                seen.add(name)
                teams.append(name)
        return sorted(teams)

    async def refresh(self) -> int:
        """Force a cache refresh. Returns the number of entries loaded."""
        async with self._lock:
            entries = await self._fetch()
            self._cache = entries
            self._cache_ts = time.monotonic()
            return len(entries)

    # ----- Internal ---------------------------------------------------------

    async def _get_entries(self) -> list[dict]:
        if self._test_mode:
            return TEST_ENTRIES

        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and (now - self._cache_ts) < CACHE_TTL:
                return self._cache

            entries = await self._fetch()
            self._cache = entries
            self._cache_ts = time.monotonic()
            return entries

    async def _fetch(self) -> list[dict]:
        if not self._sheet_id:
            return TEST_ENTRIES

        url = _build_csv_url(self._sheet_id, self._gid)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        print(f"Sheet fetch failed (HTTP {resp.status}), using cached data")
                        return self._cache or []
                    text = await resp.text()
        except Exception as e:
            print(f"Sheet fetch error: {e}, using cached data")
            return self._cache or []

        return self._parse_csv(text)

    @staticmethod
    def _parse_csv(text: str) -> list[dict]:
        reader = csv.DictReader(io.StringIO(text))
        entries = []
        # Normalize header names to lowercase
        for row in reader:
            normalized = {k.strip().lower(): v for k, v in row.items() if k}
            entries.append(normalized)
        return entries


# ---------------------------------------------------------------------------
# Singleton instance (shared across the bot)
# ---------------------------------------------------------------------------
validator = SheetValidator()

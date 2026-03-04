"""
Verification data validator.

Two modes:
  1. TEST MODE (default when no sheet is configured):
     Hardcoded entries pass validation so the flow can be tested end-to-end.

  2. GOOGLE SHEET MODE:
     Fetches a view-only Google Sheet as CSV (no API key required).
     The sheet must be shared as "Anyone with the link can view".

     The expected sheet is the "FINAL Teams Database" tab with columns:
       A: Team Name, B: Abbrev, C: Team Logo, D: Player Name,
       E: # (player number), F: IGN, G: UID, H: Server, ...

     - uid / server: integer identifiers used for verification lookup
     - team_name: full team name
     - abbrev: team abbreviation used in nickname (e.g. NU)
     - ign: in-game name used in nickname (e.g. ESTACIO)
     - role: not present in sheet; defaults to "player" for all entries
"""

import asyncio
import csv
import io
import time
import re
import urllib.parse
import aiohttp

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CACHE_TTL = 300  # seconds (5 minutes)

# Column header mapping: sheet header (lowercased) -> internal key
COLUMN_MAP = {
    "team name": "team_name",
    "abbrev": "abbrev",
    "ign": "ign",
    "uid": "uid",
    "server": "server",
}

# Hardcoded test entries (used when no sheet is configured)
TEST_ENTRIES = [
    {"team_name": "Test Team", "abbrev": "TT", "ign": "TestPlayer1", "uid": "123456789", "server": "1001", "role": "player"},
    {"team_name": "Test Team", "abbrev": "TT", "ign": "TestPlayer2", "uid": "987654321", "server": "1002", "role": "player"},
    {"team_name": "Test Team", "abbrev": "TT", "ign": "TestPlayer3", "uid": "111111111", "server": "1003", "role": "player"},
    {"team_name": "Alpha Squad", "abbrev": "AS", "ign": "AlphaLead", "uid": "222222222", "server": "1001", "role": "player"},
    {"team_name": "Alpha Squad", "abbrev": "AS", "ign": "AlphaSub", "uid": "333333333", "server": "1001", "role": "player"},
    {"team_name": "Staff Team", "abbrev": "STAFF", "ign": "StaffMember1", "uid": "100000001", "server": "1001", "role": "staff"},
    {"team_name": "Staff Team", "abbrev": "STAFF", "ign": "StaffMember2", "uid": "100000002", "server": "1001", "role": "league ops"},
]


def _extract_sheet_id(url_or_id: str) -> str:
    """Accept a full Google Sheets URL or a bare sheet ID."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    return url_or_id.strip()


def _build_csv_url(sheet_id: str, gid: str = "0") -> str:
    """Build CSV export URL using GID (legacy)."""
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/export?format=csv&gid={gid}"
    )


def _build_csv_url_by_name(sheet_id: str, tab_name: str) -> str:
    """Build CSV export URL using sheet tab name."""
    encoded = urllib.parse.quote(tab_name)
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={encoded}"
    )


class SheetValidator:
    """
    Thread-safe, async-friendly validator that checks (uid, server)
    against a cached Google Sheet CSV or test data.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._cache: list[dict] | None = None
        self._cache_ts: float = 0
        self._sheet_id: str | None = None
        self._gid: str = "0"
        self._tab_name: str | None = None
        self._test_mode: bool = True

    # ----- Public API -------------------------------------------------------

    def configure_sheet(
        self, url_or_id: str, gid: str = "0", tab_name: str | None = None
    ) -> str:
        """Set the Google Sheet to validate against. Returns the sheet ID."""
        self._sheet_id = _extract_sheet_id(url_or_id)
        self._gid = gid
        self._tab_name = tab_name
        self._test_mode = False
        self._cache = None
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

    @property
    def tab_name(self) -> str | None:
        return self._tab_name

    async def validate(self, uid: str, server: str) -> dict | None:
        """
        Check if (uid, server) matches an entry.
        Returns the matched entry dict (team_name, abbrev, ign, role, etc.)
        or None if no match.
        """
        entries = await self._get_entries()
        u = uid.strip()
        s = server.strip()
        for entry in entries:
            if (
                entry.get("uid", "").strip() == u
                and entry.get("server", "").strip() == s
            ):
                return entry
        return None

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

    async def get_all_entries(self) -> list[dict]:
        """Return all cached entries (for cross-referencing with DB)."""
        return await self._get_entries()

    async def get_team_roster(self, team_name: str) -> list[dict]:
        """Return all sheet entries for a specific team."""
        entries = await self._get_entries()
        return [
            e for e in entries
            if e.get("team_name", "").strip().lower() == team_name.strip().lower()
        ]

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

        # Prefer tab-name URL; fall back to GID-based URL
        if self._tab_name:
            url = _build_csv_url_by_name(self._sheet_id, self._tab_name)
        else:
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
        for row in reader:
            # Normalize headers to lowercase and map to internal keys
            normalized = {}
            for k, v in row.items():
                if not k:
                    continue
                key_lower = k.strip().lower()
                internal_key = COLUMN_MAP.get(key_lower)
                if internal_key:
                    normalized[internal_key] = v.strip() if v else ""

            # Skip rows with empty UID or empty IGN (blank substitute slots)
            uid_val = normalized.get("uid", "")
            ign_val = normalized.get("ign", "")
            if not uid_val or not ign_val:
                continue

            # All sheet entries are players (no role column in sheet)
            normalized["role"] = "player"

            entries.append(normalized)
        return entries


# ---------------------------------------------------------------------------
# Singleton instance (shared across the bot)
# ---------------------------------------------------------------------------
validator = SheetValidator()

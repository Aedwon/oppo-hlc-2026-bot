"""
Async wrapper around the Challonge REST API v1.

Usage:
    client = ChallongeClient()          # reads CHALLONGE_API_KEY from env
    tournament = await client.get_tournament("my_slug")
    matches    = await client.get_matches("my_slug", state="open")
    await client.update_match("my_slug", match_id, winner_id, "2-1")

Helper functions:
    parse_challonge_url(url) -> slug or None
    build_participant_cache(participants) -> {id: name}
    find_participant_by_name(cache, name) -> (id, name) or None
    format_match_display(match, cache) -> str
"""
import os
import re
import aiohttp
from typing import Optional


BASE_URL = "https://api.challonge.com/v1"


class ChallongeAPIError(Exception):
    """Raised when the Challonge API returns an error."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"Challonge API {status}: {message}")


class ChallongeClient:
    """Async client for Challonge API v1."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("CHALLONGE_API_KEY")
        if not self.api_key:
            raise ValueError(
                "CHALLONGE_API_KEY not set. "
                "Get one at https://challonge.com/settings/developer"
            )

    async def _request(self, method: str, path: str, **kwargs) -> dict | list:
        """Make an authenticated request to the Challonge API."""
        url = f"{BASE_URL}{path}"
        params = kwargs.pop("params", {})
        params["api_key"] = self.api_key

        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, params=params, **kwargs) as resp:
                if resp.status >= 400:
                    try:
                        body = await resp.json()
                        errors = body.get("errors", [str(body)])
                        msg = "; ".join(errors) if isinstance(errors, list) else str(errors)
                    except Exception:
                        msg = await resp.text()
                    raise ChallongeAPIError(resp.status, msg)
                return await resp.json()

    # -- Tournament ----------------------------------------------------------

    async def get_tournament(self, slug: str) -> dict:
        """Fetch tournament info by slug."""
        data = await self._request("GET", f"/tournaments/{slug}.json")
        return data.get("tournament", data)

    async def validate_tournament(self, slug: str) -> tuple[bool, dict, Optional[str]]:
        """Validate that a tournament exists and return (success, data, error)."""
        try:
            tournament = await self.get_tournament(slug)
            return True, tournament, None
        except ChallongeAPIError as e:
            if e.status == 404:
                return False, {}, "Tournament not found. Check the URL."
            return False, {}, e.message

    # -- Participants --------------------------------------------------------

    async def get_participants(self, slug: str) -> list[dict]:
        """List all participants in a tournament."""
        data = await self._request("GET", f"/tournaments/{slug}/participants.json")
        return [item.get("participant", item) for item in data]

    # -- Matches -------------------------------------------------------------

    async def get_matches(self, slug: str, state: str = "open") -> list[dict]:
        """List matches. state: open, pending, complete, all."""
        params = {}
        if state != "all":
            params["state"] = state
        data = await self._request(
            "GET", f"/tournaments/{slug}/matches.json", params=params
        )
        return [item.get("match", item) for item in data]

    async def update_match(
        self, slug: str, match_id: int, winner_id: int, score: str
    ) -> dict:
        """Report a match result."""
        data = await self._request(
            "PUT",
            f"/tournaments/{slug}/matches/{match_id}.json",
            json={
                "match": {
                    "winner_id": winner_id,
                    "scores_csv": score,
                }
            },
        )
        return data.get("match", data)


# ────────────────────────────────────────────────────────────────
# Helper functions
# ────────────────────────────────────────────────────────────────

def parse_challonge_url(url: str) -> Optional[str]:
    """Extract the tournament slug from a Challonge URL.

    Handles:
        https://challonge.com/my_tournament
        https://subdomain.challonge.com/my_tournament
        my_tournament  (plain slug)
    """
    url = url.strip()

    # Plain slug (no slashes, no dots)
    if re.match(r"^[\w-]+$", url):
        return url

    # Full URL
    match = re.match(
        r"https?://(?:(\w+)\.)?challonge\.com/(?:tournaments/)?([^/?#]+)", url
    )
    if match:
        subdomain = match.group(1)
        slug = match.group(2)
        # Subdomain tournaments use "subdomain-slug" format in the API
        if subdomain and subdomain not in ("www",):
            return f"{subdomain}-{slug}"
        return slug

    return None


def build_participant_cache(participants: list[dict]) -> dict[int, str]:
    """Build {participant_id: display_name} lookup."""
    cache = {}
    for p in participants:
        pid = p.get("id")
        name = (
            p.get("display_name")
            or p.get("name")
            or p.get("username")
            or f"Participant #{pid}"
        )
        cache[pid] = name
    return cache


def find_participant_by_name(
    cache: dict[int, str], name: str
) -> Optional[tuple[int, str]]:
    """Fuzzy-find a participant by name (case-insensitive substring)."""
    name_lower = name.lower().strip()

    # Exact match first
    for pid, pname in cache.items():
        if pname.lower() == name_lower:
            return pid, pname

    # Substring match
    for pid, pname in cache.items():
        if name_lower in pname.lower():
            return pid, pname

    return None


def format_match_display(
    match: dict,
    participant_cache: dict[int, str],
    include_state: bool = False,
) -> str:
    """Format a match for embed display."""
    p1_id = match.get("player1_id")
    p2_id = match.get("player2_id")
    p1 = participant_cache.get(p1_id, "TBD") if p1_id else "TBD"
    p2 = participant_cache.get(p2_id, "TBD") if p2_id else "TBD"

    order = match.get("suggested_play_order") or match.get("id", "?")
    line = f"`#{order}` **{p1}** vs **{p2}**"

    state = match.get("state", "")
    if state == "complete":
        score = match.get("scores_csv", "N/A")
        winner_id = match.get("winner_id")
        winner = participant_cache.get(winner_id, "?") if winner_id else "?"
        line += f" — 🏆 {winner} ({score})"
    elif include_state:
        line += f" — {state.title()}"

    return line

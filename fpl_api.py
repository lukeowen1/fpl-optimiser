"""
Thin client around the official (unofficial/public) Fantasy Premier League API.

No auth required for the read-only endpoints used here.
Docs (community-maintained): https://www.oliverlooney.com/blogs/FPL-APIs-Explained
"""
from __future__ import annotations

from dataclasses import dataclass, field

import requests

BASE = "https://fantasy.premierleague.com/api"

# Element type id -> squad position
POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

# Squad composition rules for a standard FPL squad
SQUAD_RULES = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
SQUAD_SIZE = 15
MAX_PER_TEAM = 3
DEFAULT_BUDGET = 100.0  # in £m

# Valid starting-XI formations: (GK, DEF, MID, FWD)
VALID_FORMATIONS = [
    (1, d, m, f)
    for d in range(3, 6)
    for m in range(2, 6)
    for f in range(1, 4)
    if d + m + f == 10
]


def _get(path: str) -> dict:
    resp = requests.get(f"{BASE}/{path}", timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_bootstrap() -> dict:
    """Main endpoint: players, teams, gameweeks, settings."""
    return _get("bootstrap-static/")


def get_fixtures(event: int | None = None) -> list[dict]:
    path = "fixtures/" if event is None else f"fixtures/?event={event}"
    return _get(path)


def get_current_and_next_gw(bootstrap: dict) -> tuple[dict | None, dict | None]:
    events = bootstrap["events"]
    current = next((e for e in events if e["is_current"]), None)
    nxt = next((e for e in events if e["is_next"]), None)
    return current, nxt


def build_players_df(bootstrap: dict):
    """Return a list of player dicts enriched with position/team names."""
    teams_by_id = {t["id"]: t["name"] for t in bootstrap["teams"]}
    players = []
    for el in bootstrap["elements"]:
        players.append(
            {
                "id": el["id"],
                "web_name": el["web_name"],
                "full_name": f"{el['first_name']} {el['second_name']}",
                "team": teams_by_id[el["team"]],
                "team_id": el["team"],
                "position": POSITIONS[el["element_type"]],
                "now_cost": el["now_cost"] / 10.0,  # API stores cost x10
                "start_cost": (el["now_cost"] - el.get("cost_change_start", 0)) / 10.0,
                "ep_next": float(el.get("ep_next") or 0.0),
                "form": float(el.get("form") or 0.0),
                "total_points": el.get("total_points", 0),
                "minutes": el.get("minutes", 0),
                "chance_of_playing_next_round": el.get(
                    "chance_of_playing_next_round"
                ),
                "status": el.get("status"),  # 'a' available, 'i' injured, etc.
                "news": el.get("news", ""),
            }
        )
    return players


# ---------------------------------------------------------------------------
# Your current team
# ---------------------------------------------------------------------------

@dataclass
class CurrentTeam:
    entry_id: int
    team_name: str
    gameweek: int  # the GW whose picks this squad came from
    player_ids: list[int]
    selling_prices: dict[int, float] = field(default_factory=dict)  # £m
    bank: float = 0.0  # £m

    @property
    def budget(self) -> float:
        """Money available if you sold everyone: bank + selling prices."""
        return round(self.bank + sum(self.selling_prices.values()), 1)


def get_entry(entry_id: int) -> dict:
    return _get(f"entry/{entry_id}/")


def get_entry_picks(entry_id: int, gw: int) -> dict:
    return _get(f"entry/{entry_id}/event/{gw}/picks/")


def get_entry_transfers(entry_id: int) -> list[dict]:
    return _get(f"entry/{entry_id}/transfers/")


def selling_price(purchase: float, now: float) -> float:
    """
    FPL selling rule: if a player's price has risen since you bought him you
    only get half the rise (rounded down to the nearest £0.1m). If it's
    fallen, you take the full drop.
    """
    p, n = round(purchase * 10), round(now * 10)
    if n <= p:
        return n / 10.0
    return (p + (n - p) // 2) / 10.0


def get_current_team(entry_id: int, bootstrap: dict, players: list[dict]) -> CurrentTeam:
    """
    Load a manager's current 15-man squad and bank from the public API.

    Uses the picks for the current gameweek (i.e. the squad as of the last
    deadline). Transfers you've already made for the *next* gameweek aren't
    visible publicly, so load the team before making any.

    Purchase prices aren't exposed publicly either, so they're reconstructed:
    the cost of the most recent transfer-in of that player, or his
    start-of-season price if he's been in your squad since GW1.
    """
    current, _ = get_current_and_next_gw(bootstrap)
    if current is None:
        raise ValueError("The season hasn't started yet, so there's no current team to load.")
    gw = current["id"]

    entry = get_entry(entry_id)
    picks = get_entry_picks(entry_id, gw)

    # A Free Hit squad reverts after the gameweek, so use the squad from before it.
    free_hit_gw = None
    if picks.get("active_chip") == "freehit" and gw > 1:
        free_hit_gw = gw
        picks = get_entry_picks(entry_id, gw - 1)

    player_ids = [p["element"] for p in picks["picks"]]
    bank = picks["entry_history"]["bank"] / 10.0

    by_id = {p["id"]: p for p in players}
    transfers = sorted(get_entry_transfers(entry_id), key=lambda t: t["time"], reverse=True)
    purchase_price: dict[int, float] = {}
    for t in transfers:
        if t["event"] == free_hit_gw:
            continue
        purchase_price.setdefault(t["element_in"], t["element_in_cost"] / 10.0)

    selling = {}
    for pid in player_ids:
        now = by_id[pid]["now_cost"]
        bought = purchase_price.get(pid, by_id[pid]["start_cost"])
        selling[pid] = selling_price(bought, now)

    return CurrentTeam(
        entry_id=entry_id,
        team_name=entry.get("name", f"Team {entry_id}"),
        gameweek=gw,
        player_ids=player_ids,
        selling_prices=selling,
        bank=bank,
    )
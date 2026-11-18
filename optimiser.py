"""
Budget-constrained squad optimiser + starting-XI/captain picker for FPL.

Uses integer programming (PuLP / CBC solver, bundled free with PuLP) so the
squad selection is a true optimum for the objective given, not a heuristic.
"""
from __future__ import annotations

import pulp

from fpl_api import MAX_PER_TEAM, SQUAD_RULES, SQUAD_SIZE, VALID_FORMATIONS, CurrentTeam


def optimize_squad(
    players: list[dict],
    budget: float = 100.0,
    objective: str = "ep_next",
    exclude_unavailable: bool = True,
    locked_ids: list[int] | None = None,
    excluded_ids: list[int] | None = None,
    current_team: CurrentTeam | None = None,
    free_transfers: int = 1,
    hit_cost: float = 4.0,
    max_transfers: int | None = None,
    bench_weight: float = 0.1,
) -> list[dict]:
    """
    Pick the 15-man squad whose best starting XI (captain doubled) maximises
    `objective`, with bench players counted at `bench_weight`, subject to:
      - total cost <= budget
      - 2 GK / 5 DEF / 5 MID / 3 FWD
      - max 3 players from any one real-life team
      - locked_ids are forced in, excluded_ids are forced out

    If `current_team` is given, the squad is built *from your existing team*:
      - budget becomes your bank + the selling prices of your players
        (the `budget` argument is ignored)
      - keeping a current player is free; each new player is one transfer
      - transfers beyond `free_transfers` cost `hit_cost` points each, which
        is subtracted from the objective
      - at most `max_transfers` changes (None = unlimited)
    Pass free_transfers=SQUAD_SIZE to model a Wildcard / Free Hit.

    Returns the list of chosen player dicts (unordered).
    """
    locked_ids = set(locked_ids or [])
    excluded_ids = set(excluded_ids or [])
    current_ids = set(current_team.player_ids) if current_team else set()

    pool = players
    if exclude_unavailable:
        # keep players who are available, doubtful, or have no flag set.
        # Your own players always stay in the pool: keeping an injured player
        # costs no transfer, so the solver should decide whether to sell.
        pool = [p for p in pool if p["id"] in current_ids or p["status"] not in ("i", "u", "s", "n")]
    pool = [p for p in pool if p["id"] not in excluded_ids]

    if any(p["id"] not in {pp["id"] for pp in pool} for p in players if p["id"] in locked_ids):
        raise ValueError("A locked player is unavailable or excluded — conflicting constraints.")

    def cost(p: dict) -> float:
        # A player you own "costs" his selling price; anyone else his market price.
        if p["id"] in current_ids:
            return current_team.selling_prices[p["id"]]
        return p["now_cost"]

    if current_team:
        budget = current_team.budget

    prob = pulp.LpProblem("fpl_squad", pulp.LpMaximize)
    x = {p["id"]: pulp.LpVariable(f"x_{p['id']}", cat="Binary") for p in pool}  # in squad
    s = {p["id"]: pulp.LpVariable(f"s_{p['id']}", cat="Binary") for p in pool}  # starts
    c = {p["id"]: pulp.LpVariable(f"c_{p['id']}", cat="Binary") for p in pool}  # captain

    # Only the starting XI (+ captain doubled) actually scores. Bench gets a
    # small weight so it isn't filled with zero-point players, but upgrading
    # the bench should never justify a points hit.
    score = pulp.lpSum(
        p[objective] * (s[p["id"]] + c[p["id"]] + bench_weight * (x[p["id"]] - s[p["id"]]))
        for p in pool
    )
    for pid in x:
        prob += s[pid] <= x[pid]
        prob += c[pid] <= s[pid]
    prob += pulp.lpSum(s.values()) == 11
    prob += pulp.lpSum(c.values()) == 1
    for pos, lo in {"GK": 1, "DEF": 3, "MID": 2, "FWD": 1}.items():
        starters = pulp.lpSum(s[p["id"]] for p in pool if p["position"] == pos)
        prob += starters >= lo
        if pos == "GK":
            prob += starters <= 1

    if current_team:
        kept = pulp.lpSum(x[pid] for pid in current_ids if pid in x)
        n_transfers = SQUAD_SIZE - kept
        hits = pulp.LpVariable("hits", lowBound=0, cat="Integer")
        prob += score - hit_cost * hits
        prob += hits >= n_transfers - free_transfers
        if max_transfers is not None:
            prob += n_transfers <= max_transfers
    else:
        prob += score

    prob += pulp.lpSum(cost(p) * x[p["id"]] for p in pool) <= budget
    prob += pulp.lpSum(x.values()) == SQUAD_SIZE

    for pos, count in SQUAD_RULES.items():
        prob += pulp.lpSum(x[p["id"]] for p in pool if p["position"] == pos) == count

    team_ids = {p["team_id"] for p in pool}
    for tid in team_ids:
        prob += pulp.lpSum(x[p["id"]] for p in pool if p["team_id"] == tid) <= MAX_PER_TEAM

    for pid in locked_ids:
        if pid in x:
            prob += x[pid] == 1

    status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(f"Solver could not find an optimal squad: {pulp.LpStatus[status]}")

    squad = [p for p in pool if pulp.value(x[p["id"]]) > 0.5]
    if current_team:
        squad = [{**p, "now_cost": cost(p)} if p["id"] in current_ids else p for p in squad]
    return squad


def suggest_transfers(
    players: list[dict],
    current_team: CurrentTeam,
    free_transfers: int = 1,
    max_transfers: int | None = None,
    objective: str = "ep_next",
    hit_cost: float = 4.0,
) -> dict:
    """
    Run the optimiser against your current team and summarise the result:
    {"out": [...], "in": [...] (paired by position), "hits": int,
     "xi_before": float, "xi_after": float, "net": float, "captain": dict}
    """
    by_id = {p["id"]: p for p in players}
    squad = optimize_squad(
        players, objective=objective, current_team=current_team,
        free_transfers=free_transfers, max_transfers=max_transfers, hit_cost=hit_cost,
    )
    order = ["GK", "DEF", "MID", "FWD"]
    cur_ids = set(current_team.player_ids)
    new_ids = {p["id"] for p in squad}
    outs = sorted((by_id[i] for i in cur_ids - new_ids), key=lambda p: order.index(p["position"]))
    ins = sorted((p for p in squad if p["id"] not in cur_ids), key=lambda p: order.index(p["position"]))
    hits = max(0, len(ins) - free_transfers)

    def best_xi(sq):
        xi, cap, _ = pick_starting_xi(sq, objective=objective)
        return sum(p[objective] for p in xi) + cap[objective], cap

    before, _ = best_xi([by_id[i] for i in current_team.player_ids])
    after, captain = best_xi(squad)
    return {
        "out": outs, "in": ins, "hits": hits,
        "xi_before": before, "xi_after": after,
        "net": after - hit_cost * hits - before, "captain": captain,
    }


def pick_starting_xi(
    squad: list[dict], objective: str = "ep_next"
) -> tuple[list[dict], dict, dict]:
    """
    From a 15-man squad, pick the best valid starting XI (one valid formation),
    plus captain and vice-captain (highest and second-highest `objective`
    among the XI). Captain's points are doubled in the objective used to
    choose the XI, matching how FPL scoring actually works.

    Returns (starting_xi, captain, vice_captain).
    """
    best = None
    for gk, d, m, f in VALID_FORMATIONS:
        need = {"GK": gk, "DEF": d, "MID": m, "FWD": f}
        prob = pulp.LpProblem("fpl_xi", pulp.LpMaximize)
        x = {p["id"]: pulp.LpVariable(f"y_{p['id']}", cat="Binary") for p in squad}
        cap = {p["id"]: pulp.LpVariable(f"c_{p['id']}", cat="Binary") for p in squad}

        # captain must be one of the selected XI, and only one captain
        for p in squad:
            prob += cap[p["id"]] <= x[p["id"]]
        prob += pulp.lpSum(cap.values()) == 1

        prob += pulp.lpSum(
            p[objective] * x[p["id"]] + p[objective] * cap[p["id"]] for p in squad
        )

        for pos, count in need.items():
            prob += pulp.lpSum(x[p["id"]] for p in squad if p["position"] == pos) == count

        status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
        if pulp.LpStatus[status] != "Optimal":
            continue

        score = pulp.value(prob.objective)
        if best is None or score > best[0]:
            xi = [p for p in squad if pulp.value(x[p["id"]]) > 0.5]
            captain = next(p for p in squad if pulp.value(cap[p["id"]]) > 0.5)
            best = (score, xi, captain)

    if best is None:
        raise RuntimeError("No valid formation could be built from this squad.")

    _, xi, captain = best
    bench_or_xi_sorted = sorted(xi, key=lambda p: p[objective], reverse=True)
    vice = next(p for p in bench_or_xi_sorted if p["id"] != captain["id"])
    return xi, captain, vice
"""
FPL Team Picker — Streamlit dashboard.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # dotenv is optional; env vars can be set another way

from fpl_api import (
    SQUAD_SIZE,
    build_players_df,
    get_bootstrap,
    get_current_and_next_gw,
    get_current_team,
)
from optimiser import optimize_squad, pick_starting_xi

st.set_page_config(page_title="FPL Team Picker", page_icon="⚽", layout="wide")

POS_ORDER = ["GK", "DEF", "MID", "FWD"]


@st.cache_data(ttl=3600)
def load_data():
    bootstrap = get_bootstrap()
    players = build_players_df(bootstrap)
    current_gw, next_gw = get_current_and_next_gw(bootstrap)
    return bootstrap, players, current_gw, next_gw


@st.cache_data(ttl=600)
def load_team(entry_id: int):
    bootstrap, players, _, _ = load_data()
    return get_current_team(entry_id, bootstrap, players)


def xi_score(xi, captain, objective):
    return sum(p[objective] for p in xi) + captain[objective]  # captain doubled


st.title("⚽ FPL Team Picker")

with st.spinner("Fetching live FPL data..."):
    try:
        _, players, current_gw, next_gw = load_data()
    except Exception as e:
        st.error(f"Couldn't reach the FPL API: {e}")
        st.stop()

col1, col2 = st.columns(2)
with col1:
    if current_gw:
        st.metric("Current gameweek", f"GW{current_gw['id']}")
with col2:
    if next_gw:
        st.metric("Next deadline", next_gw["deadline_time"].replace("T", " ").replace("Z", " UTC"))

players_by_id = {p["id"]: p for p in players}

# --- Your team -------------------------------------------------------------
st.sidebar.header("Your team")
entry_input = st.sidebar.text_input(
    "FPL team ID",
    value=os.environ.get("FPL_TEAM_ID", ""),
    help="The number in your Points page URL: fantasy.premierleague.com/entry/<ID>/event/...",
)
current_team = None
if entry_input.strip():
    if not entry_input.strip().isdigit():
        st.sidebar.error("Team ID should be a number.")
    else:
        try:
            current_team = load_team(int(entry_input.strip()))
        except Exception as e:
            st.sidebar.error(f"Couldn't load that team: {e}")

if current_team:
    free_transfers = st.sidebar.number_input(
        "Free transfers available", min_value=0, max_value=5, value=1, step=1,
        help="Check the Transfers page in the FPL app — this isn't available from the public API.",
    )
    chip = st.sidebar.selectbox("Chip", ["None", "Wildcard", "Free Hit"])
    if chip != "None":
        free_transfers = SQUAD_SIZE  # unlimited free transfers
    max_transfers = st.sidebar.slider("Max transfers to suggest", 0, SQUAD_SIZE, SQUAD_SIZE)
else:
    free_transfers, max_transfers = 1, None
    st.sidebar.caption("Leave blank to pick a squad from scratch.")

# --- Settings --------------------------------------------------------------
st.sidebar.header("Settings")
if current_team:
    budget = current_team.budget
    st.sidebar.metric("Budget (bank + selling prices)", f"£{budget:.1f}m")
else:
    budget = st.sidebar.number_input("Budget (£m)", min_value=80.0, max_value=100.0, value=100.0, step=0.1)
objective = st.sidebar.selectbox(
    "Optimise for",
    options=["ep_next", "form", "total_points"],
    format_func=lambda x: {
        "ep_next": "Expected points (next GW)",
        "form": "Recent form",
        "total_points": "Total points this season",
    }[x],
)
exclude_unavailable = st.sidebar.checkbox("Exclude injured/unavailable players", value=True)

all_names = {p["id"]: f"{p['web_name']} ({p['team']})" for p in players}
locked = st.sidebar.multiselect(
    "Lock these players in (optional)",
    options=list(all_names.keys()),
    format_func=lambda i: all_names[i],
)
excluded = st.sidebar.multiselect(
    "Exclude these players (optional)",
    options=list(all_names.keys()),
    format_func=lambda i: all_names[i],
)

if current_team:
    st.subheader(f"Current squad — {current_team.team_name} (as of GW{current_team.gameweek})")
    cur_rows = []
    for pid in current_team.player_ids:
        p = players_by_id[pid]
        cur_rows.append(
            {
                "Pos": p["position"],
                "Player": p["web_name"],
                "Team": p["team"],
                "Sell": f"£{current_team.selling_prices[pid]:.1f}",
                "EP (next)": p["ep_next"],
                "Form": p["form"],
                "News": p["news"],
            }
        )
    cur_rows.sort(key=lambda r: POS_ORDER.index(r["Pos"]))
    st.dataframe(pd.DataFrame(cur_rows), hide_index=True, use_container_width=True)
    st.caption(f"Bank: £{current_team.bank:.1f}m · Selling prices are estimated from your transfer history.")

button_label = "Suggest transfers" if current_team else "Optimise squad"
if st.sidebar.button(button_label, type="primary"):
    with st.spinner("Solving..."):
        try:
            squad = optimize_squad(
                players,
                budget=budget,
                objective=objective,
                exclude_unavailable=exclude_unavailable,
                locked_ids=locked,
                excluded_ids=excluded,
                current_team=current_team,
                free_transfers=free_transfers,
                max_transfers=max_transfers,
            )
            xi, captain, vice = pick_starting_xi(squad, objective=objective)
        except Exception as e:
            st.error(str(e))
            st.stop()

    bench = [p for p in squad if p["id"] not in {p2["id"] for p2 in xi}]
    total_cost = sum(p["now_cost"] for p in squad)
    total_ep = xi_score(xi, captain, objective)

    if current_team:
        squad_ids = {p["id"] for p in squad}
        current_ids = set(current_team.player_ids)
        outs = sorted((players_by_id[i] for i in current_ids - squad_ids), key=lambda p: POS_ORDER.index(p["position"]))
        ins = sorted((p for p in squad if p["id"] not in current_ids), key=lambda p: POS_ORDER.index(p["position"]))
        n_hits = max(0, len(ins) - free_transfers)
        hit_pts = 4 * n_hits

        cur_squad = [{**players_by_id[i], "now_cost": current_team.selling_prices[i]} for i in current_team.player_ids]
        cur_xi, cur_cap, _ = pick_starting_xi(cur_squad, objective=objective)
        cur_ep = xi_score(cur_xi, cur_cap, objective)
        bank_after = budget - total_cost

        st.subheader("Suggested transfers")
        if not ins:
            st.success(f"Roll the transfer — no change beats your current squad. Projected {cur_ep:.1f} pts.")
        else:
            rows = [
                {
                    "Out": f"{o['web_name']} ({o['team']}) £{current_team.selling_prices[o['id']]:.1f}",
                    "In": f"{i['web_name']} ({i['team']}) £{i['now_cost']:.1f}",
                    "Pos": i["position"],
                    "Gain": round(i[objective] - o[objective], 1),
                }
                for o, i in zip(outs, ins)
            ]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            hit_text = f" − {hit_pts} pt hit" if n_hits else ""
            st.success(
                f"{len(ins)} transfer(s){hit_text}. Starting XI: {cur_ep:.1f} → {total_ep:.1f} pts "
                f"(net {total_ep - hit_pts - cur_ep:+.1f}). Bank after: £{bank_after:.1f}m"
            )
    else:
        st.success(f"Optimal squad found — £{total_cost:.1f}m spent, projected {total_ep:.1f} pts (with captain)")

    st.subheader("Starting XI")
    xi_rows = []
    for p in sorted(xi, key=lambda p: ["GK", "DEF", "MID", "FWD"].index(p["position"])):
        tag = "🇨" if p["id"] == captain["id"] else ("🇻" if p["id"] == vice["id"] else "")
        xi_rows.append(
            {
                "": tag,
                "Pos": p["position"],
                "Player": p["web_name"],
                "Team": p["team"],
                "Cost": f"£{p['now_cost']:.1f}",
                "EP (next)": p["ep_next"],
                "Form": p["form"],
            }
        )
    st.dataframe(pd.DataFrame(xi_rows), hide_index=True, use_container_width=True)
    st.caption("🇨 Captain (points doubled) · 🇻 Vice-captain")

    st.subheader("Bench")
    bench_rows = [
        {
            "Pos": p["position"],
            "Player": p["web_name"],
            "Team": p["team"],
            "Cost": f"£{p['now_cost']:.1f}",
            "EP (next)": p["ep_next"],
        }
        for p in sorted(bench, key=lambda p: ["GK", "DEF", "MID", "FWD"].index(p["position"]))
    ]
    st.dataframe(pd.DataFrame(bench_rows), hide_index=True, use_container_width=True)

else:
    st.info(f"Enter your FPL team ID and adjust settings in the sidebar, then click **{button_label}**.")
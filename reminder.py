"""
Sends an email reminder once per gameweek when the next FPL deadline is
within REMINDER_HOURS_BEFORE hours. If FPL_TEAM_ID is set, the email also
includes suggested transfers and a captain pick for your team.

Designed to be run frequently by a scheduler (launchd/cron, e.g. hourly).
It records the last gameweek it reminded you about in .last_reminder_gw, so
it only sends once per deadline however often it runs.

Config is read from environment variables / .env (set either or both):
  NTFY_TOPIC  — phone push notifications via the free ntfy app
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, FROM_EMAIL, TO_EMAIL
  REMINDER_HOURS_BEFORE (default 24)
  FPL_TEAM_ID (optional)

Usage:
  python3 reminder.py           # normal scheduled run
  python3 reminder.py --test    # send a reminder now, ignoring the window
"""
from __future__ import annotations

import os
import smtplib
import sys
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

from fpl_api import build_players_df, get_bootstrap, get_current_and_next_gw, get_current_team

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / ".last_reminder_gw"

try:
    from dotenv import load_dotenv

    load_dotenv(HERE / ".env")
except ImportError:
    pass  # dotenv is optional; env vars can be set another way


def send_email(subject: str, body: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    from_email = os.environ.get("FROM_EMAIL") or user
    to_email = os.environ["TO_EMAIL"]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(from_email, [to_email], msg.as_string())


def send_push(title: str, body: str) -> None:
    """Phone push notification via ntfy.sh (free app, no account needed)."""
    import requests

    resp = requests.post(
        f"https://ntfy.sh/{os.environ['NTFY_TOPIC']}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Tags": "soccer", "Priority": "high"},
        timeout=15,
    )
    resp.raise_for_status()


def notify(title: str, body: str) -> None:
    """Send via every channel that's configured in .env."""
    sent = []
    if os.environ.get("NTFY_TOPIC"):
        send_push(title, body)
        sent.append("push")
    if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"):
        send_email(f"⚽ {title}", body)
        sent.append("email")
    if not sent:
        raise RuntimeError("No notification channel configured: set NTFY_TOPIC and/or SMTP_* in .env")
    print(f"Reminder sent via {' + '.join(sent)}.")


def team_section(bootstrap: dict, team_id: int) -> str:
    """Suggested transfers + captain for your team, as plain text."""
    from optimiser import suggest_transfers  # imported lazily: PuLP is slow to load

    players = build_players_df(bootstrap)
    team = get_current_team(team_id, bootstrap, players)
    lines = [f"Your team: {team.team_name} (bank £{team.bank:.1f}m)", ""]

    def describe(title: str, s: dict) -> None:
        lines.append(title)
        if not s["in"]:
            lines.append("  No transfer improves your team — roll it.")
        for o, i in zip(s["out"], s["in"]):
            lines.append(
                f"  OUT {o['web_name']} ({o['team']}, {o['ep_next']:.1f} xP)  ->  "
                f"IN {i['web_name']} ({i['team']}, £{i['now_cost']:.1f}m, {i['ep_next']:.1f} xP)"
            )
        hit = f", -{4 * s['hits']} hit" if s["hits"] else ""
        lines.append(
            f"  Starting XI: {s['xi_before']:.1f} -> {s['xi_after']:.1f} xP"
            f"{hit} (net {s['net']:+.1f})"
        )
        lines.append("")

    single = suggest_transfers(players, team, free_transfers=1, max_transfers=1)
    best = suggest_transfers(players, team, free_transfers=1)
    describe("Best single transfer:", single)
    if len(best["in"]) > 1:
        describe("Best overall (assuming 1 free transfer, hits included):", best)

    cap = single["captain"]
    lines.append(f"Captain: {cap['web_name']} ({cap['team']}, {cap['ep_next']:.1f} xP)")

    flagged = [p for p in players if p["id"] in team.player_ids and p["news"]]
    if flagged:
        lines += ["", "Injury/availability news in your squad:"]
        lines += [f"  {p['web_name']}: {p['news']}" for p in flagged]

    lines += ["", "xP = FPL's expected points for next gameweek. Run the dashboard for the full picture."]
    return "\n".join(lines)


def main(test: bool = False) -> None:
    hours_before = float(os.environ.get("REMINDER_HOURS_BEFORE", "24"))

    bootstrap = get_bootstrap()
    _, next_gw = get_current_and_next_gw(bootstrap)
    if not next_gw:
        print("No upcoming gameweek found (season may be over).")
        return

    gw = next_gw["id"]
    deadline = datetime.fromisoformat(next_gw["deadline_time"].replace("Z", "+00:00"))
    time_left = deadline - datetime.now(timezone.utc)
    print(f"{datetime.now():%Y-%m-%d %H:%M} | GW{gw} deadline {deadline:%a %d %b %H:%M} UTC | {time_left} left")

    already_sent = STATE_FILE.exists() and STATE_FILE.read_text().strip() == str(gw)
    in_window = timedelta(0) < time_left <= timedelta(hours=hours_before)
    if not test and (already_sent or not in_window):
        print("Already reminded for this GW." if already_sent else "Outside reminder window — no email sent.")
        return

    hours = time_left.total_seconds() / 3600
    left = f"{hours:.0f}h" if hours >= 2 else f"{time_left.total_seconds() / 60:.0f} min"
    body = (
        f"Gameweek {gw} deadline: {deadline:%a %d %b, %H:%M} UTC "
        f"(about {left} from now).\n\n"
    )
    team_id = os.environ.get("FPL_TEAM_ID", "").strip()
    if team_id:
        try:
            body += team_section(bootstrap, int(team_id))
        except Exception:
            traceback.print_exc()
            body += "(Couldn't build transfer suggestions this week — check the dashboard.)"
    else:
        body += "Don't forget to set your team!"

    notify(f"FPL GW{gw} deadline in {left}", body)
    if not test:
        STATE_FILE.write_text(str(gw))


if __name__ == "__main__":
    main(test="--test" in sys.argv)

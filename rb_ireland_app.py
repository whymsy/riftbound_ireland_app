import json
import streamlit as st
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
from collections import defaultdict
from datetime import datetime, timedelta

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

PAIRINGS_FILE = "riftbound_pairings.json"
PLAYERS_FILE  = "players.json"

DEFAULT_RATING = 900.0

K_FACTORS = {
    "release": 8,
    "locals":  16,
    "ss":      40,
}

st.set_page_config(
    page_title="Irish Riftbound Rankings",
    page_icon="⚔️",
    layout="wide",
)

# ─────────────────────────────────────────────────────────────────────────────
# ELO ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def expected_score(ra, rb):
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))

def process_pairing(p1_id, p2_id, result, K, ratings):
    ra = ratings.get(p1_id, DEFAULT_RATING)
    rb = ratings.get(p2_id, DEFAULT_RATING)
    ea = expected_score(ra, rb)

    if result == "p1":
        sa, sb = 1.0, 0.0
    elif result == "p2":
        sa, sb = 0.0, 1.0
    else:
        sa, sb = 0.5, 0.5

    ratings[p1_id] = ra + K * (sa - ea)
    ratings[p2_id] = rb + K * (sb - (1.0 - ea))

def run_elo(pairings_data):
    events      = sorted(pairings_data, key=lambda e: e.get("date") or "")
    ratings     = {}
    event_count = defaultdict(int)
    match_count = defaultdict(int)
    history     = defaultdict(list)

    for event in events:
        tier = event.get("tier", "locals")
        K    = K_FACTORS.get(tier, 16)
        seen_in_event = set()

        for round_ in event.get("rounds", []):
            for pairing in round_.get("pairings", []):
                p1_id  = pairing["p1_id"]
                p2_id  = pairing["p2_id"]
                result = pairing["result"]

                if p1_id not in ratings:
                    ratings[p1_id] = DEFAULT_RATING
                if p2_id not in ratings:
                    ratings[p2_id] = DEFAULT_RATING

                process_pairing(p1_id, p2_id, result, K, ratings)
                match_count[p1_id] += 1
                match_count[p2_id] += 1
                seen_in_event.add(p1_id)
                seen_in_event.add(p2_id)

        for pid in seen_in_event:
            event_count[pid] += 1
            history[pid].append((event["date"], round(ratings[pid], 1)))

    return ratings, event_count, match_count, history

# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING (cached so it only runs once per session)
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data
def load_data():
    with open(PAIRINGS_FILE) as f:
        pairings_data = json.load(f)
    with open(PLAYERS_FILE) as f:
        registry = json.load(f)
    ratings, event_count, match_count, history = run_elo(pairings_data)
    ranked = sorted(ratings.items(), key=lambda x: -x[1])
    return pairings_data, registry, ratings, event_count, match_count, history, ranked

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_rating_delta(pid, history, days=7):
    if pid not in history or not history[pid]:
        return None
    cutoff  = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    current = history[pid][-1][1]
    before  = [r for date, r in history[pid] if date <= cutoff]
    if not before:
        return None
    return current - before[-1]

def find_player(name_query, registry):
    """Return (pid, name) for exact or partial match, or None."""
    name_to_id = {v.lower(): k for k, v in registry.items()}
    pid = name_to_id.get(name_query.lower())
    if pid:
        return pid, registry[pid]
    # Partial
    matches = [(k, registry[k]) for k, v in registry.items()
               if name_query.lower() in v.lower()]
    if len(matches) == 1:
        return matches[0]
    return None, None

def tier_badge(tier):
    colours = {"ss": "🟣", "locals": "🔵", "release": "🟢"}
    labels  = {"ss": "SS", "locals": "Locals", "release": "Release"}
    return f"{colours.get(tier,'⚪')} {labels.get(tier, tier.title())}"

# ─────────────────────────────────────────────────────────────────────────────
# PAGE: RANKINGS
# ─────────────────────────────────────────────────────────────────────────────

def page_rankings(pairings_data, registry, ratings, event_count, match_count, history, ranked):
    st.title("⚔️ Irish Riftbound — ELO Rankings")

    last_event = max((e.get("date","") for e in pairings_data), default="?")
    st.caption(f"Last updated: {last_event}  ·  {len(pairings_data)} events  ·  {len(ranked)} rated players")

    col1, col2, col3 = st.columns(3)
    top_n      = col1.slider("Show top N players", 10, len(ranked), 50)
    min_events = col2.slider("Min events played", 0, 20, 0)
    delta_days = col3.selectbox("Rating delta window", [7, 14, 30], index=0,
                                format_func=lambda d: f"±{d} days")

    filtered = [
        (pid, rating) for pid, rating in ranked
        if event_count[pid] >= min_events
        and rating != DEFAULT_RATING
    ][:top_n]

    rows = []
    for i, (pid, rating) in enumerate(filtered, 1):
        name    = registry.get(pid, f"Unknown({pid})")
        delta   = get_rating_delta(pid, history, delta_days)
        if delta is None:
            delta_str = "NEW"
        elif delta > 0:
            delta_str = f"+{delta:.1f}"
        elif delta < 0:
            delta_str = f"{delta:.1f}"
        else:
            delta_str = "—"

        rows.append({
            "Rank":    i,
            "Player":  name,
            "Rating":  round(rating, 1),
            f"±{delta_days}d": delta_str,
            "Events":  event_count[pid],
            "Matches": match_count[pid],
        })

    df = pd.DataFrame(rows)

    def colour_delta(val):
        if isinstance(val, str):
            if val.startswith("+"):
                return "color: #2ecc71"
            if val.startswith("-"):
                return "color: #e74c3c"
        return ""

    styled = df.style.applymap(colour_delta, subset=[f"±{delta_days}d"])
    st.dataframe(styled, use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────────────────────────────────────
# PAGE: PLAYER PROFILE
# ─────────────────────────────────────────────────────────────────────────────

def page_player(pairings_data, registry, ratings, event_count, match_count, history, ranked):
    st.title("👤 Player Profile")

    name_query = st.text_input("Search player name", placeholder="e.g. Nuge")
    if not name_query:
        return

    pid, name = find_player(name_query, registry)
    if not pid:
        st.error(f"No player found matching '{name_query}'")
        return

    rating  = ratings.get(pid, DEFAULT_RATING)
    rank    = next((i + 1 for i, (p, _) in enumerate(ranked) if p == pid), None)

    # Summary cards
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("ELO Rating", f"{rating:.1f}")
    c2.metric("Rank", f"#{rank}")
    c3.metric("Events", event_count[pid])
    c4.metric("Matches", match_count[pid])

    st.divider()

    # Rating trend
    snapshots = history.get(pid, [])
    if snapshots:
        st.subheader("📈 Rating Over Time")
        dates_raw = [datetime.strptime(d, "%Y-%m-%d") for d, _ in snapshots]
        vals      = [r for _, r in snapshots]

        # Weekly resample
        week_map = {}
        for date, r in zip(dates_raw, vals):
            week_key = date - timedelta(days=date.weekday())
            week_map[week_key] = r
        dates_w = sorted(week_map.keys())
        vals_w  = [week_map[d] for d in dates_w]

        fig, ax = plt.subplots(figsize=(10, 3))
        ax.plot(dates_w, vals_w, marker="o", markersize=4, linewidth=2, color="#7c3aed")
        ax.axhline(DEFAULT_RATING, color="grey", linestyle="--", linewidth=0.8, alpha=0.5)
        ax.fill_between(dates_w, DEFAULT_RATING, vals_w,
                        where=[v >= DEFAULT_RATING for v in vals_w],
                        alpha=0.15, color="#7c3aed")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO, interval=2))
        plt.xticks(rotation=45, ha="right")
        ax.set_ylabel("ELO Rating")
        ax.grid(True, alpha=0.2)
        plt.tight_layout()
        st.pyplot(fig)
        plt.close()

    st.divider()

    # Tournament history
    st.subheader("🏆 Tournament History")
    sorted_events = sorted(pairings_data, key=lambda e: e.get("date") or "", reverse=True)

    total_w = total_l = total_d = 0

    for event in sorted_events:
        player_rounds = []
        for round_ in event.get("rounds", []):
            rp = [p for p in round_.get("pairings", [])
                  if p["p1_id"] == pid or p["p2_id"] == pid]
            if rp:
                player_rounds.append((round_["round"], rp))

        if not player_rounds:
            continue

        standing = next(
            (s for s in event.get("standings", []) if s["player_id"] == pid), None
        )

        placement = f"#{standing['rank']}  {standing['record']}" if standing else "—"
        with st.expander(
            f"{tier_badge(event.get('tier','?'))}  {event['date']}  ·  "
            f"**{event['name']}**  ·  {placement}"
        ):
            match_rows = []
            for round_num, pairings in player_rounds:
                for p in pairings:
                    if p["p1_id"] == pid:
                        opponent = p["p2"]
                        result   = p["result"]
                        outcome  = "W" if result == "p1" else ("L" if result == "p2" else "D")
                    else:
                        opponent = p["p1"]
                        result   = p["result"]
                        outcome  = "W" if result == "p2" else ("L" if result == "p1" else "D")

                    if outcome == "W":   total_w += 1
                    elif outcome == "L": total_l += 1
                    else:                total_d += 1

                    match_rows.append({
                        "Round":    f"R{round_num}",
                        "Result":   outcome,
                        "Opponent": opponent,
                    })

            mdf = pd.DataFrame(match_rows)

            def colour_result(val):
                if val == "W": return "color: #2ecc71; font-weight: bold"
                if val == "L": return "color: #e74c3c; font-weight: bold"
                return "color: #f39c12; font-weight: bold"

            st.dataframe(
                mdf.style.applymap(colour_result, subset=["Result"]),
                use_container_width=True,
                hide_index=True,
            )

    if total_w + total_l + total_d > 0:
        total   = total_w + total_l + total_d
        win_pct = total_w / total * 100
        st.divider()
        cc1, cc2, cc3, cc4 = st.columns(4)
        cc1.metric("Wins",   total_w)
        cc2.metric("Losses", total_l)
        cc3.metric("Draws",  total_d)
        cc4.metric("Win Rate", f"{win_pct:.1f}%")

# ─────────────────────────────────────────────────────────────────────────────
# PAGE: HEAD TO HEAD
# ─────────────────────────────────────────────────────────────────────────────

def page_h2h(pairings_data, registry, ratings, ranked):
    st.title("⚔️ Head-to-Head")

    col1, col2 = st.columns(2)
    name_a = col1.text_input("Player A", placeholder="e.g. Nuge")
    name_b = col2.text_input("Player B", placeholder="e.g. Cia Reeves")

    if not name_a or not name_b:
        return

    pid_a, full_a = find_player(name_a, registry)
    pid_b, full_b = find_player(name_b, registry)

    if not pid_a:
        st.error(f"Player not found: {name_a}")
        return
    if not pid_b:
        st.error(f"Player not found: {name_b}")
        return

    rank_a = next((i + 1 for i, (p, _) in enumerate(ranked) if p == pid_a), "?")
    rank_b = next((i + 1 for i, (p, _) in enumerate(ranked) if p == pid_b), "?")

    wins_a = wins_b = draws = 0
    match_rows = []

    for event in sorted(pairings_data, key=lambda e: e.get("date") or ""):
        for round_ in event.get("rounds", []):
            for p in round_.get("pairings", []):
                ids = {p["p1_id"], p["p2_id"]}
                if pid_a not in ids or pid_b not in ids:
                    continue

                result = p["result"]
                if result == "draw":
                    outcome = "Draw"
                    draws  += 1
                elif (result == "p1" and p["p1_id"] == pid_a) or \
                     (result == "p2" and p["p2_id"] == pid_a):
                    outcome = f"{full_a} wins"
                    wins_a += 1
                else:
                    outcome = f"{full_b} wins"
                    wins_b += 1

                match_rows.append({
                    "Date":    event["date"],
                    "Event":   event["name"],
                    "Tier":    event.get("tier","?").title(),
                    "Round":   f"R{round_['round']}",
                    "Result":  outcome,
                })

    if not match_rows:
        st.info(f"No matches found between {full_a} and {full_b}.")
        return

    total = wins_a + wins_b + draws

    # Summary
    c1, c2, c3 = st.columns(3)
    c1.metric(f"{full_a}  (#{rank_a})", f"{wins_a} wins")
    c2.metric("Draws", draws)
    c3.metric(f"{full_b}  (#{rank_b})", f"{wins_b} wins")

    # Win bar
    if total > 0:
        pct_a = wins_a / total
        pct_b = wins_b / total
        fig, ax = plt.subplots(figsize=(8, 0.6))
        ax.barh(0, pct_a, color="#7c3aed", height=0.5)
        ax.barh(0, pct_b, left=pct_a, color="#e74c3c", height=0.5)
        ax.barh(0, draws / total, left=pct_a + pct_b, color="#555", height=0.5)
        ax.set_xlim(0, 1)
        ax.axis("off")
        ax.text(pct_a / 2, 0, f"{pct_a*100:.0f}%", ha="center", va="center",
                color="white", fontsize=10, fontweight="bold")
        ax.text(pct_a + pct_b / 2, 0, f"{pct_b*100:.0f}%", ha="center", va="center",
                color="white", fontsize=10, fontweight="bold")
        plt.tight_layout()
        st.pyplot(fig)
        plt.close()

    st.divider()

    df = pd.DataFrame(match_rows)

    def colour_result(val):
        if full_a in val:   return "color: #7c3aed; font-weight: bold"
        if full_b in val:   return "color: #e74c3c; font-weight: bold"
        return "color: #888"

    st.dataframe(
        df.style.applymap(colour_result, subset=["Result"]),
        use_container_width=True,
        hide_index=True,
    )

# ─────────────────────────────────────────────────────────────────────────────
# PAGE: RATING COMPARISON
# ─────────────────────────────────────────────────────────────────────────────

def page_compare(pairings_data, registry, history):
    st.title("📈 Rating Comparison")

    names_input = st.text_input(
        "Enter player names (comma-separated)",
        placeholder="e.g. Nuge, Cia Reeves, Topazgoose"
    )
    if not names_input:
        return

    name_queries = [n.strip() for n in names_input.split(",") if n.strip()]

    fig, ax = plt.subplots(figsize=(12, 6))
    colours = ["#7c3aed","#e74c3c","#2ecc71","#f39c12","#3498db",
               "#e91e63","#00bcd4","#ff5722","#8bc34a","#9c27b0"]
    found = 0

    for i, name_query in enumerate(name_queries):
        pid, name = find_player(name_query, registry)
        if not pid:
            st.warning(f"Player not found: {name_query}")
            continue

        snapshots = history.get(pid, [])
        if not snapshots:
            st.warning(f"No history for {name}")
            continue

        dates_raw = [datetime.strptime(d, "%Y-%m-%d") for d, _ in snapshots]
        vals      = [r for _, r in snapshots]

        week_map = {}
        for date, r in zip(dates_raw, vals):
            week_key = date - timedelta(days=date.weekday())
            week_map[week_key] = r
        dates_w = sorted(week_map.keys())
        vals_w  = [week_map[d] for d in dates_w]

        colour = colours[i % len(colours)]
        ax.plot(dates_w, vals_w, marker="o", markersize=4, linewidth=2,
                label=name, color=colour)
        ax.annotate(
            f"  {name}  {vals_w[-1]:.0f}",
            xy=(dates_w[-1], vals_w[-1]),
            xytext=(6, 0),
            textcoords="offset points",
            fontsize=9,
            va="center",
            color=colour,
        )
        found += 1

    if found == 0:
        plt.close()
        return

    ax.axhline(DEFAULT_RATING, color="grey", linestyle="--",
               linewidth=0.8, alpha=0.5, label="Starting rating")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO, interval=2))
    plt.xticks(rotation=45, ha="right")
    ax.set_ylabel("ELO Rating")
    ax.set_title("ELO Rating Over Time")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.2)
    plt.tight_layout()
    st.pyplot(fig)
    plt.close()

# ─────────────────────────────────────────────────────────────────────────────
# PAGE: EVENTS
# ─────────────────────────────────────────────────────────────────────────────

def page_events(pairings_data, registry):
    st.title("📅 Events")

    sorted_events = sorted(pairings_data, key=lambda e: e.get("date") or "", reverse=True)

    for event in sorted_events:
        standings = event.get("standings", [])
        n_players = len(standings) or "?"
        with st.expander(
            f"{tier_badge(event.get('tier','?'))}  {event['date']}  ·  "
            f"**{event['name']}**  ·  {n_players} players"
        ):
            st.caption(f"📍 {event.get('location','?')}  ·  {event.get('n_rounds','?')} rounds")

            if standings:
                rows = []
                for s in standings:
                    rows.append({
                        "Rank":   s["rank"],
                        "Player": s["name"],
                        "Record": s["record"],
                        "Points": s["points"],
                    })
                st.dataframe(
                    pd.DataFrame(rows),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("No standings data for this event.")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    try:
        pairings_data, registry, ratings, event_count, match_count, history, ranked = load_data()
    except FileNotFoundError as e:
        st.error(f"Data file not found: {e}\n\nMake sure `riftbound_pairings.json` and `players.json` are in the same folder as `app.py`.")
        return

    page = st.sidebar.radio(
        "Navigation",
        ["🏅 Rankings", "👤 Player Profile", "⚔️ Head-to-Head", "📈 Compare Ratings", "📅 Events"],
    )

    st.sidebar.divider()
    st.sidebar.caption("Irish Riftbound Rankings")
    st.sidebar.caption(f"{len(pairings_data)} events · {len(ranked)} players")

    if page == "🏅 Rankings":
        page_rankings(pairings_data, registry, ratings, event_count, match_count, history, ranked)
    elif page == "👤 Player Profile":
        page_player(pairings_data, registry, ratings, event_count, match_count, history, ranked)
    elif page == "⚔️ Head-to-Head":
        page_h2h(pairings_data, registry, ratings, ranked)
    elif page == "📈 Compare Ratings":
        page_compare(pairings_data, registry, history)
    elif page == "📅 Events":
        page_events(pairings_data, registry)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
Riftbound Event Scraper
========================
Scrapes pairing data from locator.riftbound.uvsgames.com into JSON files.

Usage:
    python riftbound_scraper.py                        # scrape all unscraped events
    python riftbound_scraper.py --register 224858      # register a new event
    python riftbound_scraper.py --register 224858 229100 231500  # register multiple
    python riftbound_scraper.py --list                 # list all registered events
    python riftbound_scraper.py --reset 224858         # mark event as unscraped
    python riftbound_scraper.py --force                # re-scrape all events

Files:
    events.json          - event registry (metadata + scrape status)
    players.json         - player ID → display name registry
    riftbound_pairings.json - all scraped pairing data
"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import argparse
import json
import re
import time
import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from playwright.sync_api import sync_playwright

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BASE_API = "https://api.cloudflare.riftbound.uvsgames.com/hydraproxy/api/v2"
BASE_URL = "https://locator.riftbound.uvsgames.com"

EVENTS_FILE   = "events.json"
PLAYERS_FILE  = "players.json"
PAIRINGS_FILE = "riftbound_pairings.json"

# ─────────────────────────────────────────────────────────────────────────────
# FILE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def load_registry():
    return load_json(PLAYERS_FILE, {})

def save_registry(registry):
    save_json(PLAYERS_FILE, registry)

def load_events():
    return load_json(EVENTS_FILE, [])

def save_events(events):
    save_json(EVENTS_FILE, events)

def load_pairings():
    return load_json(PAIRINGS_FILE, [])

def save_pairings(pairings):
    save_json(PAIRINGS_FILE, pairings)

# ─────────────────────────────────────────────────────────────────────────────
# PLAYER REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

def update_registry(registry, match):
    """Pull the latest display name for each player in a match into the registry."""
    for rel in match["player_match_relationships"]:
        pid = str(rel["player"]["id"])
        name = rel["user_event_status"]["best_identifier"]
        registry[pid] = name

def lookup_name(pid, registry):
    return registry.get(str(pid), f"Unknown({pid})")

# ─────────────────────────────────────────────────────────────────────────────
# EVENT METADATA
# ─────────────────────────────────────────────────────────────────────────────

def fetch_event_metadata(event_id):
    """Fetch event name, date, location and tier from static HTML."""
    url = f"{BASE_URL}/events/{event_id}"
    r = requests.get(url)
    soup = BeautifulSoup(r.text, "html.parser")

    # Event name
    name_el = soup.find("h1", {"data-testid": "event-title"})
    name = name_el.text.strip() if name_el else None

    # Date
    date = None
    for svg in soup.find_all("svg", class_=lambda c: c and "lucide-calendar" in c):
        span = svg.find_next_sibling("span", class_="font-medium")
        if span:
            raw = span.text.strip()
            try:
                date = datetime.strptime(raw, "%b %d, %Y").strftime("%Y-%m-%d")
            except ValueError:
                pass
            break

    # Location
    location = None
    store_link = soup.find("a", {"aria-label": lambda v: v and v.startswith("View store page")})
    if store_link:
        span = store_link.find("span", class_="font-medium")
        location = span.text.strip() if span else None

    # Tier
    tier = "locals"
    for div in soup.find_all("div", class_="text-sm text-white font-semibold uppercase mb-1"):
        if div.text.strip() == "Tournament Format":
            format_div = div.find_next_sibling("div")
            if format_div and ("Modified Champion Deck" in format_div.text or "Sealed" in format_div.text):
                tier = "release"
            break

    return {
        "event_id": str(event_id),
        "name": name,
        "date": date,
        "location": location,
        "tier": tier,
    }

# ─────────────────────────────────────────────────────────────────────────────
# ROUND DISCOVERY (via Playwright)
# ─────────────────────────────────────────────────────────────────────────────

def get_round_info(event_id, retries=3, timeout=60000):
    for attempt in range(1, retries + 1):
        try:
            final_round_id = None

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()

                def handle_request(request):
                    nonlocal final_round_id
                    m = re.search(r"/tournament-rounds/(\d+)/matches/paginated/", request.url)
                    if m:
                        final_round_id = int(m.group(1))

                page.on("request", handle_request)
                page.goto(f"{BASE_URL}/events/{event_id}", timeout=timeout)
                page.wait_for_load_state("networkidle", timeout=timeout)
                html = page.content()
                browser.close()

            soup = BeautifulSoup(html, "html.parser")
            n_rounds = None
            for trigger in soup.find_all("button", {"data-testid": "pairings-round-dropdown-trigger"}):
                span = trigger.find("span", {"data-slot": "select-value"})
                if span:
                    m = re.search(r"Round\s+(\d+)", span.text.strip())
                    if m:
                        n_rounds = int(m.group(1))
                        break

            if final_round_id is None or n_rounds is None:
                raise ValueError(f"Could not determine round info (final_round_id={final_round_id}, n_rounds={n_rounds})")

            first_round_id = final_round_id - (n_rounds - 1)
            return first_round_id, n_rounds

        except Exception as e:
            print(f"  Attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                print(f"  Retrying in 5 seconds...")
                time.sleep(5)

    raise RuntimeError(f"Failed to get round info for event {event_id} after {retries} attempts")

# ─────────────────────────────────────────────────────────────────────────────
# MATCH FETCHING
# ─────────────────────────────────────────────────────────────────────────────

def fetch_round(round_id):
    """Fetch all matches for a round, handling pagination."""
    matches = []
    page = 1
    while True:
        url = f"{BASE_API}/tournament-rounds/{round_id}/matches/paginated/"
        params = {"page": page, "page_size": 50, "avoid_cache": "false"}
        r = requests.get(url, params=params)
        if r.status_code != 200:
            return []
        data = r.json()
        if not data["results"]:
            break
        matches.extend(data["results"])
        if data["next_page_number"] is None:
            break
        page += 1
    return matches

def fetch_standings(round_id):
    """Fetch final standings for a round, handling pagination."""
    standings = []
    page = 1
    while True:
        url = f"{BASE_API}/tournament-rounds/{round_id}/standings/paginated/"
        params = {"page": page, "page_size": 50, "avoid_cache": "false"}
        r = requests.get(url, params=params)
        if r.status_code != 200:
            return []
        data = r.json()
        if not data["results"]:
            break
        for entry in data["results"]:
            standings.append({
                "player_id":   str(entry["player"]["id"]),
                "name":        entry["user_event_status"]["best_identifier"],
                "rank":        entry["rank"],
                "record":      entry["record"],
                "points":      entry["match_points"],
                "omw":         entry["opponent_match_win_percentage"],
                "gw":          entry["game_win_percentage"],
                "ogw":         entry["opponent_game_win_percentage"],
            })
        if data["next_page_number"] is None:
            break
        page += 1
        time.sleep(0.3)
    return standings

def parse_match(match, registry):
    """Convert a raw API match into a clean pairing dict. Returns None for byes."""
    if match["match_is_bye"] or match["is_ghost_match"]:
        return None

    players = {}
    for rel in match["player_match_relationships"]:
        pid = rel["player"]["id"]
        name = lookup_name(pid, registry)
        players[pid] = name

    if len(players) != 2:
        return None

    p1_id, p2_id = list(players.keys())
    is_draw = match["match_is_intentional_draw"] or match["match_is_unintentional_draw"]
    winner_id = match["winning_player"]

    if is_draw or winner_id is None:
        result = "draw"
    elif winner_id == p1_id:
        result = "p1"
    else:
        result = "p2"

    return {
        "p1_id": str(p1_id),
        "p1": players[p1_id],
        "p2_id": str(p2_id),
        "p2": players[p2_id],
        "result": result,
    }

# ─────────────────────────────────────────────────────────────────────────────
# CORE ACTIONS
# ─────────────────────────────────────────────────────────────────────────────

def register_event(event_id):
    """Fetch metadata for an event and add it to events.json."""
    events = load_events()

    # Check for duplicates
    if any(e["event_id"] == str(event_id) for e in events):
        print(f"Event {event_id} is already registered — skipping.")
        return

    print(f"Fetching metadata for event {event_id}...")
    metadata = fetch_event_metadata(event_id)
    metadata["scraped"] = False
    events.append(metadata)
    save_events(events)

    print(
        f"Registered: {metadata['name']} "
        f"({metadata['date']}) @ {metadata['location']} "
        f"[{metadata['tier']}]"
    )

def scrape_event(event):
    print(f"Scraping: {event['name']} ({event['date']})")

    manual_round_ids = event.get("manual_round_ids")

    if manual_round_ids:
        round_ids = manual_round_ids
        n_rounds = len(round_ids)
        print(f"  Using {n_rounds} manual round IDs: {round_ids}")
    else:
        first_round_id, n_rounds = get_round_info(event["event_id"])
        round_ids = list(range(first_round_id, first_round_id + n_rounds))
        print(f"  {n_rounds} rounds, IDs {round_ids[0]}–{round_ids[-1]}")

    registry = load_registry()
    rounds = []

    for round_num, round_id in enumerate(round_ids, 1):
        print(f"  Round {round_num} (id={round_id})...", end=" ", flush=True)

        raw_matches = fetch_round(round_id)
        if not raw_matches:
            print("no results")
            continue

        for raw_match in raw_matches:
            update_registry(registry, raw_match)

        pairings = [parse_match(m, registry) for m in raw_matches]
        pairings = [p for p in pairings if p is not None]

        print(f"{len(pairings)} pairings")
        rounds.append({"round": round_num, "pairings": pairings})
        time.sleep(0.3)

    # Fetch final standings from the last round ID
    final_round_id = round_ids[-1]
    print(f"  Fetching standings (round id={final_round_id})...", end=" ", flush=True)
    standings = fetch_standings(final_round_id)
    print(f"{len(standings)} players")

    # Update registry with any new names from standings
    for entry in standings:
        registry[entry["player_id"]] = entry["name"]

    save_registry(registry)

    return {
        "event_id":  event["event_id"],
        "name":      event["name"],
        "date":      event["date"],
        "tier":      event.get("tier", "locals"),
        "location":  event.get("location"),
        "n_rounds":  n_rounds,
        "rounds":    rounds,
        "standings": standings,
    }

def scrape_all(force=False):
    """Scrape all unscraped events (or all events if force=True)."""
    events = load_events()

    # Sort chronologically before processing
    events.sort(key=lambda e: e.get("date") or "")

    to_scrape = [e for e in events if force or not e.get("scraped")]
    if not to_scrape:
        print("Nothing to scrape.")
        return

    # Load existing pairings — we'll append/replace entries
    all_pairings = load_pairings()
    existing_ids = {p["event_id"] for p in all_pairings}

    for event in to_scrape:
        event_id = event["event_id"]
        try:
            event_data = scrape_event(event)
        except Exception as e:
            print(f"  SKIPPED: {e}")
            continue

        # Replace existing entry if re-scraping, otherwise append
        if event_id in existing_ids:
            all_pairings = [p for p in all_pairings if p["event_id"] != event_id]
        all_pairings.append(event_data)

        event["scraped"] = True
        save_events(events)
        save_pairings(all_pairings)
        print(f"  Saved.")

    print(f"\nDone. {len(to_scrape)} event(s) processed.")

def list_events():
    """Print all registered events and their scrape status."""
    events = load_events()
    if not events:
        print("No events registered.")
        return

    events_sorted = sorted(events, key=lambda e: e.get("date") or "")
    print(f"\n{'ID':<10} {'Date':<12} {'Scraped':<8} {'Tier':<10} {'Name'}")
    print("─" * 70)
    for e in events_sorted:
        scraped = "✓" if e.get("scraped") else "✗"
        tier = e.get("tier", "?")
        print(f"{e['event_id']:<10} {e.get('date','?'):<12} {scraped:<8} {tier:<10} {e.get('name','?')}")

def reset_event(event_id):
    """Mark an event as unscraped so it will be scraped again."""
    events = load_events()
    for e in events:
        if e["event_id"] == str(event_id):
            e["scraped"] = False
            save_events(events)
            print(f"Reset: {e['name']}")
            return
    print(f"Event {event_id} not found.")

# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Riftbound event scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python riftbound_scraper.py                      Scrape all unscraped events
  python riftbound_scraper.py --register 224858    Register one event
  python riftbound_scraper.py --register 224858 229100 231500
  python riftbound_scraper.py --list               List all events
  python riftbound_scraper.py --reset 224858       Mark event for re-scraping
  python riftbound_scraper.py --force              Re-scrape everything
        """
    )

    parser.add_argument(
        "--register", nargs="+", metavar="EVENT_ID", type=int,
        help="Register one or more events by ID (from the URL)"
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List all registered events"
    )
    parser.add_argument(
        "--reset", nargs="+", metavar="EVENT_ID", type=int,
        help="Mark event(s) as unscraped"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-scrape all events, not just unscraped ones"
    )

    args = parser.parse_args()

    if args.register:
        for event_id in args.register:
            register_event(event_id)

    elif args.list:
        list_events()

    elif args.reset:
        for event_id in args.reset:
            reset_event(event_id)

    else:
        # Default: scrape
        scrape_all(force=args.force)


if __name__ == "__main__":
    main()
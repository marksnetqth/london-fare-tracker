"""
Daily Chicago -> London fare tracker (Ignav API).

Each run searches every outbound/return date combination for each cabin,
keeps the cheapest itinerary per airline, appends the results to
data/prices.csv, and rebuilds SUMMARY.md and data/trend.png.

Required environment variable: IGNAV_API_KEY
"""

import csv
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# Trip settings - edit these to change what gets tracked
# ---------------------------------------------------------------------------
ORIGIN = "CHI"            # Chicago metro (O'Hare + Midway)
DESTINATION = "LON"       # London metro (Heathrow, Gatwick, etc.)
ADULTS = 2
CABINS = ["economy", "premium_economy"]
AIRLINES = ["AA", "UA", "EI", "BA", "VS"]  # American, United, Aer Lingus, British Airways, Virgin Atlantic
MAX_STOPS = 1             # 0 = nonstop only, 1 = up to one stop each way
OUTBOUND_DATES = ["2027-06-15", "2027-06-16", "2027-06-17"]
RETURN_DATES = ["2027-06-19", "2027-06-20", "2027-06-21"]
# Must be in London for the full day of June 18: outbound must land by this local time.
ARRIVE_BY_LONDON = "2027-06-18T10:00:00"

AIRLINE_NAMES = {
    "AA": "American", "UA": "United", "EI": "Aer Lingus",
    "BA": "British Airways", "VS": "Virgin Atlantic",
}
CABIN_NAMES = {"economy": "Economy", "premium_economy": "Premium Economy",
               "business": "Business", "first": "First"}

# ---------------------------------------------------------------------------
API_BASE = "https://ignav.com/api"
DATA_DIR = Path("data")
PRICES_CSV = DATA_DIR / "prices.csv"
RUNS_CSV = DATA_DIR / "runs.csv"
BEST_CSV = DATA_DIR / "best_fares.csv"
CHART_PNG = DATA_DIR / "trend.png"
SUMMARY_MD = Path("SUMMARY.md")

PRICE_FIELDS = [
    "run_date", "run_time_utc", "cabin", "out_date", "ret_date", "airline",
    "price", "currency", "price_status",
    "out_route", "out_depart", "out_arrive", "out_stops",
    "ret_route", "ret_depart", "ret_arrive", "ret_stops",
    "total_minutes", "mixed_carriers", "ignav_id",
]
RUN_FIELDS = ["run_date", "run_time_utc", "searches_ok", "searches_not_yet_available",
              "searches_failed", "itineraries_kept", "notes"]
BEST_FIELDS = ["run_date", "cabin", "out_date", "ret_date", "airline", "search_price",
               "fare_name", "seller", "seller_price", "booking_url"]

RETRYABLE = {"server_busy", "billing_period_unavailable", "unable_to_complete_request"}
FATAL = {"api_key_required", "invalid_api_key", "email_not_verified",
         "billing_required", "billing_blocked", "monthly_spend_limit_reached"}


class FatalApiError(Exception):
    pass


class NotYetSearchable(Exception):
    pass


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------
def post(path, body, api_key, attempts=3):
    """POST to Ignav with retries for temporary errors."""
    last_error = "unknown error"
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(
                f"{API_BASE}{path}",
                json=body,
                headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
                timeout=90,
            )
        except requests.RequestException as exc:
            last_error = f"network error: {exc}"
            time.sleep(5 * attempt)
            continue

        if resp.status_code == 200:
            return resp.json()

        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        code = err.get("code", f"http_{resp.status_code}")
        message = err.get("message", resp.text[:200])
        last_error = f"{code}: {message}"

        if code in FATAL:
            raise FatalApiError(last_error)
        if code == "unsupported_search":
            raise NotYetSearchable(last_error)
        if code in RETRYABLE or resp.status_code >= 500:
            wait = resp.headers.get("Retry-After")
            time.sleep(int(wait) if wait and wait.isdigit() else 10 * attempt)
            continue
        raise RuntimeError(last_error)  # bad request: don't retry
    raise RuntimeError(last_error)


def search_round_trip(api_key, cabin, out_date, ret_date):
    body = {
        "origin": ORIGIN,
        "destination": DESTINATION,
        "departure_date": out_date,
        "return_date": ret_date,
        "adults": ADULTS,
        "cabin_class": cabin,
        "max_stops": MAX_STOPS,
        "airlines_include": AIRLINES,
        "market": "US",
    }
    return post("/fares/round-trip", body, api_key)


def booking_details(api_key, ignav_id):
    """Look up fare name (e.g. 'Main Cabin' vs 'Basic') and a booking link."""
    data = post("/fares/booking-links", {"ignav_id": ignav_id}, api_key, attempts=2)
    links = [link for opt in data.get("booking_options", []) for link in opt.get("links", [])]
    if not links:
        return None
    airline_links = [l for l in links if l.get("provider_type") == "airline"]
    pick = (airline_links or links)[0]
    return {
        "fare_name": pick.get("fare_name", ""),
        "seller": pick.get("provider_name", ""),
        "seller_price": (pick.get("price") or {}).get("amount", ""),
        "booking_url": pick.get("url", ""),
    }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def route(segments):
    if not segments:
        return ""
    return "-".join([segments[0]["departure_airport"]] + [s["arrival_airport"] for s in segments])


def main_airline(leg):
    """Airline of the longest flight in the leg (the transatlantic one)."""
    segs = leg.get("segments") or []
    if not segs:
        return ""
    longest = max(segs, key=lambda s: s.get("duration_minutes") or 0)
    return longest.get("marketing_carrier_code") or ""


def flatten(itin, run_date, run_time, cabin, out_date, ret_date):
    out = itin.get("outbound") or {}
    inn = itin.get("inbound") or {}
    out_segs = out.get("segments") or []
    in_segs = inn.get("segments") or []
    price = itin.get("price") or {}
    return {
        "run_date": run_date,
        "run_time_utc": run_time,
        "cabin": cabin,
        "out_date": out_date,
        "ret_date": ret_date,
        "airline": main_airline(out),
        "price": price.get("amount"),
        "currency": price.get("currency", ""),
        "price_status": price.get("status", ""),
        "out_route": route(out_segs),
        "out_depart": out_segs[0]["departure_time_local"] if out_segs else "",
        "out_arrive": out_segs[-1]["arrival_time_local"] if out_segs else "",
        "out_stops": max(len(out_segs) - 1, 0),
        "ret_route": route(in_segs),
        "ret_depart": in_segs[0]["departure_time_local"] if in_segs else "",
        "ret_arrive": in_segs[-1]["arrival_time_local"] if in_segs else "",
        "ret_stops": max(len(in_segs) - 1, 0),
        "total_minutes": (out.get("duration_minutes") or 0) + (inn.get("duration_minutes") or 0),
        "mixed_carriers": itin.get("has_mixed_carriers", ""),
        "ignav_id": itin.get("ignav_id", ""),
    }


def lands_in_time(row):
    """True if the outbound lands in London by ARRIVE_BY_LONDON (local time)."""
    return bool(row["out_arrive"]) and row["out_arrive"] <= ARRIVE_BY_LONDON


def valid_price(row):
    try:
        return float(row["price"]) > 0 and row["currency"] == "USD"
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
def append_rows(path, fields, rows):
    DATA_DIR.mkdir(exist_ok=True)
    new_file = not path.exists()
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


def read_rows(path):
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Daily run
# ---------------------------------------------------------------------------
def run(api_key):
    now = datetime.now(timezone.utc)
    run_date = now.astimezone(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    run_time = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    kept, ok, not_yet, failed, notes = [], 0, 0, 0, []
    for cabin in CABINS:
        for out_date in OUTBOUND_DATES:
            for ret_date in RETURN_DATES:
                label = f"{cabin} {out_date}->{ret_date}"
                try:
                    data = search_round_trip(api_key, cabin, out_date, ret_date)
                except NotYetSearchable as exc:
                    not_yet += 1
                    notes.append(f"{label}: not searchable yet")
                    print(f"[not yet] {label}: {exc}")
                    continue
                except FatalApiError:
                    raise
                except Exception as exc:  # one failed search shouldn't stop the run
                    failed += 1
                    notes.append(f"{label}: {exc}")
                    print(f"[failed] {label}: {exc}")
                    continue

                ok += 1
                rows = [flatten(i, run_date, run_time, cabin, out_date, ret_date)
                        for i in data.get("itineraries", [])]
                rows = [r for r in rows if valid_price(r) and lands_in_time(r)
                        and r["airline"] in AIRLINES]
                cheapest = {}
                for r in rows:
                    a = r["airline"]
                    if a not in cheapest or float(r["price"]) < float(cheapest[a]["price"]):
                        cheapest[a] = r
                kept.extend(cheapest.values())
                print(f"[ok] {label}: {len(data.get('itineraries', []))} found, "
                      f"{len(cheapest)} airlines kept")

    append_rows(PRICES_CSV, PRICE_FIELDS, kept)

    # Fare name + booking link for today's single best fare in each cabin.
    best_rows = []
    for cabin in CABINS:
        options = [r for r in kept if r["cabin"] == cabin]
        if not options:
            continue
        best = min(options, key=lambda r: float(r["price"]))
        entry = {"run_date": run_date, "cabin": cabin, "out_date": best["out_date"],
                 "ret_date": best["ret_date"], "airline": best["airline"],
                 "search_price": best["price"], "fare_name": "", "seller": "",
                 "seller_price": "", "booking_url": ""}
        try:
            details = booking_details(api_key, best["ignav_id"])
            if details:
                entry.update(details)
        except FatalApiError:
            raise
        except Exception as exc:
            notes.append(f"booking lookup ({cabin}): {exc}")
        best_rows.append(entry)
    append_rows(BEST_CSV, BEST_FIELDS, best_rows)

    append_rows(RUNS_CSV, RUN_FIELDS, [{
        "run_date": run_date, "run_time_utc": run_time, "searches_ok": ok,
        "searches_not_yet_available": not_yet, "searches_failed": failed,
        "itineraries_kept": len(kept), "notes": " | ".join(notes)[:1000],
    }])
    return ok, not_yet, failed


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def money(v):
    return f"${float(v):,.0f}"


def link(url):
    return url if url.startswith("http") else f"https://{url}"


def short_date(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%a %b %-d")


def build_summary():
    rows = [r for r in read_rows(PRICES_CSV) if r.get("price")]
    runs = read_rows(RUNS_CSV)
    best_info = read_rows(BEST_CSV)
    lines = ["# Chicago → London fare tracker", ""]
    lines.append(f"Trip: out {', '.join(short_date(d) for d in OUTBOUND_DATES)}; "
                 f"back {', '.join(short_date(d) for d in RETURN_DATES)} · "
                 f"{ADULTS} adults · up to {MAX_STOPS} stop(s) · "
                 f"{', '.join(AIRLINE_NAMES.get(a, a) for a in AIRLINES)}")
    lines.append(f"Outbound must land in London by "
                 f"{datetime.fromisoformat(ARRIVE_BY_LONDON).strftime('%-I:%M %p on %b %-d')}.")
    lines.append("")

    if runs:
        last = runs[-1]
        lines.append(f"**Last run:** {last['run_time_utc']} UTC · "
                     f"{last['searches_ok']} searches ok, "
                     f"{last['searches_not_yet_available']} not bookable yet, "
                     f"{last['searches_failed']} failed")
        lines.append("")

    if not rows:
        lines.append("No fares recorded yet. If searches show as 'not bookable yet', "
                     "the airlines haven't opened June 2027 inventory to this data source; "
                     "the tracker will keep checking daily.")
        SUMMARY_MD.write_text("\n".join(lines) + "\n")
        return "\n".join(lines)

    # daily low per cabin
    daily_low = defaultdict(dict)  # cabin -> date -> row
    for r in rows:
        cur = daily_low[r["cabin"]].get(r["run_date"])
        if cur is None or float(r["price"]) < float(cur["price"]):
            daily_low[r["cabin"]][r["run_date"]] = r
    latest_date = max(r["run_date"] for r in rows)

    lines.append("Prices are the round-trip fare for the whole party as quoted by Ignav (USD).")
    lines.append("")

    for cabin in CABINS:
        series = daily_low.get(cabin, {})
        if not series:
            continue
        dates = sorted(series)
        lines.append(f"## {CABIN_NAMES.get(cabin, cabin)}")
        lines.append("")
        if latest_date not in series:
            lines.append("_No fares found in today's run._")
            lines.append("")
            continue
        today = series[latest_date]
        price = float(today["price"])
        prev = [d for d in dates if d < latest_date]
        week = [float(series[d]["price"]) for d in dates if d < latest_date][-7:]
        all_low_date = min(dates, key=lambda d: float(series[d]["price"]))
        all_low = float(series[all_low_date]["price"])

        lines.append(f"**Today's best: {money(price)}** · "
                     f"{AIRLINE_NAMES.get(today['airline'], today['airline'])} · "
                     f"out {short_date(today['out_date'])} ({today['out_route']}, "
                     f"lands {today['out_arrive'][11:16]}) · "
                     f"back {short_date(today['ret_date'])} ({today['ret_route']})")
        extra = [b for b in best_info if b["run_date"] == latest_date and b["cabin"] == cabin]
        if extra and extra[-1].get("fare_name"):
            b = extra[-1]
            lines.append(f"Fare type: **{b['fare_name']}** via {b['seller']}"
                         + (f" · [booking link]({link(b['booking_url'])})"
                            if b.get("booking_url") else ""))
        lines.append("")
        facts = []
        if prev:
            y = float(series[prev[-1]]["price"])
            diff = price - y
            facts.append(f"vs last check: {'▲' if diff > 0 else '▼' if diff < 0 else '='} "
                         f"{money(abs(diff))}")
        if week:
            avg = mean(week)
            pct = (price - avg) / avg * 100
            facts.append(f"vs 7-day avg ({money(avg)}): {pct:+.1f}%")
        facts.append(f"lowest seen: {money(all_low)} on {all_low_date}")
        facts.append(f"days tracked: {len(dates)}")
        lines.append(" · ".join(facts))
        if len(dates) > 1 and price <= all_low:
            lines.append("")
            lines.append("🔔 **Lowest price seen so far.**")
        lines.append("")

        # date grid for today
        todays = [r for r in rows if r["run_date"] == latest_date and r["cabin"] == cabin]
        grid = {}
        for r in todays:
            k = (r["out_date"], r["ret_date"])
            if k not in grid or float(r["price"]) < float(grid[k]["price"]):
                grid[k] = r
        lines.append("| Out \\ Back | " + " | ".join(short_date(d) for d in RETURN_DATES) + " |")
        lines.append("|---|" + "---|" * len(RETURN_DATES))
        for o in OUTBOUND_DATES:
            cells = []
            for rd in RETURN_DATES:
                g = grid.get((o, rd))
                cells.append(f"{money(g['price'])} {g['airline']}" if g else "—")
            lines.append(f"| {short_date(o)} | " + " | ".join(cells) + " |")
        lines.append("")

        # by airline today
        by_air = {}
        for r in todays:
            a = r["airline"]
            if a not in by_air or float(r["price"]) < float(by_air[a]["price"]):
                by_air[a] = r
        if by_air:
            lines.append("Cheapest today by airline: " + " · ".join(
                f"{AIRLINE_NAMES.get(a, a)} {money(by_air[a]['price'])}"
                for a in sorted(by_air, key=lambda a: float(by_air[a]["price"]))))
            missing = [AIRLINE_NAMES.get(a, a) for a in AIRLINES if a not in by_air]
            if missing:
                lines.append(f"No qualifying fares today: {', '.join(missing)}")
            lines.append("")

    if make_chart(daily_low):
        lines.append("## Trend")
        lines.append("")
        lines.append("![Daily lowest fare](data/trend.png)")
        lines.append("")

    text = "\n".join(lines) + "\n"
    SUMMARY_MD.write_text(text)
    return text


def make_chart(daily_low):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        return False
    fig, ax = plt.subplots(figsize=(9, 4.5))
    plotted = False
    for cabin in CABINS:
        series = daily_low.get(cabin, {})
        if not series:
            continue
        dates = sorted(series)
        ax.plot([datetime.strptime(d, "%Y-%m-%d") for d in dates],
                [float(series[d]["price"]) for d in dates],
                marker="o", label=CABIN_NAMES.get(cabin, cabin))
        plotted = True
    if not plotted:
        plt.close(fig)
        return False
    ax.set_title("Lowest round-trip fare per day (2 adults, USD)")
    ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=10))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %-d"))
    ax.xaxis.set_minor_locator(mdates.DayLocator())
    if len({d for s in daily_low.values() for d in s}) < 10:
        ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.grid(alpha=0.3)
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    DATA_DIR.mkdir(exist_ok=True)
    fig.savefig(CHART_PNG, dpi=120)
    plt.close(fig)
    return True


def main():
    api_key = os.environ.get("IGNAV_API_KEY", "").strip()
    if not api_key:
        sys.exit("IGNAV_API_KEY is not set. Add it as a repository secret.")
    try:
        ok, not_yet, failed = run(api_key)
    except FatalApiError as exc:
        append_rows(RUNS_CSV, RUN_FIELDS, [{
            "run_date": datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d"),
            "run_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "searches_ok": 0, "searches_not_yet_available": 0, "searches_failed": 0,
            "itineraries_kept": 0, "notes": f"STOPPED: {exc}"}])
        build_summary()
        sys.exit(f"Stopped: {exc}")

    summary = build_summary()
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as f:
            f.write(summary.replace("data/trend.png", "trend chart in repo: data/trend.png"))
    print(summary)
    if ok == 0 and not_yet == 0:
        sys.exit("Every search failed - check data/runs.csv for details.")


if __name__ == "__main__":
    main()

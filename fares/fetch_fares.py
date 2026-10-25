#!/usr/bin/env python3
"""Daily fare check for the DC -> LA -> Orlando -> DC holiday trip.

Calls SerpApi's Google Flights engine once per leg and date, with every
airport of a leg in the same call, so a full run costs about 20 searches:
16 date searches, 3 Main Cabin (no basic economy) checks on each leg's
cheapest date, and 1 multi-city price on American, Delta and United.

Writes the dashboard's data files to fares/out/ and appends to
data/history.json. Raw answers are cached per day in fares/cache/, so a
re-run on the same day costs nothing.

  FLIGHT_API_KEY=... python3 fares/fetch_fares.py          # full run
  FLIGHT_API_KEY=... python3 fares/fetch_fares.py --test   # one search only
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
OUT = ROOT / "out"
CACHE = ROOT / "cache"
HISTORY = REPO / "data" / "history.json"
ENDPOINT = "https://serpapi.com/search.json"

CONFIG = json.loads((ROOT / "config.json").read_text())
BAGS = {b["airline"]: b for b in json.loads((ROOT / "bag_fees.json").read_text())}
ADULTS = CONFIG["adults"]
BASIC_CARRIERS = {"American", "Delta", "United"}


def ssl_context():
    for path in (os.environ.get("SSL_CERT_FILE"), "/root/.ccr/ca-bundle.crt"):
        if path and Path(path).exists():
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


class Searcher:
    def __init__(self, key, today):
        self.key = key
        self.calls = 0
        self.cache_dir = CACHE / today
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ctx = ssl_context()

    def search(self, **params):
        params = {"engine": "google_flights", "hl": "en", "gl": "us",
                  "currency": CONFIG["currency"], "adults": ADULTS, **params}
        tag = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        cached = self.cache_dir / f"{tag}.json"
        if cached.exists():
            return json.loads(cached.read_text())
        url = ENDPOINT + "?" + urllib.parse.urlencode({**params, "api_key": self.key})
        self.calls += 1
        with urllib.request.urlopen(url, context=self.ctx, timeout=90) as resp:
            body = json.loads(resp.read())
        if body.get("error"):
            raise RuntimeError(f"SerpApi: {body['error']}")
        cached.write_text(json.dumps(body))
        return body


def parse_options(body, leg, date, fare):
    """Flatten SerpApi results into one row per bookable option."""
    rows = []
    for item in body.get("best_flights", []) + body.get("other_flights", []):
        segs = item.get("flights") or []
        if not segs or item.get("price") is None:
            continue
        first, last = segs[0], segs[-1]
        dep = first["departure_airport"]["time"]
        arr = last["arrival_airport"]["time"]
        airlines = sorted({s.get("airline", "") for s in segs})
        airline = airlines[0] if len(airlines) == 1 else " + ".join(airlines)
        dep_day, arr_day = dep[:10], arr[:10]
        overnight = arr_day > dep_day
        rows.append({
            "leg": leg["id"],
            "date": date,
            "airline": airline,
            "flights": ", ".join(s.get("flight_number", "") for s in segs),
            "route": "–".join([first["departure_airport"]["id"]] + [s["arrival_airport"]["id"] for s in segs]),
            "origin": first["departure_airport"]["id"],
            "destination": last["arrival_airport"]["id"],
            "depart": dep[11:16],
            "arrive": arr[11:16] + (" +1" if overnight else ""),
            "stops": len(segs) - 1,
            "duration_min": item.get("total_duration"),
            "fare": fare,
            "price_2": item["price"],
            "redeye": dep[11:13] >= "21" and overnight and arr[11:13] < "12",
        })
    return rows


def fare_label(row):
    if row["fare"] == "main":
        return "Main Cabin / Economy"
    return "Lowest (likely Basic)" if row["airline"] in BASIC_CARRIERS else "Lowest fare"


def bag_cost_2(row):
    """Carry-on + 1 checked bag for both travellers on this flight."""
    fees = BAGS.get(row["airline"])
    if not fees:
        return None, "Bag fees unknown for this airline"
    note = ""
    if row["fare"] == "lowest" and not fees["basic_carry_on"] and not fees["carry_on_pp"]:
        note = "Basic fare has no carry-on; buy Main Cabin for one"
    return ADULTS * (fees["carry_on_pp"] + fees["checked_pp"]), note


def with_bags(row):
    return row["price_2"] + (bag_cost_2(row)[0] or 0)


def best_for_date(rows, min_saving_2):
    """Cheapest nonstop with bags, unless a 1-stop beats it by the required saving."""
    nonstop = [r for r in rows if r["stops"] == 0]
    one_stop = [r for r in rows if r["stops"] == 1]
    best_ns = min(nonstop, key=with_bags, default=None)
    best_1s = min(one_stop, key=with_bags, default=None)
    if best_ns and (not best_1s or with_bags(best_ns) - with_bags(best_1s) < min_saving_2):
        return best_ns
    return best_1s


def leg_rows(searcher, leg):
    rows = []
    for date in leg["dates"]:
        params = {"type": 2, "departure_id": leg["from"], "arrival_id": leg["to"],
                  "outbound_date": date, "stops": 2, "sort_by": 2}
        if leg.get("outbound_times"):
            params["outbound_times"] = leg["outbound_times"]
        found = parse_options(searcher.search(**params), leg, date, "lowest")
        if leg.get("redeye"):
            found = [r for r in found if r["redeye"]]
        rows += found
    return rows


def main_cabin_rows(searcher, leg, date):
    params = {"type": 2, "departure_id": leg["from"], "arrival_id": leg["to"],
              "outbound_date": date, "stops": 2, "sort_by": 2, "exclude_basic": "true"}
    if leg.get("outbound_times"):
        params["outbound_times"] = leg["outbound_times"]
    found = parse_options(searcher.search(**params), leg, date, "main")
    return [r for r in found if r["redeye"]] if leg.get("redeye") else found


def finish(row, leg, rank=None):
    bags, note = bag_cost_2(row)
    out = {**row, "leg_label": leg["label"], "fare": fare_label(row),
           "bags_2": bags, "total_2": row["price_2"] + (bags or 0),
           "preferred_date": row["date"] in leg["preferred"], "note": note}
    if rank is not None:
        out["rank"] = rank
    return out


def run_test(searcher):
    leg = CONFIG["legs"][0]
    body = searcher.search(type=2, departure_id=leg["from"], arrival_id=leg["to"],
                           outbound_date=leg["preferred"][0], stops=2, sort_by=2)
    rows = parse_options(body, leg, leg["preferred"][0], "lowest")
    print(f"Test search {leg['from']} -> {leg['to']} on {leg['preferred'][0]}: {len(rows)} options, {searcher.calls} API call(s)")
    for r in sorted(rows, key=lambda r: r["price_2"])[:8]:
        print(f"  ${r['price_2']:>5}  {r['airline']:<20} {r['flights']:<18} {r['route']:<12} {r['depart']}-{r['arrive']}  stops={r['stops']}")
    print("price_insights:", json.dumps(body.get("price_insights", {}))[:300])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="run one search and print it")
    args = ap.parse_args()
    key = os.environ.get("FLIGHT_API_KEY") or os.environ.get("SERPAPI_KEY")
    if not key:
        sys.exit("FLIGHT_API_KEY is not set")
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    searcher = Searcher(key, today)
    if args.test:
        run_test(searcher)
        return

    min_saving_2 = CONFIG["one_stop_min_saving_pp"] * ADULTS
    grid, top, best_by_leg = [], [], {}
    for leg in CONFIG["legs"]:
        rows = leg_rows(searcher, leg)
        per_date = []
        for date in leg["dates"]:
            day = [r for r in rows if r["date"] == date]
            pick = best_for_date(day, min_saving_2)
            nonstop = min((r["price_2"] for r in day if r["stops"] == 0), default=None)
            grid.append({"leg": leg["id"], "leg_label": leg["label"], "date": date,
                         "best_price_2": pick["price_2"] if pick else None,
                         "best_total_2": with_bags(pick) if pick else None,
                         "nonstop_price_2": nonstop,
                         "airline": pick["airline"] if pick else None,
                         "stops": pick["stops"] if pick else None,
                         "preferred_date": date in leg["preferred"]})
            if pick:
                per_date.append(pick)
        ranked = sorted(per_date, key=with_bags)[:3]
        top += [finish(r, leg, i + 1) for i, r in enumerate(ranked)]
        if ranked:
            main = main_cabin_rows(searcher, leg, ranked[0]["date"])
            pick = best_for_date(main, min_saving_2)
            if pick:
                top.append(finish(pick, leg, "Main"))
        best_by_leg[leg["id"]] = {"rows": rows, "ranked": ranked}

    itineraries = build_itineraries(searcher, best_by_leg, now)
    write_outputs(now, grid, top, itineraries)
    print(f"Done: {searcher.calls} API call(s) this run")


def best_value_pick(leg, rows, min_saving_2):
    """Nonstop on a preferred date at a reasonable hour; DCA if preferred."""
    def ok(r):
        if r["stops"] or r["date"] not in leg["preferred"]:
            return False
        if leg.get("preferred_arrival") and r["destination"] != leg["preferred_arrival"]:
            return False
        return leg.get("redeye") or "07:00" <= r["depart"] <= "20:00"
    good = [r for r in rows if ok(r)]
    if not good and leg.get("preferred_arrival"):
        good = [r for r in rows if r["stops"] == 0 and r["date"] in leg["preferred"]]
    return min(good, key=with_bags, default=None)


def build_itineraries(searcher, best_by_leg, now):
    legs = CONFIG["legs"]
    min_saving_2 = CONFIG["one_stop_min_saving_pp"] * ADULTS
    out = []

    def add(kind, label, picks):
        if not all(picks):
            return
        rows = [finish(p, leg) for p, leg in zip(picks, legs)]
        out.append({
            "kind": kind, "label": label,
            **{f"{leg['id']}": f"{r['date']} · {r['airline']} {r['flights']} · {r['route']} {r['depart']}–{r['arrive']}"
               for leg, r in zip(legs, rows)},
            "fare_2": sum(r["price_2"] for r in rows),
            "bags_2": sum(r["bags_2"] or 0 for r in rows),
            "total_2": sum(r["total_2"] for r in rows),
        })

    cheapest = [(best_by_leg[l["id"]]["ranked"] or [None])[0] for l in legs]
    add("cheapest", "Cheapest (three one-ways)", cheapest)
    add("best_value", "Best value (nonstops, preferred dates)",
        [best_value_pick(l, best_by_leg[l["id"]]["rows"], min_saving_2) for l in legs])

    # One multi-city search on AA/DL/UA for the cheapest dates found above.
    if all(cheapest):
        segments = []
        for leg, pick in zip(legs, cheapest):
            seg = {"departure_id": leg["from"], "arrival_id": leg["to"], "date": pick["date"]}
            if leg.get("outbound_times"):
                seg["times"] = leg["outbound_times"]
            segments.append(seg)
        body = searcher.search(type=3, multi_city_json=json.dumps(segments), stops=2,
                               sort_by=2, include_airlines=CONFIG["multi_city_airlines"])
        prices = [i for i in body.get("best_flights", []) + body.get("other_flights", []) if i.get("price")]
        if prices:
            best = min(prices, key=lambda i: i["price"])
            carriers = sorted({s.get("airline", "") for s in best.get("flights", [])})
            fees = BAGS.get(carriers[0]) if len(carriers) == 1 else None
            bags = ADULTS * 3 * (fees["carry_on_pp"] + fees["checked_pp"]) if fees else None
            one_ways = sum(c["price_2"] for c in cheapest)
            out.append({
                "kind": "multi_city", "label": f"Multi-city on {' + '.join(carriers)}",
                "leg1": f"{cheapest[0]['date']} · first flight {best['flights'][0].get('flight_number', '')}",
                "leg2": cheapest[1]["date"], "leg3": cheapest[2]["date"],
                "fare_2": best["price"], "bags_2": bags,
                "total_2": best["price"] + (bags or 0),
                "vs_one_ways_2": best["price"] - one_ways,
            })
    return out


def write_outputs(now, grid, top, itineraries):
    OUT.mkdir(exist_ok=True)
    stamp = now.isoformat(timespec="seconds")
    for row in grid + top + itineraries:
        row["checked_at"] = stamp
    (OUT / "date_grid.json").write_text(json.dumps(grid, indent=1))
    (OUT / "top_options.json").write_text(json.dumps(top, indent=1))
    (OUT / "itineraries.json").write_text(json.dumps(itineraries, indent=1))
    (OUT / "bag_fees.json").write_text(json.dumps(list(BAGS.values()), indent=1))

    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    day = now.date().isoformat()
    history = [h for h in history if h["run_date"] != day]
    for it in itineraries:
        history.append({"run_date": day, "series": it["kind"], "total_2": it["total_2"]})
    for leg in CONFIG["legs"]:
        prices = [g["best_total_2"] for g in grid if g["leg"] == leg["id"] and g["best_total_2"]]
        if prices:
            history.append({"run_date": day, "series": leg["id"], "total_2": min(prices)})
    HISTORY.parent.mkdir(exist_ok=True)
    HISTORY.write_text(json.dumps(history, indent=1))
    (OUT / "history.json").write_text(json.dumps(history, indent=1))


if __name__ == "__main__":
    main()

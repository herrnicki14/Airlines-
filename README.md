# Holiday trip fares

Daily fare check for a DC → Los Angeles → Orlando → DC trip for 2 adults (Dec 2026 – Jan 2027).

- `fares/config.json`: legs, airports, flexible date windows, red-eye window, nonstop rule.
- `fares/bag_fees.json`: estimated carry-on, checked bag and seat fees per airline.
- `fares/fetch_fares.py`: searches Google Flights through SerpApi (15 searches a run: nonstops only, no basic economy) and writes the dashboard's data files to `fares/out/`.
- `data/history.json`: one row per series per daily run, used for the price trend.

```sh
FLIGHT_API_KEY=... python3 fares/fetch_fares.py --test   # one search
FLIGHT_API_KEY=... python3 fares/fetch_fares.py          # full run
```

The environment needs `serpapi.com` in its allowed domains and the SerpApi key in the `FLIGHT_API_KEY` environment variable.

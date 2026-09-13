"""
FINRA Reg SHO daily short sale volume (consolidated NMS, off-exchange/TRF only).
File: https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt
Format: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market

We keep a rolling history {symbol: {date: short_ratio}} in data/short_history.json and
only download the days that are missing. Ratio = ShortVolume / TotalVolume (0-1).
NOTE: this covers off-exchange (ATS/TRF) volume only, not exchange volume — a
sentiment/pressure proxy, not a full-market short ratio.
"""
import json, os, sys, time, urllib.request

URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d}.txt"
UA = {"User-Agent": "sector-flow-monitor/1.0"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch_day(date_iso: str) -> dict:
    d = date_iso.replace("-", "")
    req = urllib.request.Request(URL.format(d=d), headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode("utf-8", "replace")
    out = {}
    for line in text.splitlines()[1:]:
        parts = line.split("|")
        if len(parts) < 5:
            continue
        try:
            sv, tv = float(parts[2]), float(parts[4])
        except ValueError:
            continue
        if tv > 0:
            out[parts[1]] = sv / tv
    return out


def update(path: str, dates: list, wanted: set, max_fetch: int = 8) -> dict:
    hist = {}
    if os.path.exists(path):
        try:
            hist = json.load(open(path))
        except Exception:
            hist = {}
    have = set()
    for sym_d in hist.values():
        have.update(sym_d.keys())
    missing = [d for d in dates if d not in have][-max_fetch:]   # newest N missing days only
    log(f"short volume: {len(missing)} missing day(s) to fetch: {missing}")
    for d in missing:
        try:
            day = fetch_day(d)
        except Exception as e:
            log(f"  short volume {d} failed: {e}")
            continue
        n = 0
        for sym, ratio in day.items():
            if sym in wanted:
                hist.setdefault(sym, {})[d] = round(ratio, 4)
                n += 1
        log(f"  {d}: {n} symbols")
        time.sleep(1)
    # trim to the output window
    keep = set(dates)
    for sym in list(hist):
        hist[sym] = {k: v for k, v in hist[sym].items() if k in keep}
        if not hist[sym]:
            del hist[sym]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(hist, f, separators=(",", ":"), sort_keys=True)
    return hist

#!/usr/bin/env python3
"""
S&P 500 constituents -> sector classification -> daily OHLCV
-> estimated sector money flow. Writes data/latest.json for the HTML app.

Data sources
  * Constituents + GICS sector / sub-industry :
        Wikipedia "List of S&P 500 companies" (fallback: datasets/s-and-p-500-companies on GitHub)
  * Sector scheme : SECTOR_SCHEME = "gics"  -> GICS (S&P official, from the constituents list; no API calls)
                    SECTOR_SCHEME = "yahoo" -> Yahoo Finance quoteSummary sector (Morningstar-based),
                                               GICS mapped to Yahoo names for symbols Yahoo cannot supply
  * Shares outstanding (for market-cap normalisation) : Yahoo Finance via yfinance Ticker.info
  * Prices       : Yahoo Finance daily OHLCV (via yfinance.download)

Flow definition (ESTIMATE, not actual fund flows)
  per stock/day : net_flow = sign(close - prev_close) * close * volume
  per sector/day: sum of net_flow over constituents
"""
import json, math, os, sys, time, datetime as dt
import numpy as np
import pandas as pd
import yfinance as yf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
CACHE_PATH = os.path.join(DATA_DIR, "sector_map.json")
OUT_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_DAYS = 60          # trading days kept in the output
DOWNLOAD_PERIOD = "6mo"    # enough history for 60 trading days + 20-day averages
CACHE_TTL_DAYS = 45        # refresh shares outstanding / sector after this many days
# Sector scheme: "gics" (default, S&P official — complete on day one) or "yahoo".
SECTOR_SCHEME = os.environ.get("SECTOR_SCHEME", "gics").lower()
# Ticker.info calls per run. Set high so the first run fills the whole universe; later runs
# only touch entries older than CACHE_TTL_DAYS (~12/day) or symbols new to the index.
MAX_INFO_PER_RUN = int(os.environ.get("MAX_INFO_PER_RUN", "600"))

GICS_SECTORS = [
    "Information Technology", "Health Care", "Financials", "Consumer Discretionary",
    "Communication Services", "Industrials", "Consumer Staples", "Energy",
    "Materials", "Real Estate", "Utilities",
]

YAHOO_SECTORS = [
    "Technology", "Healthcare", "Financial Services", "Consumer Cyclical",
    "Communication Services", "Industrials", "Consumer Defensive", "Energy",
    "Basic Materials", "Real Estate", "Utilities",
]
GICS_TO_YAHOO = {
    "Information Technology": "Technology",
    "Health Care": "Healthcare",
    "Financials": "Financial Services",
    "Consumer Discretionary": "Consumer Cyclical",
    "Consumer Staples": "Consumer Defensive",
    "Materials": "Basic Materials",
    "Communication Services": "Communication Services",
    "Industrials": "Industrials",
    "Energy": "Energy",
    "Real Estate": "Real Estate",
    "Utilities": "Utilities",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- constituents
def load_constituents() -> pd.DataFrame:
    try:
        import io, urllib.request
        req = urllib.request.Request(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "sector-flow-monitor/1.0 (GitHub Actions; contact via repo issues)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            html = r.read().decode("utf-8")
        tables = pd.read_html(io.StringIO(html), match="Symbol")
        df = tables[0]
        df = df.rename(columns={"Symbol": "symbol", "Security": "name",
                                "GICS Sector": "gics_sector", "GICS Sub-Industry": "gics_sub"})
        df = df[["symbol", "name", "gics_sector", "gics_sub"]]
        log(f"constituents: wikipedia ({len(df)} rows)")
        src = "wikipedia"
    except Exception as e:
        log("wikipedia failed:", e)
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
        df = pd.read_csv(url).rename(columns={"Symbol": "symbol", "Security": "name",
                                              "GICS Sector": "gics_sector", "GICS Sub-Industry": "gics_sub"})
        df = df[["symbol", "name", "gics_sector", "gics_sub"]]
        log(f"constituents: github datasets ({len(df)} rows)")
        src = "github:datasets/s-and-p-500-companies"
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["yf_symbol"] = df["symbol"].str.replace(".", "-", regex=False)   # BRK.B -> BRK-B
    df.attrs["source"] = src
    return df.drop_duplicates("symbol").reset_index(drop=True)


# ---------------------------------------------------------------- sector cache
def load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def save_cache(cache: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=0, sort_keys=True)


def refresh_cache(cons: pd.DataFrame, cache: dict) -> dict:
    """Fetch Yahoo sector/industry/sharesOutstanding for symbols missing or stale in cache.
    Cache keys are Yahoo-style symbols (BRK-B). Entries for symbols no longer in the index are dropped."""
    today = dt.date.today()
    current = set(cons["yf_symbol"])
    stale_keys = [k for k in cache if k not in current]
    for k in stale_keys:
        del cache[k]
    if stale_keys:
        log(f"sector cache: removed {len(stale_keys)} symbols no longer in index: {stale_keys}")
    todo = []
    for sym in cons["yf_symbol"]:
        ent = cache.get(sym)
        need = (ent is None) or (SECTOR_SCHEME == "yahoo" and not ent.get("sector")) or (not ent.get("shares"))
        if need:
            todo.append(sym)
        else:
            try:
                age = (today - dt.date.fromisoformat(ent.get("updated", "2000-01-01"))).days
            except Exception:
                age = 10**6
            if age > CACHE_TTL_DAYS:
                todo.append(sym)
    # missing entries first, then stale ones
    todo = sorted(todo, key=lambda s: 0 if s not in cache else 1)[:MAX_INFO_PER_RUN]
    log(f"sector cache: {len(todo)} symbols to (re)fetch")
    consecutive_fail = 0
    for i, sym in enumerate(todo):
        for attempt in range(3):
            try:
                info = yf.Ticker(sym).info or {}
                consecutive_fail = 0
                sector = info.get("sector")
                ent = cache.get(sym, {})
                ent.update({
                    "sector": sector or ent.get("sector"),
                    "industry": info.get("industry") or ent.get("industry"),
                    "shares": info.get("sharesOutstanding") or ent.get("shares"),
                    "name": info.get("shortName") or ent.get("name"),
                    "updated": today.isoformat(),
                })
                cache[sym] = ent
                break
            except Exception as e:
                log(f"  info {sym} attempt {attempt+1} failed: {e}")
                time.sleep(3 + 5 * attempt)
        else:
            consecutive_fail += 1
            if consecutive_fail >= 10:
                log("  10 consecutive failures — probably rate limited; stopping info fetch for this run")
                break
        time.sleep(0.5)
        if (i + 1) % 25 == 0:
            save_cache(cache)
            log(f"  ... {i+1}/{len(todo)}")
    save_cache(cache)
    return cache


# ---------------------------------------------------------------- prices
def download_prices(symbols: list) -> pd.DataFrame:
    """Returns a long-format DataFrame: date, symbol, close, volume, high, low."""
    frames = []
    chunk = 100
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        for attempt in range(3):
            try:
                raw = yf.download(part, period=DOWNLOAD_PERIOD, interval="1d", group_by="ticker",
                                  auto_adjust=False, actions=False, threads=True, progress=False)
                break
            except Exception as e:
                log(f"download chunk {i} attempt {attempt+1} failed: {e}")
                time.sleep(5)
        else:
            continue
        if raw is None or raw.empty:
            continue
        if not isinstance(raw.columns, pd.MultiIndex):     # single ticker edge case
            raw.columns = pd.MultiIndex.from_product([[part[0]], raw.columns])
        for sym in part:
            if sym not in raw.columns.get_level_values(0):
                continue
            d = raw[sym][["Close", "Volume", "High", "Low"]].copy()
            d.columns = ["close", "volume", "high", "low"]
            d = d.dropna(subset=["close"])
            d["symbol"] = sym
            d.index.name = "date"
            frames.append(d.reset_index())
        time.sleep(1)
    if not frames:
        raise RuntimeError("no price data downloaded")
    px = pd.concat(frames, ignore_index=True)
    px["date"] = pd.to_datetime(px["date"]).dt.strftime("%Y-%m-%d")
    return px


# ---------------------------------------------------------------- metrics
def compute(cons: pd.DataFrame, cache: dict, px: pd.DataFrame) -> dict:
    meta = cons.set_index("yf_symbol")
    px = px.sort_values(["symbol", "date"]).copy()
    px["prev_close"] = px.groupby("symbol")["close"].shift(1)
    px = px.dropna(subset=["prev_close"])
    px["ret"] = px["close"] / px["prev_close"] - 1.0
    px["dv"] = px["close"] * px["volume"].fillna(0)                  # dollar volume
    px["nf"] = np.sign(px["close"] - px["prev_close"]) * px["dv"]     # estimated net flow

    def sector_of(sym):
        g = meta.loc[sym, "gics_sector"] if sym in meta.index else None
        if SECTOR_SCHEME == "gics":
            return (g if g in GICS_SECTORS else "Other"), "gics"
        ent = cache.get(sym) or {}
        s = ent.get("sector")
        if s in YAHOO_SECTORS:
            return s, "yahoo"
        return GICS_TO_YAHOO.get(g, "Other"), "gics_fallback"

    def industry_of(sym):
        if SECTOR_SCHEME == "yahoo":
            ind = (cache.get(sym) or {}).get("industry")
            if ind:
                return ind
        return meta.loc[sym, "gics_sub"] if sym in meta.index else None

    SECTOR_ORDER = GICS_SECTORS if SECTOR_SCHEME == "gics" else YAHOO_SECTORS

    sec = {s: sector_of(s) for s in px["symbol"].unique()}
    px["sector"] = px["symbol"].map(lambda s: sec[s][0])
    px["shares"] = px["symbol"].map(lambda s: (cache.get(s) or {}).get("shares") or np.nan)
    px["mcap"] = px["shares"] * px["close"]

    dates_all = sorted(px["date"].unique())
    dates = dates_all[-HISTORY_DAYS:]
    p = px[px["date"].isin(dates)]

    # sector x date aggregates
    g = p.groupby(["sector", "date"])
    agg = pd.DataFrame({
        "nf": g["nf"].sum(),
        "dv": g["dv"].sum(),
        "mcap": g["mcap"].sum(min_count=1),
        "adv": g["ret"].apply(lambda r: int((r > 0).sum())),
        "dec": g["ret"].apply(lambda r: int((r < 0).sum())),
        "n": g["ret"].size(),
        # market-cap weighted return; fallback to equal weight when caps missing
        "ret": g.apply(lambda d: float(np.average(d["ret"], weights=d["mcap"].fillna(0)))
                       if d["mcap"].fillna(0).sum() > 0 else float(d["ret"].mean())),
    }).reset_index()

    # 20-day average dollar volume per sector (over full history, not just window)
    full = px.groupby(["sector", "date"])["dv"].sum().reset_index().sort_values(["sector", "date"])
    full["dv20"] = full.groupby("sector")["dv"].transform(lambda s: s.rolling(20, min_periods=5).mean())
    agg = agg.merge(full[["sector", "date", "dv20"]], on=["sector", "date"], how="left")

    sectors_out = {}
    for s in SECTOR_ORDER + (["Other"] if (agg["sector"] == "Other").any() else []):
        a = agg[agg["sector"] == s].set_index("date").reindex(dates)
        if a["n"].isna().all():
            continue
        sectors_out[s] = {
            "n": int(a["n"].fillna(0).max()),
            "nf": [None if pd.isna(v) else round(float(v)) for v in a["nf"]],
            "dv": [None if pd.isna(v) else round(float(v)) for v in a["dv"]],
            "dv20": [None if pd.isna(v) else round(float(v)) for v in a["dv20"]],
            "mcap": [None if pd.isna(v) else round(float(v)) for v in a["mcap"]],
            "ret": [None if pd.isna(v) else round(float(v), 5) for v in a["ret"]],
            "adv": [None if pd.isna(v) else int(v) for v in a["adv"]],
            "dec": [None if pd.isna(v) else int(v) for v in a["dec"]],
        }

    # per-stock rows for the latest date (for drill-down)
    last = dates[-1]
    pl = p[p["date"] == last].copy()
    # 5-day / 20-day per-stock net flow
    tail5 = dates[-5:]
    tail20 = dates[-20:]
    nf5 = p[p["date"].isin(tail5)].groupby("symbol")["nf"].sum()
    nf20 = p[p["date"].isin(tail20)].groupby("symbol")["nf"].sum()
    stocks = []
    for _, r in pl.iterrows():
        sym = r["symbol"]
        ent = cache.get(sym) or {}
        m = meta.loc[sym] if sym in meta.index else None
        stocks.append({
            "s": m["symbol"] if m is not None else sym,
            "nm": (m["name"] if m is not None else ent.get("name")) or sym,
            "sec": r["sector"],
            "ind": industry_of(sym),
            "src": sec[sym][1],
            "c": round(float(r["close"]), 2),
            "r": round(float(r["ret"]), 5),
            "v": int(r["volume"]) if not pd.isna(r["volume"]) else None,
            "dv": round(float(r["dv"])),
            "nf": round(float(r["nf"])),
            "nf5": round(float(nf5.get(sym, 0))),
            "nf20": round(float(nf20.get(sym, 0))),
            "mc": None if pd.isna(r["mcap"]) else round(float(r["mcap"])),
        })
    stocks.sort(key=lambda x: -abs(x["nf"]))

    n_yahoo = sum(1 for v in sec.values() if v[1] == "yahoo")
    n_shares = sum(1 for sym in px["symbol"].unique() if (cache.get(sym) or {}).get("shares"))
    return {
        "as_of": last,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dates": dates,
        "sectors": sectors_out,
        "stocks": stocks,
        "meta": {
            "universe": "S&P 500 constituents",
            "constituents_source": cons.attrs.get("source"),
            "n_symbols_priced": int(px["symbol"].nunique()),
            "sector_scheme": "gics" if SECTOR_SCHEME == "gics" else "yahoo",
            "sector_scheme_label": ("GICS (S&P Dow Jones Indices / MSCI) — from constituents list"
                                    if SECTOR_SCHEME == "gics" else "Yahoo Finance (11 sectors)"),
            "n_sector_from_yahoo": n_yahoo,
            "n_sector_from_gics_fallback": (len(sec) - n_yahoo) if SECTOR_SCHEME == "yahoo" else 0,
            "n_shares_from_yahoo": n_shares,
            "n_shares_missing": int(px["symbol"].nunique()) - n_shares,
            "flow_definition": "sign(close - prev_close) * close * volume, summed per sector (estimate)",
            "price_source": "Yahoo Finance via yfinance",
        },
    }


def main():
    cons = load_constituents()
    cache = refresh_cache(cons, load_cache())
    px = download_prices(list(cons["yf_symbol"]))
    log(f"prices: {px['symbol'].nunique()} symbols, {px['date'].nunique()} dates")
    out = compute(cons, cache, px)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    log(f"wrote {OUT_PATH}: as_of={out['as_of']} sectors={len(out['sectors'])} stocks={len(out['stocks'])}")


if __name__ == "__main__":
    main()

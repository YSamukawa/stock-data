#!/usr/bin/env python3
"""
Market-wide money-flow indicators -> data/market.json

Each source is fetched independently. If a source fails, the previous run's data for that
source is kept and its status is marked "stale" (so one broken site never blanks the page).

Sources
  * ETFs / indices (daily)   : Yahoo Finance via yfinance  — SPY QQQ IWM RSP HYG LQD TLT GLD UUP ^VIX ^VIX3M + sector ETFs
  * Credit spreads, Fed      : FRED CSV  — BAMLH0A0HYM2 (HY OAS), BAMLC0A0CM (IG OAS), WALCL, RRPONTSYD, WTREGEN, VIXCLS,
                               DGS10, DGS2, DFII10, T10YIE, DTWEXBGS, WRESBAL
  * Fund flows (weekly)      : ICI combined estimated long-term flows (HTML table / xls)
  * Money market assets      : ICI weekly MMF assets (HTML table)
  * Margin debt (monthly)    : FINRA margin-statistics.xlsx
  * Futures positioning      : CFTC Disaggregated COT via public Socrata API (E-mini S&P 500)
  * Retail sentiment (weekly): AAII sentiment.xls
  * Put/Call (daily)         : Cboe daily market statistics page (HTML) — accumulated day by day
  * ETF flow estimate        : delta(shares outstanding) * close, from yfinance info — accumulated day by day (experimental)
"""
import io, json, os, re, sys, time, datetime as dt, urllib.request
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
OUT_PATH = os.path.join(DATA_DIR, "market.json")
PC_PATH = os.path.join(DATA_DIR, "putcall_history.json")
ETFSH_PATH = os.path.join(DATA_DIR, "etf_shares.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; sector-flow-monitor/1.0; +https://github.com)"}
DAYS = 130   # trading days of daily series kept

ETFS = ["SPY", "QQQ", "IWM", "RSP", "HYG", "LQD", "TLT", "GLD", "UUP", "^VIX", "^VIX3M",
        "^SKEW", "^VVIX", "JPY=X", "CL=F", "HG=F"]   # + options gauges, USD/JPY, WTI crude, copper (continuous futures)
SECTOR_ETFS = {"XLK": "Information Technology", "XLV": "Health Care", "XLF": "Financials",
               "XLY": "Consumer Discretionary", "XLC": "Communication Services", "XLI": "Industrials",
               "XLP": "Consumer Staples", "XLE": "Energy", "XLB": "Materials", "XLRE": "Real Estate",
               "XLU": "Utilities"}
FRED = {"hy_oas": "BAMLH0A0HYM2", "ig_oas": "BAMLC0A0CM", "fed_assets": "WALCL",
        "rrp": "RRPONTSYD", "tga": "WTREGEN", "vix": "VIXCLS",
        "dgs10": "DGS10", "dgs2": "DGS2", "dfii10": "DFII10", "t10yie": "T10YIE",   # nominal / real yields, breakeven
        "dtwex": "DTWEXBGS", "reserves": "WRESBAL"}                                # broad dollar index, bank reserves


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, timeout=60, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def clean(v, nd=4):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), nd)


# ---------------------------------------------------------------- ETFs
def fetch_etfs():
    import yfinance as yf
    syms = ETFS + list(SECTOR_ETFS)
    raw = yf.download(syms, period="1y", interval="1d", group_by="ticker", auto_adjust=False,
                      actions=False, threads=True, progress=False)
    out = {}
    dates = None
    for s in syms:
        if s not in raw.columns.get_level_values(0):
            continue
        d = raw[s][["Close", "Volume"]].dropna(subset=["Close"])
        d.index = pd.to_datetime(d.index).strftime("%Y-%m-%d")
        out[s] = d
    if not out:
        raise RuntimeError("no ETF data")
    # common date axis from SPY
    dates = list(out["SPY"].index[-DAYS:])
    res = {"dates": dates, "series": {}}
    for s, d in out.items():
        d = d.reindex(dates)
        res["series"][s] = {"c": [clean(v, 3) for v in d["Close"]],
                            "v": [None if pd.isna(v) else int(v) for v in d["Volume"]]}
    return res


# ---------------------------------------------------------------- FRED
def fetch_fred():
    res = {}
    for key, sid in FRED.items():
        try:
            txt = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}")
            df = pd.read_csv(io.StringIO(txt))
            df.columns = ["date", "value"]
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
            df = df.dropna().tail(300)
            res[key] = {"id": sid, "dates": list(df["date"].astype(str)), "values": [clean(v) for v in df["value"]]}
        except Exception as e:
            log(f"  FRED {sid} failed: {e}")
    if not res:
        raise RuntimeError("all FRED series failed")
    return res


# ---------------------------------------------------------------- ICI weekly flows
def _find_tables(html, must):
    try:
        tables = pd.read_html(io.StringIO(html))
    except Exception:
        return []
    keep = []
    for t in tables:
        flat = " ".join(str(c) for c in t.columns) + " " + " ".join(t.astype(str).values.ravel()[:200])
        if all(m.lower() in flat.lower() for m in must):
            keep.append(t)
    return keep


def _parse_money(x):
    if x is None:
        return None
    s = str(x).replace(",", "").replace("$", "").strip()
    neg = s.startswith("(") or s.startswith("-") or s.startswith("−") or s.startswith("▲")
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    v = float(s)
    return -v if neg else v


def fetch_ici_flows():
    """Weekly combined (mutual fund + ETF) estimated long-term flows, $ millions.
    Parses the HTML table on the ICI page; layout: rows = categories, columns = week-ending dates."""
    html = get("https://www.ici.org/research/stats/combined_flows")
    tables = _find_tables(html, ["equity", "bond"])
    if not tables:
        raise RuntimeError("ICI combined flows table not found")
    t = tables[0]
    # first column = category label; remaining columns = weeks
    t = t.rename(columns={t.columns[0]: "cat"})
    t["cat"] = t["cat"].astype(str).str.strip()
    weeks, wk = [], []
    for c in t.columns[1:]:
        try:
            wk.append(pd.to_datetime(str(c)).strftime("%Y-%m-%d")); weeks.append(c)
        except Exception:
            pass   # skip "$ Change" style columns
    def row(label_regex):
        m = t[t["cat"].str.contains(label_regex, case=False, regex=True, na=False)]
        if m.empty:
            return None
        return [_parse_money(v) for v in m.iloc[0][weeks]]
    res = {"weeks": wk, "equity": row(r"^equity"), "domestic_equity": row(r"domestic"),
           "world_equity": row(r"world"), "bond": row(r"^bond"), "hybrid": row(r"hybrid"),
           "total": row(r"^total"), "units": "USD millions", "source": "ICI combined estimated long-term flows (weekly)"}
    if res["equity"] is None:
        raise RuntimeError("ICI equity row not found")
    return res


def fetch_ici_mmf():
    html = get("https://www.ici.org/research/stats/mmf")
    tables = _find_tables(html, ["total", "government"])
    if not tables:
        raise RuntimeError("ICI MMF table not found")
    t = tables[0]
    t = t.rename(columns={t.columns[0]: "cat"})
    t["cat"] = t["cat"].astype(str).str.strip()
    weeks, wk = [], []
    for c in t.columns[1:]:
        try:
            wk.append(pd.to_datetime(str(c)).strftime("%Y-%m-%d")); weeks.append(c)
        except Exception:
            pass
    tot = t[t["cat"].str.contains(r"^total", case=False, na=False)]
    if tot.empty:
        raise RuntimeError("ICI MMF total row not found")
    vals = [None if _parse_money(v) is None else _parse_money(v) * 1000 for v in tot.iloc[0][weeks]]   # billions -> millions
    return {"weeks": wk, "total_assets": vals, "units": "USD millions", "source": "ICI weekly money market fund assets"}


# ---------------------------------------------------------------- FINRA margin debt
def fetch_margin():
    data = get("https://www.finra.org/sites/default/files/2021-03/margin-statistics.xlsx", binary=True)
    xl = pd.ExcelFile(io.BytesIO(data))
    df = xl.parse(xl.sheet_names[0])
    # find a date-like column and the "debit balances" column
    date_col = None
    for c in df.columns:
        parsed = pd.to_datetime(df[c], errors="coerce")
        if parsed.notna().sum() > len(df) * 0.5:
            date_col = c
            break
    if date_col is None:
        raise RuntimeError("FINRA margin: no date column")
    debit_col = next((c for c in df.columns if "debit" in str(c).lower()), None)
    if debit_col is None:
        num = [c for c in df.columns if c != date_col and pd.to_numeric(df[c], errors="coerce").notna().sum() > 10]
        debit_col = num[0]
    d = pd.DataFrame({"date": pd.to_datetime(df[date_col], errors="coerce"),
                      "debit": pd.to_numeric(df[debit_col], errors="coerce")}).dropna().sort_values("date").tail(36)
    return {"months": [x.strftime("%Y-%m") for x in d["date"]], "debit": [clean(v, 0) for v in d["debit"]],
            "units": "USD millions", "source": "FINRA margin statistics (monthly)"}


# ---------------------------------------------------------------- CFTC COT
def fetch_cot():
    url = ("https://publicreporting.cftc.gov/resource/gpe5-46if.json?$limit=60"
           "&$order=report_date_as_yyyy_mm_dd%20DESC&contract_market_name=E-MINI%20S%26P%20500")
    rows = json.loads(get(url))
    if not rows:
        raise RuntimeError("CFTC: empty")
    rows = sorted(rows, key=lambda r: r["report_date_as_yyyy_mm_dd"])
    def f(r, k):
        try:
            return float(r.get(k))
        except Exception:
            return None
    out = {"dates": [], "asset_mgr_net": [], "lev_money_net": [], "dealer_net": [], "open_interest": [],
           "source": "CFTC Disaggregated COT, E-mini S&P 500 futures only (weekly, Tuesday positions)"}
    for r in rows:
        out["dates"].append(str(r["report_date_as_yyyy_mm_dd"])[:10])
        am = (f(r, "asset_mgr_positions_long") or 0) - (f(r, "asset_mgr_positions_short") or 0)
        lm = (f(r, "lev_money_positions_long") or 0) - (f(r, "lev_money_positions_short") or 0)
        dl = (f(r, "dealer_positions_long_all") or 0) - (f(r, "dealer_positions_short_all") or 0)
        out["asset_mgr_net"].append(am); out["lev_money_net"].append(lm); out["dealer_net"].append(dl)
        out["open_interest"].append(f(r, "open_interest_all"))
    return out


# ---------------------------------------------------------------- AAII
def fetch_aaii():
    data = get("https://www.aaii.com/files/surveys/sentiment.xls", binary=True)
    df = pd.read_excel(io.BytesIO(data), header=None)
    # locate header row containing "Bullish"
    hdr = None
    for i in range(min(10, len(df))):
        if df.iloc[i].astype(str).str.contains("Bullish", case=False).any():
            hdr = i
            break
    if hdr is None:
        raise RuntimeError("AAII: header not found")
    df.columns = [str(c).strip() for c in df.iloc[hdr]]
    df = df.iloc[hdr + 1:]
    dcol = df.columns[0]
    bcol = next(c for c in df.columns if "bull" in c.lower())
    ncol = next(c for c in df.columns if "neutral" in c.lower())
    rcol = next(c for c in df.columns if "bear" in c.lower())
    d = pd.DataFrame({"date": pd.to_datetime(df[dcol], errors="coerce"),
                      "bull": pd.to_numeric(df[bcol], errors="coerce"),
                      "neutral": pd.to_numeric(df[ncol], errors="coerce"),
                      "bear": pd.to_numeric(df[rcol], errors="coerce")}).dropna().sort_values("date").tail(52)
    scale = 100 if d["bull"].max() <= 1.0 else 1
    return {"dates": [x.strftime("%Y-%m-%d") for x in d["date"]],
            "bull": [clean(v * scale, 1) for v in d["bull"]], "neutral": [clean(v * scale, 1) for v in d["neutral"]],
            "bear": [clean(v * scale, 1) for v in d["bear"]], "source": "AAII Investor Sentiment Survey (weekly)"}


# ---------------------------------------------------------------- Cboe put/call (accumulated)
def fetch_putcall(as_of: str):
    hist = json.load(open(PC_PATH)) if os.path.exists(PC_PATH) else {}
    try:
        html = get("https://www.cboe.com/us/options/market_statistics/daily/")
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        def grab(label):
            m = re.search(label + r"[^0-9]{0,40}([0-9]+\.[0-9]+)", text, re.I)
            return float(m.group(1)) if m else None
        tot, eq, idx = grab(r"TOTAL PUT/CALL RATIO"), grab(r"EQUITY PUT/CALL RATIO"), grab(r"INDEX PUT/CALL RATIO")
        # date on page, if any
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", text)
        date = pd.to_datetime(m.group(1)).strftime("%Y-%m-%d") if m else as_of
        if tot is not None:
            hist[date] = {"total": tot, "equity": eq, "index": idx}
    except Exception as e:
        log(f"  Cboe put/call failed: {e}")
    hist = dict(sorted(hist.items())[-120:])
    with open(PC_PATH, "w") as f:
        json.dump(hist, f, separators=(",", ":"))
    if not hist:
        raise RuntimeError("no put/call data yet")
    return {"dates": list(hist), "total": [hist[d]["total"] for d in hist], "equity": [hist[d]["equity"] for d in hist],
            "index": [hist[d]["index"] for d in hist], "source": "Cboe daily market statistics (scraped; accumulated)"}


# ---------------------------------------------------------------- ETF flow estimate (accumulated shares outstanding)
def fetch_etf_flows(etf_data, as_of: str):
    import yfinance as yf
    hist = json.load(open(ETFSH_PATH)) if os.path.exists(ETFSH_PATH) else {}
    today = {}
    for s in ["SPY", "QQQ", "IWM", "RSP", "HYG", "LQD", "TLT", "GLD"] + list(SECTOR_ETFS):
        try:
            info = yf.Ticker(s).info or {}
            sh = info.get("sharesOutstanding")
            if sh:
                today[s] = int(sh)
        except Exception as e:
            log(f"  ETF shares {s}: {e}")
        time.sleep(0.5)
    if today:
        hist[as_of] = today
    hist = dict(sorted(hist.items())[-130:])
    with open(ETFSH_PATH, "w") as f:
        json.dump(hist, f, separators=(",", ":"))
    dates = list(hist)
    if len(dates) < 2:
        raise RuntimeError("ETF flow estimate needs at least 2 daily snapshots")
    closes = etf_data["series"]
    out = {"dates": dates[1:], "flows": {}, "units": "USD",
           "source": "Estimate: delta(shares outstanding) x close, from Yahoo Finance snapshots taken by this pipeline (experimental)"}
    for s in set().union(*[set(v) for v in hist.values()]):
        vals = []
        for i in range(1, len(dates)):
            a, b = hist[dates[i - 1]].get(s), hist[dates[i]].get(s)
            c = None
            if s in closes and dates[i] in etf_data["dates"]:
                c = closes[s]["c"][etf_data["dates"].index(dates[i])]
            vals.append(None if (a is None or b is None or c is None) else round((b - a) * c))
        out["flows"][s] = vals
    return out


# ---------------------------------------------------------------- main
def main():
    prev = {}
    if os.path.exists(OUT_PATH):
        try:
            prev = json.load(open(OUT_PATH))
        except Exception:
            prev = {}
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {"generated_at": now, "status": {}, "sector_etfs": SECTOR_ETFS}

    def run(name, fn, *args):
        try:
            out[name] = fn(*args)
            out["status"][name] = {"state": "ok", "fetched_at": now}
            log(f"{name}: ok")
        except Exception as e:
            log(f"{name}: FAILED — {e}")
            if prev.get(name):
                out[name] = prev[name]
                st = dict(prev.get("status", {}).get(name, {}))
                st.update({"state": "stale", "error": str(e)[:200]})
                out["status"][name] = st
            else:
                out["status"][name] = {"state": "missing", "error": str(e)[:200]}

    run("etf", fetch_etfs)
    as_of = out.get("etf", {}).get("dates", [dt.date.today().isoformat()])[-1]
    out["as_of"] = as_of
    run("fred", fetch_fred)
    run("ici_flows", fetch_ici_flows)
    run("ici_mmf", fetch_ici_mmf)
    # ICI pages show ~5 weeks; merge with previous runs to build a longer history (ascending weeks)
    for name, keys in (("ici_flows", ["equity", "domestic_equity", "world_equity", "bond", "hybrid", "total"]),
                       ("ici_mmf", ["total_assets"])):
        cur, old = out.get(name), prev.get(name)
        if not cur:
            continue
        merged = {}
        for src in (old, cur):
            if not src:
                continue
            for i, w in enumerate(src.get("weeks", [])):
                merged[w] = {k: (src.get(k) or [None] * len(src["weeks"]))[i] for k in keys}
        weeks = sorted(merged)[-104:]
        cur["weeks"] = weeks
        for k in keys:
            cur[k] = [merged[w].get(k) for w in weeks]
    run("margin", fetch_margin)
    run("cot", fetch_cot)
    run("aaii", fetch_aaii)
    run("putcall", fetch_putcall, as_of)
    if out.get("etf"):
        run("etf_flows", fetch_etf_flows, out["etf"], as_of)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    log(f"wrote {OUT_PATH}; status={ {k: v['state'] for k, v in out['status'].items()} }")


if __name__ == "__main__":
    main()

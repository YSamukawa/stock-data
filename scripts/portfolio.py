#!/usr/bin/env python3
"""
Portfolio Watch: for each ticker in watchlist.txt, compute momentum context and risk signals
from prices, option chains (Yahoo Finance via yfinance), earnings dates, insider transactions
and analyst targets. Writes data/portfolio.json and accumulates data/portfolio_history.json
(IV / skew history for rank calculations).

Nothing here is a forecast. Option-implied numbers describe what the market is pricing, not
where the stock will go. Thresholds for flags are initial values (see THRESH) — not backtested.
"""
import json, math, os, re, sys, time, datetime as dt
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
WATCHLIST = os.path.join(ROOT, "watchlist.txt")
OUT_PATH = os.path.join(DATA_DIR, "portfolio.json")
HIST_PATH = os.path.join(DATA_DIR, "portfolio_history.json")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
SHORT_PATH = os.path.join(DATA_DIR, "short_history.json")
RISK_FREE = 0.04            # assumed annual rate for Black-Scholes deltas (only affects delta mapping slightly)
MAX_EXPIRIES = 7            # option expiries fetched per ticker (nearest ones + those around 30/60/90 days)
HIST_KEEP_DAYS = 400

# Flag thresholds — initial values, shown on the page. Not backtested.
THRESH = {
    "iv_rank_high": 80,         # IV rank (percentile of accumulated history) above this
    "term_ratio_high": 1.20,    # near-term IV / ~90d IV above this = event premium
    "skew_pctl_high": 85,       # 25-delta put-call skew percentile (accumulated) above this
    "earnings_days": 7,         # trading days to next earnings at or below this
    "drawdown_52w": -0.20,      # distance from 52-week high below this
    "rs20_weak": -0.10,         # 20-day return minus SPY 20-day return below this
    "short_ratio_jump": 0.10,   # off-exchange short ratio 5d avg minus 20d avg above this
    "insider_sells_90d": 3,     # number of insider sale transactions in ~90 days at or above this
    "unusual_vol_oi": 0.5,      # total option volume / total open interest above this
    "iv_hv_gap": 0.10,          # IV30 minus HV20 above this (market braces for more than realized)
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def clean(v, nd=4):
    try:
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            return None
        return round(float(v), nd)
    except Exception:
        return None


# ---------------------------------------------------------------- watchlist
def read_watchlist():
    syms = []
    if os.path.exists(WATCHLIST):
        for line in open(WATCHLIST, encoding="utf-8"):
            s = line.split("#")[0].strip().upper()
            if s:
                syms.append(s)
    seen, out = set(), []
    for s in syms:
        if s not in seen:
            seen.add(s); out.append(s)
    return out


# ---------------------------------------------------------------- Black-Scholes helpers
def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(S, K, T, sigma, call=True, r=RISK_FREE):
    if S <= 0 or K <= 0 or T <= 0 or sigma is None or sigma <= 0:
        return None
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return _ncdf(d1) if call else _ncdf(d1) - 1.0


# ---------------------------------------------------------------- option analytics (pure functions; testable)
def analyze_chain(S, today, chains):
    """chains: list of dicts {expiry: 'YYYY-MM-DD', calls: DataFrame, puts: DataFrame}
    DataFrames need columns: strike, impliedVolatility, openInterest, volume, bid, ask, lastPrice."""
    term, per_exp = [], []
    tot_call_oi = tot_put_oi = tot_call_vol = tot_put_vol = 0.0
    for ch in chains:
        try:
            exp = dt.date.fromisoformat(ch["expiry"])
        except Exception:
            continue
        days = (exp - today).days
        if days <= 0:
            continue
        T = days / 365.0
        calls, puts = ch["calls"], ch["puts"]
        if calls is None or puts is None or len(calls) == 0 or len(puts) == 0:
            continue
        c = calls.copy(); p = puts.copy()
        for d in (c, p):
            for col in ("strike", "impliedVolatility", "openInterest", "volume", "bid", "ask", "lastPrice"):
                if col not in d.columns:
                    d[col] = np.nan
                d[col] = pd.to_numeric(d[col], errors="coerce")
            d["mid"] = np.where((d["bid"] > 0) & (d["ask"] > 0), (d["bid"] + d["ask"]) / 2, d["lastPrice"])
            d.dropna(subset=["strike"], inplace=True)
        # drop absurd IVs (Yahoo returns ~1e-5 for illiquid strikes)
        c_iv = c[(c["impliedVolatility"] > 0.02) & (c["impliedVolatility"] < 5)]
        p_iv = p[(p["impliedVolatility"] > 0.02) & (p["impliedVolatility"] < 5)]

        def interp_iv(d, K):
            d = d.sort_values("strike")
            if d.empty:
                return None
            return float(np.interp(K, d["strike"].values, d["impliedVolatility"].values))
        atm_c, atm_p = interp_iv(c_iv, S), interp_iv(p_iv, S)
        atm = None if (atm_c is None and atm_p is None) else float(np.nanmean([v for v in (atm_c, atm_p) if v is not None]))

        # 25-delta skew
        def iv_at_delta(d, target, call):
            rows = []
            for _, r in d.iterrows():
                dl = bs_delta(S, r["strike"], T, r["impliedVolatility"], call=call)
                if dl is not None:
                    rows.append((dl, r["impliedVolatility"]))
            if len(rows) < 2:
                return None
            rows.sort()
            xs, ys = zip(*rows)
            if target < xs[0] or target > xs[-1]:
                return None
            return float(np.interp(target, xs, ys))
        iv_p25 = iv_at_delta(p_iv, -0.25, False)
        iv_c25 = iv_at_delta(c_iv, 0.25, True)
        skew = None if (iv_p25 is None or iv_c25 is None) else iv_p25 - iv_c25
        # fallback skew by moneyness (95% put vs 105% call)
        if skew is None and not c_iv.empty and not p_iv.empty:
            a, b = interp_iv(p_iv, 0.95 * S), interp_iv(c_iv, 1.05 * S)
            skew = None if (a is None or b is None) else a - b

        # straddle (ATM) mid price -> implied move to expiry
        def nearest(d, K):
            if d.empty:
                return None
            i = (d["strike"] - K).abs().idxmin()
            return d.loc[i]
        nc, npt = nearest(c, S), nearest(p, S)
        straddle = None
        if nc is not None and npt is not None and nc["mid"] > 0 and npt["mid"] > 0:
            straddle = float(nc["mid"] + npt["mid"])

        coi, poi = float(c["openInterest"].fillna(0).sum()), float(p["openInterest"].fillna(0).sum())
        cvol, pvol = float(c["volume"].fillna(0).sum()), float(p["volume"].fillna(0).sum())
        tot_call_oi += coi; tot_put_oi += poi; tot_call_vol += cvol; tot_put_vol += pvol
        # OI by strike (top walls) and max pain
        oi_c = c.groupby("strike")["openInterest"].sum().fillna(0)
        oi_p = p.groupby("strike")["openInterest"].sum().fillna(0)
        strikes = sorted(set(oi_c.index) | set(oi_p.index))
        pain = None
        if strikes:
            best, bestv = None, None
            for K in strikes:
                v = sum(max(0.0, K - k2) * oi_c.get(k2, 0) for k2 in strikes) + sum(max(0.0, k2 - K) * oi_p.get(k2, 0) for k2 in strikes)
                if bestv is None or v < bestv:
                    best, bestv = K, v
            pain = float(best)
        call_wall = float(oi_c.idxmax()) if len(oi_c) and oi_c.max() > 0 else None
        put_wall = float(oi_p.idxmax()) if len(oi_p) and oi_p.max() > 0 else None
        # compact OI distribution around spot (±25%) for the page
        dist = []
        for K in strikes:
            if 0.75 * S <= K <= 1.25 * S:
                dist.append([float(K), int(oi_c.get(K, 0)), int(oi_p.get(K, 0))])
        term.append({"days": days, "expiry": ch["expiry"], "atm_iv": clean(atm), "skew25": clean(skew),
                     "straddle_pct": clean(straddle / S if straddle else None),
                     "call_oi": int(coi), "put_oi": int(poi), "call_vol": int(cvol), "put_vol": int(pvol),
                     "max_pain": clean(pain, 2), "call_wall": clean(call_wall, 2), "put_wall": clean(put_wall, 2)})
        per_exp.append({"expiry": ch["expiry"], "days": days, "dist": dist})
    term.sort(key=lambda x: x["days"])
    if not term:
        return None

    def interp_days(key, target):
        pts = [(t["days"], t[key]) for t in term if t.get(key) is not None]
        if not pts:
            return None
        xs, ys = zip(*pts)
        if target <= xs[0]:
            return ys[0]
        if target >= xs[-1]:
            return ys[-1]
        return float(np.interp(target, xs, ys))
    iv30, iv60, iv90 = interp_days("atm_iv", 30), interp_days("atm_iv", 60), interp_days("atm_iv", 90)
    near = next((t for t in term if t["days"] >= 5), term[0])
    skew30 = interp_days("skew25", 30)
    tot_oi = tot_call_oi + tot_put_oi
    tot_vol = tot_call_vol + tot_put_vol
    # nearest "monthly-like" expiry with the most OI for walls
    walls = max(term, key=lambda t: t["call_oi"] + t["put_oi"]) if term else None
    return {
        "iv30": clean(iv30), "iv60": clean(iv60), "iv90": clean(iv90),
        "term_ratio": clean(near["atm_iv"] / iv90) if (near.get("atm_iv") and iv90) else None,
        "skew25": clean(skew30),
        "pcr_oi": clean(tot_put_oi / tot_call_oi) if tot_call_oi else None,
        "pcr_vol": clean(tot_put_vol / tot_call_vol) if tot_call_vol else None,
        "vol_oi": clean(tot_vol / tot_oi) if tot_oi else None,
        "total_oi": int(tot_oi), "total_vol": int(tot_vol),
        "exp_move_30d": clean(iv30 * math.sqrt(30 / 365)) if iv30 else None,
        "walls": {"expiry": walls["expiry"], "call_wall": walls["call_wall"], "put_wall": walls["put_wall"], "max_pain": walls["max_pain"]} if walls else None,
        "term": term,
        "oi_dist": next((e for e in per_exp if e["expiry"] == (walls["expiry"] if walls else None)), per_exp[0] if per_exp else None),
    }


# ---------------------------------------------------------------- price analytics (pure)
def analyze_prices(px: pd.DataFrame, spy: pd.DataFrame):
    """px: DataFrame with columns date, close, high, low, volume (ascending). spy: date, close."""
    px = px.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    if len(px) < 30:
        return None
    c = px["close"].astype(float)
    ret = c.pct_change()
    lr = np.log(c).diff()
    out = {}
    out["close"] = clean(c.iloc[-1], 2)
    out["r1"] = clean(ret.iloc[-1], 5)
    for n in (5, 20, 60, 120):
        out[f"r{n}"] = clean(c.iloc[-1] / c.iloc[-1 - n] - 1, 5) if len(c) > n else None
    out["hv20"] = clean(lr.tail(20).std() * math.sqrt(252))
    out["hv60"] = clean(lr.tail(60).std() * math.sqrt(252))
    ma50 = c.rolling(50).mean(); ma200 = c.rolling(200).mean()
    out["ma50"] = clean(ma50.iloc[-1], 2); out["ma200"] = clean(ma200.iloc[-1], 2)
    out["ma50_slope20"] = clean(ma50.iloc[-1] / ma50.iloc[-21] - 1, 5) if len(ma50.dropna()) > 21 else None
    hi252 = c.tail(252).max(); lo252 = c.tail(252).min()
    out["from_52w_high"] = clean(c.iloc[-1] / hi252 - 1); out["from_52w_low"] = clean(c.iloc[-1] / lo252 - 1)
    # max drawdown last 60d
    w = c.tail(60); out["mdd60"] = clean((w / w.cummax() - 1).min())
    # ATR14 %
    hl = (px["high"] - px["low"]).abs(); hc = (px["high"] - c.shift(1)).abs(); lc = (px["low"] - c.shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    out["atr14_pct"] = clean(tr.tail(14).mean() / c.iloc[-1])
    # RSI14
    d = c.diff(); up = d.clip(lower=0).rolling(14).mean(); dn = (-d.clip(upper=0)).rolling(14).mean()
    rs = up / dn.replace(0, np.nan); out["rsi14"] = clean(100 - 100 / (1 + rs.iloc[-1]), 1)
    # MACD histogram sign (12/26/9)
    ema12 = c.ewm(span=12).mean(); ema26 = c.ewm(span=26).mean(); macd = ema12 - ema26; sig = macd.ewm(span=9).mean()
    out["macd_hist"] = clean((macd - sig).iloc[-1] / c.iloc[-1], 5)
    # volume trend
    v = px["volume"].astype(float)
    out["vol20_vs_60"] = clean(v.tail(20).mean() / v.tail(60).mean()) if v.tail(60).mean() else None
    out["vol_ratio"] = clean(v.iloc[-1] / v.tail(20).mean()) if v.tail(20).mean() else None
    # gaps (>3% open gap) in last 60 days — needs open; approximate with |ret|>4%
    out["big_moves60"] = int((ret.tail(60).abs() > 0.04).sum())
    # relative strength vs SPY
    if spy is not None and len(spy) > 60:
        s = spy.set_index("date")["close"].reindex(px["date"]).ffill().values
        for n in (20, 60):
            if len(c) > n and s[-1 - n] and s[-1]:
                out[f"rs{n}"] = clean((c.iloc[-1] / c.iloc[-1 - n]) - (s[-1] / s[-1 - n]), 5)
        # 60-day correlation and beta
        sr = pd.Series(s).pct_change().tail(60); pr = ret.tail(60)
        out["corr60"] = clean(np.corrcoef(pr.values[-len(sr):], sr.values)[0, 1]) if sr.notna().sum() > 30 else None
        out["beta60"] = clean(np.cov(pr.values[-len(sr):], sr.values)[0, 1] / np.var(sr.values)) if sr.notna().sum() > 30 else None
    # series for charts (last 120d)
    tail = px.tail(120)
    out["series"] = {"dates": list(tail["date"]), "c": [clean(x, 2) for x in tail["close"]],
                     "v": [None if pd.isna(x) else int(x) for x in tail["volume"]],
                     "ma50": [clean(x, 2) for x in ma50.tail(120)], "ma200": [clean(x, 2) for x in ma200.tail(120)]}
    # estimated net flow (same definition as the main app)
    nf = (np.sign(c.diff()) * c * v).tail(120)
    out["series"]["nf"] = [clean(x, 0) for x in nf]
    return out


# ---------------------------------------------------------------- scoring
def momentum_score(pr):
    """-5..+5, rule based: sign of r20, r60, ma50>ma200, rs20, macd hist."""
    if not pr:
        return None, []
    parts = []
    def vote(name, cond_pos, cond_neg):
        v = 1 if cond_pos else (-1 if cond_neg else 0)
        parts.append({"k": name, "v": v}); return v
    s = 0
    s += vote("20日リターン", (pr.get("r20") or 0) > 0, (pr.get("r20") or 0) < 0)
    s += vote("60日リターン", (pr.get("r60") or 0) > 0, (pr.get("r60") or 0) < 0)
    s += vote("50日線＞200日線", (pr.get("ma50") or 0) > (pr.get("ma200") or 0) if pr.get("ma200") else False,
              (pr.get("ma50") or 0) < (pr.get("ma200") or 0) if pr.get("ma200") else False)
    s += vote("対SPY 20日", (pr.get("rs20") or 0) > 0, (pr.get("rs20") or 0) < 0)
    s += vote("MACD ヒストグラム", (pr.get("macd_hist") or 0) > 0, (pr.get("macd_hist") or 0) < 0)
    return s, parts


def risk_flags(pr, op, ctx):
    """ctx: dict with iv_rank, skew_pctl, earnings_days, short_jump, insider_sells."""
    f = []
    T = THRESH
    def add(key, label, detail):
        f.append({"k": key, "label": label, "detail": detail})
    if ctx.get("iv_rank") is not None and ctx["iv_rank"] >= T["iv_rank_high"]:
        add("iv_rank", "IV が高水準", f"IV ランク {ctx['iv_rank']:.0f}（蓄積 {ctx.get('iv_hist_n',0)} 日）")
    if op and op.get("term_ratio") and op["term_ratio"] >= T["term_ratio_high"]:
        add("term", "近月 IV が突出（イベント警戒）", f"近月IV ÷ 90日IV = {op['term_ratio']:.2f}")
    if ctx.get("skew_pctl") is not None and ctx["skew_pctl"] >= T["skew_pctl_high"]:
        add("skew", "プット・スキュー拡大（下落ヘッジ需要）", f"スキュー百分位 {ctx['skew_pctl']:.0f}")
    if ctx.get("earnings_days") is not None and 0 <= ctx["earnings_days"] <= T["earnings_days"]:
        add("earnings", "決算が近い", f"{ctx['earnings_days']} 営業日後（{ctx.get('earnings_date')}）")
    if pr and pr.get("from_52w_high") is not None and pr["from_52w_high"] <= T["drawdown_52w"]:
        add("dd", "52週高値から大きく下落", f"{pr['from_52w_high']*100:.1f}%")
    if pr and pr.get("ma200") and pr.get("close") and pr["close"] < pr["ma200"]:
        add("ma200", "200日線割れ", f"終値 {pr['close']} ＜ 200日線 {pr['ma200']}")
    if pr and pr.get("rs20") is not None and pr["rs20"] <= T["rs20_weak"]:
        add("rs", "市場より大きく弱い", f"対SPY 20日 {pr['rs20']*100:+.1f}%")
    if ctx.get("short_jump") is not None and ctx["short_jump"] >= T["short_ratio_jump"]:
        add("short", "空売り比率が急上昇", f"5日平均−20日平均 {ctx['short_jump']*100:+.1f}pt")
    if ctx.get("insider_sells") is not None and ctx["insider_sells"] >= T["insider_sells_90d"]:
        add("insider", "インサイダー売りが集中", f"直近90日 売り {ctx['insider_sells']} 件 / 買い {ctx.get('insider_buys',0)} 件")
    if op and op.get("vol_oi") and op["vol_oi"] >= T["unusual_vol_oi"]:
        add("unusual", "オプション出来高が異常", f"出来高 ÷ 建玉 = {op['vol_oi']:.2f}")
    if op and op.get("iv30") and pr and pr.get("hv20") is not None and op["iv30"] - pr["hv20"] >= T["iv_hv_gap"]:
        add("ivhv", "IV が実現ボラを大きく上回る", f"IV30 {op['iv30']*100:.0f}% − HV20 {pr['hv20']*100:.0f}%")
    return f


# ---------------------------------------------------------------- history (IV / skew accumulation)
def load_hist():
    if os.path.exists(HIST_PATH):
        try:
            return json.load(open(HIST_PATH))
        except Exception:
            pass
    return {}


def pctl(values, x):
    vals = [v for v in values if v is not None]
    if x is None or len(vals) < 5:
        return None
    return 100.0 * sum(1 for v in vals if v <= x) / len(vals)


# ---------------------------------------------------------------- Yahoo fetch
def fetch_ticker(sym, yf, today):
    t = yf.Ticker(sym)
    info = {}
    try:
        info = t.info or {}
    except Exception as e:
        log(f"  {sym} info failed: {e}")
    # option chains
    chains = []
    try:
        exps = list(t.options or [])
        # choose nearest expiries + those closest to 30/60/90 days
        def days(e):
            return (dt.date.fromisoformat(e) - today).days
        exps = [e for e in exps if days(e) > 0]
        pick = exps[:3]
        for target in (30, 60, 90):
            if exps:
                best = min(exps, key=lambda e: abs(days(e) - target))
                if best not in pick:
                    pick.append(best)
        pick = sorted(set(pick), key=days)[:MAX_EXPIRIES]
        for e in pick:
            try:
                oc = t.option_chain(e)
                chains.append({"expiry": e, "calls": oc.calls, "puts": oc.puts})
                time.sleep(0.3)
            except Exception as ex:
                log(f"  {sym} chain {e} failed: {ex}")
    except Exception as e:
        log(f"  {sym} options list failed: {e}")
    # earnings
    earnings_date = None
    try:
        ed = t.earnings_dates
        if ed is not None and len(ed):
            fut = [d for d in pd.to_datetime(ed.index).date if d >= today]
            if fut:
                earnings_date = min(fut).isoformat()
    except Exception as e:
        log(f"  {sym} earnings_dates failed: {e}")
    if earnings_date is None:
        try:
            cal = t.calendar
            if isinstance(cal, dict) and cal.get("Earnings Date"):
                ds = cal["Earnings Date"]
                ds = ds if isinstance(ds, list) else [ds]
                fut = [pd.to_datetime(x).date() for x in ds if pd.to_datetime(x).date() >= today]
                if fut:
                    earnings_date = min(fut).isoformat()
        except Exception:
            pass
    # insider transactions (last ~90 days)
    ins_buys = ins_sells = None
    try:
        it = t.insider_transactions
        if it is not None and len(it):
            it = it.copy()
            dcol = next((c for c in it.columns if "date" in str(c).lower()), None)
            tcol = next((c for c in it.columns if str(c).lower() in ("text", "transaction")), None)
            if dcol is not None:
                it["_d"] = pd.to_datetime(it[dcol], errors="coerce")
                it = it[it["_d"] >= pd.Timestamp(today - dt.timedelta(days=90))]
            txt = it[tcol].astype(str).str.lower() if tcol is not None else pd.Series([""] * len(it))
            ins_sells = int(txt.str.contains("sale").sum())
            ins_buys = int(txt.str.contains("purchase|buy").sum())
    except Exception as e:
        log(f"  {sym} insider failed: {e}")
    # analyst targets / recommendations
    targets, recs = None, None
    try:
        apt = t.analyst_price_targets
        if isinstance(apt, dict) and apt:
            targets = {k: clean(apt.get(k), 2) for k in ("current", "low", "high", "mean", "median")}
    except Exception:
        pass
    try:
        rc = t.recommendations
        if rc is not None and len(rc):
            r0 = rc.iloc[0]
            recs = {k: int(r0[k]) for k in ("strongBuy", "buy", "hold", "sell", "strongSell") if k in rc.columns}
    except Exception:
        pass
    return {"info": info, "chains": chains, "earnings_date": earnings_date,
            "insider_buys": ins_buys, "insider_sells": ins_sells, "targets": targets, "recs": recs}


def trading_days_until(today, date_iso):
    if not date_iso:
        return None
    d = dt.date.fromisoformat(date_iso)
    return int(np.busday_count(today, d))


# ---------------------------------------------------------------- main
def build(sym, px, spy, fetched, hist, short_hist, sp_stocks, today):
    pr = analyze_prices(px, spy) if px is not None and len(px) else None
    S = pr["close"] if pr else None
    op = analyze_chain(S, today, fetched["chains"]) if (S and fetched["chains"]) else None
    # history accumulation
    h = hist.setdefault(sym, {})
    if op:
        h[today.isoformat()] = {"iv30": op.get("iv30"), "skew": op.get("skew25"), "term": op.get("term_ratio"), "pcr": op.get("pcr_oi")}
    keys = sorted(h)[-HIST_KEEP_DAYS:]
    hist[sym] = {k: h[k] for k in keys}
    iv_hist = [h[k].get("iv30") for k in keys]
    skew_hist = [h[k].get("skew") for k in keys]
    ctx = {
        "iv_rank": pctl(iv_hist, op.get("iv30")) if op else None,
        "iv_hist_n": sum(1 for v in iv_hist if v is not None),
        "skew_pctl": pctl(skew_hist, op.get("skew25")) if op else None,
        "earnings_date": fetched["earnings_date"],
        "earnings_days": trading_days_until(today, fetched["earnings_date"]),
        "insider_buys": fetched["insider_buys"], "insider_sells": fetched["insider_sells"],
    }
    # short ratio jump from main pipeline's history (S&P 500 names only)
    sh = short_hist.get(sym) or short_hist.get(sym.replace("-", "."))
    if sh:
        ks = sorted(sh)
        a5 = np.mean([sh[k] for k in ks[-5:]]); a20 = np.mean([sh[k] for k in ks[-20:]])
        ctx["short_jump"] = clean(a5 - a20)
        ctx["short_ratio"] = clean(sh[ks[-1]])
    info = fetched["info"] or {}
    spx = sp_stocks.get(sym) or sp_stocks.get(sym.replace("-", "."))
    score, parts = momentum_score(pr)
    flags = risk_flags(pr, op, ctx)
    # expected move to earnings via straddle at first expiry after earnings
    em_earn = None
    if op and ctx["earnings_date"]:
        after = [t for t in op["term"] if t["expiry"] >= ctx["earnings_date"] and t.get("straddle_pct")]
        if after:
            em_earn = {"expiry": after[0]["expiry"], "straddle_pct": after[0]["straddle_pct"]}
    return {
        "sym": sym,
        "name": info.get("shortName") or info.get("longName") or (spx or {}).get("nm") or sym,
        "sector": (spx or {}).get("sec") or info.get("sector"),
        "industry": (spx or {}).get("ind") or info.get("industry"),
        "in_sp500": bool(spx),
        "mcap": info.get("marketCap"),
        "beta_yahoo": clean(info.get("beta")),
        "short_pct_float": clean(info.get("shortPercentOfFloat")),
        "price": pr, "options": op, "ctx": ctx,
        "momentum_score": score, "momentum_parts": parts, "flags": flags,
        "exp_move_earnings": em_earn,
        "targets": fetched["targets"], "recs": fetched["recs"],
        "sector_flow": None if not spx else {"nf20_stock": spx.get("nf20"), "sr": spx.get("sr")},
    }


def main():
    import yfinance as yf
    today = dt.date.today()
    syms = read_watchlist()
    if not syms:
        log("watchlist.txt is empty — nothing to do")
        json.dump({"as_of": today.isoformat(), "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "stocks": [], "thresholds": THRESH, "note": "watchlist.txt に銘柄がありません"}, open(OUT_PATH, "w"))
        return
    log(f"watchlist: {syms}")
    # prices (batch)
    raw = yf.download(syms + ["SPY"], period="1y", interval="1d", group_by="ticker", auto_adjust=False, actions=False, threads=True, progress=False)
    def frame(s):
        if s not in raw.columns.get_level_values(0):
            return None
        d = raw[s][["Close", "High", "Low", "Volume"]].copy(); d.columns = ["close", "high", "low", "volume"]
        d.index = pd.to_datetime(d.index).strftime("%Y-%m-%d"); d.index.name = "date"
        return d.reset_index().dropna(subset=["close"])
    spy = frame("SPY")
    hist = load_hist()
    short_hist = json.load(open(SHORT_PATH)) if os.path.exists(SHORT_PATH) else {}
    sp_stocks = {}
    if os.path.exists(LATEST_PATH):
        try:
            sp_stocks = {x["s"]: x for x in json.load(open(LATEST_PATH)).get("stocks", [])}
        except Exception:
            pass
    stocks, errors = [], {}
    for sym in syms:
        try:
            fetched = fetch_ticker(sym, yf, today)
            stocks.append(build(sym, frame(sym), spy, fetched, hist, short_hist, sp_stocks, today))
            log(f"{sym}: ok (chains={len(fetched['chains'])}, flags={len(stocks[-1]['flags'])})")
        except Exception as e:
            errors[sym] = str(e)[:200]
            log(f"{sym}: FAILED — {e}")
        time.sleep(0.5)
    # portfolio-level: correlation matrix of daily returns (60d)
    corr = None
    try:
        rets = {}
        for sym in syms:
            f = frame(sym)
            if f is not None and len(f) > 61:
                rets[sym] = f.set_index("date")["close"].pct_change().tail(60)
        if len(rets) >= 2:
            dfc = pd.DataFrame(rets).corr()
            corr = {"syms": list(dfc.columns), "m": [[clean(v, 2) for v in row] for row in dfc.values]}
    except Exception as e:
        log(f"corr failed: {e}")
    stocks.sort(key=lambda x: (-len(x["flags"]), -(x["momentum_score"] or 0)))
    out = {"as_of": (spy["date"].iloc[-1] if spy is not None else today.isoformat()),
           "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "stocks": stocks, "errors": errors, "corr": corr, "thresholds": THRESH,
           "spy": None if spy is None else {"dates": list(spy["date"].tail(120)), "c": [clean(x, 2) for x in spy["close"].tail(120)]},
           "meta": {"n_watchlist": len(syms), "history_days": max((len(v) for v in hist.values()), default=0),
                    "sources": "Yahoo Finance via yfinance (prices, option chains ~15min delayed EOD, earnings dates, insider transactions, analyst targets); FINRA short volume via main pipeline"}}
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(out, open(OUT_PATH, "w"), separators=(",", ":"))
    json.dump(hist, open(HIST_PATH, "w"), separators=(",", ":"))
    log(f"wrote {OUT_PATH}: {len(stocks)} stocks, {len(errors)} errors")


if __name__ == "__main__":
    main()

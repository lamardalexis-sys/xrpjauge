#!/usr/bin/env python3
"""
Thermomètre de stress XRP — V2 — collecte des données et calcul du score 0-100.

Sources 100 % gratuites, sans clé API, joignables depuis les runners GitHub :
  - Prix + volume journaliers      : Kraken (fallback CoinGecko, puis Binance)
  - Funding (dernier + moyenne 7 j): OKX (fallback Binance)
  - Open interest 7 j              : OKX (fallback Binance)
  - Ratio comptes long/short       : OKX
  - Liquidations récentes          : OKX
  - Volatilité implicite BTC (DVOL): Deribit
  - VIX (peur actions US)          : Yahoo Finance (fallback stooq)
  - Capitalisation stablecoins     : DefiLlama
  - Dominance BTC                  : CoinGecko (optionnel)
  - Fear & Greed                   : alternative.me
  - Événements binaires            : events.json (édité à la main)

Usage :
  python collect.py            # collecte réelle, écrit docs/data.json
  python collect.py --mock     # données fictives, pour tester hors ligne
  python collect.py --verbose  # affiche le détail du calcul

Le score n'est PAS une prédiction de black swan. C'est un indicateur de
conditions : il monte quand les ingrédients d'une capitulation se mettent en
place (tendance cassée, levier, volatilité, peur, retrait de liquidité,
événement binaire imminent). Plus de sources = moins de bruit, pas plus de
prescience.
"""

import argparse
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
DATA_PATH = os.path.join(DOCS, "data.json")
EVENTS_PATH = os.path.join(ROOT, "events.json")
MANUAL_PATH = os.path.join(ROOT, "manual.json")  # saisies optionnelles (flux ETF…)

BUY_ZONE_HIGH = float(os.environ.get("BUY_ZONE_HIGH", "1.12"))
BUY_ZONE_LOW = float(os.environ.get("BUY_ZONE_LOW", "0.90"))

WEIGHTS = {
    "trend": 18,       # XRP/BTC vs SMA 50/200, ratio XRP/BTC
    "momentum": 14,    # chute 7/30 j, hausse parabolique, vol réalisée, volume anormal
    "leverage": 18,    # funding (dernier + 7 j), OI, long/short, liquidations
    "volatility": 10,  # vol implicite BTC (DVOL) + VIX
    "liquidity": 10,   # stablecoins 7 j, dominance BTC, flux ETF (manuel)
    "sentiment": 8,    # Fear & Greed
    "events": 12,      # événements binaires < 14 j
    "news": 10,        # géopolitique & news (GDELT, RSS, lexique, IA optionnelle) — voir news.py
}
THEMES_PATH = os.path.join(ROOT, "themes.json")
LEVELS_PATH = os.path.join(ROOT, "levels.json")
EXCHANGES_PATH = os.path.join(ROOT, "exchanges.json")
WHALES_PATH = os.path.join(ROOT, "whales.json")

UA = {"User-Agent": "Mozilla/5.0 (compatible; xrp-stress-gauge/2.0; +github pages)"}


# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #
def log(verbose, *a):
    if verbose:
        print(*a, file=sys.stderr)


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_text(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def try_sources(name, sources, verbose):
    for label, fn in sources:
        try:
            v = fn()
            if v is not None:
                log(verbose, f"  [{name}] ok via {label}")
                return v, label
        except Exception as e:  # noqa: BLE001
            log(verbose, f"  [{name}] {label} échec : {e}")
    return None, None


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def lin(x, x0, y0, x1, y1):
    if x0 == x1:
        return y0
    t = (x - x0) / (x1 - x0)
    t = max(0.0, min(1.0, t))
    return y0 + t * (y1 - y0)


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def stdev(xs):
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def realized_vol(closes, n):
    """Volatilité annualisée des rendements log sur les n derniers jours (en %)."""
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    s = stdev(rets)
    return None if s is None else s * math.sqrt(365) * 100


def wavg(parts):
    """parts = [(score, poids), ...] en ignorant les None."""
    parts = [(s, w) for s, w in parts if s is not None]
    if not parts:
        return None
    return sum(s * w for s, w in parts) / sum(w for _, w in parts)


# --------------------------------------------------------------------------- #
# Collecte : prix + volume journaliers
# --------------------------------------------------------------------------- #
def ohlc_kraken(pair):
    d = get_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440")
    res = d["result"]
    key = [k for k in res if k != "last"][0]
    rows = res[key][-230:]
    return {"closes": [float(r[4]) for r in rows], "volumes": [float(r[6]) for r in rows],
            "times": [int(r[0]) for r in rows],
            "opens": [float(r[1]) for r in rows], "highs": [float(r[2]) for r in rows], "lows": [float(r[3]) for r in rows]}


def ohlc_coingecko(coin_id):
    d = get_json(f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency=usd&days=230&interval=daily")
    return {"closes": [p[1] for p in d["prices"]], "volumes": [v[1] for v in d["total_volumes"]],
            "times": [int(p[0] / 1000) for p in d["prices"]]}


def ohlc_binance(symbol):
    d = get_json(f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval=1d&limit=230")
    return {"closes": [float(k[4]) for k in d], "volumes": [float(k[5]) for k in d],
            "times": [int(k[0] / 1000) for k in d],
            "opens": [float(k[1]) for k in d], "highs": [float(k[2]) for k in d], "lows": [float(k[3]) for k in d]}


def fetch_ohlc(asset, verbose):
    cfg = {
        "XRP": [("kraken", lambda: ohlc_kraken("XRPUSD")),
                ("coingecko", lambda: ohlc_coingecko("ripple")),
                ("binance", lambda: ohlc_binance("XRP"))],
        "BTC": [("kraken", lambda: ohlc_kraken("XBTUSD")),
                ("coingecko", lambda: ohlc_coingecko("bitcoin")),
                ("binance", lambda: ohlc_binance("BTC"))],
    }[asset]
    return try_sources(f"prix {asset}", cfg, verbose)


# --------------------------------------------------------------------------- #
# Collecte : dérivés (OKX en premier : joignable depuis GitHub)
# --------------------------------------------------------------------------- #
def funding_hist_okx():
    """Renvoie la liste des funding (fraction) du plus ancien au plus récent, ~7 j (3/j)."""
    d = get_json("https://www.okx.com/api/v5/public/funding-rate-history?instId=XRP-USDT-SWAP&limit=21")
    rows = list(reversed(d["data"]))
    return [float(r["fundingRate"]) for r in rows]


def funding_hist_binance():
    d = get_json("https://fapi.binance.com/fapi/v1/fundingRate?symbol=XRPUSDT&limit=21")
    return [float(r["fundingRate"]) for r in d]


def oi_okx():
    d = get_json("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=XRP&period=1D")
    rows = list(reversed(d["data"]))[-8:]
    return [float(r[1]) for r in rows]  # OI en USD


def oi_binance():
    d = get_json("https://fapi.binance.com/futures/data/openInterestHist?symbol=XRPUSDT&period=1d&limit=8")
    return [float(x["sumOpenInterestValue"]) for x in d]


def longshort_okx():
    """Ratio comptes longs / comptes shorts (1.0 = équilibre)."""
    d = get_json("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=XRP&period=1D")
    return float(d["data"][0][1])


def liquidations_okx():
    """Notionnel liquidé côté long vs short sur les dernières liquidations connues (USD)."""
    d = get_json("https://www.okx.com/api/v5/public/liquidation-orders?instType=SWAP&uly=XRP-USDT&state=filled&limit=100")
    longs = shorts = 0.0
    n = 0
    for inst in d.get("data", []):
        for det in inst.get("details", []):
            notional = float(det["sz"]) * 100.0 * float(det["bkPx"])  # 1 contrat = 100 XRP
            n += 1
            # côté de la position liquidée : posSide 'long'/'short', sinon déduit de side
            side = det.get("posSide") or ("long" if det.get("side") == "sell" else "short")
            if side == "long":
                longs += notional
            else:
                shorts += notional
    if n == 0:
        return None
    return {"long_usd": longs, "short_usd": shorts, "count": n}


# --------------------------------------------------------------------------- #
# Collecte : volatilité implicite, macro, liquidité, sentiment
# --------------------------------------------------------------------------- #
def dvol_deribit():
    """Indice de vol implicite BTC (DVOL), série journalière ~30 j, du plus ancien au plus récent."""
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = now - 35 * 86400 * 1000
    d = get_json(f"https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&resolution=86400&start_timestamp={start}&end_timestamp={now}")
    rows = d["result"]["data"]
    return [float(r[4]) for r in rows]  # close


def vix_yahoo():
    d = get_json("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=1mo&interval=1d")
    closes = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    return [c for c in closes if c is not None]


def vix_stooq():
    txt = get_text("https://stooq.com/q/d/l/?s=^vix&i=d")
    rows = [l.split(",") for l in txt.strip().splitlines()[1:]]
    return [float(r[4]) for r in rows if len(r) >= 5][-25:]


def stablecoins_llama():
    """Capitalisation totale des stablecoins USD, série journalière (du plus ancien au plus récent)."""
    d = get_json("https://stablecoins.llama.fi/stablecoincharts/all")
    out = []
    for row in d[-40:]:
        v = row.get("totalCirculatingUSD", {}).get("peggedUSD")
        if v is not None:
            out.append(float(v))
    return out or None


def dominance_coingecko():
    d = get_json("https://api.coingecko.com/api/v3/global")
    return float(d["data"]["market_cap_percentage"]["btc"])


def fng_alternative():
    d = get_json("https://api.alternative.me/fng/?limit=8")
    vals = [int(x["value"]) for x in reversed(d["data"])]
    return vals  # du plus ancien au plus récent


# --------------------------------------------------------------------------- #
# Événements et saisies manuelles
# --------------------------------------------------------------------------- #
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def events_component(events, now):
    scored, upcoming = [], []
    for ev in events:
        try:
            d = datetime.fromisoformat(ev["date"]).replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            continue
        days = (d - now).total_seconds() / 86400.0
        impact = float(ev.get("impact", 2)) / 3.0
        if -2.0 <= days < 0:
            s = 50.0 * impact
        elif 0 <= days <= 1:
            s = 100.0 * impact
        elif 1 < days <= 14:
            s = lin(days, 1, 100.0, 14, 0.0) * impact
        else:
            s = 0.0
        if days >= -2.0:
            upcoming.append({"name": ev.get("name", "?"), "date": ev["date"], "impact": int(ev.get("impact", 2)),
                             "days": round(days, 1), "score": round(s, 1)})
        if s > 0:
            scored.append(s)
    scored.sort(reverse=True)
    total = scored[0] + 0.25 * sum(scored[1:3]) if scored else 0.0
    upcoming.sort(key=lambda e: e["days"])
    return clamp(total), upcoming[:6]


# --------------------------------------------------------------------------- #
# Composants
# --------------------------------------------------------------------------- #
def trend_component(xrp, btc):
    def vs_sma(closes):
        if not closes:
            return None, {}
        p = closes[-1]
        s200, s50 = sma(closes, 200), sma(closes, 50)
        parts, det = [], {}
        if s200:
            d200 = p / s200 - 1
            parts.append((lin(d200, -0.25, 100, 0.10, 0), 2))
            det["sma200"] = round(s200, 4)
            det["dist_sma200_pct"] = round(d200 * 100, 2)
        if s50:
            d50 = p / s50 - 1
            parts.append((lin(d50, -0.15, 100, 0.08, 0), 1))
            det["sma50"] = round(s50, 4)
            det["dist_sma50_pct"] = round(d50 * 100, 2)
        return wavg(parts), det

    sx, dx = vs_sma(xrp)
    sb, db = vs_sma(btc)
    detail = {"xrp": dx, "btc": db}

    # Ratio XRP/BTC : XRP qui sous-performe BTC sur 30 j = faiblesse relative (alt en fin de bear / risk-off)
    sr = None
    if xrp and btc and len(xrp) >= 31 and len(btc) >= 31:
        r_now = xrp[-1] / btc[-1]
        r_30 = xrp[-31] / btc[-31]
        rel = r_now / r_30 - 1
        sr = lin(rel, -0.20, 100, 0.10, 0)
        detail["xrp_btc_ratio_sats"] = round(r_now * 1e8)
        detail["xrp_vs_btc_30d_pct"] = round(rel * 100, 2)

    score = wavg([(sx, 0.5), (sb, 0.3), (sr, 0.2)])
    return score, detail


def momentum_component(xrp):
    if not xrp or len(xrp["closes"]) < 31:
        return None, {}
    c = xrp["closes"]
    v = xrp.get("volumes") or []
    p = c[-1]
    c7 = p / c[-8] - 1
    c30 = p / c[-31] - 1
    det = {"change_7d_pct": round(c7 * 100, 2), "change_30d_pct": round(c30 * 100, 2)}

    drop = 0.7 * lin(c7, -0.20, 100, 0.0, 0) + 0.3 * lin(c30, -0.35, 100, 0.0, 0)
    parabolic = lin(c7, 0.15, 25, 0.35, 45) if c7 >= 0.15 else 0.0

    # Volatilité réalisée : expansion (vol7 >> vol30) = stress ; compression extrême = complaisance légère
    rv7, rv30 = realized_vol(c, 7), realized_vol(c, 30)
    vol_s = None
    if rv7 and rv30:
        ratio = rv7 / rv30
        vol_s = lin(ratio, 1.0, 0, 2.2, 100)
        if ratio < 0.55 and rv7 < 45:
            vol_s = 15.0
        vol_s = max(vol_s, lin(rv7, 60, 0, 140, 100) * 0.8)
        det["rvol_7d_pct"] = round(rv7, 1)
        det["rvol_30d_pct"] = round(rv30, 1)

    # Volume anormal : dernier jour complet vs moyenne 30 j, aggravé si journée baissière
    vol_anom_s = None
    if len(v) >= 32 and v[-2] > 0:
        avg30 = sum(v[-32:-2]) / 30.0
        ratio_v = v[-2] / avg30 if avg30 > 0 else 1.0
        down_day = c[-2] < c[-3] if len(c) >= 3 else False
        vol_anom_s = lin(ratio_v, 1.2, 0, 3.0, 100) * (1.0 if down_day else 0.5)
        det["volume_vs_30d"] = round(ratio_v, 2)

    score = wavg([(max(drop, parabolic), 0.55), (vol_s, 0.30), (vol_anom_s, 0.15)])
    return score, det


def leverage_component(funding_hist, oi_series, xrp_closes, ls_ratio, liq):
    det, parts = {}, []

    if funding_hist:
        f_last = funding_hist[-1] * 100
        f_avg = sum(funding_hist) / len(funding_hist) * 100
        def fscore(f_pct):
            if f_pct >= 0.01:
                return lin(f_pct, 0.01, 0, 0.05, 80) if f_pct <= 0.05 else lin(f_pct, 0.05, 80, 0.10, 100)
            return lin(f_pct, 0.01, 0, -0.02, 70) if f_pct >= -0.02 else lin(f_pct, -0.02, 70, -0.06, 100)
        fs = 0.5 * fscore(f_last) + 0.5 * fscore(f_avg)
        det["funding_pct_8h"] = round(f_last, 4)
        det["funding_avg7d_pct_8h"] = round(f_avg, 4)
        parts.append((fs, 0.40))

    if oi_series and len(oi_series) >= 2:
        oi_chg = oi_series[-1] / oi_series[0] - 1
        det["oi_change_7d_pct"] = round(oi_chg * 100, 2)
        base = lin(abs(oi_chg), 0.0, 0, 0.15, 100)
        if xrp_closes and len(xrp_closes) >= 8:
            c7 = xrp_closes[-1] / xrp_closes[-8] - 1
            if oi_chg > 0.03 and c7 < -0.03:
                base = clamp(base * 1.3 + 10)   # OI monte, prix baisse : shorts qui s'empilent
            elif oi_chg > 0.05 and c7 > 0.10:
                base = clamp(base * 1.2 + 10)   # OI monte, prix flambe : longs à levier
            elif oi_chg < -0.10:
                base = clamp(base * 0.6)        # deleveraging déjà fait
        parts.append((base, 0.25))

    if ls_ratio is not None:
        # >1 : plus de comptes longs. Extrême dans un sens ou l'autre = foule d'un seul côté
        ls = lin(ls_ratio, 1.2, 0, 2.5, 100) if ls_ratio >= 1.0 else lin(ls_ratio, 0.9, 0, 0.5, 80)
        det["long_short_ratio"] = round(ls_ratio, 2)
        parts.append((ls, 0.20))

    if liq and (liq["long_usd"] + liq["short_usd"]) > 0:
        tot = liq["long_usd"] + liq["short_usd"]
        share_long = liq["long_usd"] / tot
        # cascade de longs (capitulation) = stress fort ; cascade de shorts = squeeze, stress modéré
        lq = lin(share_long, 0.5, 20, 0.9, 100) if share_long >= 0.5 else lin(share_long, 0.5, 20, 0.1, 55)
        det["liq_long_share_pct"] = round(share_long * 100, 1)
        det["liq_total_usd"] = round(tot)
        parts.append((lq, 0.15))

    return wavg(parts), det


def volatility_component(dvol, vix):
    det, parts = {}, []
    if dvol and len(dvol) >= 2:
        d_now = dvol[-1]
        d_avg = sum(dvol[:-1]) / (len(dvol) - 1)
        # DVOL absolu : <45 calme, >90 panique ; et saut relatif vs moyenne 30 j
        abs_s = lin(d_now, 45, 0, 95, 100)
        rel_s = lin(d_now / d_avg if d_avg else 1.0, 1.0, 0, 1.6, 100)
        # compression extrême (< 40 et sous 0,8× la moyenne) = complaisance légère
        comp = 15.0 if (d_now < 40 and d_avg and d_now / d_avg < 0.8) else 0.0
        parts.append((max(0.6 * abs_s + 0.4 * rel_s, comp), 0.65))
        det["btc_dvol"] = round(d_now, 1)
        det["btc_dvol_avg30"] = round(d_avg, 1)
    if vix and len(vix) >= 2:
        v_now = vix[-1]
        v_avg = sum(vix[:-1]) / (len(vix) - 1)
        abs_s = lin(v_now, 15, 0, 35, 100)
        rel_s = lin(v_now / v_avg if v_avg else 1.0, 1.0, 0, 1.5, 100)
        parts.append((0.6 * abs_s + 0.4 * rel_s, 0.35))
        det["vix"] = round(v_now, 2)
        det["vix_avg"] = round(v_avg, 2)
    return wavg(parts), det


def liquidity_component(stables, dominance, manual, whale_score=None):
    det, parts = {}, []
    if whale_score is not None:
        det["whale_flow_score"] = round(whale_score, 1)
        parts.append((whale_score, 0.30))
    if stables and len(stables) >= 8:
        chg7 = stables[-1] / stables[-8] - 1
        # Stablecoins qui se contractent = liquidité qui sort du système
        s = lin(chg7, 0.0, 0, -0.03, 100)
        if chg7 > 0.01:
            s = 0.0
        det["stablecoins_7d_pct"] = round(chg7 * 100, 2)
        det["stablecoins_usd_bn"] = round(stables[-1] / 1e9, 1)
        parts.append((s, 0.55))
    if dominance is not None:
        # Dominance BTC élevée = fuite hors des alts (mauvais pour XRP), > 62 % = stress alt
        s = lin(dominance, 55, 0, 66, 100)
        det["btc_dominance_pct"] = round(dominance, 2)
        parts.append((s, 0.25))
    etf = (manual or {}).get("xrp_etf_weekly_flow_musd")
    if etf is not None:
        # saisie manuelle (M$/semaine) : sorties nettes = stress
        s = lin(float(etf), 0, 20, -100, 100) if etf < 0 else lin(float(etf), 0, 20, 100, 0)
        det["etf_weekly_flow_musd"] = float(etf)
        det["etf_flow_asof"] = (manual or {}).get("asof")
        parts.append((s, 0.20))
    return wavg(parts), det


def sentiment_component(fng_hist):
    if not fng_hist:
        return None, {}
    fng = fng_hist[-1]
    if fng <= 20:
        s = lin(fng, 0, 100, 20, 80)
    elif fng <= 40:
        s = lin(fng, 20, 80, 40, 35)
    elif fng <= 60:
        s = lin(fng, 40, 35, 60, 15)
    elif fng <= 75:
        s = lin(fng, 60, 15, 75, 30)
    else:
        s = lin(fng, 75, 30, 100, 65)
    det = {"fear_greed": fng}
    if len(fng_hist) >= 8:
        drop = fng_hist[-8] - fng
        det["fear_greed_change_7d"] = -drop
        if drop >= 20:
            s = clamp(s + lin(drop, 20, 10, 40, 25))  # chute rapide du sentiment
    return s, det


def zone(score):
    if score < 30:
        return "calme", "Calme"
    if score < 55:
        return "vigilance", "Vigilance"
    if score < 75:
        return "eleve", "Stress élevé"
    return "critique", "Capitulation probable"


# --------------------------------------------------------------------------- #
# Entrées
# --------------------------------------------------------------------------- #
def mock_inputs():
    xrp_c, xrp_v = [], []
    for i in range(230):
        t = i / 229
        base = 2.1 - 1.1 * min(1, t / 0.85)
        if t > 0.85:
            base = 1.0 + 0.40 * ((t - 0.85) / 0.15)
        xrp_c.append(round(base + 0.03 * math.sin(i / 3.0), 4))
        xrp_v.append(round(2.0e9 + 0.6e9 * math.sin(i / 5.0) + (2.5e9 if 195 <= i <= 197 else 0)))
    btc_c, btc_v = [], []
    for i in range(230):
        t = i / 229
        base = 95000 - 30000 * min(1, t / 0.75)
        if t > 0.75:
            base = 65000 + 14000 * ((t - 0.75) / 0.25)
        btc_c.append(round(base + 800 * math.sin(i / 4.0), 2))
        btc_v.append(round(30000 + 8000 * math.sin(i / 6.0)))
    t0 = int(datetime.now(timezone.utc).timestamp()) - 229 * 86400
    times = [t0 + i * 86400 for i in range(230)]
    import random
    rnd = random.Random(7)
    xo = [xrp_c[i - 1] if i else xrp_c[0] for i in range(230)]
    xh = [max(xo[i], xrp_c[i]) * (1 + rnd.random() * 0.03) for i in range(230)]
    xl = [min(xo[i], xrp_c[i]) * (1 - rnd.random() * 0.03) for i in range(230)]
    return {
        "xrp": {"closes": xrp_c, "volumes": xrp_v, "times": times, "opens": xo, "highs": xh, "lows": xl},
        "btc": {"closes": btc_c, "volumes": btc_v, "times": times},
        "funding_hist": [0.0001] * 15 + [0.00012] * 6,
        "oi": [2.55e9, 2.58e9, 2.60e9, 2.63e9, 2.66e9, 2.70e9, 2.72e9, 2.75e9],
        "ls_ratio": 1.35,
        "liq": {"long_usd": 4.2e6, "short_usd": 2.8e6, "count": 60},
        "dvol": [52 + 4 * math.sin(i / 3) for i in range(30)] + [55.0],
        "vix": [17 + 1.5 * math.sin(i / 2) for i in range(22)] + [18.4],
        "stables": [300e9 + 0.2e9 * i for i in range(40)],
        "dominance": 59.1,
        "fng_hist": [66, 64, 63, 60, 58, 57, 57, 57],
        "manual": {},
        "sources": {k: "mock" for k in ["xrp", "btc", "funding", "oi", "long_short", "liquidations",
                                         "dvol", "vix", "stablecoins", "dominance", "fng"]},
    }


def real_inputs(verbose):
    xrp, s_xrp = fetch_ohlc("XRP", verbose)
    btc, s_btc = fetch_ohlc("BTC", verbose)
    funding, s_f = try_sources("funding", [("okx", funding_hist_okx), ("binance", funding_hist_binance)], verbose)
    oi, s_oi = try_sources("open interest", [("okx", oi_okx), ("binance", oi_binance)], verbose)
    ls, s_ls = try_sources("long/short", [("okx", longshort_okx)], verbose)
    liq, s_liq = try_sources("liquidations", [("okx", liquidations_okx)], verbose)
    dvol, s_dv = try_sources("dvol", [("deribit", dvol_deribit)], verbose)
    vix, s_vix = try_sources("vix", [("yahoo", vix_yahoo), ("stooq", vix_stooq)], verbose)
    stables, s_st = try_sources("stablecoins", [("defillama", stablecoins_llama)], verbose)
    dom, s_dom = try_sources("dominance", [("coingecko", dominance_coingecko)], verbose)
    fng, s_fng = try_sources("fear&greed", [("alternative.me", fng_alternative)], verbose)
    manual = load_json(MANUAL_PATH, {})
    return {
        "xrp": xrp, "btc": btc, "funding_hist": funding, "oi": oi, "ls_ratio": ls, "liq": liq,
        "dvol": dvol, "vix": vix, "stables": stables, "dominance": dom, "fng_hist": fng, "manual": manual,
        "sources": {"xrp": s_xrp, "btc": s_btc, "funding": s_f, "oi": s_oi, "long_short": s_ls,
                    "liquidations": s_liq, "dvol": s_dv, "vix": s_vix, "stablecoins": s_st,
                    "dominance": s_dom, "fng": s_fng},
    }


# --------------------------------------------------------------------------- #
# Assemblage
# --------------------------------------------------------------------------- #
def compute(inputs, events, now, verbose=False):
    xrp_c = inputs["xrp"]["closes"] if inputs.get("xrp") else None
    btc_c = inputs["btc"]["closes"] if inputs.get("btc") else None

    comps, details = {}, {}
    comps["trend"], details["trend"] = trend_component(xrp_c, btc_c)
    comps["momentum"], details["momentum"] = momentum_component(inputs.get("xrp"))
    comps["leverage"], details["leverage"] = leverage_component(inputs.get("funding_hist"), inputs.get("oi"), xrp_c,
                                                                inputs.get("ls_ratio"), inputs.get("liq"))
    comps["volatility"], details["volatility"] = volatility_component(inputs.get("dvol"), inputs.get("vix"))
    whales = inputs.get("whales") or {}
    comps["liquidity"], details["liquidity"] = liquidity_component(inputs.get("stables"), inputs.get("dominance"),
                                                                   inputs.get("manual"), whales.get("score"))
    details["whales"] = whales
    comps["sentiment"], details["sentiment"] = sentiment_component(inputs.get("fng_hist"))
    comps["events"], upcoming = events_component(events, now)
    details["events"] = {"upcoming": upcoming}
    comps["news"] = inputs.get("news_score")
    details["news"] = inputs.get("news_detail") or {}

    avail = {k: v for k, v in comps.items() if v is not None}
    wsum = sum(WEIGHTS[k] for k in avail)
    score = round(clamp(sum(avail[k] * WEIGHTS[k] for k in avail) / wsum), 1) if wsum else 0.0
    zkey, zlabel = zone(score)

    xrp_price = xrp_c[-1] if xrp_c else None
    btc_price = btc_c[-1] if btc_c else None
    buy = None
    if xrp_price:
        buy = {"high": BUY_ZONE_HIGH, "low": BUY_ZONE_LOW,
               "dist_to_high_pct": round((BUY_ZONE_HIGH / xrp_price - 1) * 100, 1),
               "dist_to_low_pct": round((BUY_ZONE_LOW / xrp_price - 1) * 100, 1),
               "in_zone": BUY_ZONE_LOW <= xrp_price <= BUY_ZONE_HIGH}

    for k in comps:
        log(verbose, f"  {k:10s} = {'n/d' if comps[k] is None else round(comps[k], 1)}  (poids {WEIGHTS[k]})")
    log(verbose, f"  SCORE = {score}  → {zlabel}")

    # Séries pour le graphique (180 derniers jours) + niveaux
    series = []
    if xrp_c:
        times = (inputs["xrp"].get("times") or [])
        opens, highs, lows = inputs["xrp"].get("opens"), inputs["xrp"].get("highs"), inputs["xrp"].get("lows")
        n = len(xrp_c)
        for i in range(max(0, n - 180), n):
            d_ = datetime.fromtimestamp(times[i], tz=timezone.utc).strftime("%Y-%m-%d") if i < len(times) else None
            series.append({"d": d_, "c": round(xrp_c[i], 4),
                           "o": round(opens[i], 4) if opens else None,
                           "h": round(highs[i], 4) if highs else None,
                           "l": round(lows[i], 4) if lows else None,
                           "s50": round(sma(xrp_c[:i + 1], 50), 4) if i + 1 >= 50 else None,
                           "s200": round(sma(xrp_c[:i + 1], 200), 4) if i + 1 >= 200 else None})
    levels = inputs.get("levels") or {}

    return {
        "series": series,
        "levels": {"buy_zone": [BUY_ZONE_LOW, BUY_ZONE_HIGH], "invalidation": levels.get("invalidation"),
                   "sell_zones": levels.get("sell_zones", [])},
        "version": 4,
        "updated_at": now.isoformat(timespec="seconds"),
        "score": score,
        "zone": zkey,
        "zone_label": zlabel,
        "weights": WEIGHTS,
        "components": {k: (None if v is None else round(v, 1)) for k, v in comps.items()},
        "details": details,
        "prices": {"xrp": xrp_price, "btc": btc_price},
        "buy_zone": buy,
        "sources": inputs["sources"],
        "missing": [k for k, v in comps.items() if v is None],
    }


def merge_history(new, old):
    old = old or {}
    hist = old.get("history", [])
    day = new["updated_at"][:10]
    prev_same_day = [h for h in hist if h.get("d") == day]
    hist = [h for h in hist if h.get("d") != day]
    hist.append({"d": day, "s": new["score"], "p": new["prices"]["xrp"],
                 "c": {k: v for k, v in new["components"].items() if v is not None}})
    hist.sort(key=lambda h: h["d"])
    new["history"] = hist[-180:]

    # Variation : vs exécution précédente et vs veille (dernier point d'un autre jour)
    prev_score = old.get("score")
    new["delta_prev_run"] = None if prev_score is None else round(new["score"] - prev_score, 1)
    others = [h for h in new["history"] if h["d"] != day]
    new["delta_24h"] = round(new["score"] - others[-1]["s"], 1) if others else (
        round(new["score"] - prev_same_day[0]["s"], 1) if prev_same_day else None)
    return new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--out", default=DATA_PATH)
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    events = load_json(EVENTS_PATH, {}).get("events", [])
    levels = load_json(LEVELS_PATH, {})
    exchanges = load_json(EXCHANGES_PATH, {}).get("exchanges", {})
    global BUY_ZONE_LOW, BUY_ZONE_HIGH
    if not os.environ.get("BUY_ZONE_LOW") and levels.get("buy_zone"):
        BUY_ZONE_LOW, BUY_ZONE_HIGH = float(levels["buy_zone"][0]), float(levels["buy_zone"][1])
    inputs = mock_inputs() if args.mock else real_inputs(args.verbose)
    inputs["levels"] = levels

    old = load_json(args.out, None)

    # Baleines (module séparé ; ne bloque jamais le reste)
    try:
        import whales as whalemod
        prev_log = ((old or {}).get("details", {}).get("whales", {}) or {}).get("log")
        price_now = inputs["xrp"]["closes"][-1] if inputs.get("xrp") else None
        inputs["whales"] = whalemod.collect_whales(now, price_now, levels, exchanges, previous_log=prev_log,
                                                  verbose=args.verbose, mock=args.mock)
        prev_pos = ((old or {}).get("details", {}).get("whales", {}) or {}).get("positions")
        inputs["whales"]["positions"] = whalemod.track_positions(now, load_json(WHALES_PATH, {}), prev_pos,
                                                                 verbose=args.verbose, mock=args.mock)
    except Exception as e:  # noqa: BLE001
        log(args.verbose, f"  [whales] module en échec : {e}")
        inputs["whales"] = None

    # Géopolitique & news (module séparé ; ne bloque jamais le reste)
    try:
        import news as newsmod
        themes_cfg = load_json(THEMES_PATH, {})
        if themes_cfg.get("themes"):
            prev = {"news": (old or {}).get("details", {}).get("news", {})} if old else None
            inputs["news_score"], inputs["news_detail"] = newsmod.collect_news(
                themes_cfg, now, previous=prev, verbose=args.verbose, mock=args.mock)
    except Exception as e:  # noqa: BLE001
        log(args.verbose, f"  [news] module en échec : {e}")
        inputs["news_score"], inputs["news_detail"] = None, {}

    result = merge_history(compute(inputs, events, now, args.verbose), old)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"score={result['score']} zone={result['zone_label']} delta24h={result['delta_24h']} "
          f"missing={result['missing']} → {args.out}")
    if len(result["missing"]) >= 6:
        sys.exit(2)


if __name__ == "__main__":
    main()

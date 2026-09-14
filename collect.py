#!/usr/bin/env python3
"""
Thermomètre de stress XRP — collecte des données et calcul du score 0-100.

Sources 100 % gratuites, sans clé API :
  - Prix / moyennes mobiles : Binance spot (fallback Kraken, puis CoinGecko)
  - Funding rate            : Binance Futures (fallback Bybit, puis OKX)
  - Open interest           : Binance Futures (fallback Bybit)
  - Fear & Greed            : alternative.me
  - Événements binaires     : events.json (édité à la main)

Usage :
  python collect.py            # collecte réelle, écrit docs/data.json
  python collect.py --mock     # données fictives, pour tester hors ligne
  python collect.py --verbose  # affiche le détail du calcul

Le score n'est PAS une prédiction de black swan (personne ne peut prédire
l'imprévisible). C'est un indicateur de conditions : il monte quand les
ingrédients d'une capitulation se mettent en place (levier, tendance cassée,
peur, événement binaire imminent).
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
DATA_PATH = os.path.join(DOCS, "data.json")
EVENTS_PATH = os.path.join(ROOT, "events.json")

# Zone d'achat personnelle (modifiable ici ou via variables d'environnement)
BUY_ZONE_HIGH = float(os.environ.get("BUY_ZONE_HIGH", "1.12"))
BUY_ZONE_LOW = float(os.environ.get("BUY_ZONE_LOW", "0.90"))

WEIGHTS = {
    "trend": 25,      # tendance : prix vs SMA 50/200 (XRP + BTC)
    "momentum": 15,   # chute récente ou hausse parabolique
    "leverage": 25,   # funding + open interest
    "sentiment": 15,  # Fear & Greed
    "events": 20,     # événements binaires à venir
}

UA = {"User-Agent": "xrp-stress-gauge/1.0 (+github pages)"}


# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #
def log(verbose, *a):
    if verbose:
        print(*a, file=sys.stderr)


def get_json(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def try_sources(name, sources, verbose):
    """Essaie chaque source dans l'ordre, renvoie (valeur, nom_source) ou (None, None)."""
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
    """Interpolation linéaire bornée entre (x0,y0) et (x1,y1)."""
    if x0 == x1:
        return y0
    t = (x - x0) / (x1 - x0)
    t = max(0.0, min(1.0, t))
    return y0 + t * (y1 - y0)


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


# --------------------------------------------------------------------------- #
# Collecte : prix journaliers (clôtures)
# --------------------------------------------------------------------------- #
def closes_binance(symbol):
    d = get_json(f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval=1d&limit=220")
    closes = [float(k[4]) for k in d]
    return closes


def closes_kraken(pair):
    d = get_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440")
    res = d["result"]
    key = [k for k in res if k != "last"][0]
    return [float(row[4]) for row in res[key]][-220:]


def closes_coingecko(coin_id):
    d = get_json(f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency=usd&days=220&interval=daily")
    return [p[1] for p in d["prices"]]


def fetch_closes(asset, verbose):
    cfg = {
        "XRP": [("binance", lambda: closes_binance("XRP")),
                ("kraken", lambda: closes_kraken("XRPUSD")),
                ("coingecko", lambda: closes_coingecko("ripple"))],
        "BTC": [("binance", lambda: closes_binance("BTC")),
                ("kraken", lambda: closes_kraken("XBTUSD")),
                ("coingecko", lambda: closes_coingecko("bitcoin"))],
    }[asset]
    return try_sources(f"prix {asset}", cfg, verbose)


# --------------------------------------------------------------------------- #
# Collecte : funding rate (par 8 h, en fraction : 0.0001 = 0,01 %)
# --------------------------------------------------------------------------- #
def funding_binance():
    d = get_json("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=XRPUSDT")
    return float(d["lastFundingRate"])


def funding_bybit():
    d = get_json("https://api.bybit.com/v5/market/tickers?category=linear&symbol=XRPUSDT")
    return float(d["result"]["list"][0]["fundingRate"])


def funding_okx():
    d = get_json("https://www.okx.com/api/v5/public/funding-rate?instId=XRP-USDT-SWAP")
    return float(d["data"][0]["fundingRate"])


# --------------------------------------------------------------------------- #
# Collecte : open interest (valeur en USD, série journalière)
# --------------------------------------------------------------------------- #
def oi_binance():
    d = get_json("https://fapi.binance.com/futures/data/openInterestHist?symbol=XRPUSDT&period=1d&limit=8")
    return [float(x["sumOpenInterestValue"]) for x in d]


def oi_bybit():
    d = get_json("https://api.bybit.com/v5/market/open-interest?category=linear&symbol=XRPUSDT&intervalTime=1d&limit=8")
    lst = list(reversed(d["result"]["list"]))  # bybit renvoie du plus récent au plus ancien
    # openInterest est en contrats (= XRP) ; on n'a pas le prix ici, on renvoie la quantité,
    # seule la variation relative est utilisée.
    return [float(x["openInterest"]) for x in lst]


# --------------------------------------------------------------------------- #
# Collecte : Fear & Greed
# --------------------------------------------------------------------------- #
def fng_alternative():
    d = get_json("https://api.alternative.me/fng/?limit=1")
    return int(d["data"][0]["value"])


# --------------------------------------------------------------------------- #
# Événements
# --------------------------------------------------------------------------- #
def load_events():
    if not os.path.exists(EVENTS_PATH):
        return []
    with open(EVENTS_PATH, encoding="utf-8") as f:
        return json.load(f).get("events", [])


def events_component(events, now):
    """Score 0-100 selon la proximité d'événements binaires.
    impact 1 (mineur) … 3 (majeur). 100 % de l'impact à J-1 ou moins,
    décroît linéairement jusqu'à 0 à J-14. Un événement passé depuis moins
    de 2 jours compte à 50 % (période de digestion)."""
    scored = []
    upcoming = []
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
            upcoming.append({
                "name": ev.get("name", "?"),
                "date": ev["date"],
                "impact": int(ev.get("impact", 2)),
                "days": round(days, 1),
                "score": round(s, 1),
            })
        if s > 0:
            scored.append(s)
    scored.sort(reverse=True)
    total = 0.0
    if scored:
        total = scored[0] + 0.25 * sum(scored[1:3])
    upcoming.sort(key=lambda e: e["days"])
    return clamp(total), upcoming[:6]


# --------------------------------------------------------------------------- #
# Calcul des composants
# --------------------------------------------------------------------------- #
def trend_component(xrp, btc):
    """Prix sous ses moyennes = stress. 0 si ≥ +10 % au-dessus de la SMA200,
    100 si ≤ -25 % en dessous. La SMA50 pèse un tiers."""
    def one(closes):
        if not closes:
            return None, {}
        p = closes[-1]
        s200, s50 = sma(closes, 200), sma(closes, 50)
        parts, detail = [], {}
        if s200:
            d200 = p / s200 - 1
            parts.append((lin(d200, -0.25, 100, 0.10, 0), 2))
            detail["sma200"] = round(s200, 4)
            detail["dist_sma200_pct"] = round(d200 * 100, 2)
        if s50:
            d50 = p / s50 - 1
            parts.append((lin(d50, -0.15, 100, 0.08, 0), 1))
            detail["sma50"] = round(s50, 4)
            detail["dist_sma50_pct"] = round(d50 * 100, 2)
        if not parts:
            return None, detail
        score = sum(s * w for s, w in parts) / sum(w for _, w in parts)
        return score, detail

    sx, dx = one(xrp)
    sb, db = one(btc)
    if sx is None and sb is None:
        return None, {}
    if sb is None:
        return sx, {"xrp": dx}
    if sx is None:
        return sb, {"btc": db}
    return 0.6 * sx + 0.4 * sb, {"xrp": dx, "btc": db}


def momentum_component(xrp):
    """Chute sur 7 j = stress (0 à 0 %, 100 à -20 %). Hausse parabolique
    (≥ +15 % / 7 j) = fragilité, plafonnée à 45. Chute sur 30 j ajoute."""
    if not xrp or len(xrp) < 31:
        return None, {}
    p = xrp[-1]
    c7 = p / xrp[-8] - 1
    c30 = p / xrp[-31] - 1
    drop7 = lin(c7, -0.20, 100, 0.0, 0)
    drop30 = lin(c30, -0.35, 100, 0.0, 0)
    parabolic = lin(c7, 0.15, 25, 0.35, 45) if c7 >= 0.15 else 0.0
    score = max(0.7 * drop7 + 0.3 * drop30, parabolic)
    return score, {"change_7d_pct": round(c7 * 100, 2), "change_30d_pct": round(c30 * 100, 2)}


def leverage_component(funding, oi_series, xrp):
    """Funding : neutre à 0,01 %/8 h. ≥ 0,05 % = longs surchargés (80),
    ≤ -0,02 % = shorts surchargés / capitulation (70).
    Open interest : variation 7 j, amplifiée si elle diverge du prix."""
    detail = {}
    parts = []
    if funding is not None:
        f_pct = funding * 100  # en %
        if f_pct >= 0.01:
            fs = lin(f_pct, 0.01, 0, 0.05, 80) if f_pct <= 0.05 else lin(f_pct, 0.05, 80, 0.10, 100)
        else:
            fs = lin(f_pct, 0.01, 0, -0.02, 70) if f_pct >= -0.02 else lin(f_pct, -0.02, 70, -0.06, 100)
        detail["funding_pct_8h"] = round(f_pct, 4)
        parts.append((fs, 0.6))
    if oi_series and len(oi_series) >= 2:
        oi_chg = oi_series[-1] / oi_series[0] - 1
        detail["oi_change_7d_pct"] = round(oi_chg * 100, 2)
        base = lin(abs(oi_chg), 0.0, 0, 0.15, 100)
        # divergence : OI monte alors que le prix baisse (shorts) ou monte fort (longs à levier)
        if xrp and len(xrp) >= 8:
            c7 = xrp[-1] / xrp[-8] - 1
            if oi_chg > 0.03 and c7 < -0.03:
                base = clamp(base * 1.3 + 10)
            elif oi_chg > 0.05 and c7 > 0.10:
                base = clamp(base * 1.2 + 10)
            elif oi_chg < -0.10:
                base = clamp(base * 0.6)  # deleveraging déjà fait : moins de stress
        parts.append((base, 0.4))
    if not parts:
        return None, detail
    score = sum(s * w for s, w in parts) / sum(w for _, w in parts)
    return score, detail


def sentiment_component(fng):
    """Fear & Greed : peur extrême = capitulation en cours (haut),
    avidité extrême = complaisance (moyen), neutre = bas."""
    if fng is None:
        return None, {}
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
    return s, {"fear_greed": fng}


def zone(score):
    if score < 30:
        return "calme", "Calme"
    if score < 55:
        return "vigilance", "Vigilance"
    if score < 75:
        return "eleve", "Stress élevé"
    return "critique", "Capitulation probable"


# --------------------------------------------------------------------------- #
# Données fictives (tests hors ligne)
# --------------------------------------------------------------------------- #
def mock_inputs():
    import math
    # série XRP : descente de 2.0 vers 1.0 puis rebond vers 1.38
    xrp = []
    for i in range(220):
        t = i / 219
        base = 2.1 - 1.1 * min(1, t / 0.85)
        if t > 0.85:
            base = 1.0 + 0.38 * ((t - 0.85) / 0.15)
        xrp.append(round(base + 0.03 * math.sin(i / 3.0), 4))
    btc = []
    for i in range(220):
        t = i / 219
        base = 95000 - 30000 * min(1, t / 0.75)
        if t > 0.75:
            base = 65000 + 15000 * ((t - 0.75) / 0.25)
        btc.append(round(base + 800 * math.sin(i / 4.0), 2))
    return {
        "xrp": xrp, "btc": btc,
        "funding": 0.00012,          # 0,012 % / 8 h
        "oi": [2.55e9, 2.58e9, 2.60e9, 2.63e9, 2.66e9, 2.70e9, 2.72e9, 2.75e9],
        "fng": 62,
        "sources": {"xrp": "mock", "btc": "mock", "funding": "mock", "oi": "mock", "fng": "mock"},
    }


def real_inputs(verbose):
    xrp, s_xrp = fetch_closes("XRP", verbose)
    btc, s_btc = fetch_closes("BTC", verbose)
    funding, s_f = try_sources("funding", [("binance", funding_binance), ("bybit", funding_bybit), ("okx", funding_okx)], verbose)
    oi, s_oi = try_sources("open interest", [("binance", oi_binance), ("bybit", oi_bybit)], verbose)
    fng, s_fng = try_sources("fear&greed", [("alternative.me", fng_alternative)], verbose)
    return {
        "xrp": xrp, "btc": btc, "funding": funding, "oi": oi, "fng": fng,
        "sources": {"xrp": s_xrp, "btc": s_btc, "funding": s_f, "oi": s_oi, "fng": s_fng},
    }


# --------------------------------------------------------------------------- #
# Assemblage
# --------------------------------------------------------------------------- #
def compute(inputs, events, now, verbose=False):
    comps = {}
    details = {}

    comps["trend"], details["trend"] = trend_component(inputs["xrp"], inputs["btc"])
    comps["momentum"], details["momentum"] = momentum_component(inputs["xrp"])
    comps["leverage"], details["leverage"] = leverage_component(inputs["funding"], inputs["oi"], inputs["xrp"])
    comps["sentiment"], details["sentiment"] = sentiment_component(inputs["fng"])
    comps["events"], upcoming = events_component(events, now)
    details["events"] = {"upcoming": upcoming}

    # Pondération en ignorant les composants indisponibles (re-normalisation)
    avail = {k: v for k, v in comps.items() if v is not None}
    wsum = sum(WEIGHTS[k] for k in avail)
    score = sum(avail[k] * WEIGHTS[k] for k in avail) / wsum if wsum else 0.0
    score = round(clamp(score), 1)
    zkey, zlabel = zone(score)

    xrp_price = inputs["xrp"][-1] if inputs["xrp"] else None
    btc_price = inputs["btc"][-1] if inputs["btc"] else None
    buy = None
    if xrp_price:
        buy = {
            "high": BUY_ZONE_HIGH, "low": BUY_ZONE_LOW,
            "dist_to_high_pct": round((BUY_ZONE_HIGH / xrp_price - 1) * 100, 1),
            "dist_to_low_pct": round((BUY_ZONE_LOW / xrp_price - 1) * 100, 1),
            "in_zone": BUY_ZONE_LOW <= xrp_price <= BUY_ZONE_HIGH,
        }

    for k in comps:
        log(verbose, f"  {k:10s} = {comps[k] if comps[k] is None else round(comps[k], 1)}  (poids {WEIGHTS[k]})")
    log(verbose, f"  SCORE = {score}  → {zlabel}")

    return {
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
    """Conserve un point par jour (le dernier), 180 jours max."""
    hist = (old or {}).get("history", [])
    day = new["updated_at"][:10]
    hist = [h for h in hist if h.get("d") != day]
    hist.append({"d": day, "s": new["score"], "p": new["prices"]["xrp"]})
    hist.sort(key=lambda h: h["d"])
    new["history"] = hist[-180:]
    return new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="données fictives (hors ligne)")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--out", default=DATA_PATH)
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    events = load_events()
    inputs = mock_inputs() if args.mock else real_inputs(args.verbose)

    old = None
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:  # noqa: BLE001
            old = None

    result = compute(inputs, events, now, args.verbose)
    result = merge_history(result, old)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"score={result['score']} zone={result['zone_label']} missing={result['missing']} → {args.out}")
    # Échec seulement si RIEN n'a pu être calculé
    if len(result["missing"]) >= 4:
        sys.exit(2)


if __name__ == "__main__":
    main()

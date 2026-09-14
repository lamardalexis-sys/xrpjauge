#!/usr/bin/env python3
"""
Suivi des baleines XRP pour le thermomètre.

Deux sources, la première disponible gagne, la seconde complète :
  1. Whale Alert (clé gratuite, secret WHALE_ALERT_KEY) : tous les transferts XRP
     ≥ whale_min_usd de la dernière heure, avec l'étiquette de l'exchange quand elle
     est connue (Binance, Upbit, …).
  2. XRP Ledger direct (sans clé, API publique JSON-RPC) : lit les N derniers
     ledgers validés et garde les paiements XRP ≥ whale_min_xrp_ledger. Couverture
     partielle (~N × 4 s par run) et étiquetage limité à exchanges.json.

Interprétation (la seule qui soit honnête avec des données on-chain) :
  - exchange → inconnu  : RETRAIT  = quelqu'un a acheté et sort ses XRP (accumulation)
  - inconnu → exchange  : DÉPÔT    = préparation probable d'une vente
  - exchange → exchange : INTERNE  = rééquilibrage, neutre
  - inconnu → inconnu   : TRANSFERT = gros mouvement, sens inconnu

Le journal est conservé 7 jours dans data.json (details.whales.log), avec le
prix XRP au moment du run pour le placer sur le graphique.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

UA = {"User-Agent": "Mozilla/5.0 (compatible; xrp-stress-gauge/4.0; +github pages)"}
XRPL_RPC = os.environ.get("XRPL_RPC", "https://xrplcluster.com/")
XRPL_LEDGERS = int(os.environ.get("XRPL_LEDGERS", "150"))   # ~10 min de ledgers par run


def _log(verbose, *a):
    if verbose:
        print(*a, file=sys.stderr)


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _rpc(method, params, timeout=30):
    body = json.dumps({"method": method, "params": [params]}).encode("utf-8")
    req = urllib.request.Request(XRPL_RPC, data=body, headers={"content-type": "application/json", **UA}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8")).get("result", {})


def classify(from_owner_type, to_owner_type):
    fe = from_owner_type == "exchange"
    te = to_owner_type == "exchange"
    if fe and not te:
        return "retrait"
    if te and not fe:
        return "depot"
    if fe and te:
        return "interne"
    return "transfert"


# --------------------------------------------------------------------------- #
# 1. Whale Alert
# --------------------------------------------------------------------------- #
def whale_alert(now, min_usd, verbose=False):
    key = (os.environ.get("WHALE_ALERT_KEY") or "").strip()
    if not key:
        _log(verbose, "  [whales] whale alert : pas de clé (secret WHALE_ALERT_KEY absent) → XRP Ledger seul")
        return None
    start = int((now - timedelta(minutes=70)).timestamp())  # plan gratuit : 1 h d'historique
    url = ("https://api.whale-alert.io/v1/transactions?"
           + urllib.parse.urlencode({"api_key": key, "min_value": int(max(500000, min_usd)),
                                     "start": start, "currency": "xrp"}))
    try:
        d = _get(url)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:160]
        except Exception:  # noqa: BLE001
            body = ""
        _log(verbose, f"  [whales] whale alert échec : HTTP {e.code} {body}")
        return None
    except Exception as e:  # noqa: BLE001
        _log(verbose, f"  [whales] whale alert échec : {e}")
        return None
    if d.get("result") != "success":
        _log(verbose, f"  [whales] whale alert : {d.get('message', d)}")
        return None
    out = []
    for t in d.get("transactions", []):
        if t.get("symbol", "").lower() != "xrp":
            continue
        f, to = t.get("from", {}), t.get("to", {})
        out.append({
            "id": f"wa:{t.get('hash') or t.get('id')}",
            "ts": datetime.fromtimestamp(int(t["timestamp"]), tz=timezone.utc).isoformat(timespec="seconds"),
            "xrp": round(float(t.get("amount", 0))),
            "usd": round(float(t.get("amount_usd", 0))),
            "from": f.get("owner") or ("inconnu" if f.get("owner_type") != "exchange" else "exchange"),
            "to": to.get("owner") or ("inconnu" if to.get("owner_type") != "exchange" else "exchange"),
            "kind": classify(f.get("owner_type"), to.get("owner_type")),
            "src": "whale-alert",
        })
    _log(verbose, f"  [whales] whale alert ok : {len(out)} transfert(s) ≥ {int(min_usd):,} $".replace(",", " "))
    return out


# --------------------------------------------------------------------------- #
# 2. XRP Ledger direct
# --------------------------------------------------------------------------- #
def xrpl_scan(now, min_xrp, exchanges, verbose=False, max_ledgers=None):
    n = max_ledgers or XRPL_LEDGERS
    try:
        top = _rpc("ledger", {"ledger_index": "validated", "transactions": False})
        top_idx = int(top["ledger"]["ledger_index"])
    except Exception as e:  # noqa: BLE001
        _log(verbose, f"  [whales] xrpl échec (ledger validé) : {e}")
        return None
    out, scanned, errors = [], 0, 0
    for idx in range(top_idx, top_idx - n, -1):
        try:
            res = _rpc("ledger", {"ledger_index": idx, "transactions": True, "expand": True})
        except Exception as e:  # noqa: BLE001
            errors += 1
            if errors >= 5:
                _log(verbose, f"  [whales] xrpl : trop d'erreurs, arrêt ({e})")
                break
            continue
        ledger = res.get("ledger", {})
        close_time = ledger.get("close_time")
        # close_time XRPL = secondes depuis le 1er janvier 2000
        ts = datetime.fromtimestamp(int(close_time) + 946684800, tz=timezone.utc) if close_time else now
        for tx in ledger.get("transactions", []):
            if not isinstance(tx, dict) or tx.get("TransactionType") != "Payment":
                continue
            meta = tx.get("metaData") or tx.get("meta") or {}
            if meta.get("TransactionResult") != "tesSUCCESS":
                continue
            amt = meta.get("delivered_amount", tx.get("Amount"))
            if not isinstance(amt, str):
                continue  # IOU (RLUSD, USD…) : pas du XRP natif
            xrp = int(amt) / 1e6
            if xrp < min_xrp:
                continue
            fa, ta = tx.get("Account"), tx.get("Destination")
            f_owner, t_owner = exchanges.get(fa), exchanges.get(ta)
            out.append({
                "id": f"xrpl:{tx.get('hash')}",
                "ts": ts.isoformat(timespec="seconds"),
                "xrp": round(xrp),
                "usd": None,
                "from": f_owner or (fa[:6] + "…" + fa[-4:] if fa else "?"),
                "to": t_owner or (ta[:6] + "…" + ta[-4:] if ta else "?"),
                "kind": classify("exchange" if f_owner else "unknown", "exchange" if t_owner else "unknown"),
                "src": "xrpl",
            })
        scanned += 1
    _log(verbose, f"  [whales] xrpl ok : {scanned} ledgers lus, {len(out)} paiement(s) ≥ {int(min_xrp):,} XRP".replace(",", " "))
    return out


# --------------------------------------------------------------------------- #
# Assemblage + journal glissant
# --------------------------------------------------------------------------- #
def collect_whales(now, price, levels, exchanges, previous_log=None, verbose=False, mock=False):
    min_usd = float(levels.get("whale_min_usd", 500000))
    min_xrp = float(levels.get("whale_min_xrp_ledger", 5000000))

    if mock:
        new = _mock(now)
        sources = {"whale_alert": "mock", "xrpl": "mock"}
    else:
        wa = whale_alert(now, min_usd, verbose)
        xl = xrpl_scan(now, min_xrp, exchanges, verbose)
        new = (wa or []) + (xl or [])
        sources = {"whale_alert": "ok" if wa is not None else None, "xrpl": "ok" if xl is not None else None}

    # Fusion avec le journal précédent, dédoublonnage par id, fenêtre 7 jours
    log = {e["id"]: e for e in (previous_log or []) if e.get("id")}
    for e in new:
        if e["id"] not in log:
            e["price"] = price
            log[e["id"]] = e
        else:
            # Whale Alert peut compléter une entrée XRPL du même transfert (même hash)
            old = log[e["id"]]
            if e.get("usd") and not old.get("usd"):
                old["usd"] = e["usd"]
            if old.get("from", "").endswith("…") and e.get("from") and not e["from"].endswith("…"):
                old.update({"from": e["from"], "to": e["to"], "kind": e["kind"]})
    cutoff = now - timedelta(days=7)
    entries = [e for e in log.values() if datetime.fromisoformat(e["ts"]) >= cutoff]
    entries.sort(key=lambda e: e["ts"], reverse=True)

    # Flux net exchanges 24 h : dépôts − retraits (XRP). Positif = pression vendeuse potentielle.
    c24 = now - timedelta(hours=24)
    dep = sum(e["xrp"] for e in entries if e["kind"] == "depot" and datetime.fromisoformat(e["ts"]) >= c24)
    wd = sum(e["xrp"] for e in entries if e["kind"] == "retrait" and datetime.fromisoformat(e["ts"]) >= c24)
    net = dep - wd
    n24 = sum(1 for e in entries if datetime.fromisoformat(e["ts"]) >= c24)

    # Sous-score 0-100 pour le composant liquidité (uniquement si on a des données)
    score = None
    if entries or sources.get("whale_alert") == "ok":
        if net >= 0:
            score = _lin(net, 0, 15, 50e6, 100)
        else:
            score = _lin(-net, 0, 15, 50e6, 0)

    return {
        "log": entries[:60],
        "count_24h": n24,
        "deposits_24h_xrp": round(dep),
        "withdrawals_24h_xrp": round(wd),
        "net_exchange_flow_24h_xrp": round(net),
        "sources": sources,
        "score": None if score is None else round(score, 1),
        "thresholds": {"min_usd": min_usd, "min_xrp_ledger": min_xrp},
    }


def _lin(x, x0, y0, x1, y1):
    if x0 == x1:
        return y0
    t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)


def _mock(now):
    def e(h, xrp, usd, f, t, kind, src="whale-alert"):
        return {"id": f"mock:{h}", "ts": (now - timedelta(hours=h)).isoformat(timespec="seconds"),
                "xrp": xrp, "usd": usd, "from": f, "to": t, "kind": kind, "src": src}
    return [
        e(0.4, 25_000_000, 35_000_000, "Binance", "inconnu", "retrait"),
        e(0.7, 8_500_000, 11_900_000, "inconnu", "Upbit", "depot"),
        e(2.0, 40_000_000, 56_000_000, "inconnu", "inconnu", "transfert", "xrpl"),
        e(5.0, 12_000_000, 16_800_000, "Bitstamp", "Binance", "interne"),
        e(30.0, 60_000_000, 84_000_000, "inconnu", "Binance", "depot"),
        e(50.0, 18_000_000, 25_000_000, "Kraken", "inconnu", "retrait"),
    ]

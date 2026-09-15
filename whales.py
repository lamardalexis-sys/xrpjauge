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
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

UA = {"User-Agent": "Mozilla/5.0 (compatible; xrp-stress-gauge/4.0; +github pages)"}
XRPL_RPC = os.environ.get("XRPL_RPC", "https://xrplcluster.com/")
XRPL_LEDGERS = int(os.environ.get("XRPL_LEDGERS", "450"))   # ~30 min de ledgers par run
XRPL_WORKERS = int(os.environ.get("XRPL_WORKERS", "8"))     # requêtes en parallèle
XRPL_BUDGET_S = float(os.environ.get("XRPL_BUDGET_S", "90"))  # temps max consacré au scan


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
    t0 = time.time()
    indexes = list(range(top_idx, top_idx - n, -1))

    def fetch(idx):
        return _rpc("ledger", {"ledger_index": idx, "transactions": True, "expand": True}, timeout=20)

    results = []
    with ThreadPoolExecutor(max_workers=XRPL_WORKERS) as pool:
        futures = {pool.submit(fetch, idx): idx for idx in indexes}
        for fut in as_completed(futures):
            if time.time() - t0 > XRPL_BUDGET_S:
                _log(verbose, f"  [whales] xrpl : budget de {XRPL_BUDGET_S:.0f} s atteint, arrêt du scan")
                for f in futures:
                    f.cancel()
                break
            try:
                results.append(fut.result())
            except Exception:  # noqa: BLE001
                errors += 1
    for res in results:
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
    _log(verbose, f"  [whales] xrpl ok : {scanned} ledgers lus en {time.time() - t0:.0f} s ({errors} erreurs), "
                  f"{len(out)} paiement(s) ≥ {int(min_xrp):,} XRP".replace(",", " "))
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


# --------------------------------------------------------------------------- #
# 3. Positions : baleines déjà en place (soldes suivis dans le temps)
# --------------------------------------------------------------------------- #
XRPSCAN = "https://api.xrpscan.com/api/v1"


def _xrpscan_richlist(top_n, verbose=False):
    """Tente d'importer le classement des plus gros comptes via XRPScan.
    L'endpoint n'est pas documenté officiellement : on essaie deux formes et on
    accepte plusieurs formats de réponse. Renvoie [{address, label, balance}] ou None."""
    for url in (f"{XRPSCAN}/richlist?limit={top_n}", f"{XRPSCAN}/richlist"):
        try:
            d = _get(url, timeout=25)
        except Exception as e:  # noqa: BLE001
            _log(verbose, f"  [positions] richlist {url.split('/')[-1]} échec : {e}")
            continue
        rows = d if isinstance(d, list) else (d.get("accounts") or d.get("data") or d.get("richlist") or [])
        out = []
        for r in rows[: top_n * 2]:
            if not isinstance(r, dict):
                continue
            addr = r.get("account") or r.get("address") or r.get("Account")
            if not addr:
                continue
            bal = r.get("balance") or r.get("Balance") or r.get("xrp")
            try:
                bal = float(bal)
                if bal > 1e11:  # drops
                    bal /= 1e6
            except Exception:  # noqa: BLE001
                bal = None
            name = r.get("name") or (r.get("accountName") or {}).get("name") if isinstance(r.get("accountName"), dict) else r.get("accountName")
            out.append({"address": addr, "label": name or "", "balance": bal})
        if out:
            _log(verbose, f"  [positions] richlist ok via XRPScan : {len(out)} comptes")
            return out
    return None


def _xrpscan_label(addr):
    d = _get(f"{XRPSCAN}/account/{addr}", timeout=15)
    an = d.get("accountName") or {}
    if isinstance(an, dict):
        parts = [an.get("name"), an.get("desc")]
        return " · ".join(p for p in parts if p) or None
    return None


def _xrpl_balance(addr):
    res = _rpc("account_info", {"account": addr, "ledger_index": "validated"}, timeout=15)
    data = res.get("account_data") or {}
    return int(data["Balance"]) / 1e6 if "Balance" in data else None


def track_positions(now, cfg, previous, verbose=False, mock=False):
    """cfg = contenu de whales.json ; previous = details.whales.positions du run précédent."""
    prev = {p["address"]: p for p in ((previous or {}).get("accounts") or [])}
    excl = [x.lower() for x in cfg.get("exclude_labels_containing", [])]
    tracked = {t["address"]: {"address": t["address"], "label": t.get("label", "")} for t in cfg.get("track", []) if t.get("address")}

    auto_ok = None
    if mock:
        for i in range(1, 9):
            a = f"rMockWhale{i:02d}xxxxxxxxxxxxxxxxxxxx"
            tracked[a] = {"address": a, "label": f"Baleine #{i}"}
        auto_ok = True
    elif cfg.get("auto_richlist"):
        rl = _xrpscan_richlist(int(cfg.get("auto_top_n", 40)), verbose)
        auto_ok = rl is not None
        for r in rl or []:
            lab = (r.get("label") or "").lower()
            if any(x in lab for x in excl):
                continue
            tracked.setdefault(r["address"], {"address": r["address"], "label": r.get("label") or ""})
            if r.get("balance") and "balance_hint" not in tracked[r["address"]]:
                tracked[r["address"]]["balance_hint"] = r["balance"]

    if not tracked:
        _log(verbose, "  [positions] aucune baleine à suivre (whales.json vide et richlist indisponible)")
        return {"accounts": [], "auto_richlist": auto_ok, "tracked": 0, "updated_at": now.isoformat(timespec="seconds")}

    day = now.strftime("%Y-%m-%d")
    accounts = []

    def one(t):
        addr = t["address"]
        if mock:
            import random
            rnd = random.Random(addr + day)
            base = 50e6 + (hash(addr) % 400) * 1e6
            bal = base * (1 + rnd.uniform(-0.03, 0.05))
            label = t["label"]
        else:
            try:
                bal = _xrpl_balance(addr)
            except Exception as e:  # noqa: BLE001
                _log(verbose, f"  [positions] {addr[:8]}… solde échec : {e}")
                bal = None
            label = t.get("label") or prev.get(addr, {}).get("label") or ""
            if not label:
                try:
                    label = _xrpscan_label(addr) or ""
                except Exception:  # noqa: BLE001
                    label = ""
        return addr, label, bal

    with ThreadPoolExecutor(max_workers=6) as pool:
        for addr, label, bal in pool.map(one, list(tracked.values())):
            lab_l = (label or "").lower()
            if any(x in lab_l for x in excl):
                continue  # exchange détecté via l'étiquette : on ne le suit pas comme baleine
            hist = list(prev.get(addr, {}).get("history") or [])
            if bal is not None:
                hist = [h for h in hist if h.get("d") != day]
                hist.append({"d": day, "b": round(bal)})
                hist = hist[-180:]
            cur = bal if bal is not None else (hist[-1]["b"] if hist else None)

            def chg(days):
                cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
                older = [h for h in hist if h["d"] <= cutoff]
                if not older or cur is None:
                    return None
                return round(cur - older[-1]["b"])
            accounts.append({
                "address": addr, "label": label or "", "balance": None if cur is None else round(cur),
                "chg_1d": chg(1), "chg_7d": chg(7), "chg_30d": chg(30), "history": hist,
            })

    accounts.sort(key=lambda a: -(a["balance"] or 0))
    accounts = accounts[:60]
    tot = sum(a["balance"] or 0 for a in accounts)
    tot7 = sum(a["chg_7d"] or 0 for a in accounts if a["chg_7d"] is not None)
    tot30 = sum(a["chg_30d"] or 0 for a in accounts if a["chg_30d"] is not None)
    acc7 = sum(1 for a in accounts if (a["chg_7d"] or 0) > 0)
    dist7 = sum(1 for a in accounts if (a["chg_7d"] or 0) < 0)
    _log(verbose, f"  [positions] {len(accounts)} baleines suivies, total {tot/1e6:.0f} M XRP, 7 j {tot7/1e6:+.1f} M")
    return {
        "accounts": accounts, "tracked": len(accounts), "auto_richlist": auto_ok,
        "total_xrp": round(tot), "total_chg_7d": round(tot7), "total_chg_30d": round(tot30),
        "accumulating_7d": acc7, "distributing_7d": dist7,
        "updated_at": now.isoformat(timespec="seconds"),
    }

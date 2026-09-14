#!/usr/bin/env python3
"""
Composant « Géopolitique & news » du thermomètre XRP.

Trois couches, toutes optionnelles et indépendantes :
  1. GDELT (gratuit, sans clé) : tonalité et volume de la presse mondiale par thème,
     sur 24 h comparés aux 7 derniers jours.
  2. Google News RSS (gratuit, sans clé) : les derniers titres par thème + score
     lexical (mots à risque / mots apaisants, voir themes.json).
  3. Classification IA (optionnelle) : si LLM_API_KEY est défini, un modèle lit
     les titres du jour et renvoie un risque 0-100 par thème + une phrase de
     synthèse. Provider : LLM_PROVIDER=anthropic|openai, modèle : LLM_MODEL.
     Résultat mis en cache LLM_EVERY_HOURS heures (défaut 3) pour limiter le coût.

Le composant final = moyenne pondérée par thème de :
   0.35 tonalité GDELT + 0.15 volume GDELT + 0.50 lexique   (sans IA)
   0.20 tonalité GDELT + 0.10 volume GDELT + 0.25 lexique + 0.45 IA   (avec IA)

Ce composant est volontairement à poids modéré dans le score global : les news
sont bruyantes et souvent déjà pricées. Son vrai intérêt est le panneau
« Contexte monde » de la page, qui montre ce qui se passe à côté du score.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

UA = {"User-Agent": "Mozilla/5.0 (compatible; xrp-stress-gauge/3.0; +github pages)"}


def _log(verbose, *a):
    if verbose:
        print(*a, file=sys.stderr)


def _get(url, timeout=25, as_json=True):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="replace")
    return json.loads(raw) if as_json else raw


def _lin(x, x0, y0, x1, y1):
    if x0 == x1:
        return y0
    t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- #
# 1. GDELT
# --------------------------------------------------------------------------- #
GDELT_PAUSE_S = float(os.environ.get("GDELT_PAUSE_S", "6"))      # GDELT demande ≥ 5 s entre requêtes
GDELT_RETRIES = int(os.environ.get("GDELT_RETRIES", "2"))         # réessais sur 429 (IP partagée GitHub)
GDELT_VOLUME = os.environ.get("GDELT_VOLUME", "0") == "1"         # 2e requête (volume) par thème : off par défaut
_gdelt_dead = False  # si GDELT refuse tout, on arrête d'insister pour ce run


def gdelt_timeline(query, mode):
    """mode = 'timelinetone' ou 'timelinevol'. Renvoie [(datetime, valeur), ...] sur 7 j.
    Réessaie sur HTTP 429 avec une attente croissante (15 s, 30 s)."""
    global _gdelt_dead
    if _gdelt_dead:
        raise RuntimeError("GDELT indisponible pour ce run (429 répétés)")
    q = urllib.parse.quote(query, safe="")
    url = f"https://api.gdeltproject.org/api/v2/doc/doc?query={q}&mode={mode}&timespan=7d&format=json"
    raw = None
    for attempt in range(GDELT_RETRIES + 1):
        try:
            raw = _get(url, timeout=40, as_json=False)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < GDELT_RETRIES:
                time.sleep(15 * (attempt + 1))
                continue
            if e.code == 429:
                _gdelt_dead = True
            raise
    try:
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        # GDELT renvoie du texte brut en cas de requête refusée
        raise RuntimeError(f"réponse non-JSON : {raw.strip()[:140]!r}")
    series = d.get("timeline", [])
    if not series:
        return []
    pts = []
    for p in series[0].get("data", []):
        try:
            ts = datetime.strptime(p["date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            pts.append((ts, float(p["value"])))
        except Exception:  # noqa: BLE001
            continue
    return pts


def gdelt_theme(query, now, verbose=False):
    """Tonalité 24 h vs 7 j, volume 24 h vs 7 j."""
    out = {}
    tone, vol = [], []
    try:
        tone = gdelt_timeline(query, "timelinetone")
        _log(verbose, "    gdelt tone ok")
    except Exception as e:  # noqa: BLE001
        _log(verbose, f"    gdelt tone échec : {e}")
    if not _gdelt_dead:
        time.sleep(GDELT_PAUSE_S)
    if GDELT_VOLUME and not _gdelt_dead:
        try:
            vol = gdelt_timeline(query, "timelinevol")
            _log(verbose, "    gdelt vol ok")
        except Exception as e:  # noqa: BLE001
            _log(verbose, f"    gdelt vol échec : {e}")
        time.sleep(GDELT_PAUSE_S)
    if not tone and not vol:
        return None
    cutoff = now - timedelta(hours=24)
    if tone:
        t24 = [v for ts, v in tone if ts >= cutoff]
        t7 = [v for _, v in tone]
        if t24 and t7:
            out["tone_24h"] = round(sum(t24) / len(t24), 2)
            out["tone_7d"] = round(sum(t7) / len(t7), 2)
    if vol:
        v24 = [v for ts, v in vol if ts >= cutoff]
        v7 = [v for _, v in vol]
        if v24 and v7 and sum(v7) > 0:
            out["vol_ratio"] = round((sum(v24) / len(v24)) / (sum(v7) / len(v7)), 2)
    return out or None


def gdelt_score(g):
    """Tonalité GDELT : ~0 neutre, -3 franchement négatif, -6 très négatif.
    Score = niveau absolu + dégradation vs 7 j. Volume = pic d'attention."""
    if not g:
        return None, None
    tone_s = None
    if "tone_24h" in g:
        abs_s = _lin(g["tone_24h"], -1.0, 0, -6.0, 100)
        delta = g["tone_24h"] - g.get("tone_7d", g["tone_24h"])
        rel_s = _lin(delta, 0.0, 0, -2.5, 100)
        tone_s = 0.6 * abs_s + 0.4 * rel_s
    vol_s = None
    if "vol_ratio" in g:
        vol_s = _lin(g["vol_ratio"], 1.1, 0, 2.5, 100)
    return tone_s, vol_s


# --------------------------------------------------------------------------- #
# 2. Google News RSS + lexique
# --------------------------------------------------------------------------- #
def rss_headlines(query, now, max_items=8, max_age_h=30):
    q = urllib.parse.quote(query, safe="")
    url = f"https://news.google.com/rss/search?q={q}+when:1d&hl=en-US&gl=US&ceid=US:en"
    raw = _get(url, as_json=False)
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        src_el = it.find("source")
        src = (src_el.text or "").strip() if src_el is not None else ""
        age_h = None
        if pub:
            try:
                age_h = (now - parsedate_to_datetime(pub).astimezone(timezone.utc)).total_seconds() / 3600
            except Exception:  # noqa: BLE001
                age_h = None
        if age_h is not None and age_h > max_age_h:
            continue
        # Google News met " - Source" en fin de titre
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3].strip()
        if title:
            items.append({"t": title, "src": src, "url": link, "age_h": None if age_h is None else round(age_h, 1)})
    items.sort(key=lambda x: (x["age_h"] is None, x["age_h"] or 0))
    return items[:max_items]


def lexicon_raw(headlines, risk_lex, calm_lex):
    """Densité brute de mots à risque par titre (≈0 calme, ≈2 tendu, ≈4 très tendu)."""
    if not headlines:
        return None
    total = 0.0
    for h in headlines:
        t = h["t"].lower()
        s = 0.0
        for w, wt in risk_lex.items():
            if w in t:
                s += wt
        for w, wt in calm_lex.items():
            if w in t:
                s += wt  # négatif
        total += max(0.0, s)
    return total / len(headlines)


def lexicon_score(raw, history):
    """Score 0-100 à partir de la densité brute ET de son écart à la moyenne glissante
    du thème (≈ 7 jours d'exécutions horaires). Un thème de guerre contient toujours des
    mots de guerre : c'est la déviation qui est le signal, pas le niveau."""
    if raw is None:
        return None
    abs_s = _lin(raw, 0.5, 0, 5.0, 100)          # niveau absolu, courbe douce
    hist = [x for x in (history or []) if x is not None]
    if len(hist) >= 12:
        base = sum(hist) / len(hist)
        dev = (raw - base) / base if base > 0.2 else (raw - base)
        dev_s = _lin(dev, 0.0, 0, 1.0, 100)      # +100 % vs habitude = 100
        return 0.4 * abs_s + 0.6 * dev_s
    return 0.7 * abs_s                            # sans historique : niveau seul, plafonné à 70


# --------------------------------------------------------------------------- #
# 3. Classification IA (optionnelle)
# --------------------------------------------------------------------------- #
PROMPT = """Tu es un analyste risque pour un investisseur crypto (XRP) prudent.
Voici les titres de presse des dernières 24 h, groupés par thème. Pour chaque thème, note de 0 à 100
le risque que ces nouvelles déclenchent ou aggravent une baisse brutale des actifs risqués dans les
7 prochains jours (0 = rien de notable, 30 = tension ordinaire, 60 = risque sérieux et concret,
85+ = choc en cours). Ne surestime pas : le bruit politique habituel vaut 20-35. Puis donne une note
globale et UNE phrase de synthèse en français (max 30 mots), factuelle, sans conseil.

Réponds UNIQUEMENT avec un JSON de la forme :
{"themes": {"<key>": <int>, ...}, "global": <int>, "summary": "<phrase>"}

Titres :
"""


def _extract_json(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else None


def llm_classify(theme_headlines, verbose=False):
    key = (os.environ.get("LLM_API_KEY") or "").strip()
    if not key:
        _log(verbose, "  [news] llm : pas de clé (secret LLM_API_KEY absent ou vide) → lexique seul")
        return None
    _log(verbose, f"  [news] llm : appel {os.environ.get('LLM_PROVIDER', 'anthropic')} …")
    provider = os.environ.get("LLM_PROVIDER", "anthropic").lower()
    model = os.environ.get("LLM_MODEL") or ("claude-haiku-4-5" if provider == "anthropic" else "gpt-4o-mini")

    body_txt = PROMPT
    for key_, (label, hs) in theme_headlines.items():
        body_txt += f"\n## {key_} — {label}\n"
        for h in hs[:8]:
            body_txt += f"- {h['t']}" + (f" ({h['src']})" if h.get("src") else "") + "\n"

    try:
        if provider == "anthropic":
            payload = {"model": model, "max_tokens": 600,
                       "messages": [{"role": "user", "content": body_txt}]}
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages", data=json.dumps(payload).encode("utf-8"),
                headers={"content-type": "application/json", "x-api-key": key,
                         "anthropic-version": "2023-06-01", **UA}, method="POST")
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read().decode("utf-8"))
            txt = "".join(b.get("text", "") for b in d.get("content", []))
        else:
            payload = {"model": model, "temperature": 0.2,
                       "response_format": {"type": "json_object"},
                       "messages": [{"role": "user", "content": body_txt}]}
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions", data=json.dumps(payload).encode("utf-8"),
                headers={"content-type": "application/json", "authorization": f"Bearer {key}", **UA}, method="POST")
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read().decode("utf-8"))
            txt = d["choices"][0]["message"]["content"]
        parsed = _extract_json(txt)
        if not parsed or "global" not in parsed:
            _log(verbose, f"    llm : réponse non exploitable : {txt[:200]}")
            return None
        _log(verbose, f"  [news] llm : ok ({model}) risque global {parsed['global']}")
        return {"provider": provider, "model": model,
                "global": int(_clamp(float(parsed["global"]))),
                "themes": {k: int(_clamp(float(v))) for k, v in (parsed.get("themes") or {}).items()},
                "summary": str(parsed.get("summary", "")).strip()[:240],
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001
            body = ""
        _log(verbose, f"  [news] llm échec : HTTP {e.code} {body}")
        return None
    except Exception as e:  # noqa: BLE001
        _log(verbose, f"  [news] llm échec : {e}")
        return None


# --------------------------------------------------------------------------- #
# Assemblage
# --------------------------------------------------------------------------- #
def collect_news(themes_cfg, now, previous=None, verbose=False, mock=False):
    themes = themes_cfg.get("themes", [])
    risk_lex = themes_cfg.get("risk_lexicon", {})
    calm_lex = themes_cfg.get("calm_lexicon", {})
    results = []
    theme_headlines = {}
    prev_news = (previous or {}).get("news", {}) or {}
    lex_history = dict(prev_news.get("lex_history") or {})

    for th in themes:
        _log(verbose, f"  [news] thème {th['key']}")
        if mock:
            g, hs = _mock_theme(th["key"])
        else:
            g = gdelt_theme(th["gdelt"], now, verbose)
            try:
                hs = rss_headlines(th["rss"], now)
            except Exception as e:  # noqa: BLE001
                _log(verbose, f"    rss échec : {e}")
                hs = []
        tone_s, vol_s = gdelt_score(g)
        raw = lexicon_raw(hs, risk_lex, calm_lex)
        lex_s = lexicon_score(raw, lex_history.get(th["key"]))
        if raw is not None:
            lex_history[th["key"]] = (lex_history.get(th["key"]) or [])[-167:] + [round(raw, 3)]
        results.append({"key": th["key"], "label": th["label"], "weight": float(th.get("weight", 1)),
                        "gdelt": g, "tone_s": tone_s, "vol_s": vol_s, "lex_s": lex_s, "lex_raw": raw,
                        "headlines": hs})
        theme_headlines[th["key"]] = (th["label"], hs)

    # IA : cache si le précédent résultat est récent
    llm = None
    every_h = float(os.environ.get("LLM_EVERY_HOURS", "3"))
    prev_llm = prev_news.get("llm")
    if prev_llm and prev_llm.get("at"):
        try:
            age_h = (now - datetime.fromisoformat(prev_llm["at"])).total_seconds() / 3600
            if age_h < every_h:
                llm = dict(prev_llm, cached=True)
                _log(verbose, f"  [news] llm : cache ({age_h:.1f} h)")
        except Exception:  # noqa: BLE001
            llm = None
    if llm is None and any(hs for _, hs in theme_headlines.values()):
        if mock:
            llm = {"provider": "mock", "model": "mock", "global": 38, "at": now.isoformat(timespec="seconds"),
                   "themes": {t["key"]: 30 + (i * 7) % 40 for i, t in enumerate(themes)},
                   "summary": "Tensions commerciales US-Chine et vote crypto au Sénat dominent ; pas de choc en cours."}
        else:
            llm = llm_classify(theme_headlines, verbose)

    # Score par thème puis pondération
    out_themes = []
    acc, wacc = 0.0, 0.0
    for r in results:
        parts = []
        if llm and r["key"] in (llm.get("themes") or {}):
            parts += [(r["tone_s"], 0.20), (r["vol_s"], 0.10), (r["lex_s"], 0.25), (float(llm["themes"][r["key"]]), 0.45)]
        else:
            parts += [(r["tone_s"], 0.35), (r["vol_s"], 0.15), (r["lex_s"], 0.50)]
        parts = [(s, w) for s, w in parts if s is not None]
        score = sum(s * w for s, w in parts) / sum(w for _, w in parts) if parts else None
        if score is not None:
            acc += score * r["weight"]
            wacc += r["weight"]
        g = r["gdelt"] or {}
        out_themes.append({
            "key": r["key"], "label": r["label"], "score": None if score is None else round(score, 1),
            "tone_24h": g.get("tone_24h"), "tone_7d": g.get("tone_7d"), "vol_ratio": g.get("vol_ratio"),
            "lex": None if r["lex_s"] is None else round(r["lex_s"], 1),
            "ai": (llm or {}).get("themes", {}).get(r["key"]),
            "headlines": r["headlines"][:6],
        })

    component = round(acc / wacc, 1) if wacc else None
    return component, {"llm": llm, "themes": out_themes, "lex_history": lex_history}


def _mock_theme(key):
    import random
    rnd = random.Random(key)
    tone = -2.0 - rnd.random() * 2
    g = {"tone_24h": round(tone, 2), "tone_7d": round(tone + 0.6, 2), "vol_ratio": round(0.9 + rnd.random() * 0.8, 2)}
    samples = {
        "trump": ["Trump threatens new tariffs on EU autos", "White House signals support for crypto market structure bill"],
        "russia": ["Kremlin warns NATO over missile deployments", "Ukraine talks stall as strikes continue"],
        "china": ["China conducts drills near Taiwan", "Beijing responds to US chip export curbs"],
        "mideast": ["Oil steadies as Iran talks resume", "Israel strike reported in southern Lebanon"],
        "fed": ["Traders split on Fed decision as inflation data looms", "Powell faces pressure ahead of FOMC"],
        "crypto": ["Senate sets cloture vote on CLARITY Act", "XRP ETF inflows slow ahead of Fed"],
        "ai": ["Nvidia shares dip on AI spending worries", "OpenAI announces new enterprise model"],
        "markets": ["Stocks slip as bond yields climb", "Economists see recession odds rising"],
    }
    hs = [{"t": t, "src": "Mock News", "url": "https://example.com", "age_h": round(2 + i * 5.0, 1)}
          for i, t in enumerate(samples.get(key, ["Headline"]))]
    return g, hs

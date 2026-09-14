# Thermomètre de stress XRP (V2)

Une jauge 0-100 qui monte quand les conditions d'une capitulation du marché se
mettent en place. Mise à jour toutes les heures par GitHub Actions, affichée sur
une page GitHub Pages consultable depuis le téléphone. Zéro clé API, zéro coût.

> Ce n'est **pas** un prédicteur de black swan. Un black swan est, par définition,
> l'événement que les signaux ne voient pas venir. Le thermomètre mesure des
> *conditions* : tendance cassée, levier excessif, volatilité, peur ou
> complaisance extrêmes, retrait de liquidité, événement binaire imminent. Il
> sert à exécuter un plan fixé à l'avance, pas à le renégocier à chaud.
> Plus de sources = moins de bruit, pas plus de prescience.

## Ce que mesure le score

| Composant | Poids | Sources (gratuites, joignables depuis GitHub) | Ce qui fait monter le score |
|---|---|---|---|
| Tendance | 20 | Kraken → CoinGecko → Binance | XRP et BTC sous leurs SMA 50 / 200 j ; XRP qui sous-performe BTC sur 30 j |
| Momentum & vol. réalisée | 15 | idem | Chute 7 j / 30 j ; hausse parabolique (+15 %/7 j) ; vol 7 j ≫ vol 30 j ; volume anormal sur journée baissière |
| Levier | 20 | OKX (→ Binance) | Funding ≥ 0,05 %/8 h ou ≤ -0,02 % (dernier + moyenne 7 j) ; OI qui grimpe contre le prix ; ratio long/short extrême ; cascade de liquidations de longs |
| Vol. implicite & macro | 10 | Deribit (DVOL BTC), Yahoo → stooq (VIX) | DVOL > 45 et/ou en saut vs moyenne 30 j ; VIX > 15 et/ou en saut |
| Liquidité | 10 | DefiLlama, CoinGecko, `manual.json` | Stablecoins en contraction sur 7 j ; dominance BTC > 55 % ; sorties nettes des ETF XRP (saisie manuelle) |
| Sentiment | 10 | alternative.me | Fear & Greed ≤ 20 ; ≥ 80 compte aussi (complaisance) ; chute ≥ 20 pts en 7 j |
| Événements | 15 | `events.json` (à la main) | Décision binaire (Fed, Sénat…) à moins de 14 jours ; 100 % de l'impact à J-1 |

Zones : 0-30 **Calme** · 30-55 **Vigilance** · 55-75 **Stress élevé** · 75-100 **Capitulation probable**.

La page affiche aussi la **variation du score sur 24 h** : c'est souvent plus
parlant que le niveau absolu (un passage de 25 à 45 en une nuit est un signal,
un 45 stable depuis dix jours ne l'est pas).

Si une source est en panne, le composant est ignoré et le score est re-normalisé
sur les composants disponibles (la page l'indique). Binance et Bybit bloquent
les serveurs GitHub (IP américaines) : ils ne servent que de fallback.

## Installation (10 minutes)

1. Crée un dépôt GitHub **public** (nécessaire pour GitHub Pages gratuit).
2. Copie tout le contenu de ce dossier dedans (y compris le dossier caché
   `.github/`), puis pousse.
3. **Settings → Pages → Build and deployment** : Source = *Deploy from a branch*,
   Branch = `main`, dossier = `/docs`. Enregistre.
4. **Settings → Actions → General → Workflow permissions** : coche
   *Read and write permissions*. Enregistre.
5. Onglet **Actions** → workflow « Mise à jour du thermomètre XRP » →
   **Run workflow** pour lancer la première collecte.
6. Deux minutes plus tard, la page est sur
   `https://<ton-pseudo>.github.io/<nom-du-repo>/`. Ajoute-la à l'écran
   d'accueil de ton téléphone.

Ensuite ça tourne seul, toutes les heures. GitHub désactive les crons d'un
dépôt sans activité depuis 60 jours : un simple commit le réveille.

## Mettre à jour depuis ton PC

Le bot committe `docs/data.json` toutes les heures, donc **toujours** :

```bash
git pull --rebase origin main
# … tes modifications …
git add -A && git commit -m "…" && git push
```

## À entretenir

- **`events.json`** : la seule chose à tenir à jour. Ajoute les prochaines
  décisions Fed (federalreserve.gov), votes au Sénat, audiences, déblocages
  escrow. `impact` : 1 mineur, 2 notable, 3 majeur.
- **`manual.json`** (optionnel) : flux net hebdomadaire des ETF XRP en M$ quand
  tu vois le chiffre passer. `null` = ignoré.
- **Zone d'achat** : `.github/workflows/update.yml`, variables
  `BUY_ZONE_HIGH` / `BUY_ZONE_LOW`.
- **Pondérations et seuils** : en tête de `collect.py` (`WEIGHTS`) et dans
  chaque fonction `*_component`. Tout est commenté.

## Tester en local

```bash
python collect.py --mock -v          # données fictives, hors ligne
python collect.py -v                 # collecte réelle
python -m http.server -d docs 8000   # puis http://localhost:8000
```

## Limites assumées

- Pas de flux on-chain (réserves des exchanges, MVRV, baleines) ni de flux
  ETF automatiques : les seules sources fiables sont payantes. Funding, OI,
  liquidations et stablecoins en sont des proxys corrects.
- Pas de X ni de Telegram : l'API X coûte 100 $+/mois, et lire des groupes
  Telegram demande de connecter un compte personnel. Un bot Telegram qui
  *envoie* le score quand il franchit un seuil se rajoute en 20 lignes.
- Le score est heuristique, pas statistique : il n'a pas été backtesté. Les
  seuils viennent de l'expérience des cycles 2022-2026, pas d'une optimisation.
  L'historique que le bot accumule (score et composants, jour par jour) permettra
  de le vérifier a posteriori.

# Thermomètre de stress XRP

Une jauge 0-100 qui monte quand les conditions d'une capitulation du marché se
mettent en place. Mise à jour toutes les heures par GitHub Actions, affichée sur
une page GitHub Pages consultable depuis le téléphone. Zéro clé API, zéro coût.

> Ce n'est **pas** un prédicteur de black swan. Un black swan est, par définition,
> l'événement que les signaux ne voient pas venir. Le thermomètre mesure des
> *conditions* : tendance cassée, levier excessif, peur ou complaisance extrêmes,
> événement binaire imminent. Il sert à exécuter un plan fixé à l'avance, pas à
> le renégocier à chaud.

## Ce que mesure le score

| Composant | Poids | Source (gratuite) | Ce qui fait monter le score |
|---|---|---|---|
| Tendance | 25 | Binance spot → Kraken → CoinGecko | XRP et BTC sous leurs SMA 50 / 200 jours |
| Momentum | 15 | idem | Chute > 10 % sur 7 j, ou hausse parabolique (+15 %/7 j, fragile) |
| Levier | 25 | Binance Futures → Bybit → OKX | Funding ≥ 0,05 %/8 h (longs surchargés) ou ≤ -0,02 % (shorts, capitulation), open interest qui grimpe contre le prix |
| Sentiment | 15 | alternative.me | Fear & Greed ≤ 20 (peur extrême) ; ≥ 80 compte aussi (complaisance) |
| Événements | 20 | `events.json` (à la main) | Décision binaire (Fed, Sénat…) à moins de 14 jours ; 100 % de l'impact à J-1 |

Zones : 0-30 **Calme** · 30-55 **Vigilance** · 55-75 **Stress élevé** · 75-100 **Capitulation probable**.

Si une source est en panne, le composant est ignoré et le score est re-normalisé
sur les composants disponibles (la page l'indique).

## Installation (10 minutes)

1. Crée un dépôt GitHub **public** (nécessaire pour GitHub Pages gratuit), par
   exemple `xrp-stress-gauge`.
2. Copie tout le contenu de ce dossier dedans (y compris le dossier caché
   `.github/`), puis pousse.
3. Dans le dépôt : **Settings → Pages → Build and deployment** :
   Source = *Deploy from a branch*, Branch = `main`, dossier = `/docs`. Enregistre.
4. **Settings → Actions → General → Workflow permissions** : coche
   *Read and write permissions*. Enregistre.
5. Onglet **Actions** → workflow « Mise à jour du thermomètre XRP » →
   **Run workflow** pour lancer la première collecte tout de suite.
6. Deux minutes plus tard, la page est disponible sur
   `https://<ton-pseudo>.github.io/xrp-stress-gauge/`. Ajoute-la à l'écran
   d'accueil de ton téléphone.

Ensuite ça tourne seul, toutes les heures. GitHub désactive les crons d'un
dépôt sans activité depuis 60 jours : un simple commit (par exemple une mise à
jour d'`events.json`) le réveille.

## À entretenir

- **`events.json`** : la seule chose à tenir à jour. Ajoute les prochaines
  décisions Fed (dates sur federalreserve.gov), votes au Sénat, audiences,
  déblocages escrow. `impact` : 1 mineur, 2 notable, 3 majeur.
- **Zone d'achat** : dans `.github/workflows/update.yml`, variables
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

- Pas de flux ETF ni de données de liquidations : les seules sources fiables
  sont payantes. L'open interest et le funding en sont des proxys corrects.
- Pas de X ni de Telegram : l'API X coûte 100 $+/mois, et lire des groupes
  Telegram demande de connecter un compte personnel. Si un jour tu veux des
  alertes, un bot Telegram qui *envoie* le score quand il franchit un seuil se
  rajoute en 20 lignes dans `collect.py`.
- Le score est heuristique, pas statistique : il n'a pas été backtesté. Il
  aurait été rouge pendant le crash de février 2026 et orange début août
  avant le creux à 1 $, mais c'est une lecture a posteriori.

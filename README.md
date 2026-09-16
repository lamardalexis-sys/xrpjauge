# Thermomètre de stress XRP (V4)

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
| Tendance | 18 | Kraken → CoinGecko → Binance | XRP et BTC sous leurs SMA 50 / 200 j ; XRP qui sous-performe BTC sur 30 j |
| Momentum & vol. réalisée | 14 | idem | Chute 7 j / 30 j ; hausse parabolique (+15 %/7 j) ; vol 7 j ≫ vol 30 j ; volume anormal sur journée baissière |
| Levier | 18 | OKX (→ Binance) | Funding ≥ 0,05 %/8 h ou ≤ -0,02 % (dernier + moyenne 7 j) ; OI qui grimpe contre le prix ; ratio long/short extrême ; cascade de liquidations de longs |
| Vol. implicite & macro | 10 | Deribit (DVOL BTC), Yahoo → stooq (VIX) | DVOL > 45 et/ou en saut vs moyenne 30 j ; VIX > 15 et/ou en saut |
| Liquidité | 10 | DefiLlama, CoinGecko, `manual.json` | Stablecoins en contraction sur 7 j ; dominance BTC > 55 % ; sorties nettes des ETF XRP (saisie manuelle) |
| Sentiment | 8 | alternative.me | Fear & Greed ≤ 20 ; ≥ 80 compte aussi (complaisance) ; chute ≥ 20 pts en 7 j |
| Événements | 12 | `events.json` (à la main) | Décision binaire (Fed, Sénat…) à moins de 14 jours ; 100 % de l'impact à J-1 |
| Géopolitique & news | 10 | GDELT, Google News RSS, `themes.json`, IA optionnelle | Presse mondiale qui vire au négatif et/ou explose en volume sur un thème ; titres chargés en mots à risque ; note de risque IA |

### Le composant « Géopolitique & news » (V3)

Huit thèmes suivis par défaut (`themes.json`) : Trump / Maison-Blanche, Russie /
Ukraine / OTAN, Chine / Taïwan, Moyen-Orient / pétrole, Fed / macro US, Crypto /
régulation, IA / tech, Marchés / récession. Pour chacun :

- **GDELT** (projet universitaire, gratuit, sans clé) mesure la tonalité moyenne
  et le volume de la presse mondiale sur 24 h, comparés aux 7 derniers jours.
  C'est de la vraie télémétrie médiatique : « ce sujet explose et vire au
  négatif ».
- **Google News RSS** fournit les derniers titres, affichés sur la page dans le
  panneau « Contexte monde », et notés par un lexique de mots à risque
  (sanctions, tarifs, guerre, hack, défaut…) et de mots apaisants (cessez-le-feu,
  accord, approbation…).
- **IA (optionnelle)** : si le secret `LLM_API_KEY` existe, un modèle lit les
  titres du jour et note le risque 0-100 par thème, plus une phrase de synthèse
  en français affichée en tête du panneau. Appel au plus toutes les 3 h
  (cache) : quelques centimes par jour avec un petit modèle.

Ce composant ne pèse que 10 points, volontairement : les news sont bruyantes
et souvent déjà pricées (le VIX et le DVOL les captent déjà en partie). Son
vrai intérêt est le panneau, qui montre *ce qui se passe* à côté du score.

**Activer l'IA** : Settings → Secrets and variables → Actions → *New repository
secret* → nom `LLM_API_KEY`, valeur = ta clé. Provider et modèle se règlent
dans `.github/workflows/update.yml` (`LLM_PROVIDER` = `anthropic` ou `openai`,
`LLM_MODEL` vide = défaut). Sans secret, tout fonctionne avec le lexique seul.

Ce que ce composant **ne fait pas** : lire les posts de Trump ou de qui que ce
soit directement (pas d'API), ni distinguer la posture du fait sans l'IA. Il
capte la couverture médiatique, avec quelques heures de décalage.

**GDELT depuis GitHub Actions** : l'IP partagée des runners est limitée par
GDELT (HTTP 429 permanent). Le code le gère (réessais, puis abandon propre) ;
la tonalité GDELT restera vide tant que le script tourne chez GitHub. Les
titres, le lexique et l'IA suffisent au composant.

### Graphique de prix et zones (V4)

Sous la jauge : graphique en direct (bibliothèque Lightweight Charts de TradingView + bougies Kraken chargées par ton navigateur, 5 min / 15 min / 1 h / 4 h / 1 j, zoom et déplacement, sous-graphiques RSI et MACD synchronisés), SMA 50 et 200, **zone d'achat** (vert),
**ligne d'invalidation** (clôture hebdo en dessous = thèse « fond touché »
fausse) et **zones de vente / allègement** (orange). Tout se règle dans
`levels.json`. Un onglet **TradingView** affiche le widget complet (indicateurs,
outils de tracé) sans nos zones. Si Kraken est injoignable depuis le navigateur,
le graphique retombe sur les bougies journalières collectées par le bot.

### Signal de confluence « 3 indices » (V4)

Sous le graphique, un bandeau donne l'état de trois indicateurs calculés sur
l'unité de temps affichée : **tendance** (prix vs EMA 20/50), **momentum**
(RSI 14 : > 55 haussier, < 45 baissier) et **force** (histogramme MACD 12/26/9,
signe et pente). Un triangle **bleu** sous la bougie apparaît quand les trois
basculent haussiers ensemble, un triangle **rouge** au-dessus quand les trois
basculent baissiers ; il faut repasser par le neutre pour qu'un nouveau signal
soit émis. Un **petit point** signale un 2/3 (deux indices d'accord, le
troisième neutre) : plus précoce, moins fiable. Le survol d'une bougie affiche
O/H/L/C et le détail des marqueurs présents.

**Filtre anti-bruit** (bouton « Filtre », actif par défaut en 5 min, 15 min et
1 h) : un 3/3 n'est marqué que s'il va dans le sens de la tendance supérieure
(1 h pour le 5 min, 4 h pour le 15 min, 1 j pour le 1 h), s'il tient deux
bougies consécutives en intraday, si la bougie a un volume ≥ 1,2× la moyenne 20
(5 min) et s'il est espacé du précédent. Les 3/3 écartés apparaissent en points
gris (survol = raison). Le bandeau indique la tendance supérieure et le nombre
de signaux écartés. C'est un filtre de confluence : peu de signaux, peu de faux
positifs, mais un retard inhérent. Une confirmation, jamais une prédiction.

### Baleines (V4)

Les gros transferts XRP sont placés sur le graphique (▲ vert = retrait d'un
exchange, ▼ rouge = dépôt vers un exchange) et listés dans le panneau
« Baleines » avec le flux net 24 h.

Ce que ça veut dire, honnêtement : sur la blockchain on ne voit pas « une
baleine achète à 0,65 $ », on voit des transferts. Un **retrait** d'exchange
vers un portefeuille privé est la signature la plus fiable d'un achat (on
retire ce qu'on vient d'acheter) ; un **dépôt** vers un exchange précède
souvent une vente. Le prix affiché est celui du run qui a détecté le transfert
(précision : l'heure).

Deux sources :

- **Whale Alert** (recommandé) : compte gratuit sur whale-alert.io → clé API →
  secret GitHub `WHALE_ALERT_KEY`. Plan gratuit : transferts ≥ 500 k$, dernière
  heure (parfait pour un cron horaire), étiquettes d'exchanges fournies.
- **XRP Ledger direct** (sans clé, toujours actif) : le script lit les 450
  derniers ledgers validés (~30 minutes, en parallèle, 90 s max) via l'API publique et garde les
  paiements ≥ 1 M XRP. Couverture partielle et étiquettes limitées à
  `exchanges.json` (six adresses vérifiables ; ajoute-en depuis xrpscan.com).

Le flux net exchanges sur 24 h entre dans le composant Liquidité (30 % de ce
composant) : dépôts nets massifs = stress, retraits nets = apaisement.

### Baleines positionnées (V4)

Le panneau « Baleines positionnées » suit le **solde** d'une liste de gros
portefeuilles, relevé à chaque run sur le XRP Ledger, et affiche la variation
sur 24 h, 7 j et 30 j (vert = accumulation, rouge = distribution) avec une
mini-courbe par portefeuille et le total. C'est la réponse à « qui s'est déjà
positionné » : une baleine dont le solde monte semaine après semaine accumule.

La liste vit dans `whales.json` :

- `auto_richlist: true` : le script tente d'importer le classement des plus gros
  comptes via XRPScan à chaque run (endpoint non garanti : si l'import échoue, la
  page l'indique et seule ta liste est utilisée).
- `track` : tes adresses. Va sur https://xrpscan.com/richlist, ignore les
  exchanges (Binance, Upbit, Bitstamp…) et Ripple/escrow, copie les adresses des
  gros comptes privés dans `track` avec un `label` libre. Dix minutes, une fois.
- `exclude_labels_containing` : les étiquettes d'exchanges à écarter.

L'historique se construit au fil des runs : 24 h dès le lendemain, 7 j au bout
d'une semaine, 30 j au bout d'un mois. Un gros portefeuille non étiqueté peut
être un fonds, un custodian ou un escrow : le lien vers XRPScan permet de
vérifier.

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
- **`themes.json`** : les thèmes news (requêtes GDELT et Google News, poids) et
  les lexiques. Ajoute un thème si un sujet devient central (ex. une élection).
- **`levels.json`** : zone d'achat, invalidation, zones de vente, seuils
  baleines. C'est ici que vit ton plan.
- **`exchanges.json`** : adresses d'exchanges pour l'étiquetage XRP Ledger.
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

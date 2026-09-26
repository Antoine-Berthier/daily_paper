# Daily Paper — notes pour agents

Newsletter quotidienne personnelle en français, générée par des agents headless.
Lecteur : un développeur à Bruxelles. Tout le texte produit est en français.

## Architecture

```
serve (process unique, lancé à l'ouverture de session Windows)
 ├─ serveur HTTP local (server.py)        éditions, lectures, 👍/👎, commentaires, clics
 └─ planificateur (server.scheduler_loop) une tentative par jour de parution, rattrapage au démarrage
pipeline.run()
  gate      pas un jour de parution / édition récente non lue → rien
  feedback  feedback.process_pending() : bandit (notes/clics) + agent TALOS (commentaires)
  plan      planner.plan() : rubriques par crédit + graine de diversité par article
  write     writer.call_claude() : 1 `claude -p` par article, WebSearch/WebFetch, sortie JSON schema
  validate  validate.check() : liens vivants, pas de page d'accueil, longueur, anti-doublon → 1 réécriture
  render    render.py + templates/ : Markdown augmenté → HTML épuré, images og:image en local
  history   data/history.jsonl (sujet, entités, URLs, graine) ; state.json (crédits, sacs, bandit, lectures)
```

- `config/settings.yaml` : ton, nombre d'articles, longueur, horaire, modèles.
- `config/topics.yaml` : rubriques (probabilité, instructions, flux, graines, sous-types). Format documenté en tête de fichier.
- `config/pools/*.yaml` : listes de tirage (régions, genres, artisanats…).
- `data/` et `editions/` : état et sorties, ignorés par git.

## Principes à conserver

- **Le hasard vient de Python, jamais du LLM** (planner.py). Le modèle exécute un brief.
- **Tout est sourcé** : le rédacteur n'a que WebSearch/WebFetch, validate.py rejette liens morts et pages d'accueil.
- **Config = git** : toute modification de config/ passe par `customize.py` (outils partagés TALOS / CLI) et est commitée.
- L'état du planificateur n'est commité qu'après publication (une édition ratée ne consomme rien).
- HTML sobre, une colonne, mode sombre, pas de dépendances externes dans les pages.

## Commandes

```
uv run pytest -q                         # doit passer
uv run daily-paper plan [--topics a,b]   # aperçu du tirage, rien n'est écrit
uv run daily-paper plan --simulate 300   # fréquences des rubriques
uv run daily-paper run --force [--topics a,b]
uv run daily-paper cfg list | cfg <outil> '<json>' [--commit]
uv run daily-paper rerender              # après une modification des templates
```

Ajouter un outil de personnalisation = une fonction + une entrée `ToolSpec` dans `customize.TOOLS`
(elle devient disponible pour TALOS, la CLI et Claude).

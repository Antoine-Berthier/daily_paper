# Daily Paper

Une newsletter quotidienne en français, écrite par des agents à partir de sources web réelles.
Elle paraît chaque jour de semaine, sauf si une édition de moins de 5 jours n'a pas encore été lue.
Tu peux aussi en demander une à tout moment.

- **Rédaction** : un appel `claude -p` par article (Sonnet, WebSearch + WebFetch seulement, sortie JSON validée).
- **Diversité** : tout le hasard est tiré en Python (crédits, sacs mélangés, facettes, flux RSS, dico2rue,
  échantillonnage verbalisé, angles). Détails en tête de `src/daily_paper/planner.py` et de `config/topics.yaml`.
- **Lecture** : serveur local `http://localhost:8765/` avec notification Windows cliquable ; l'édition est marquée
  comme lue quand tu arrives en bas.
- **Feedback** : 👍/👎, commentaires par article ou sur l'édition entière. Les notes alimentent un bandit (poids des
  rubriques) ; les commentaires sont appliqués par un agent TALOS limité aux outils de `customize.py`. Il escalade vers
  Claude (sur une branche, avec merge si les tests passent) ce que ces outils ne couvrent pas.

## Installation

```bash
uv sync --all-extras                 # l'extra "talos" pointe vers ../TALOS
uv run daily-paper notify-test       # vérifier la notification Windows
uv run daily-paper install-windows --start   # serveur + parution auto à chaque ouverture de session
```

Sans WSL : lance `uv run daily-paper serve` dans un service systemd utilisateur (voir `deploy/`).

## Usage

```bash
uv run daily-paper run --force                     # édition maintenant
uv run daily-paper run --topics cuisine,musique    # rubriques imposées
uv run daily-paper plan                            # aperçu du tirage (rien n'est écrit)
uv run daily-paper plan --simulate 300             # fréquences réelles des rubriques
uv run daily-paper feedback [--apply]              # retours en attente / les appliquer
uv run daily-paper cfg list                        # outils de réglage
uv run daily-paper cfg set_probability '{"topic": "musique", "probability": 0.33}' --commit
uv run daily-paper status
```

## Rubriques

| id | rubrique | fréquence | diversité |
|---|---|---|---|
| actu | Actualités (Bruxelles, Belgique, monde si majeur) | toujours | titres RSS recoupés (importance = nombre de rédactions) |
| genai | IA générative | toujours, peut sauter | seulement si annonce majeure, sinon rien |
| blog_ia | Fabriquer avec l'IA | 1/3 | billet tiré dans des flux IA (< 30 j) × domaine (sac) × échantillonnage verbalisé |
| expression | L'expression du jour | 1/2 | lettre et page au hasard, entrée jamais publiée ; affichée telle quelle (mot + définition), sans LLM |
| cuisine | recette débutant / astuce | 1/3 | cuisine du monde (sac) × légume de saison (récence) ; technique (sac) |
| voyage | lieu / culture / aventure | 1/3 | 48 régions (sac) × échelle ou thème ; billet de blog d'aventure × discipline ; angle |
| musique | genre peu connu / mouvement | 1/5 | 100+ genres (sac) ; famille × décennie × échantillonnage verbalisé ; angle |
| style | tendances underground | 1/5 | 40 sous-cultures (sac) ; angle |
| making | making accessible | 1/5 | 40 techniques (sac) |

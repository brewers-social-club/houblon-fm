# Houblon FM — radio thématique produite par IA

Application pour collecter l'actualité brassicole, extraire des faits sourcés, écrire et
vérifier des émissions francophones, produire les voix, diffuser un flux continu et publier
un site transparent.

> L'application informe objectivement sur la bière. Elle n'incite pas à la consommation et
> affiche la mention sanitaire française. Une validation juridique reste nécessaire avant
> exploitation commerciale.

## Démarrage local

Prérequis : Python 3.12, `uv`, ffmpeg et Docker Compose.

```bash
cp .env.example .env
uv sync --extra dev
uv run radio init
uv run radio demo
uv run radio publish
docker compose up --build
```

- site : <http://localhost:8080>
- direct Icecast : <http://localhost:8000/live.mp3>
- API : <http://localhost:8081/health>
- administration : <http://localhost:8080/admin/> (Basic Auth nginx)

Le mode `mock` produit une démonstration intégrale sans clé API. Passe ensuite
`RADIO_AI_PROVIDER=anthropic` et `RADIO_TTS_PROVIDER=gemini` pour la production.

## Commandes

```bash
radio collect
radio facts
radio topics --hours 48
radio run --fmt journal
radio import-scripts emissions
radio voice-queue --max 3
radio say "Une India Pale Ale houblonnée" --character presenter
radio publish
radio withdraw 42
radio feedback
radio airings
radio stats
radio sidecar
```

## Architecture

```text
collecte -> faits -> sujets -> écriture -> vérification -> voix -> publication
                           SQLite WAL + MP3 + site statique
```

Le pod local reproduit les quatre rôles de production : Icecast, Liquidsoap, nginx et
sidecar Python. Les étapes de contenu restent des commandes déterministes et rejouables.

## Sécurité éditoriale

- Le texte brut ne sert qu'à extraire des faits et est purgé après 30 jours.
- Le script est généré uniquement depuis les faits reformulés.
- Chaque phrase référence ses faits et est vérifiée avant publication.
- Une phrase non soutenue est réécrite une fois, puis supprimée.
- Une émission perdant plus de 20 % de ses phrases est rejetée.
- Les voix sont des personnages fictifs et la mention IA ouvre chaque émission.
- Les collecteurs honorent `robots.txt`, un délai par domaine et l'âge des articles.

## Documentation

- [Architecture et exploitation](docs/ARCHITECTURE.md)
- [Modèle de données](docs/DATA_MODEL.md)
- [Déploiement](docs/DEPLOYMENT.md)
- [Politique éditoriale](docs/EDITORIAL.md)

## Tests

```bash
uv run pytest
uv run ruff check .
```

## Licence

AGPL-3.0-or-later. Les contenus, marques, voix et sources restent soumis à leurs droits.

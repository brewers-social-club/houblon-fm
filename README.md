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
- administration : <http://localhost:8080/admin/> (session applicative)

L'administration utilise un formulaire applicatif et une session `HttpOnly` valable 12 heures.
Configurez les identifiants dans `.env` avant toute exposition réseau :

```env
RADIO_ADMIN_LOGIN=admin
RADIO_ADMIN_PASSWORD=change-me
RADIO_SESSION_SECRET=change-this-with-32-random-bytes
```

Le mot de passe n'est jamais conservé dans le navigateur. Le secret de session signe le cookie
administrateur ; le changer ferme toutes les sessions existantes.

Le mode `mock` produit une démonstration intégrale sans clé API. Pour utiliser le compte
ChatGPT/Codex connecté sur la machine, lance une fois `codex login` (ou
`codex login --device-auth` sur un serveur sans navigateur), vérifie avec `codex login status`,
puis configure `RADIO_AI_PROVIDER=codex`. Aucune clé OpenAI ou Anthropic n'est alors nécessaire.
Le pipeline utilise `CODEX_MODEL_LIGHT` pour l'extraction, le regroupement et la vérification,
et `CODEX_MODEL_WRITE` pour l'écriture et la réécriture. Configuration recommandée :

```env
CODEX_MODEL_LIGHT=gpt-6-luna
CODEX_MODEL_WRITE=gpt-6.1-sol
```

L'ancien `CODEX_MODEL` reste accepté comme valeur commune de secours. Si les variables sont
vides, Codex choisit son modèle recommandé. Le provider Anthropic reste disponible avec
`uv sync --extra anthropic`.

Le provider Codex lance chaque génération dans un dossier temporaire vide, en lecture seule,
avec une session éphémère et une sortie JSON contrainte. Les commandes éditoriales utilisant
Codex doivent être exécutées sur l'hôte où le CLI est connecté ; l'image Docker du sidecar
n'embarque volontairement ni le CLI ni ses identifiants.

Pour une vraie voix, configurez `RADIO_TTS_PROVIDER=elevenlabs` avec
`ELEVENLABS_API_KEY`, ou `RADIO_TTS_PROVIDER=gemini` avec `GEMINI_API_KEY`. Le provider
ElevenLabs utilise par défaut Adam, `eleven_turbo_v2_5`, un style à `0.6` et produit du MP3
128 kb/s. Chaque personnage peut toutefois utiliser sa propre voix dans
`config/characters.yaml`, avec une entrée par fournisseur :

```yaml
presenter:
  name: Camille
  voice:
    gemini: Kore
    elevenlabs: O31r762Gb3WFygrEOGh0
```

Les identifiants ElevenLabs sont visibles dans la bibliothèque de voix du compte. La commande
`radio voices` affiche le casting effectivement résolu. `radio say "Test" --character Camille`
permet d'écouter un personnage, et `radio revoice <id>` remplace atomiquement l'audio d'une
émission existante avec le casting courant.

## Commandes

```bash
radio collect
radio facts
radio topics --hours 48
radio run --fmt journal
radio episodes
radio episodes --status failed
radio resume 42
radio import-scripts emissions
radio voice-queue --max 3
radio voices
radio say "Une India Pale Ale houblonnée" --character presenter
radio imaging
radio antenna-status
radio logs --lines 100
radio runs --limit 20
radio publish
radio withdraw 42
radio delete 42 --confirm
radio feedback
radio airings
radio stats
radio sidecar
```

`radio collect` et `radio run` affichent chaque étape en direct et écrivent un journal JSONL
rotatif dans `data/logs/radio.jsonl` (5 Mio par fichier, avec 5 archives). `RADIO_LOG_LEVEL` accepte
notamment `DEBUG`, `INFO`, `WARNING` ou `ERROR`. Les erreurs complètes restent dans le fichier,
tandis que les clés, jetons, mots de passe et paramètres sensibles sont masqués.

Les sources peuvent aussi être rangées dans `config/sources.d/*.yaml`. Par défaut,
`item_max_age_days: null` collecte tout l'historique daté exposé par les flux. Une limite peut
être réactivée globalement dans `config/radio.yaml` ou par source avec `max_age_days`. Pour les
podcasts, `history: all` force également l'historique complet, et
`content_mode: feed` exploite directement la description RSS détaillée sans ouvrir la page de
chaque épisode. `min_content_chars` adapte le seuil aux formats courts, tandis que
`refresh_existing: true` actualise les épisodes modifiés sans les dupliquer. Les liens audio,
durées, saisons, numéros, images et catégories disponibles dans le flux sont conservés dans
`item.metadata_json`, sans télécharger les MP3.

### Réseaux sociaux et veille Google

`radio collect` accepte aussi les types `youtube`, `facebook`, `instagram` et
`google_grounded`. Des modèles désactivés se trouvent dans
`config/sources.d/social.example.yaml`.

- **YouTube** : sans clé, le collecteur tente le flux public de la chaîne pour les dernières
  vidéos, mais YouTube peut ne pas l'exposer. `YOUTUBE_API_KEY` utilise l'API Data officielle,
  pagine la playlist des publications et importe l'historique exposé par la chaîne
  (`max_pages`, 50 vidéos par page) ; c'est le mode fiable recommandé.
- **Facebook** : nécessite `page_id` et `META_ACCESS_TOKEN`. L'API Graph ne donne accès qu'aux
  Pages et publications autorisées par le jeton ; aucune session web n'est scrapée.
- **Instagram** : nécessite `ig_user_id` et le même `META_ACCESS_TOKEN`. L'API officielle cible
  les comptes professionnels/créateurs reliés et autorisés, pas tous les comptes publics.
- **Google** : `google_grounded` utilise `GEMINI_API_KEY` et le Grounding with Google Search pour
  produire une veille synthétique accompagnée de citations. Ce n'est pas l'encart « Aperçu IA »
  de la page Google — celui-ci n'a pas d'API publique — mais son équivalent officiel exploitable.

Les appels authentifiés placent les clés dans des en-têtes HTTP afin qu'elles ne finissent ni
dans les URL ni dans les logs. `META_GRAPH_VERSION` et `GEMINI_SEARCH_MODEL` permettent de faire
évoluer les versions sans modifier le code.

## Habillage et direct vivant

`config/antenna.yaml` décrit une horloge d'antenne et les éléments générés par ElevenLabs :
jingles/stingers via Sound Effects, idents et interventions multivoix, promos, dédicaces et
musiques instrumentales originales via Music. La commande `radio imaging` génère uniquement les
éléments absents dans `data/antenna/`; `radio imaging --force` les régénère volontairement.
`radio antenna-status` affiche ce qui est réellement prêt à diffuser.

Le sidecar déroule automatiquement `clock` et saute sans erreur les catégories encore vides.
Chaque personnage possède ses propres réglages ElevenLabs (`stability`, `similarity_boost`,
`style`, `speed`) dans `config/characters.yaml`. Les morceaux sont demandés sans voix, sans
référence à un artiste ou à une chanson existante et avec signature C2PA. Les droits commerciaux
d'Eleven Music dépendent néanmoins de l'abonnement ElevenLabs utilisé : ils doivent être vérifiés
avant une diffusion commerciale.

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

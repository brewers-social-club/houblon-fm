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
- relais YouTube Live : profil Compose optionnel `youtube`
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

La section **Configuration** du studio permet de modifier les fichiers YAML gérés (`radio`,
grille, personnages, antenne, sources, séries vidéo et lexique). Chaque enregistrement est validé,
protégé contre les écrasements concurrents et sauvegardé dans `data/config-backups/` avant son
remplacement atomique. Le sidecar monte seul `./config:/config` en lecture-écriture ; nginx ne
monte ni n'expose ce dossier. Les secrets et le fichier `.env` restent volontairement hors de
l'interface.

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
ElevenLabs utilise `eleven_v4` et produit du MP3 128 kb/s. Chaque personnage utilise sa propre
voix dans
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

Les scripts récents séparent le texte factuel et jusqu'à deux directions de jeu contrôlées dans
`delivery` : énergie, rythme vif, humour pince-sans-rire, rire, soupir, curiosité, surprise,
chuchotement ou emphase. Ces indications ne passent
jamais dans la vérification des faits. Avec Eleven v4, les échanges sont envoyés à l'API
Text-to-Dialogue par séquences de moins de 2 000 caractères afin que les voix partagent le contexte
et réagissent naturellement. Les émissions plus longues sont raccordées avec un contexte
avant/après ; si Text-to-Dialogue échoue, la production retombe sur les prises séparées sans perdre
l'épisode. Les anciens scripts sans `delivery` restent compatibles. `radio revoice <id>` peut les
revocaliser avec v4, mais ne leur invente pas rétroactivement de directions de jeu.

Le rythme éditorial cible est défini par `speech_words_per_minute` dans `config/radio.yaml`
(150 par défaut). Eleven v4 Dialogue n'acceptant pas le curseur `speed`, le réglage `speed` de
chaque personnage est appliqué après génération avec un changement de tempo borné entre 0,7× et
1,2×. Les prompts imposent des répliques courtes et un fil humoristique proportionnel à la durée,
sans humour sur les personnes réelles, la santé, les mineurs ou l'abus d'alcool.

## Commandes

```bash
radio collect
radio facts
radio topics --hours 48
radio run --fmt journal
radio run --fmt journal --title "Samedi 3 octobre 2026" --hours 48
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
`--fmt` accepte uniquement un identifiant déclaré dans `config/schedule.yaml` (`journal`,
`headlines`, `week`, etc.) ; `--title` personnalise le titre public. Un verrou empêche deux
collectes/générations concurrentes. Chaque run publie un heartbeat SQLite toutes les 15 secondes :
une interruption clavier, fermeture SSH ou disparition du processus produit le statut
`interrupted` au lieu de laisser `running` éternellement.

Le format `feedback` est particulier : il ne collecte pas l'actualité et n'en extrait aucun fait.
Il utilise uniquement les messages encore `pending` reçus depuis `/avis.html`, conserve leur
provenance interne pendant la vérification, puis les marque `used` quand le script est validé.
Sans avis en attente, le run échoue immédiatement avec un message explicite. Le formulaire public
redirige vers `/merci-avis.html` après envoi ; les clients API sans redirection continuent de
recevoir une réponse JSON.

Les formats éditoriaux appliquent leur propre routage. `journal`, `headlines` et `week` couvrent
l'actualité générale ; un même fait peut être traité sous plusieurs formats mais n'est pas repris
dans deux épisodes du même format. `awards` exige des résultats de concours. `portrait`, `recipe`,
`style` et `glossary` sélectionnent un corpus cohérent issu d'un seul article et refusent de
produire si aucun contenu ne correspond. Les rôles autorisés, la fraîcheur et la durée cible de
`config/schedule.yaml` sont transmis au modèle ; tout rôle hors casting est ramené au casting du
format.

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

- **YouTube** : le flux public de la chaîne repère les dernières vidéos ; `YOUTUBE_API_KEY`
  permet en plus de paginer l'historique (`max_pages`, 50 vidéos par page). Une vidéo n'est
  jamais envoyée au pipeline éditorial depuis son titre ou sa description : `GEMINI_API_KEY`
  est obligatoire et Gemini analyse directement l'URL publique, l'audio, les paroles et les
  informations affichées à l'écran. `GEMINI_VIDEO_MODEL` vaut `gemini-3.8-flash` par défaut,
  `video_max_per_run` borne chaque source et `VIDEO_DAILY_MAX` borne la journée entière. Les
  vidéos restantes sont différées ; un échec d'analyse ne crée aucun contenu de substitution.
- **Facebook** : nécessite `page_id` et `META_ACCESS_TOKEN`. L'API Graph ne donne accès qu'aux
  Pages et publications autorisées par le jeton ; aucune session web n'est scrapée.
- **Instagram** : nécessite `ig_user_id` et le même `META_ACCESS_TOKEN`. L'API officielle cible
  les comptes professionnels/créateurs reliés et autorisés, pas tous les comptes publics.
- **Google** : `google_grounded` utilise `GEMINI_API_KEY`, l'Interactions API et le Grounding with
  Google Search (`gemini-3.8-flash` par défaut) pour produire une veille synthétique accompagnée
  de citations. Ce n'est pas l'encart « Aperçu IA » de la page Google — celui-ci n'a pas d'API
  publique — mais son équivalent officiel exploitable.

Les appels authentifiés placent les clés dans des en-têtes HTTP afin qu'elles ne finissent ni
dans les URL ni dans les logs. `META_GRAPH_VERSION`, `GEMINI_SEARCH_MODEL` et
`GEMINI_VIDEO_MODEL` permettent de faire évoluer les versions sans modifier le code.

## Habillage et direct vivant

### Générer une émission en arrière-plan

`deploy/run-format.sh` lance un format dans une unité systemd utilisateur détachée, construit le
titre avec la date française du jour et conserve le processus après la fermeture du terminal :

```bash
deploy/run-format.sh week
deploy/run-format.sh journal 72
deploy/run-format.sh portrait 720 "Portrait brassicole du"
```

Les formats acceptés sont `journal`, `headlines`, `awards`, `week`, `portrait`, `recipe`, `style`,
`glossary` et `feedback`. Le script utilise `--skip-collect` : planifier `radio collect` séparément
avant les générations automatisées. Les journaux sont disponibles avec, par exemple,
`journalctl --user -fu houblon-radio-week`.

`config/antenna.yaml` décrit une horloge d'antenne et les éléments générés par ElevenLabs :
jingles/stingers via Sound Effects, idents et interventions multivoix, promos, dédicaces et
musiques instrumentales originales via Music. La commande `radio imaging` génère uniquement les
éléments absents dans `data/antenna/`; `radio imaging --force` les régénère volontairement.
`radio imaging --spoken-only --force` remplace uniquement les idents, blagues, promos et
dédicaces avec le modèle vocal actif, sans repayer les musiques et stingers. Les éléments
parlés acceptent les mêmes directions de jeu Eleven v4 que les émissions (`delivery`).
`radio antenna-status` affiche ce qui est réellement prêt à diffuser.

Le studio admin permet d'écouter directement chaque MP3 d'épisode ou d'habillage. Ces
prévisualisations restent protégées par la session administrateur et ne rendent pas le dossier
`data/antenna/` public.

Le sidecar déroule automatiquement `clock` et saute sans erreur les catégories encore vides.
Chaque personnage possède ses propres réglages ElevenLabs (`stability`, `similarity_boost`,
`style`, `speed`) dans `config/characters.yaml`. Avec Eleven v4, `stability` et
`similarity_boost` sont envoyés au TTS classique, tandis que `speed` est appliqué au MP3 après
génération ; `style` reste utile aux anciens modèles compatibles. Les morceaux sont demandés sans voix, sans
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

### Relais YouTube Live

Le profil Compose `youtube` ajoute un encodeur FFmpeg sans modifier le direct Icecast. Il lit
`live.mp3`, compose une vidéo 720p30 à partir du visuel Houblon FM et envoie le résultat en RTMPS
avec H.264 CBR 4 Mbit/s, images clés toutes les deux secondes et AAC stéréo 128 kbit/s.

Créer une clé de flux personnalisée dans YouTube Live Control Room, activer l'auto-start et
l'auto-stop si le direct doit suivre automatiquement l'encodeur, puis fournir la clé uniquement
via `YOUTUBE_STREAM_KEY` dans le fichier `.env` local non versionné. La clé ne doit jamais être
placée dans Git, une commande, un ticket ou un journal.

```bash
docker compose --profile youtube up -d --build youtube-live
docker compose ps youtube-live
docker compose logs --tail 50 youtube-live
```

`YOUTUBE_RTMPS_URL` vaut par défaut `rtmps://a.rtmps.youtube.com:443/live2`. Si Live Control Room
fournit une autre URL RTMPS, recopier uniquement cette URL dans `.env`. Le service attend que le
flux Icecast réponde, masque la clé dans les diagnostics FFmpeg et redémarre automatiquement après
une coupure.

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

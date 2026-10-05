# Déploiement

## Local

Copier `.env.example` vers `.env`, changer les mots de passe, lancer `radio init`, puis
`docker compose up --build`.

Définir `RADIO_ADMIN_LOGIN`, `RADIO_ADMIN_PASSWORD` et un `RADIO_SESSION_SECRET` long et
aléatoire. L'administration utilise un cookie signé `HttpOnly` valable 12 heures ; aucun fichier
`.htpasswd` nginx n'est nécessaire.

Pour `RADIO_AI_PROVIDER=codex`, installer le CLI Codex et effectuer `codex login` sur l'hôte
(`codex login --device-auth` sur un serveur sans navigateur).
Lancer les commandes de production éditoriale (`radio facts`, `radio run`, `radio stock`) sur
ce même hôte. Ne montez pas le répertoire d'authentification Codex dans le conteneur web ou le
sidecar : ils n'en ont pas besoin.

## Production

- HTTPS obligatoire ;
- secrets via le gestionnaire de la plateforme ;
- sauvegarde quotidienne du fichier SQLite et du volume audio ;
- SMTP et alertes testés ;
- plafond mensuel activé avant les providers payants ;
- playlist de secours vérifiée ;
- revue juridique avant commercialisation.

## YouTube Live

Le relais YouTube est isolé derrière le profil Compose `youtube`. Il consomme le direct Icecast
et n'altère ni Liquidsoap ni le lecteur web. La clé de flux est un secret : elle reste dans le
`.env` local exclu de Git et n'est jamais copiée dans la configuration versionnée.

Préparer dans YouTube Live Control Room un flux privé ou non répertorié, récupérer son URL RTMPS
et sa clé, puis démarrer le relais :

```bash
docker compose --profile youtube up -d --build youtube-live
docker compose ps youtube-live
```

Valider d'abord l'image, le son et l'état de connexion dans Live Control Room. Ne rendre le direct
public qu'après ce contrôle. L'arrêt ciblé n'interrompt pas Icecast :

```bash
docker compose stop youtube-live
```

## Programmation systemd utilisateur

La collecte et les formats Houblon FM sont planifiés par des timers systemd utilisateur versionnés
dans `deploy/systemd/user/`. Ils utilisent `Persistent=true` : une échéance manquée pendant un arrêt
est rejouée au prochain démarrage du gestionnaire utilisateur. Le linger du compte `florian` doit
rester activé afin que les timers fonctionnent sans session interactive.

Installer ou mettre à jour la grille :

```bash
deploy/install-systemd-schedule.sh
systemctl --user list-timers --all 'houblon-radio-*'
```

Les services écrivent directement dans le journal utilisateur :

```bash
journalctl --user -u houblon-radio-collect.service
journalctl --user -u 'houblon-radio-format@*.service'
```

`deploy/run-format.sh` conserve son comportement détaché lors d'un lancement manuel. Les unités
systemd définissent `RADIO_RUN_FOREGROUND=1` afin d'exécuter le pipeline dans le service supervisé,
sans créer une seconde unité transitoire. Ne pas conserver en parallèle les anciennes entrées cron
Houblon FM, sous peine de lancer chaque format deux fois.

Les tâches planifiées et les lancements manuels de formats attendent jusqu'à six heures sur
`data/scheduler.lock`. Ce verrou sérialise les rattrapages `Persistent=true` et évite de perdre un
format si une collecte ou une autre génération est déjà active. Le journal planifié conserve sa
fenêtre historique de 96 heures ; les autres formats utilisent les valeurs par défaut de
`deploy/run-format.sh`.

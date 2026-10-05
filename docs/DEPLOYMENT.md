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

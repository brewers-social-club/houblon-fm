# Déploiement

## Local

Copier `.env.example` vers `.env`, changer les mots de passe, lancer `radio init`, puis
`docker compose up --build`.

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

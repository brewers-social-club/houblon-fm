# Déploiement

## Local

Copier `.env.example` vers `.env`, changer les mots de passe, lancer `radio init`, puis
`docker compose up --build`.

## Production

- HTTPS obligatoire ;
- secrets via le gestionnaire de la plateforme ;
- sauvegarde quotidienne du fichier SQLite et du volume audio ;
- SMTP et alertes testés ;
- plafond mensuel activé avant les providers payants ;
- playlist de secours vérifiée ;
- revue juridique avant commercialisation.

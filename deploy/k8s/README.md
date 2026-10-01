# Déploiement k3s

La première cible supportée est Docker Compose. Pour k3s, convertir les quatre services en
un Deployment multi-conteneurs partageant un PVC monté sur `/data`, puis ajouter des CronJobs
pour `radio collect`, `radio facts`, `radio run` et `radio voice-queue`. Ne jamais placer les
clés dans les manifests : utiliser des Secrets Kubernetes.


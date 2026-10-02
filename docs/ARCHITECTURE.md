# Architecture

Le domaine éditorial ne dépend d'aucun fournisseur. Les providers `mock`, Codex, Anthropic et
Gemini partagent des interfaces minimales. Codex est invoqué par son CLI local avec le compte
ChatGPT connecté : entrée sur stdin, répertoire temporaire vide, sandbox en lecture seule,
session éphémère, outils désactivés et réponse sous JSON Schema. Chaque étape est rejouable et
idempotente autant que possible. SQLite utilise WAL, `busy_timeout=30000` et des transactions
courtes.

Le sidecar sert le programmateur, les comptes, les avis, les statistiques et l'administration.
nginx sert le site statique et délègue les opérations dynamiques au sidecar. Liquidsoap demande
un fichier à `/next` et bascule vers une piste de secours si le sidecar échoue.

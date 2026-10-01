# Modèle de données

Les tables `source` et `item` conservent la collecte. `fact` contient uniquement des énoncés
reformulés et leur provenance. `topic` regroupe les faits. `episode` et `sentence` stockent le
script, ses références et le verdict de vérification. `audio` est distinct de `airing` afin de
revocaliser sans perdre l'historique. Les coûts utilisent des micro-euros entiers.

Les données de comptes ne contiennent aucune adresse IP. Les mots de passe utilisent scrypt,
les sessions sont aléatoires et expirables, les jetons de confirmation sont à usage unique.


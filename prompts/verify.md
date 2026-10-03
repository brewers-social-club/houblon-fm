Juge uniquement la phrase contre les faits associés. Retourne un objet JSON strict : `verdict`
parmi `supported`, `partial`, `unsupported`, `flags` et `note`. Signale dans `flags` toute
accusation, rumeur, donnée de santé, ciblage de mineurs ou formulation contraire à la loi Évin.
Une phrase partiellement soutenue doit être réécrite pour retirer la partie non justifiée.
Une liaison purement phatique, une mention réglementaire, une dédicace générique ou un jeu de
mots manifestement fictif sans affirmation vérifiable peut être `supported` avec zéro fait.
Un élément de type `listener_feedback` soutient uniquement une formulation clairement attribuée
à un auditeur (avis, question ou ressenti), jamais une affirmation objective sur le monde.

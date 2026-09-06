# ADR-003 — Le LLM renvoie un JSON validé par schéma, jamais du texte libre

**Statut** : Accepté
**Date** : 2026-09-06
**Décideurs** : Novafrik (direction), agent de développement
**Référence** : cahier des charges NVK-CDC-NB-2026-V1.0 §15.3, décision n° 3

> Cette décision est figée par le cahier des charges. Elle ne se modifie pas :
> elle se remplace par un nouvel ADR (numérotation à partir de ADR-011).

## Contexte

L'extraction produit des objets structurés (décisions, tâches, responsables,
échéances) qui alimentent directement la base et l'interface. Un modèle qui
répond en texte libre, ou en JSON approximatif, oblige à écrire un analyseur
tolérant — c'est-à-dire un composant qui invente du sens quand la sortie est
mauvaise. C'est exactement le mode de défaillance que §2.3 interdit.

## Décision

Toute sortie de LLM est validée par un schéma Pydantic v2 (§18.4). Un échec de
validation est une erreur de traitement, pas un contenu.

- La sortie JSON est forcée côté fournisseur (mode structuré / `json_schema`).
- L'échec de validation déclenche une relance bornée, puis fait passer la réunion
  en `FAILED` avec le `debug_id` ; il ne produit jamais de compte rendu partiel
  ni de champ « deviné ».
- Aucun champ n'est rempli par défaut pour satisfaire le schéma : un responsable
  non nommé dans la transcription reste `null`.

## Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Analyser du texte libre avec des expressions régulières | Fragile, silencieusement faux, et incompatible avec le principe de fidélité |
| Accepter un JSON partiel et compléter les champs manquants | Revient à inventer des données ; détruit la confiance, qui est le seul actif du produit |

## Conséquences

**Positives** — les erreurs sont visibles au lieu d'être diffuses ; le contrat de
données est unique et partagé avec le frontend (ADR-010).

**Négatives et coûts acceptés** — un taux d'échec non nul à traiter (relances,
état `FAILED`, support), plutôt qu'un résultat dégradé livré à l'utilisateur.

## Comment on vérifie qu'elle est respectée

Tests unitaires sur des sorties de modèle malformées : chacune doit lever une
erreur de validation, jamais produire un objet. Le jeu d'évaluation (§18.6)
mesure le taux d'hallucination, cible 0 sur les formulations vagues.

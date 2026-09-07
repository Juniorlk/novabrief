# ADR-000 — Gabarit de décision d'architecture

> Copier ce fichier sous `docs/adr/ADR-0NN-titre-court.md` (numérotation continue,
> **les nouvelles décisions démarrent à ADR-011** : ADR-001 à ADR-010 sont figées
> par le cahier des charges §15.3). Un ADR par décision, jamais modifié une fois
> « Accepté » : on l'amende par un nouvel ADR qui le remplace.

**Statut** : Proposé | Accepté | Remplacé par ADR-0NN | Abandonné
**Date** : AAAA-MM-JJ
**Décideurs** : Novafrik (direction), agent de développement
**Référence** : section du cahier des charges NVK-CDC-NB-2026-V1.0, brief de tâche concerné

## Contexte

Le problème à trancher, les contraintes qui s'imposent (marché camerounais, coût
par heure de réunion, absence d'équipe d'exploitation, Windows only en V1), et ce
qui se passe si l'on ne décide pas.

## Décision

Une phrase à l'impératif : ce qui est décidé. Puis les règles concrètes que le
code doit respecter, vérifiables en revue.

## Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
|  |  |

## Conséquences

**Positives** — ce que cette décision rend possible ou simple.

**Négatives et coûts acceptés** — ce qu'elle rend plus lourd, et pourquoi c'est
accepté.

## Comment on vérifie qu'elle est respectée

Le contrôle automatisable (test, règle de lint, revue de PR, requête SQL) qui
détecte une violation. Un ADR sans contrôle est un vœu.

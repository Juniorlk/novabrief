# ADR-010 — Monorepo, avec les schémas Pydantic comme source de vérité

**Statut** : Accepté
**Date** : 2026-09-06
**Décideurs** : Novafrik (direction), agent de développement
**Référence** : cahier des charges NVK-CDC-NB-2026-V1.0 §15.3, décision n° 10

> Cette décision est figée par le cahier des charges. Elle ne se modifie pas :
> elle se remplace par un nouvel ADR (numérotation à partir de ADR-011).

## Contexte

Quatre livrables partagent un même contrat de données : le client desktop, l'API,
les workers et l'interface web. Des définitions dupliquées entre Python et
TypeScript divergent toujours, et la divergence se découvre en production.

## Décision

Un dépôt unique (`apps/desktop`, `apps/api`, `apps/web`, `packages/schemas`,
`packages/ai`). Les schémas Pydantic v2 de `packages/schemas` sont la source de
vérité et **génèrent** les types TypeScript.

- Les types TypeScript sont générés, jamais écrits à la main, et le résultat de la
  génération est vérifié en CI.
- Le client `api-client` du desktop est généré depuis l'OpenAPI produit par
  FastAPI.
- Une évolution de schéma se fait en un seul commit qui traverse les trois
  applications.

## Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Dépôts séparés avec un paquet de types partagé | Décalage de version entre dépôts, PR croisées, et un contrat qui se met à jour en retard |
| Types TypeScript écrits à la main | Divergence silencieuse avec le backend, découverte à l'exécution |

## Conséquences

**Positives** — un contrat unique, une CI unique, des changements atomiques ;
l'agent de développement travaille avec le contexte complet.

**Négatives et coûts acceptés** — une CI plus longue et qui doit être découpée par
langage ; des outils de build hétérogènes (cargo, pnpm, pytest) à orchestrer, ce
que fait le `Makefile`.

## Comment on vérifie qu'elle est respectée

CI : le job de génération régénère les types TypeScript et échoue si le
répertoire de travail n'est pas propre (types non régénérés après un changement de
schéma). Les trois chaînes de lint et de test tournent sur chaque PR.

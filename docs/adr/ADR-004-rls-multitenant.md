# ADR-004 — Isolation multi-tenant par Row-Level Security PostgreSQL

**Statut** : Accepté
**Date** : 2026-09-06
**Décideurs** : Novafrik (direction), agent de développement
**Référence** : cahier des charges NVK-CDC-NB-2026-V1.0 §15.3, décision n° 4

> Cette décision est figée par le cahier des charges. Elle ne se modifie pas :
> elle se remplace par un nouvel ADR (numérotation à partir de ADR-011).

## Contexte

NovaBrief est vendu par organisation et stocke ce qu'une entreprise a de plus
sensible : ses conversations internes. Une fuite d'une organisation vers une
autre ne serait pas un incident technique mais la fin commerciale du produit
(§21.1). Les filtres applicatifs seuls ne suffisent pas : un `WHERE` oublié dans
une requête suffit à tout compromettre.

## Décision

L'isolation repose sur la Row-Level Security de PostgreSQL **en plus** des
filtres applicatifs, jamais à leur place.

- Une dépendance FastAPI positionne `SET LOCAL app.current_org_id` dans chaque
  transaction (§17.3).
- Toutes les tables portant des données de client ont une politique RLS active.
- Le rôle applicatif n'a **jamais** l'attribut `BYPASSRLS`. Aucun code, aucune
  migration, aucun script d'exploitation ne désactive RLS.
- Les ressources sont adressées par UUID v7, non énumérables.

## Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Filtres applicatifs seuls | Une seule requête sans filtre, dans le code ou dans un script d'exploitation, expose toutes les organisations |
| Une base de données par organisation | Ingérable en exploitation pour une équipe réduite (migrations, sauvegardes, coût) au niveau de volume visé |

## Conséquences

**Positives** — un oubli de filtre applicatif ne devient pas une fuite ; les
scripts d'exploitation et les jobs Celery héritent de la même garantie.

**Négatives et coûts acceptés** — chaque nouvelle table exige sa politique RLS et
son test ; un léger surcoût de planification des requêtes ; le débogage local
demande de positionner la variable de session.

## Comment on vérifie qu'elle est respectée

Test d'intégration obligatoire par table exposée : avec `app.current_org_id`
positionnée sur l'organisation A, une requête sur une ligne de l'organisation B
retourne zéro ligne. Une migration Alembic qui crée une table sans politique RLS
fait échouer la CI.

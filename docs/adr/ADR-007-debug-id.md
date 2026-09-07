# ADR-007 — Chaque réunion porte un debug_id de bout en bout

**Statut** : Accepté
**Date** : 2026-09-06
**Décideurs** : Novafrik (direction), agent de développement
**Référence** : cahier des charges NVK-CDC-NB-2026-V1.0 §15.3, décision n° 7

> Cette décision est figée par le cahier des charges. Elle ne se modifie pas :
> elle se remplace par un nouvel ADR (numérotation à partir de ADR-011).

## Contexte

Le pipeline traverse le poste client, l'API, Redis, plusieurs workers et deux
fournisseurs externes. Quand un client signale « mon compte rendu n'est pas
arrivé », sans identifiant commun le support consiste à fouiller des journaux
sans point d'ancrage — coût de support insoutenable pour une équipe réduite.

## Décision

Un `debug_id` lisible (forme `DBG-XXX-AAAAMMJJ-NNNN`) est généré à la
déclaration de la réunion et propagé partout.

- Le desktop le reçoit à la création de la réunion et l'affiche à l'utilisateur.
- Il figure dans chaque log `structlog` du chemin de traitement, avec
  `organization_id` et `meeting_id` quand ils existent.
- Il est transmis aux appels fournisseurs quand ceux-ci acceptent une référence
  client, et enregistré dans `usage_ledger`.
- Il figure dans toute réponse d'erreur Problem Details (RFC 9457).
- Les journaux ne contiennent jamais de contenu de réunion.

## Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Corrélation par meeting_id seul | Un UUID n'est ni dictable au téléphone ni lisible dans un ticket ; il n'existe pas encore au moment des toutes premières erreurs |
| Traçage distribué complet (OpenTelemetry) dès la V1 | Dépendance et coût d'exploitation disproportionnés au palier 1 ; le debug_id couvre le besoin support réel |

## Conséquences

**Positives** — un ticket support se résout par une recherche ; les journaux, les
métriques et le coût d'une réunion se recoupent.

**Négatives et coûts acceptés** — chaque nouveau chemin de traitement doit penser
à propager le champ ; c'est un point de revue systématique.

## Comment on vérifie qu'elle est respectée

Test : pour une réunion traitée de bout en bout, le `debug_id` apparaît dans les
journaux de l'API, du worker de transcription, du worker d'analyse et dans la
ligne `usage_ledger`. Revue de PR : tout nouveau chemin journalise `debug_id`
(CLAUDE.md §7.6).

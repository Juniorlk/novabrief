# ADR-009 — Prix, quotas et devises sont des données, jamais du code

**Statut** : Accepté
**Date** : 2026-09-06
**Décideurs** : Novafrik (direction), agent de développement
**Référence** : cahier des charges NVK-CDC-NB-2026-V1.0 §15.3, décision n° 9

> Cette décision est figée par le cahier des charges. Elle ne se modifie pas :
> elle se remplace par un nouvel ADR (numérotation à partir de ADR-011).

## Contexte

Le produit s'ouvre au Cameroun (XAF) puis à l'Afrique francophone (XOF) et à
l'Europe (EUR), avec des grilles et une TVA qui diffèrent (§5.2, §5.4, §6). La
TVA de 19,25 % s'active lors du passage au régime du réel. Les prix bougeront
avant que le code ne soit stabilisé.

## Décision

Aucun montant, quota, devise ou taux n'est écrit dans le code. Tout vient des
tables `plans` et `prices`, par marché.

- Les tarifs, quotas horaires, packs et taux de TVA sont des lignes en base,
  modifiables sans déploiement.
- Le marché de l'organisation détermine la grille applicable et le
  `BillingProvider` utilisé (§20.1).
- Le paramètre `vat_applicable` de l'éditeur est une donnée, activée par Novafrik
  le moment venu.
- Les changements de prix sont historisés : une facture émise reste lisible avec
  le tarif qui s'appliquait.

## Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Constantes ou fichier de configuration versionné | Tout changement tarifaire devient un déploiement ; l'historique des factures n'est plus reconstituable |
| Prix gérés côté fournisseur de paiement | Impose la grille de Flutterwave et empêche d'exposer les mêmes plans via Stripe en V1.1 (ADR-001) |

## Conséquences

**Positives** — une grille par marché s'ajoute sans toucher au code ; les
simulations et le modèle financier lisent la même source.

**Négatives et coûts acceptés** — un référentiel à administrer et à protéger : une
erreur de saisie devient une erreur de facturation, donc back-office restreint et
`audit_log` sur toute modification.

## Comment on vérifie qu'elle est respectée

Test automatisé : aucun littéral monétaire (montant, code devise, taux) dans
`apps/api/app/**` hors des migrations de données de référence. Test fonctionnel :
changer une ligne de `prices` change le montant présenté au paiement, sans
redéploiement.

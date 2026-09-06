# ADR-008 — Le coût réel de chaque réunion est enregistré

**Statut** : Accepté
**Date** : 2026-09-06
**Décideurs** : Novafrik (direction), agent de développement
**Référence** : cahier des charges NVK-CDC-NB-2026-V1.0 §15.3, décision n° 8

> Cette décision est figée par le cahier des charges. Elle ne se modifie pas :
> elle se remplace par un nouvel ADR (numérotation à partir de ADR-011).

## Contexte

L'unité économique du produit n'est pas le client mais l'heure de réunion
traitée : elle coûte environ 151 FCFA et se vend entre 333 et 500 FCFA selon le
plan (§5.1). Toute la rentabilité tient à cet écart. Un coût fournisseur qui
dérive sans être vu — modèle plus cher, réunions plus longues, relances
répétées — détruit la marge en silence.

## Décision

Chaque réunion écrit son coût réel dans `usage_ledger` ; le tableau de bord
d'unit economics est une fonctionnalité du MVP, pas un chantier ultérieur.

- Sont enregistrés : secondes de transcription, fournisseur et modèle utilisés,
  tokens d'entrée et de sortie du LLM, latences, coût en USD et en XAF.
- Un plafond de coût par réunion (0,50 $, §18.5) déclenche une alerte et
  interrompt les relances.
- L'écart entre coût modélisé et coût facturé par les fournisseurs est rapproché
  chaque mois, objectif < 5 % (§23).

## Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Se fier aux factures mensuelles des fournisseurs | Détection à un mois de retard, sans possibilité d'attribuer le surcoût à une organisation, un plan ou un type de réunion |
| Estimer le coût à partir de la durée | L'estimation ne détecte ni les relances, ni un changement de tarif, ni un basculement de fournisseur |

## Conséquences

**Positives** — la marge par plan est observée et non supposée ; la décision de
basculer vers Whisper auto-hébergé (§5.8) repose sur des mesures.

**Négatives et coûts acceptés** — une écriture supplémentaire par appel
fournisseur et une table qui croît avec le volume.

## Comment on vérifie qu'elle est respectée

Test d'intégration : une réunion traitée produit exactement une ligne
`usage_ledger` par appel fournisseur, avec un coût non nul. Le tableau de bord
back-office recoupe la somme avec les factures fournisseurs.

# ADR-002 — Séparation stricte entre transcription et intelligence

**Statut** : Accepté
**Date** : 2026-09-06
**Décideurs** : Novafrik (direction), agent de développement
**Référence** : cahier des charges NVK-CDC-NB-2026-V1.0 §15.3, décision n° 2

> Cette décision est figée par le cahier des charges. Elle ne se modifie pas :
> elle se remplace par un nouvel ADR (numérotation à partir de ADR-011).

## Contexte

Certains fournisseurs proposent d'envoyer l'audio directement à un modèle
multimodal qui produit un compte rendu. C'est séduisant et c'est un piège pour
NovaBrief : on perdrait les horodatages exacts qui relient chaque décision à sa
source (§2.3, principe « ne jamais inventer »), la diarisation contrôlable, la
capacité de changer de fournisseur indépendamment, et la maîtrise du coût, qui
n'est pas facturé de la même façon selon la modalité.

## Décision

On n'envoie jamais l'audio à un LLM. Le pipeline est strictement :

`audio → transcription horodatée → analyse LLM sur texte → JSON validé → présentation`

- L'étape de transcription produit un `TranscriptionResult` (§18.1) ; c'est le
  seul artefact que l'étape d'analyse reçoit.
- L'étape d'analyse n'a aucun accès à l'URL de l'audio ni au stockage R2.
- Chaque élément extrait porte le timestamp de l'énoncé source dont il provient.

## Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Modèle multimodal audio → compte rendu en un appel | Perte des timestamps source et de la diarisation vérifiable, hallucinations non traçables, verrouillage fournisseur, coût moins prévisible |
| Transcription et analyse chez le même fournisseur | Contredit l'ADR-001 : on ne peut plus arbitrer les deux étages séparément (Whisper auto-hébergé côté STT, GPT-5 mini côté analyse) |

## Conséquences

**Positives** — chaque décision et chaque tâche est rattachable à un passage
audio, ce qui est la condition de la confiance ; les deux étages évoluent
séparément ; le coût de chaque étage est mesuré distinctement.

**Négatives et coûts acceptés** — deux appels au lieu d'un, donc une latence
totale un peu plus élevée, et la qualité de l'analyse est plafonnée par celle de
la transcription.

## Comment on vérifie qu'elle est respectée

Test d'intégration : la signature de l'étape d'analyse n'accepte pas d'URL ni
d'octets audio. Revue de PR sur toute modification de `packages/ai/`. Vérification
que chaque `Decision` et chaque `Task` produites portent un timestamp source non
nul.

# ADR-006 — L'audio est éphémère, les textes sont durables

**Statut** : Accepté
**Date** : 2026-09-06
**Décideurs** : Novafrik (direction), agent de développement
**Référence** : cahier des charges NVK-CDC-NB-2026-V1.0 §15.3, décision n° 6

> Cette décision est figée par le cahier des charges. Elle ne se modifie pas :
> elle se remplace par un nouvel ADR (numérotation à partir de ADR-011).

## Contexte

L'audio est le poste de stockage le plus lourd et l'actif le plus sensible ;
c'est aussi celui dont la valeur décroît le plus vite une fois le compte rendu
produit. Le conserver indéfiniment augmente le coût R2, la surface d'une fuite et
l'exposition réglementaire (loi camerounaise n° 2024/017, RGPD à l'ouverture UE,
§21.2). Les textes, eux, sont la mémoire de l'entreprise que le produit promet
(§2.1).

## Décision

L'audio est purgé automatiquement ; les transcriptions et comptes rendus sont
conservés tant que le compte existe.

- Rétention audio : 30 jours par défaut, 90 jours en plan Business.
- La purge est un job planifié (02:00 UTC) et **audité** : chaque suppression est
  tracée dans `audit_log`.
- L'audio R2 n'est pas sauvegardé : il est éphémère par nature. L'original reste
  sur le poste client 24 h après confirmation de l'upload.
- Les durées sont des données de configuration, pas des constantes (voir ADR-009).

## Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Conserver l'audio indéfiniment | Coût de stockage croissant, surface de fuite permanente, contrainte réglementaire inutile |
| Supprimer l'audio dès la publication du compte rendu | Interdit à l'utilisateur de réécouter un passage pour lever un doute, ce qui est le recours prévu quand l'IA est incertaine (§13) |

## Conséquences

**Positives** — coût de stockage borné et prévisible ; exposition réduite ;
argument de conformité vis-à-vis des clients.

**Négatives et coûts acceptés** — passé la fenêtre de rétention, on ne peut plus
relancer un traitement sur l'audio d'origine ni corriger une transcription ; il
faut le dire clairement dans l'interface.

## Comment on vérifie qu'elle est respectée

Test du job de purge sur données synthétiques : au-delà de la fenêtre, l'objet R2
est absent, la ligne `audit_log` présente, la transcription toujours lisible.
Alerte si le job échoue (§22.3).

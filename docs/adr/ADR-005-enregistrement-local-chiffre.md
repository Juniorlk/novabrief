# ADR-005 — L'enregistrement est local et chiffré ; le réseau est optionnel

**Statut** : Accepté
**Date** : 2026-09-06
**Décideurs** : Novafrik (direction), agent de développement
**Référence** : cahier des charges NVK-CDC-NB-2026-V1.0 §15.3, décision n° 5

> Cette décision est figée par le cahier des charges. Elle ne se modifie pas :
> elle se remplace par un nouvel ADR (numérotation à partir de ADR-011).

## Contexte

La cible est le Cameroun : connexion instable, coupures fréquentes, bande
passante montante limitée et coûteuse (§2.3, principe « offline-first » ;
parcours P2 §9.2). Un client qui perd une réunion à cause d'une coupure ne
revient pas. Par ailleurs le poste peut être volé (§21.1).

## Décision

L'enregistrement ne dépend jamais du réseau. Le desktop ne contacte le backend
que pour l'upload et le statut.

- Les segments audio sont écrits localement, en clair jamais : AES-256-GCM, clé
  de réunion aléatoire, chiffrée par une clé de compte protégée par DPAPI (§16.3).
- Une coupure Internet pendant la réunion n'interrompt ni ne dégrade la capture.
- L'upload est repris là où il s'est arrêté, avec retry exponentiel sans limite
  (§16.4).
- Au démarrage, une réunion laissée en `RECORDING` (crash) est finalisée avec les
  segments présents et signalée à l'utilisateur.

## Alternatives écartées

| Alternative | Pourquoi écartée |
|---|---|
| Streaming de l'audio vers le serveur pendant la réunion | Perd de l'audio à la première coupure, consomme la bande passante montante nécessaire à la visioconférence en cours |
| Enregistrement local en clair | Un portable volé expose l'intégralité des réunions non encore purgées |

## Conséquences

**Positives** — la promesse « une coupure ne détruit jamais une réunion » est
tenue par construction ; l'upload peut être limité à 80 % de la bande montante
mesurée sans risque pour la capture.

**Négatives et coûts acceptés** — le compte rendu n'est pas disponible en temps
réel ; la gestion des clés locales et de la reprise après crash est du code
délicat à tester.

## Comment on vérifie qu'elle est respectée

Test de recette T-02 / parcours P2 : couper le réseau pendant une capture, la
mener à son terme, vérifier que l'audio est complet et que l'upload reprend.
Vérifier qu'aucun segment n'est lisible sans la clé.

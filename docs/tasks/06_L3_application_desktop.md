# Lot L3 — Application desktop Windows

**Référence** : `04_APRES_LES_POC_lots_MVP.md` ligne L3 ; EF-10 à EF-20, EF-30 à EF-34, §16.
**Prérequis** : lot L2 terminé et déployé (PR #11 à #21) ; correctifs de capture longue durée (PR #27).
**Estimation du cahier des charges** : 8 à 10 jours.

## Ce que ce lot doit produire

Le logiciel que la PME installe sur son PC. C'est lui qui produit l'audio que
tout le reste attend : sans lui, l'API déployée n'a rien à traiter et le
produit n'existe pas.

Il enregistre une réunion sans bot et sans dépendre de Teams, Meet ou Zoom —
micro + audio système capturés directement par WASAPI — chiffre localement,
puis téléverse vers R2 quand le réseau le permet.

## L'état du moteur audio en entrant dans ce lot

Le moteur existe depuis le POC #1 et ses quatre défauts de longue durée sont
corrigés (PR #27) :

| | Avant | Maintenant |
|---|---|---|
| Perte réelle | 10,25 % sur 60 min | 0,000 % sur 12 s |
| RAM | 668 Mo à 32 min | 10,9 Mo |
| Dérive rapportée | −83 293 ppm (impossible) | +12,72 ppm, ou refus motivé |
| Mesure de C3 | drapeau Windows (sous-estime ×1000) | trames livrées / trames dues |

**Ce qui reste à prononcer** : la capture réelle de 60 minutes, sur deux
configurations matérielles (intégré, puis Bluetooth). Les critères 1, 2 et 5 du
brief `02_correctifs_capture_longue_duree.md` ne sont pas prononcés tant qu'elle
n'a pas tourné. Une mesure de douze secondes ne dit rien d'une dérive lente ni
d'une croissance mémoire — c'est précisément ce que les tests courts ne voient
pas, et c'est ce qui avait laissé passer les 10,25 %.

**Elle bloque la mise en main d'une PME pilote, pas le développement de ce lot.**

## Découpage en sous-tâches

Une PR par sous-tâche. Les cinq premières ne dépendent ni du réseau ni d'un
compte fournisseur, conformément à la règle de séquencement (« commencer par la
partie locale »).

### L3.1 — Coquille Tauri et internationalisation

Application Tauri 2 sans fenêtre principale : icône de zone de notification,
UI Vue 3 + TypeScript strict. i18n FR et EN **dès le premier écran** — aucune
chaîne visible codée en dur, y compris dans les messages d'erreur et les
notifications système (`CLAUDE.md` §6).

Critères : `pnpm typecheck` et `cargo clippy -D warnings` propres ;
l'application démarre sans fenêtre et sans droits administrateur.

### L3.2 — Le coffre local chiffré (EF-17)

Segments de 5 s chiffrés en AES-256-GCM dans `%LOCALAPPDATA%\NovaBrief`, clé
dérivée de la session et scellée par DPAPI. Reprise après crash ou coupure
d'alimentation.

**Le critère est mesuré en tuant le processus**, pas en le fermant proprement :
un arrêt brutal doit perdre au plus les 5 dernières secondes. Un test
d'intégration tue le processus pendant l'écriture et vérifie ce que le coffre
rend.

Crate `vault`, à ajouter aux membres du workspace Cargo.

### L3.3 — Pilotage de l'enregistrement (EF-30, EF-33, EF-34, EF-15)

Branchement du moteur `audio-engine` derrière une machine à états locale :
inactif → enregistrement → pause → envoi. La pause **ne crée pas de second
fichier** et le temps en pause n'est pas facturé (EF-33) : le quota consommé est
la durée effective, ce que la table `meetings` sait déjà distinguer
(`duration_seconds` / `paused_seconds`).

Durée maximale 4 h, avertissement à 5 min de la limite (EF-34). Le plafond Free
à 60 min **ne se code pas ici** : c'est une donnée de plan (ADR-09), lue depuis
l'API.

Changement de périphérique en cours d'enregistrement (EF-15) : au plus 500 ms de
silence, jamais une coupure. C5 (`IMMNotificationClient`) était explicitement
hors du brief des correctifs ; il arrive ici.

### L3.4 — Widget, zone de notification, raccourcis (EF-12, EF-13, EF-14)

Widget flottant toujours au premier plan : indicateur rouge pulsé, chronomètre,
**deux vu-mètres séparés** micro et système, Pause et Terminer.

Le critère d'EF-13 dit ce que le widget est vraiment pour : « un utilisateur
détecte en moins de 5 s que l'audio système n'est pas capté (vu-mètre plat) ».
Ce n'est pas de la décoration — c'est le seul garde-fou contre une réunion
enregistrée à moitié, qui est le mode de panne le plus coûteux du produit.

Raccourcis globaux Ctrl+Shift+R et Ctrl+Shift+P, qui doivent fonctionner
**quand Teams a le focus**.

### L3.5 — Liaison du poste et coffre à jetons (EF-11)

Connexion par email / mot de passe ou par code de liaison à 6 caractères valable
10 min. Refresh token scellé par DPAPI. Une fois lié, plus jamais de
reconnexion sauf révocation — la révocation côté API existe déjà (EF-03).

Le code de liaison exige un endpoint API qui n'existe pas encore : à ajouter au
lot L2 comme sous-tâche de rattrapage, ou à réduire à la connexion par mot de
passe pour le MVP. **Arbitrage à demander avant de coder.**

### L3.6 — Téléversement résilient (EF-18)

Multipart vers R2 par URL présignées, reprise au dernier segment confirmé,
retry exponentiel de 1 s à 5 min sans limite de tentatives, file d'attente pour
plusieurs réunions. L'API correspondante est livrée (L2.3).

Critère : **T-01, l'épreuve du crash réseau** de la §24 — couper le réseau en
cours d'envoi, le rétablir, et retrouver la réunion complète sans doublon.

### L3.7 — Test audio guidé (EF-16)

30 secondes qui vérifient les deux flux, le niveau et l'absence d'écho.
**Il doit échouer explicitement si le loopback ne renvoie rien** — le cas
exact que la capture de validation de ce lot a produit en local.

### L3.8 — Journal local et `debug_id` (EF-20)

Journal anonymisé, jamais de contenu de réunion (`CLAUDE.md` §6). Le `debug_id`
vient de l'API à la déclaration et suit la réunion jusqu'aux journaux du poste,
ce qui ferme la chaîne ADR-07 de bout en bout. Envoi au support sur action
explicite de l'utilisateur, jamais automatique.

### L3.9 — Installateur, signature, mise à jour (EF-10, EF-19)

Installateur par utilisateur, sans droits administrateur, **< 15 Mo**,
démarrage automatique optionnel. Mise à jour silencieuse par le updater Tauri
(signature Ed25519), **jamais pendant un enregistrement**.

## Ce qui bloque, et sur qui

| Bloqué | Ce qu'il faut | Pour |
|---|---|---|
| Absence d'avertissement SmartScreen (EF-10, T-12) | un certificat **Azure Trusted Signing** : compte Microsoft, entité vérifiée, abonnement mensuel. Sans lui, chaque installation affiche « Windows a protégé votre ordinateur » et le taux d'abandon en PME est élevé. | L3.9 |
| Mise à jour automatique (EF-19) | une paire Ed25519 et un endpoint de publication | L3.9 |
| Code de liaison (EF-11) | un endpoint API qui n'existe pas — arbitrage : le construire, ou réduire le MVP à la connexion par mot de passe | L3.5 |
| T-13 (périphériques) | un **casque Bluetooth** pour la seconde configuration | L3.3, validation |

## Critères de sortie du lot

1. **T-01** (crash réseau) passe.
2. **T-12** (SmartScreen) passe — dépend du certificat.
3. **T-13** (changement de périphérique) passe sur deux configurations.
4. Installateur **< 15 Mo**.
5. Une réunion enregistrée sur le poste traverse l'API déployée jusqu'à
   `PUBLISHED` sans intervention.
6. Chaînes FR et EN complètes sur chaque écran.

## Ce que ce lot ne fait PAS

- **EF-21** (détection heuristique de réunion) et **EF-22** (réunions privées) :
  marqués S, donc V1.1. Le champ `is_private` est déjà appliqué côté API
  (PR #22) parce qu'il y était exposé sans l'être ; le desktop ne l'expose pas.
- Pas d'interface web : lot L4.
- Pas de facturation ni de plafond Free codé en dur : lot L5, et ADR-09.

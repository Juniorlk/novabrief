# Tâche — POC #1 : capture audio Windows (micro + audio système)

**Référence** : cahier des charges §16.2, EF-30 à EF-35, tests T-02 et T-13, risque n°1 de la matrice §26.
**Phase** : 0 (Go / No-Go). Rien d'autre ne démarre tant que ce POC n'est pas validé par des mesures.
**Environnement** : Windows 10 (21H2+) ou 11, x64. Rust stable, `cargo`. Aucun droit administrateur, aucun pilote virtuel.

## Objectif

Prouver qu'on peut, depuis un programme Rust sans installation de pilote, capturer **simultanément** le microphone et l'audio système (ce que l'utilisateur entend : Teams, Meet, Zoom, navigateur), les aligner dans le temps, et produire un fichier audio compressé exploitable par un moteur de transcription.

Si ce POC échoue, NovaBrief tel que conçu n'existe pas. Il vaut donc mieux un échec mesuré rapidement qu'un succès déclaré.

## Ce que tu livres

1. La crate `apps/desktop/src-tauri/crates/audio-engine/` (bibliothèque) avec :
   - capture WASAPI en mode partagé du périphérique d'entrée par défaut ;
   - capture loopback WASAPI du périphérique de rendu par défaut (`AUDCLNT_STREAMFLAGS_LOOPBACK`) ;
   - conversion de chaque flux en mono, rééchantillonnage à 16 kHz (`rubato` ou équivalent) ;
   - compensateur de dérive d'horloge entre les deux flux (mesure du décalage cumulé, correction sub-milliseconde) ;
   - encodage Opus 32 kbps **stéréo** (canal gauche = micro, canal droit = système) par trames de 20 ms dans un conteneur Ogg ;
   - écriture par segments de 5 s avec un manifeste JSON (durée, périphériques, horodatages, SHA-256 par segment).
2. Un binaire CLI `tools/nb-capture` : `nb-capture --duration 60 --out capture.ogg [--input <device>] [--output <device>]`, qui affiche en continu les niveaux des deux voies (vu-mètres texte) et écrit le fichier.
3. Un générateur de signal de test `tools/nb-testsignal` qui joue dans les haut-parleurs des clics à intervalles connus (toutes les 10 s) et un script Python `tools/measure_drift.py` qui mesure, sur le fichier capturé, le décalage entre les clics captés par le loopback et les mêmes clics captés par le micro (haut-parleurs → micro), et trace la dérive dans le temps.
4. Le compte rendu (format §9 des instructions) avec la **matrice de résultats** ci-dessous remplie.

## Critères de succès (mesurés, pas déclarés)

| # | Critère | Cible | Comment le mesurer |
|---|---|---|---|
| C1 | Les deux flux sont captés en même temps | Les deux voies contiennent du signal sur une réunion Teams ou Meet de 10 min | Écoute + vu-mètres + spectrogramme du fichier |
| C2 | Décalage micro / système | < 40 ms après 60 min de capture continue | `measure_drift.py` sur une capture de 60 min avec `nb-testsignal` |
| C3 | Aucune perte de trames | 0 discontinuité sur 60 min | Compteur de `AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY` + continuité des horodatages |
| C4 | Loopback silencieux | Quand rien n'est joué, le fichier reste continu (silence encodé, pas de trou) | Capture de 5 min sans son système, durée du fichier = 5 min |
| C5 | Changement de périphérique en cours de capture | Coupure ≤ 500 ms, capture reprise sans redémarrage | Brancher un casque USB / Bluetooth pendant la capture |
| C6 | Casque Bluetooth en profil mains-libres (HFP) | Détecté et signalé (le format passe à 8 kHz / 16 kHz mono) | Test avec un casque BT en appel Teams |
| C7 | Consommation | < 3 % CPU et < 60 Mo RAM sur un portable modeste pendant la capture | Gestionnaire des tâches, 10 min |
| C8 | Poids du fichier | ≈ 14-15 Mo par heure | Taille du fichier / durée |
| C9 | Qualité transcriptible | Une transcription AssemblyAI (ou Whisper local) du fichier est lisible sur les deux voies | Transcription rapide d'un extrait de 3 min |

Matrice de périphériques à couvrir au minimum : haut-parleurs + micro intégrés du portable ; casque filaire jack ; casque USB ; casque Bluetooth (A2DP puis HFP) ; sortie HDMI/écran externe comme périphérique de rendu par défaut.

Applications à tester : Microsoft Teams (client), Google Meet (Chrome et Edge), Zoom, WhatsApp Desktop, une vidéo YouTube (contrôle).

## Points durs connus (traite-les explicitement, ne les découvre pas en production)

- Le loopback WASAPI ne délivre aucun paquet quand rien n'est joué : il faut soit détecter le silence et insérer des trames nulles alignées sur l'horloge, soit maintenir un flux de rendu silencieux. Documente le choix.
- Les formats de mixage varient (44,1 / 48 / 96 kHz, 1 à 8 canaux, float32 ou int16) : lire `GetMixFormat` et convertir, jamais supposer 48 kHz stéréo.
- Les deux flux ont des horloges différentes : le compensateur doit corriger par insertion / suppression d'échantillons interpolés, pas par saut brutal.
- Changement de périphérique par défaut : s'abonner à `IMMNotificationClient` et réouvrir le client audio de manière transparente.
- Thread audio à priorité élevée (MMCSS « Pro Audio ») pour éviter les pertes sous charge CPU.
- Écho : quand l'utilisateur est sur haut-parleurs, sa voix distante revient dans le micro. Pas d'annulation d'écho dans ce POC ; note simplement l'impact sur C9.

## Ce que tu ne fais PAS dans ce POC

- Pas d'interface Tauri, pas de tray, pas de widget : un CLI suffit.
- Pas de chiffrement local, pas d'upload, pas d'API : ils viennent au lot Desktop.
- Pas de pilote virtuel (VB-Cable ou autre), pas de capture par hook d'application.
- Pas de transcription complète : seulement l'extrait de 3 min du critère C9.

## Déroulé attendu

1. Squelette de la crate + CLI, capture d'un seul flux (loopback), fichier WAV brut. Vérifie que ça enregistre YouTube.
2. Ajoute le micro, puis le rééchantillonnage et le mixage en deux voies. Fichier WAV stéréo.
3. Ajoute le compensateur de dérive et l'outil de mesure. Première mesure sur 15 min, puis 60 min.
4. Encodage Opus + segments + manifeste.
5. Matrice de périphériques et d'applications, mesures C1 à C9.
6. Compte rendu. Si un critère échoue, propose au plus deux pistes de correction chiffrées en effort, puis attends la décision.

## Décision Go / No-Go (prise par Novafrik sur la base du compte rendu)

- **Go** : C1 à C4 et C9 atteints sur au moins 4 configurations de périphériques, C2 mesuré et < 40 ms.
- **Go conditionnel** : C5 ou C6 partiels, avec un plan de correction de moins de 3 jours.
- **No-Go** : C1, C2 ou C3 non atteints après deux itérations. On revoit l'approche avant tout autre développement.

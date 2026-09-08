# Tâche — Correctifs de la capture sur longue durée

**Référence** : POC #1 (`01_POC1_capture_audio.md`), critères C2, C3 et C7.
**Statut** : **différé** par décision de Novafrik le 2026-09-07. Consigné pour ne pas être perdu.
**Priorité** : bloquant avant toute mise en main d'utilisateurs réels.

## Pourquoi cette tâche existe

Le test de 60 minutes du 2026-09-07 a révélé trois défauts qu'aucun test court
ne pouvait voir. Sur une capture de 20 secondes tout est vert ; sur une heure,
**10,25 % de l'audio est perdu** et la mémoire atteint 668 Mo.

C'est le mode de défaillance le plus dangereux pour NovaBrief : la perte est
**invisible**. La synthèse de silence comble les trous, le fichier a la bonne
durée, les hachages sont corrects, et Windows ne signale que 2 à 5
discontinuités là où six minutes d'audio ont disparu. Un client ne verrait
qu'un compte rendu incomplet, sans jamais comprendre pourquoi.

## Mesures de référence (à battre)

Capture de 3600 s, micro Intel Smart Sound + haut-parleurs Realtek :

| Grandeur | Mesuré | Attendu |
|---|---|---|
| Trames réelles (micro) | 155 080 320 → 43 077,7 Hz | 172 800 000 → 48 000 Hz |
| Audio réellement perdu | **10,25 %** sur les deux endpoints | 0 % |
| Trames synthétisées | 10,25 % | ≈ 0 % hors silence réel |
| RAM à 32 min | **668 Mo** | < 50 Mo |
| CPU | ~1 % de la machine | < 10 % |
| Discontinuités signalées par Windows | 2 et 5 | — (indicateur non fiable) |
| Dérive rapportée | −83 293 ppm | valeur physiquement impossible |

## Défauts identifiés

### D1 — `Vec::drain` depuis le début, en O(n²)

```
apps/desktop/src-tauri/crates/audio-engine/src/encode.rs:228
    self.pending.drain(..FRAME_INTERLEAVED);
apps/desktop/src-tauri/crates/audio-engine/src/resample.rs:111
    let chunk: Vec<f32> = self.pending.drain(..CHUNK).collect();
```

Retirer les premiers éléments d'un `Vec` décale tout le reste : O(n) par appel,
donc O(n²) sur un gros lot. Plus le tampon grossit, plus chaque trame coûte
cher — une boucle de rétroaction, pas une pénalité constante.

**Correctif** : `VecDeque` sur les deux chemins chauds, ou consommation par
index sans décalage. Le `collect()` de `resample.rs` alloue en plus un `Vec`
par bloc : à supprimer.

### D2 — Files inter-threads non bornées

```
tools/nb-capture/src/main.rs
    let (mic_tx, mic_rx) = channel::<Chunk>();
    let (sys_tx, sys_rx) = channel::<Chunk>();
```

Rien ne limite l'accumulation quand l'écriture prend du retard. La mémoire part
en vrille silencieusement.

**Correctif** : `sync_channel` avec une borne explicite (quelques secondes
d'audio). Une file pleine doit produire une **erreur visible et un compteur**,
jamais une croissance silencieuse : mieux vaut un échec net qu'une réunion
tronquée sans que personne le sache.

### D3 — L'estimateur de dérive conclut sur des données invalides

`drift.rs` ajuste les trames réelles contre le temps écoulé. Quand des trames
manquent, il mesure le taux de perte et le présente comme une dérive d'horloge :
d'où les −83 293 ppm, soit 8,3 % — impossible pour un quartz.

**Correctif** : l'estimateur doit **refuser de conclure** au-delà d'un seuil de
trames synthétisées (proposition : 0,5 %) et le dire, plutôt que de rendre un
chiffre faux. Un ADR sans contrôle est un vœu ; une mesure sans garde-fou est
pire qu'une absence de mesure.

### D4 — C3 mesuré avec le mauvais instrument

Le compteur `AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY` a signalé 2 et 5
événements pendant que 10,25 % de l'audio disparaissait. Il sous-estime la
perte de plusieurs ordres de grandeur.

**Correctif** : C3 se mesure par l'écart entre trames réelles reçues et trames
attendues à la fréquence nominale, sur la durée de la capture. Le drapeau
Windows devient un indice complémentaire, pas la mesure.

## Cause racine probable

Une seule chaîne explique les trois symptômes, et la symétrie des 10,25 % sur
les deux endpoints indique une cause partagée **en aval** des périphériques :

> écriture en retard → files gonflent → lots plus gros → `drain` quadratique →
> écriture encore plus en retard → mémoire explose → sous pression mémoire les
> threads de capture ratent leur échéance → le tampon WASAPI de 200 ms déborde →
> audio perdu → la synthèse de silence masque la perte.

## Critères d'acceptation

1. Capture de 60 min : **perte réelle < 0,1 %**, mesurée comme trames reçues
   contre trames attendues.
2. RAM stable sous 50 Mo sur 60 min, mesurée toutes les 5 min (pas seulement à
   la fin).
3. C2 enfin mesurable : dérive relative rapportée avec moins de 0,5 % de trames
   synthétisées, ou refus explicite de conclure.
4. Un test automatisé qui injecte un producteur plus rapide que le
   consommateur et vérifie que la mémoire reste bornée et que la saturation est
   signalée.
5. Rejeu des 60 min sur les deux configurations (intégré, puis Bluetooth).

## Ce que cette tâche ne fait PAS

- Pas de compensateur de dérive : tant que C2 n'est pas mesurable, on ne sait
  pas s'il est nécessaire.
- Pas de C5 (`IMMNotificationClient`) : tâche distincte.

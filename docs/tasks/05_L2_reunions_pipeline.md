# Lot L2 — Réunions et pipeline de traitement

**Référence** : `04_APRES_LES_POC_lots_MVP.md` ligne L2 ; EF-40 à EF-47, §11, §18, ADR-02, ADR-03, ADR-05 à ADR-08.
**Prérequis** : lot L1 terminé (PR #10 mergée le 2026-09-09).
**Estimation du cahier des charges** : 6 à 8 jours.

## Ce que ce lot doit produire

Le chemin complet d'une réunion, du moment où le poste Windows déclare qu'il
enregistre jusqu'au compte rendu structuré et à la purge de l'audio. À la fin
du lot, une réunion soumise à l'API doit traverser la machine à états de la
section 11 sans intervention humaine, et un client doit pouvoir suivre sa
progression en temps réel.

Le lot **ne contient pas** d'interface : le desktop est le lot L3, le web le
lot L4. Ce qui est livré ici se pilote par l'API et se vérifie par des tests.

## Décision de Novafrik qui modifie le brief d'origine

**La campagne de validation sur 200 réunions est annulée** (décision du
2026-09-08). Le pipeline est construit et testé **avec des doubles
uniquement** : aucun appel réel à AssemblyAI, Deepgram ou OpenAI.

Conséquence à assumer explicitement plutôt qu'à contourner :

- le critère de sortie d'origine « 200 réunions de test passent en COMPLETED »
  **ne sera pas prononcé** ;
- le test **T-11** (charge, P95 ≤ 8 min) **ne sera pas prononcé** ;
- **T-06** (fallback fournisseur) sera prononcé *en simulation* : le
  basculement est déclenché par un double qui renvoie des 503, ce qui prouve la
  logique de bascule mais pas le comportement réel d'AssemblyAI en panne ;
- **EF-41 (WER ≤ 15 %) et EF-42 (0 hallucination) restent non mesurés.** Ce sont
  les deux exigences que les POC #2 et #3, également sautés, devaient établir.
  Le risque n°2 du cahier des charges — « l'IA invente une décision » — se
  révélera donc en recette (lot L7) et non en phase 0.

Ce n'est pas un défaut du lot, c'est un report de risque décidé en connaissance
de cause. Il doit rester visible dans `docs/ETAT.md` jusqu'à ce qu'il soit levé.

## Découpage en sous-tâches

Une PR par sous-tâche, dans cet ordre. Les quatre premières ne dépendent
d'aucune clé fournisseur.

### L2.1 — Le modèle `meetings` et la machine à états

Tables `meetings`, `transcript_segments`, `reports`, `decisions`, `tasks`,
`speakers`, avec RLS sur chacune et ajout à `TENANT_TABLES`.

La machine à états de la section 11 est implémentée comme une **table de
transitions autorisées**, pas comme une suite de `if`. Un passage non prévu
lève une erreur ; il n'existe aucun chemin pour écrire un état arbitraire.
Chaque transition écrit son horodatage et son acteur, comme le demande §11.

Endpoints : `POST /meetings` (déclaration, renvoie `meeting_id` et `debug_id`),
`GET /meetings` (liste filtrée), `GET /meetings/{id}`, `PATCH /meetings/{id}`
(titre, participants), `DELETE /meetings/{id}`.

*Critères* : une transition interdite est refusée avec un code stable ; deux
organisations ne se voient jamais ; le `debug_id` est posé à la création et
suit la réunion partout (ADR-07).

### L2.2 — Stockage objet et finalisation

`StorageProvider` derrière une abstraction (ADR-01), implémenté pour l'API S3 —
donc Cloudflare R2 en production et MinIO en local, sans changement de code.

`POST /meetings/{id}/finalize-local` renvoie les URL présignées multipart
(TTL 15 min, §21.1). `POST /meetings/{id}/finalize` vérifie le checksum global,
la durée et la version du client, puis répond **202 en moins de 500 ms**
(EF-40) avec `QUEUED` ou `QUOTA_HOLD`.

*Critères* : un checksum qui ne correspond pas est refusé et la réunion ne part
pas en file ; une URL présignée expirée n'ouvre rien ; l'audio ne transite
jamais par le serveur d'API (ADR — le stockage sur disque serveur est interdit).

### L2.3 — Quota et registre de consommation

`usage_ledger` (ADR-08) et décrément **atomique** du quota. Aucun prix, aucun
quota, aucune devise dans le code : tout vient des colonnes de l'organisation
et, au lot L5, des tables `plans` et `prices` (ADR-09).

Le décrément se fait à `COMPLETED → PUBLISHED`, jamais avant : §11 dit
explicitement qu'une réunion `FAILED` ne décrémente aucun quota.

*Critères* : deux finalisations simultanées ne peuvent pas faire passer le
compteur sous zéro ; une réunion échouée ne coûte rien au client ; le coût réel
(secondes STT, tokens LLM, fournisseur) est écrit pour chaque réunion (EF-47).

### L2.4 — File Celery et travaux planifiés

Celery 5 sur Redis, avec l'ordonnanceur. Il reprend
`app.jobs.purge_organizations`, aujourd'hui lancé à la main, et reçoit la purge
audio à J+30 / J+90 (ADR-06).

*Critères* : une tâche qui échoue est réessayée avec un retard exponentiel et
finit en `FAILED` plutôt qu'en boucle ; l'ordonnanceur ne lance pas deux fois
la même purge ; aucun contenu de réunion n'apparaît dans les journaux.

### L2.5 — `TranscriptionProvider` et le fallback

Interface, puis `AssemblyAIProvider` (principal) et `DeepgramProvider`
(secondaire). Le lexique de l'organisation (EF-05, déjà en base) est transmis
comme *keyterms*. Bascule automatique sur erreur 5xx, timeout, ou disjoncteur
ouvert — le timeout étant de 10 min par heure d'audio (EF-45).

Aucune fonction d'analyse du fournisseur n'est activée : ni résumé, ni thèmes,
ni intentions. L'ADR-02 impose la séparation stricte transcription /
intelligence.

*Critères* : T-06 en simulation ; le basculement écrit un incident tracé ;
changer l'ordre des fournisseurs se fait par configuration, sans redéploiement.

### L2.6 — `LLMProvider` et l'extraction structurée

Interface, `OpenAIProvider`, et le prompt versionné
`packages/ai/prompts/extract_v1.md`. La sortie est **validée par un schéma
Pydantic** (ADR-03) : un JSON invalide est une erreur, pas un contenu. Trois
tentatives avec prompt renforcé, puis `FAILED` (EF-45).

La fidélité prime sur la complétude : un responsable n'est renseigné que s'il
est nommé dans la transcription, et chaque décision et chaque tâche porte le
timestamp de son passage source (EF-43).

*Critères* : une réponse non conforme au schéma ne devient jamais un compte
rendu ; aucun élément extrait n'existe sans timestamp source ; le jeu
d'évaluation de non-hallucination est **écrit et exécutable**, même s'il n'est
pas exécuté contre le vrai modèle dans ce lot.

### L2.7 — Statut en temps réel

`WS /api/v1/ws/meetings/{id}` avec ticket à usage unique. L'état et
l'estimation de délai sont poussés à chaque transition (EF-44).

*Critères* : un ticket ne fonctionne qu'une fois ; un ticket d'une autre
organisation est refusé ; la fermeture du socket ne bloque aucun worker.

### L2.8 — Relance et purge

`POST /meetings/{id}/retry` sur une réunion `FAILED` (auteur ou Admin). Purge
audio planifiée selon `audio_retention_days`, qui fait passer la réunion en
`AUDIO_PURGED` en conservant les textes (ADR-06).

*Critères* : une relance repart à `QUEUED` sans dupliquer le registre de coût ;
après purge l'audio n'est plus accessible et le compte rendu l'est toujours.

## Critères d'acceptation du lot

1. Une réunion soumise à l'API traverse `CREATED → QUEUED → TRANSCRIBING →
   ANALYZING → COMPLETED → PUBLISHED` avec des fournisseurs doublés, sans
   intervention.
2. Une panne du fournisseur principal, simulée, produit un `FALLBACK_STT` puis
   un `COMPLETED` (T-06 en simulation).
3. Trois réponses LLM invalides produisent `FAILED`, sans décrément de quota, et
   la relance fonctionne.
4. `ruff`, `mypy --strict`, `pytest` verts en environnement reconstruit depuis
   `pyproject.toml` seul ; couverture backend ≥ 70 %.
5. Aucun contenu de réunion dans les journaux, vérifié par un test.
6. `docs/ETAT.md` à jour.

**Non prononcés, et pourquoi** : les 200 réunions et T-11 (campagne annulée),
EF-41 et EF-42 (aucun appel réel).

## Ce qu'il faut de Novafrik

| Bloqué | Ce qu'il faut | Sous-tâche |
|---|---|---|
| `DeepgramProvider` en conditions réelles | une **vraie clé Deepgram** — celle fournie le 2026-09-08 était en fait la clé AssemblyAI (vérifié : Deepgram la rejette en 401) | L2.5 |
| Stockage en production | identifiants Cloudflare R2 | L2.2, MinIO suffit en local |

Aucun des deux n'empêche d'écrire le lot : les deux fournisseurs sont codés
contre leur interface et testés avec des doubles, ce qui est précisément la
décision prise.

## Hors périmètre — ne pas anticiper

Export PDF (lot L4), traduction du résumé (EF-46, *Should*), recherche plein
texte (V1.1), réunions privées (EF-22, V1.1), détection automatique de réunion
(EF-21, V1.1), tout ce qui touche à la facturation (lot L5).

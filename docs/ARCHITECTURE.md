# NovaBrief — comment tout est monté

> Document de lecture, pas de référence normative. Le *quoi* est dans
> `docs/cahier-des-charges.md`, le *pourquoi* dans `docs/adr/`, l'état d'avancement
> dans `docs/ETAT.md`. Ici : ce qui tourne, où, et ce que vit l'utilisateur.
>
> Écrit le 2026-09-13.

---

## 1. Les trois morceaux

NovaBrief n'est pas un logiciel, c'en est trois, qui se parlent par HTTPS.

| | Où ça tourne | Ce que ça fait | État |
|---|---|---|---|
| **Le logiciel de bureau** | sur le PC Windows du client | enregistre la réunion, la chiffre, l'envoie | **fait** |
| **L'API et ses ouvriers** | sur un VPS OVHcloud | reçoit l'audio, le fait transcrire, le fait analyser, range le résultat | **fait** |
| **L'application web** | rien pour l'instant | relire et partager les comptes rendus depuis un navigateur | **lot L4, pas commencé** |

Le desktop ne parle **qu'à** l'API. Il ne connaît ni AssemblyAI, ni OpenAI, ni
la base de données. C'est volontaire : le jour où l'on change de fournisseur de
transcription, aucun PC client n'a besoin d'être mis à jour.

---

## 2. Ce qui tourne réellement sur le VPS

Oui, c'est Docker. **Sept conteneurs**, décrits par `infra/docker-compose.prod.yml`,
démarrés par une seule commande. Le serveur lui-même ne contient presque rien :
Docker, un pare-feu, et le dépôt Git.

```
                        Internet (443)
                              |
                        ┌───────────┐
                        │   caddy   │  ← le seul conteneur exposé
                        └─────┬─────┘
                              │  (réseau interne Docker)
                  ┌───────────┴────────────┐
                  │                        │
            ┌─────▼─────┐            ┌─────▼─────┐
            │    api    │            │    web    │  (pas encore déployé)
            └─────┬─────┘            └───────────┘
                  │
       ┌──────────┼───────────────┐
       │          │               │
 ┌─────▼────┐ ┌───▼───┐   ┌───────▼───────┐
 │ postgres │ │ redis │   │ worker × 1    │  ← fait le travail long
 └──────────┘ └───┬───┘   │ beat   × 1    │  ← déclenche les tâches horaires
                  └───────┴───────────────┘
```

| Conteneur | Image | Rôle | Visible d'Internet ? |
|---|---|---|---|
| `caddy` | `caddy:2-alpine` | reverse proxy, certificats HTTPS automatiques | **oui**, ports 80 et 443 |
| `api` | construite depuis `infra/api.Dockerfile` | FastAPI, répond aux requêtes | non, seulement via Caddy |
| `worker` | la même image | Celery : transcription, analyse, purge — 2 tâches en parallèle | non |
| `beat` | la même image | l'horloge de Celery (purges à 30 jours, relances) | non |
| `postgres` | `postgres:16-alpine` | **oui, PostgreSQL est dans Docker**, avec un volume `postgres_data` | non |
| `redis` | `redis:7-alpine` | la file d'attente des tâches, sans persistance volontaire | non |
| `web` | Nuxt 3 | l'application web du lot L4 | déclaré, rien derrière |

**Rien n'est publié sauf Caddy.** PostgreSQL n'écoute que sur le réseau interne
Docker : une base joignable depuis Internet est une base qui sera trouvée.

Trois détails qui comptent :

- **Redis n'est pas sauvegardé** (`--save "" --appendonly no`). Il ne contient que
  des tâches en cours ; les perdre est rattrapable par une relance, les garder
  ajouterait un fichier à sauvegarder et à corrompre.
- **L'API ne se connecte pas en superutilisateur.** `postgres-init/01-app-role.sql`
  crée un rôle `novabrief_app` sans DDL et surtout `NOBYPASSRLS` : il ne peut pas
  traverser l'isolation entre organisations, même par erreur de code (ADR-04).
  Les migrations, elles, gardent les identifiants du propriétaire.
- **L'audio ne transite pas par le serveur.** Il part du PC directement vers
  Cloudflare R2 par des URL signées. C'est ce qui permet à un petit VPS de tenir
  des centaines de mégaoctets par réunion sans devenir un relais de fichiers.

### Ce qui est vérifiable tout de suite

```
$ curl https://api.novabrief.cloud/health
{"status":"ok","environment":"prod","version":"0.1.0"}

$ curl https://api.novabrief.cloud/health/ready
{"status":"ready","checks":{"database":"ok"}}
```

### Ce qui n'existe pas encore côté serveur

- **Aucune sauvegarde.** Si le VPS disparaît, tous les comptes rendus
  disparaissent avec lui. C'est le lot L6, et c'est aujourd'hui le plus gros
  risque du projet.
- Pas de supervision (Sentry, Grafana, Uptime Kuma sont prévus, pas installés).
- Les clés fournisseurs en place sont temporaires et doivent être régénérées par
  Novafrik avant le premier client réel.

---

## 3. Comment le code est rangé

```
novabrief/
├── docs/          le cahier des charges, les décisions (ADR), l'état, les briefs
├── apps/
│   ├── desktop/   le logiciel Windows
│   │   ├── src/        la fenêtre (Vue 3 + TypeScript)
│   │   └── src-tauri/  le moteur (Rust)
│   │       └── crates/ audio-engine · vault · uploader · api-client
│   ├── api/       FastAPI + les tâches Celery
│   └── web/       Nuxt 3 (vide, lot L4)
├── packages/
│   ├── schemas/   les modèles Pydantic — la source de vérité des formats
│   └── ai/        les fournisseurs de transcription et de LLM, les prompts versionnés
├── infra/         docker-compose, Caddyfile, scripts de déploiement
└── tools/         bancs d'essai audio, jeux de test
```

Le desktop est coupé en quatre bibliothèques Rust indépendantes, et ce découpage
est le cœur de la fiabilité :

| Bibliothèque | Responsabilité unique |
|---|---|
| `audio-engine` | capter le micro et l'audio système, les mélanger, les encoder en Opus |
| `vault` | chiffrer chaque segment sur le disque (AES-256-GCM, clé scellée par Windows) |
| `uploader` | envoyer par morceaux, reprendre exactement là où ça s'est arrêté |
| `api-client` | la seule chose qui connaisse l'adresse du serveur et le jeton |

La fenêtre Vue n'a **jamais** le jeton d'authentification : elle demande au
moteur Rust, qui décide. Un bout de HTML qui détiendrait un jeton serait un
jeton exfiltrable.

---

## 4. Le chemin d'une réunion, du micro au compte rendu

```
 1. Le PC déclare la réunion          →  POST /meetings          (statut CREATED)
 2. Le PC enregistre en local, chiffré   (rien ne part, Internet est optionnel)
 3. Le PC demande où déposer          →  POST .../finalize-local (statut UPLOADING)
 4. Le PC écrit l'audio dans R2          (directement, par URL signées)
 5. Le PC dit « j'ai fini »           →  POST .../finalize       (statut QUEUED)
 6. Un ouvrier envoie à AssemblyAI                               (TRANSCRIBING)
 7. L'ouvrier envoie le *texte* à OpenAI                         (ANALYZING)
 8. Le JSON renvoyé est validé par Pydantic                      (COMPLETED)
 9. Le compte rendu devient lisible                              (PUBLISHED)
10. 30 jours plus tard, l'audio est effacé, les textes restent   (AUDIO_PURGED)
```

Trois règles gouvernent cette chaîne :

- **On n'envoie jamais l'audio à un LLM** (ADR-02). Audio → transcription →
  texte → analyse. Un LLM qui « écoute » invente ce qu'il n'a pas compris.
- **Le LLM répond en JSON validé, jamais en texte libre** (ADR-03). Un JSON qui
  ne passe pas la validation est une panne, pas un contenu à afficher.
- **Rien n'est inventé.** Pas de responsable de tâche s'il n'est pas nommé dans
  la transcription ; chaque décision porte l'horodatage de la phrase d'où elle
  vient, pour qu'on puisse vérifier.

Chaque réunion porte un `debug_id` unique qu'on retrouve du PC jusqu'aux appels
fournisseurs (ADR-07) : c'est le numéro à citer dans un ticket de support.

---

## 5. Le parcours utilisateur

### 5.1 Installer, une fois

1. La personne reçoit `NovaBrief_x.y.z_x64-setup.exe` (2,66 Mo).
2. Windows affiche « Windows a protégé votre ordinateur » → *Informations
   complémentaires* → *Exécuter quand même*. **Cet écran disparaîtra** quand le
   certificat de signature sera en place ; tant qu'il est là, c'est le premier
   point d'abandon du produit.
3. L'installation se fait **pour l'utilisateur seul, sans mot de passe
   administrateur** (EF-10) — c'est ce qui permet d'installer sur un poste
   d'entreprise sans passer par la DSI.
4. Une case propose *Démarrer avec Windows*. Elle est **décochée par défaut** :
   une application qui s'installe toute seule dans le démarrage est une
   application qu'on désinstalle.

### 5.2 Se connecter, une fois

Un écran, deux champs : email et mot de passe. Ensuite le poste reste relié à
l'organisation jusqu'à révocation (EF-11) — on ne redemande pas le mot de passe
avant chaque réunion. Techniquement : le jeton de session est scellé par
Windows (DPAPI) et renouvelé en silence.

### 5.3 Enregistrer une réunion

La fenêtre principale est volontairement petite. Trois choses seulement :

- **deux vu-mètres** — micro et audio système ;
- **un chronomètre** ;
- **un bouton**.

Le vu-mètre « audio système » est le plus important de l'écran. S'il reste plat,
la réunion s'enregistre **sans les voix distantes** — le désastre silencieux du
produit. L'interface le dit en toutes lettres au bout de cinq secondes plutôt
que d'afficher une barre immobile que personne n'interprète (EF-13).

Pendant l'enregistrement :

- **Aucun bot ne rejoint l'appel.** Personne dans la réunion ne voit qu'un
  enregistrement a lieu du côté de NovaBrief — c'est à l'utilisateur de le dire,
  et c'est sa responsabilité légale.
- **Internet peut tomber** : l'enregistrement continue, chiffré, en local
  (ADR-05). Ce qui est coupé, c'est l'envoi, pas la capture.
- **La pause n'est pas facturée** (EF-33). Le temps mis en pause est compté à
  part et affiché.
- **Quatre heures maximum**, avec un avertissement cinq minutes avant (EF-34).

### 5.4 Terminer

Un clic. À partir de là, l'utilisateur n'a plus rien à faire :

- l'envoi part tout seul, et **reprend tout seul** s'il échoue — première
  tentative après 1 seconde, puis 2, 4, 8… jusqu'à 5 minutes d'intervalle, sans
  limite de tentatives (EF-18). Fermer le PC ne perd rien : la reprise repart du
  dernier morceau confirmé ;
- la liste « Sur ce poste » montre ce qui est encore dû au serveur, distinct des
  réunions que le serveur connaît déjà. Un envoi qui n'aboutit vraiment pas est
  marqué *Bloqué — action requise*, pas caché.

### 5.5 Lire le compte rendu

Quelques minutes plus tard, la réunion passe à *Prête*. Le compte rendu
s'affiche dans la fenêtre :

- un **titre** proposé par l'analyse ;
- les **participants** repérés ;
- un **résumé** en points ;
- les **décisions**, chacune avec l'instant où elle a été prise ;
- les **tâches**, chacune avec son responsable **s'il a été nommé** — sinon
  « Responsable non nommé », jamais un nom deviné ;
- la **transcription horodatée** complète, repliée par défaut.

Tout est en français ou en anglais selon la langue choisie (aucun texte codé en
dur dans le code).

### 5.6 Ce que le parcours n'a pas encore

- **Le partage.** Aujourd'hui le compte rendu se lit dans la fenêtre du PC qui a
  enregistré. L'envoyer à un collègue par un lien, c'est l'application web (L4).
- **Le test audio guidé** avant la première réunion (L3.7).
- **Le journal local consultable** en cas de problème (L3.8).
- **La mise à jour automatique** (EF-19) : elle demande une paire de clés de
  signature qui n'existe pas encore.
- **La facturation** (L5) : aucun paiement Mobile Money n'est branché.

---

## 6. Ce qui n'a jamais été prouvé

Par honnêteté, et parce que c'est ce qui décide de la suite :

| Ce qui n'est pas prouvé | Pourquoi ça compte |
|---|---|
| **Une réunion n'a jamais fait le trajet complet** jusqu'à `PUBLISHED` | c'est aussi le tout premier appel réel à AssemblyAI et à OpenAI |
| La qualité de transcription (EF-41 : ≤ 15 % d'erreurs) n'est **pas mesurée** | c'est la promesse du produit |
| L'absence d'hallucination (EF-42) n'est **pas mesurée** | une décision inventée décrédibilise tout le reste |
| Aucune sauvegarde du serveur | perdre le VPS, c'est perdre tous les comptes rendus |
| L'installateur n'a pas été essayé sur une machine vierge (sans WebView2) | c'est là que se découvre le dernier prérequis manquant |

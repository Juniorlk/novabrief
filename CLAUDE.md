# NovaBrief — Instructions permanentes pour l'agent de développement

> Renomme ce fichier `CLAUDE.md` ou `AGENTS.md` à la racine du dépôt. Il est lu à chaque session. Le cahier des charges complet est dans `docs/cahier-des-charges.md` (réf. NVK-CDC-NB-2026-V1.0) : c'est la référence, ce fichier en est le résumé opérationnel.

## 1. Ta mission

Tu es l'équipe de développement de **NovaBrief**, un SaaS B2B édité par Novafrik (Cameroun). NovaBrief enregistre une réunion depuis un PC Windows (micro + audio système, sans bot, sans dépendre de Teams/Meet/Zoom), puis produit un compte rendu structuré : titre, participants, résumé, décisions, tâches avec responsable et échéance, transcription horodatée. Le client paie par organisation, en FCFA, par Mobile Money.

Tu réponds de la réussite du produit, pas seulement de la livraison de code. Quand une exigence est ambiguë, tu poses la question avant de coder. Quand une exigence te paraît mauvaise, tu le dis et tu proposes une alternative, puis tu appliques la décision prise.

## 2. Ordre des travaux (ne pas anticiper)

1. **Phase 0 — trois POC, dans l'ordre, chacun avec Go / No-Go** : POC #1 capture audio Windows → POC #2 benchmark transcription FR/EN → POC #3 extraction structurée. Tant que le POC courant n'est pas validé par des mesures, on ne commence pas le suivant ni le SaaS.
2. **Phase 1 — MVP par lots** (voir `docs/tasks/04_APRES_LES_POC_lots_MVP.md`) : socle backend, réunions et pipeline, desktop, web, facturation, back-office, recette.
3. Tout ce qui est marqué Should (S) ou Could (C) dans le cahier des charges attend la fin du MVP.

Tu travailles **une tâche à la fois**, définie par un brief avec des critères d'acceptation. Tu ne crées pas de fonctionnalité qui n'est pas dans le brief courant, même si elle est dans le cahier des charges.

## 3. Règles d'architecture non négociables (ADR)

- **ADR-01** Aucune dépendance directe à un fournisseur d'IA ou de paiement dans le code métier : tout passe par `TranscriptionProvider`, `LLMProvider`, `BillingProvider`. Le fournisseur se change par configuration.
- **ADR-02** Séparation stricte transcription / intelligence : audio → transcription horodatée → analyse LLM sur texte → JSON validé → présentation. On n'envoie jamais l'audio à un LLM.
- **ADR-03** Le LLM renvoie un JSON validé par un schéma Pydantic, jamais du texte libre. Un échec de validation est une erreur, pas un contenu.
- **ADR-04** Isolation multi-tenant par Row-Level Security PostgreSQL (`app.current_org_id` positionnée à chaque transaction) en plus des filtres applicatifs. Ne jamais désactiver RLS, ne jamais utiliser un rôle `BYPASSRLS`.
- **ADR-05** L'enregistrement est local et chiffré ; le réseau est optionnel pendant la réunion. Une coupure Internet ne perd jamais d'audio.
- **ADR-06** L'audio est éphémère (purge à 30 jours par défaut, 90 en Business), les textes sont durables.
- **ADR-07** Chaque réunion porte un `debug_id` de bout en bout (desktop → API → workers → appels fournisseurs → journaux).
- **ADR-08** Le coût réel de chaque réunion (secondes STT, tokens LLM, fournisseur) est écrit dans `usage_ledger`.
- **ADR-09** Prix, quotas et devises sont des données en base (`plans`, `prices`), jamais des constantes dans le code.
- **ADR-10** Monorepo ; les schémas Pydantic sont la source de vérité et génèrent les types TypeScript.
- **Fidélité de l'IA** : ne jamais inventer une décision, une tâche, un responsable ou une date. Un responsable n'est renseigné que s'il est nommé dans la transcription. Chaque élément extrait porte un timestamp source.

## 4. Stack imposée

| Composant | Choix | Interdit |
|---|---|---|
| Desktop | Tauri 2, Rust stable (moteur audio via `windows-rs` / WASAPI, encodage Opus), UI Vue 3 + TypeScript | Electron, pilotes audio virtuels, droits administrateur |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 async, Pydantic v2, Alembic, Celery 5 + Redis | Django, frameworks non listés |
| Base de données | PostgreSQL 16 (RLS, `tsvector`) | MySQL, SQLite en production |
| Stockage objet | Cloudflare R2 (API S3, URL présignées) | Stockage sur le disque du serveur |
| Web | Nuxt 3, Vue 3, TypeScript strict, Tailwind | React |
| Transcription | AssemblyAI (principal), Deepgram (fallback), Whisper (phase 3) | Appel direct hors provider |
| LLM | OpenAI GPT-5 mini (par défaut), GPT-5 nano (tâches simples) | Modèles plus chers sans décision écrite |
| Paiement | Flutterwave (Mobile Money + cartes, Cameroun), Stripe (V1.1) | — |
| Infra | Hetzner Cloud ARM, Docker Compose, Caddy, GitHub Actions | Kubernetes avant le palier 3 |
| Observabilité | Sentry, Prometheus + Grafana, Uptime Kuma, `structlog` | — |
| Email | Resend | — |

## 5. Structure du dépôt

```
novabrief/
├── CLAUDE.md / AGENTS.md          # ce fichier
├── docs/
│   ├── cahier-des-charges.md      # référence complète
│   ├── adr/                       # une décision d'architecture par fichier (ADR-011+)
│   └── tasks/                     # briefs de tâches (POC, lots)
├── apps/
│   ├── desktop/                   # Tauri (src-tauri/ Rust, src/ Vue)
│   │   └── src-tauri/crates/      # audio-engine, vault, uploader, api-client
│   ├── api/                       # FastAPI + workers Celery
│   └── web/                       # Nuxt 3
├── packages/
│   ├── schemas/                   # Pydantic (source de vérité) → génération TS
│   └── ai/                        # providers transcription / LLM, prompts versionnés, évaluation
├── infra/                         # docker-compose, Caddyfile, scripts de déploiement, backups
├── tools/                         # scripts de benchmark, jeux de test, génération de signaux audio
└── .github/workflows/             # CI : lint, tests, build, déploiement
```

## 6. Conventions

- **Langue** : code, identifiants, commits et commentaires techniques en anglais ; documentation produit, briefs et comptes rendus en français ; toute chaîne visible par l'utilisateur passe par i18n (FR et EN dès le premier écran, aucun texte codé en dur).
- **Python** : `ruff` (lint + format), `mypy --strict` sur `packages/` et `apps/api/app/`, `pytest` avec `pytest-asyncio`, couverture ≥ 70 % sur le backend. Pas de logique métier dans les routes : services testables.
- **Rust** : `cargo fmt`, `cargo clippy -D warnings`, tests unitaires sur tout ce qui ne dépend pas du matériel, tests d'intégration marqués `#[ignore]` quand ils exigent un périphérique.
- **TypeScript** : `strict: true`, ESLint, Vitest ; composants Vue en `<script setup lang="ts">`.
- **Git** : Conventional Commits (`feat(api): …`, `fix(desktop): …`), petites PR (< 400 lignes de diff hors généré), une PR par sous-tâche, jamais de commit sur `main` sans CI verte.
- **Secrets** : uniquement dans `.env` (ignoré par Git) et dans les secrets GitHub Actions ; `.env.example` documente chaque variable. Un secret commité est un incident : on le révoque immédiatement.
- **Migrations** : Alembic, une migration par changement de schéma, réversible, jamais de modification manuelle de la base.
- **Journalisation** : `structlog` en JSON, toujours avec `debug_id`, `organization_id`, `meeting_id` quand ils existent ; jamais de contenu de réunion dans les journaux.
- **Erreurs API** : format Problem Details (RFC 9457) avec `debug_id` et code stable traduit côté client.
- **Prompts** : versionnés dans `packages/ai/prompts/` (`extract_v1.md`, …), jamais modifiés sans passer le jeu d'évaluation.

## 7. Definition of Done (une tâche n'est pas finie sans tout cela)

1. Le code fait ce que le brief demande, ni plus ni moins.
2. Tests automatisés écrits et **exécutés** ; la sortie de la commande de test est collée dans le compte rendu.
3. `ruff`, `mypy`, `clippy`, `eslint` passent sans avertissement.
4. Documentation mise à jour : README du module, `.env.example`, ADR si une décision d'architecture a été prise, référence API générée.
5. Chaînes FR et EN présentes pour toute UI.
6. Journalisation avec `debug_id` sur tout nouveau chemin de traitement.
7. Compte rendu de fin de tâche (section 9) rédigé.

## 8. Interdits

- Écrire du code pour une fonctionnalité hors du brief courant (« tant qu'on y est »).
- Appeler AssemblyAI, Deepgram, OpenAI, Flutterwave ou Stripe ailleurs que dans leur provider.
- Coder un prix, un quota, une devise ou un taux dans le code.
- Désactiver ou contourner RLS, la validation Pydantic, la vérification de signature des webhooks ou les tests pour « faire passer » quelque chose.
- Envoyer de l'audio ou du texte de réunion à un service non listé, ou l'écrire dans les journaux.
- Ajouter une dépendance lourde (framework, ORM, runtime) sans ADR.
- Déclarer une tâche testée sans commande exécutée et sortie visible.
- Laisser des `TODO`, du code mort ou des `print` dans une PR.
- Toucher à `main` directement, ou pousser un secret.

## 9. Format du compte rendu de fin de tâche

```
## Compte rendu — <nom de la tâche>
**Fait** : 3 à 6 lignes, factuelles.
**Comment c'est vérifié** : commandes exécutées + sortie (extraits), mesures obtenues vs critères du brief.
**Non fait / partiel** : ce qui manque et pourquoi.
**Risques et doutes** : ce que tu n'as pas pu vérifier, ce qui pourrait casser.
**Décisions prises** : choix techniques faits sans instruction explicite (avec le fichier ADR créé s'il y a lieu).
**Questions pour Novafrik** : ce qui bloque ou nécessite un arbitrage.
**Prochaine étape proposée** : une seule.
```

## 10. Environnement

- Le desktop se développe et se teste **sur Windows 10/11 x64**. Si tu n'es pas sur Windows, dis-le explicitement et fournis les commandes exactes que l'humain doit exécuter, puis attends les résultats.
- Les clés API sont dans `.env` (voir `.env.example`). Si une clé manque, demande-la ; n'invente pas de mode « simulation » silencieux.
- Les jeux de test audio et les transcriptions annotées vivent dans `tools/datasets/` (hors Git si volumineux : documenter où ils sont).

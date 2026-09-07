# Après les POC — découpage du MVP en lots

Chaque lot devient un brief séparé (même format que les POC : objectif, livrables, critères mesurables, interdits). L'agent ne reçoit qu'un lot à la fois. Les références EF-xx et T-xx renvoient au cahier des charges (`docs/cahier-des-charges.md`, §10 et §24). Durées indicatives pour un agent IA travaillant à plein temps, avec revue humaine quotidienne.

| Lot | Contenu | Exigences | Critères de sortie | Durée |
|---|---|---|---|---|
| **L0 Socle** | Monorepo, Docker Compose (Postgres 16, Redis, MinIO en local pour simuler R2), FastAPI squelette, Alembic, `structlog`, Sentry, CI GitHub Actions (lint, tests, build), `.env.example`, Caddy, déploiement `staging` sur un CAX21 Hetzner | §15, §22 | `docker compose up` donne une API `/health` ; CI verte ; staging accessible en HTTPS | 3-4 jours |
| **L1 Comptes & organisations** | Inscription, connexion (Argon2id, JWT 15 min RS256, refresh rotatif), organisation, membres et rôles, profil, RLS sur toutes les tables, journal d'audit, limitation de débit, erreurs Problem Details | EF-01 à EF-06, §17.3, §19, ADR-04 | T-07 (isolation) passe ; T-14 partiel (tokens) ; couverture ≥ 70 % | 4-5 jours |
| **L2 Réunions & pipeline** | Modèle `meetings`, `finalize-local` → URL présignées multipart R2, `finalize` avec checksum, file Celery, `TranscriptionProvider` (POC #2) et pipeline d'analyse (POC #3) branchés, machine à états §11, `usage_ledger`, décrément de quota, WebSocket de statut, purge audio planifiée, relance d'un traitement FAILED | EF-40 à EF-47, §11, §18, ADR-05 à ADR-08 | 200 réunions de test soumises passent en COMPLETED ; T-06 (fallback) et T-11 (charge, P95 ≤ 8 min) passent | 6-8 jours |
| **L3 Desktop** | Tauri 2 : connexion / liaison par code, tray, widget d'enregistrement avec vu-mètres, raccourcis, moteur audio du POC #1, stockage local chiffré (AES-256-GCM + DPAPI), uploader résilient, test audio guidé, `debug_id`, journaux, updater signé, signature Azure Trusted Signing en CI | EF-10 à EF-20, EF-30 à EF-34, §16 | T-01 (crash réseau), T-12 (SmartScreen), T-13 (périphériques) passent ; installateur < 15 Mo | 8-10 jours |
| **L4 Web** | Nuxt 3 : inscription / connexion, tableau de bord, liste des réunions, lecteur (compte rendu + transcription synchronisée + audio, validation / rejet / édition, renommage des locuteurs), export PDF, notifications in-app, i18n FR/EN, responsive, accessibilité | EF-50 à EF-54, EF-58, EF-60, §12.3, §12.4 | T-15 (accessibilité ≥ 90, FR/EN complets) ; tableau de bord < 500 Ko | 6-8 jours |
| **L5 Facturation** | Tables `plans` / `prices` / `subscriptions` / `payments` / `invoices`, `BillingProvider` + `FlutterwaveProvider` (sandbox), checkout Mobile Money et carte, webhook signé + vérification serveur à serveur, idempotence, packs, changement de plan, cycle J-3 / J-1 / grâce / hold / rétrogradation, factures PDF, réconciliation quotidienne, emails Resend | EF-61, EF-62, EF-70 à EF-75, §20, ADR-09 | T-08 (paiement), T-09 (quota), T-10 (cycle simulé) passent | 5-7 jours |
| **L6 Back-office & observabilité** | Back-office Novafrik (organisations, réunions sans contenu, fournisseurs et disjoncteur, tableau de bord unit economics §5.9, gestes commerciaux, accès au contenu tracé), Prometheus + Grafana, Uptime Kuma, alertes, sauvegardes chiffrées vers R2 et test de restauration, runbooks | EF-80 à EF-84, §22.3, §23 | T-16 (restauration < 2 h) ; tableau de bord affiche coût réel par heure | 4-5 jours |
| **L7 Recette & bêta** | Passage complet de T-01 à T-16, jeu d'évaluation de 50 réunions, corrections, canal bêta desktop, installation chez 5 PME pilotes, documentation utilisateur FR/EN, CGV et politique de confidentialité (texte fourni par Novafrik), page de statut | §24, §25 | 16 tests passés ; NovaBrief Score ≥ 85 % ; 5 PME actives 2 réunions / semaine | 2-3 semaines (calendaire) |

Total indicatif : 8 à 10 semaines de développement après les POC, cohérent avec le chronogramme §25.2 (lancement commercial à M5 = février 2027).

## Règles de séquencement

- L0 et L1 avant tout ; L2 et L3 peuvent se chevaucher (L3 dépend de l'API L2 pour l'upload, commencer par la partie locale).
- L4 ne démarre pas avant que L2 produise de vrais comptes rendus : on conçoit l'écran sur des données réelles, pas sur des maquettes.
- L5 avant la bêta payante, pas avant : les 5 PME pilotes sont en gratuit.
- Chaque lot se termine par un déploiement en `staging` et une démonstration à Novafrik.

## Ce qui attend la V1.1 (ne pas laisser l'agent l'anticiper)

Recherche plein texte (EF-55), partage par lien (EF-56), vue « Mes tâches » (EF-57), détection automatique de réunion (EF-21), réunions privées (EF-22), traduction du résumé (EF-46), Stripe (EF-72), codes partenaires (EF-76), connexion Google, export Word / Markdown, envoi automatique aux participants.

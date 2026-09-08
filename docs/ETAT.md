# État du projet NovaBrief

> **À lire en premier au début de chaque session**, après `CLAUDE.md`.
> Ce fichier dit où en est le travail. Le *quoi* est dans
> `docs/cahier-des-charges.md`, le *pourquoi* dans `docs/adr/`, le *comment*
> dans `docs/tasks/`.
>
> Dernière mise à jour : **2026-09-08**.

---

## 1. Où on en est

| Étape | État | Preuve |
|---|---|---|
| Amorçage du dépôt | **fait** | PR #1 |
| POC #1 — capture audio | **livré, No-Go en l'état** | résultats C1-C9 dans `docs/tasks/01_POC1_capture_audio.md` |
| POC #2 — benchmark transcription | **sauté** (décision Novafrik 2026-09-08) | §5 ci-dessous |
| POC #3 — extraction structurée | **sauté** (décision Novafrik 2026-09-08) | §5 ci-dessous |
| Lot L0 — socle backend | **fait sauf staging** | PR #2 ; `docker compose up` répond sur `/health` |
| Lot L1 — comptes & organisations | **partiel** | PR #2, #4, #5/#6 |
| Lots L2 à L7 | non commencés | `docs/tasks/04_APRES_LES_POC_lots_MVP.md` |

### Lot L1 en détail

| Élément | État |
|---|---|
| 7 tables + RLS sur chacune | fait |
| **T-07 (isolation multi-tenant)** | **passe**, en CI à chaque PR |
| Argon2id, JWT RS256, rotation des refresh tokens | fait |
| Endpoints `register` / `token` / `refresh` / `logout` / `me` | fait |
| Limitation de débit (10/min auth, 600/min API) | fait, **en attente de merge** (PR #6) |
| Invitations (EF-03) | **non commencé** — bloqué sur `RESEND_API_KEY` |
| Réinitialisation de mot de passe (EF-02) | **non commencé** — bloqué sur `RESEND_API_KEY` |
| Vérification d'email | non commencé |
| Écriture du journal d'audit | partielle (inscription, connexion, réutilisation de jeton) |

---

## 2. Prochaine étape

**`EmailProvider` puis les invitations (EF-03).**

L'abstraction suit l'ADR-01 : un protocole, une implémentation Resend, une
implémentation console pour le développement. Tout est testable avec un double ;
seul l'envoi réel attend `RESEND_API_KEY`.

Avant ça : **merger la PR #6**, qui rapatrie la limitation de débit dans `main`.

---

## 3. Ce qui bloque, et sur qui

| Bloqué | Ce qu'il faut | Pour |
|---|---|---|
| Invitations, réinitialisation de mot de passe | `RESEND_API_KEY` | finir L1 |
| Staging HTTPS | déploiement sur le VPS OVHcloud | finir L0 |
| Pipeline de traitement | `ASSEMBLYAI_API_KEY`, `DEEPGRAM_API_KEY`, `OPENAI_API_KEY` | L2 |
| Paiement | clés sandbox Flutterwave | L5 |

Le VPS OVHcloud est disponible en SSH ; le déploiement est **volontairement
reporté** (décision Novafrik du 2026-09-08 : « finissons d'abord »). Les accès
ne sont pas dans le dépôt.

---

## 4. Pièges rencontrés, à ne pas redécouvrir

Chacun a coûté du temps et aucun n'était visible en relecture.

### Un superutilisateur PostgreSQL contourne RLS

Les politiques étaient correctes, `ENABLE` et `FORCE` posés, et **toutes les
lignes restaient visibles**. Le rôle applicatif était le propriétaire de la
base. L'ADR-04 interdit `BYPASSRLS`, ce qui est nécessaire mais **pas
suffisant** : un superutilisateur n'a besoin d'aucun attribut pour traverser
toutes les politiques.

L'API se connecte donc avec `novabrief_app` (`NOSUPERUSER`, `NOBYPASSRLS`, pas
de DDL), créé par `infra/postgres-init/01-app-role.sql`. Les migrations gardent
les identifiants du propriétaire.

### Signer et rafraîchir ne peuvent pas être scopés à un tenant

L'organisation est inconnue tant que l'utilisateur ou le jeton n'est pas trouvé,
et sous RLS ces recherches ne retournent rien — **personne ne pourrait se
connecter**. Deux fonctions `SECURITY DEFINER` étroites
(`auth_lookup_user`, `auth_lookup_refresh_token`) sont la voie de passage, avec
`search_path` épinglé.

Pour l'inscription : l'UUID de l'organisation est généré côté application, donc
la session est scopée **avant** l'insertion et le `WITH CHECK` passe par ses
propres règles.

### Une révocation dans une transaction qui échoue est annulée

À la détection de réutilisation d'un refresh token, la révocation était suivie
d'une exception — qui annulait la transaction. Le jeton de l'attaquant survivait
à l'événement censé le tuer. La révocation est maintenant **validée avant** que
l'erreur ne se propage.

### Les erreurs de validation Pydantic contiennent la valeur rejetée

Un mot de passe trop court revenait dans le corps de la réponse 422. Seuls le
champ, le type et le message sortent (`_safe_validation_errors` dans
`app/errors.py`), et un test vérifie que la valeur soumise n'apparaît jamais.

### S'ouvrir lentement vaut presque se fermer

Redis absent, le limiteur laissait passer — mais après **4 secondes** d'attente
par requête. Une panne Redis aurait entraîné l'API. Timeouts à 250 ms et
disjoncteur après trois échecs consécutifs.

### La perte audio est invisible

Sur 60 min, 10,25 % de l'audio a disparu pendant que Windows ne signalait que
2 à 5 discontinuités et que le fichier gardait la bonne durée. Le compteur de
drapeaux WASAPI **n'est pas** une mesure de perte. Détail dans
`docs/tasks/02_correctifs_capture_longue_duree.md`.

---

## 5. Décisions de Novafrik prises en session

Elles ne sont pas dans le cahier des charges et priment sur lui.

| Date | Décision |
|---|---|
| 2026-09-07 | C7 assoupli à < 10 % CPU, resserré à < 50 Mo RAM ; segments 5-10 s ; C6 non bloquant |
| 2026-09-07 | Correctifs de capture longue durée **différés** malgré le No-Go du POC #1 |
| 2026-09-08 | Hébergement **OVHcloud** au lieu de Hetzner → ADR-011 |
| 2026-09-08 | **POC #2 et #3 sautés**, passage direct au MVP |
| 2026-09-08 | Déploiement reporté : finir le code d'abord |

**Le risque « l'IA invente une décision » (risque n°2 du cahier des charges)
reste non mesuré.** Il se manifestera en recette plutôt qu'en phase 0.

---

## 6. Comment vérifier avant de pousser

La CI a attrapé quatre fois des dépendances installées à la main et déclarées
nulle part. La parade tient en une commande : **reproduire un environnement
neuf**, ce que fait un runner.

```bash
py -3.12 -m venv /tmp/clean && /tmp/clean/Scripts/python.exe -m pip install -e ".[dev]"
/tmp/clean/Scripts/python.exe -m ruff check . && /tmp/clean/Scripts/python.exe -m mypy
/tmp/clean/Scripts/python.exe -m pytest -q
```

Les tests base de données ont besoin de PostgreSQL **et** de Redis :

```bash
cd infra && docker compose up -d postgres redis
cd apps/api && DATABASE_ADMIN_URL=... python -m alembic upgrade head
```

Un test qui *skippe* n'est pas un test qui passe : la CI échoue si la suite
base de données rapporte un skip.

---

## 7. Dette assumée

- **PR trop grosses.** `CLAUDE.md` §6 demande < 400 lignes ; la PR #2 en faisait
  8069. Corrigé depuis, mais à surveiller.
- **`main` n'est pas protégée** sur GitHub. La règle « jamais de commit sans CI
  verte » n'est donc qu'une intention.
- **Le mot de passe du rôle applicatif est en dur** dans le script d'init pour
  le développement. En production il doit venir de l'environnement.
- **Aucune paire de clés RS256** n'existe. L'API refuse de démarrer sans, ce qui
  est voulu.
- `nb-testsignal` et `measure_drift.py` restent des squelettes : la dérive est
  mesurée par horodatage QPC, ce qui s'écarte du brief du POC #1.

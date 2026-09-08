# État du projet NovaBrief

> **À lire en premier au début de chaque session**, après `CLAUDE.md`.
> Ce fichier dit où en est le travail. Le *quoi* est dans
> `docs/cahier-des-charges.md`, le *pourquoi* dans `docs/adr/`, le *comment*
> dans `docs/tasks/`.
>
> Dernière mise à jour : **2026-09-08** (EF-04 et EF-05 faits ; reste EF-06 et la vérification d'email).

---

## 1. Où on en est

| Étape | État | Preuve |
|---|---|---|
| Amorçage du dépôt | **fait** | PR #1 |
| POC #1 — capture audio | **livré, No-Go en l'état** | résultats C1-C9 dans `docs/tasks/01_POC1_capture_audio.md` |
| POC #2 — benchmark transcription | **sauté** (décision Novafrik 2026-09-08) | §5 ci-dessous |
| POC #3 — extraction structurée | **sauté** (décision Novafrik 2026-09-08) | §5 ci-dessous |
| Lot L0 — socle backend | **fait sauf staging** | PR #2 ; `docker compose up` répond sur `/health` |
| Lot L1 — comptes & organisations | **partiel** (EF-01 a EF-05 faits ; EF-06 et la verification d'email restent) | PR #2, #4, #6, #7 |
| Lots L2 à L7 | non commencés | `docs/tasks/04_APRES_LES_POC_lots_MVP.md` |

### Lot L1 en détail

| Élément | État |
|---|---|
| 7 tables + RLS sur chacune | fait |
| **T-07 (isolation multi-tenant)** | **passe**, en CI à chaque PR |
| Argon2id, JWT RS256, rotation des refresh tokens | fait |
| Endpoints `register` / `token` / `refresh` / `logout` / `me` | fait |
| Limitation de débit (10/min auth, 600/min API) | fait |
| `EmailProvider` (Resend / console / enregistreur) | fait |
| Invitations (EF-03) : inviter, accepter, révoquer, lister | fait |
| Réinitialisation de mot de passe (EF-02) | fait |
| EF-02 vérification d'email à l'inscription | **non fait** — prouvée seulement en acceptant une invitation |
| EF-04 modification du profil (nom, langue, fuseau) | fait — `PATCH /api/v1/me` |
| EF-05 paramètres d'organisation (nom, RCCM, langue, rétention, lexique) | fait — `PATCH /api/v1/organizations/current`, Admin ou Owner |
| EF-06 export complet et suppression de l'organisation | **non fait** |
| Journal d'audit | écrit sur inscription, connexion, réutilisation de jeton, invitation, adhésion, révocation, réinitialisation, `profile.updated`, `organization.updated` |
| Envoi réel d'emails | clé reçue le 2026-09-08 et posée dans `.env` ; **pas encore branchée dans `infra/docker-compose.yml`** (à faire avec EF-02) |

---

## 2. Prochaine étape

**EF-06 : export et suppression de l'organisation**, sur une nouvelle branche
partant de `main` une fois la PR de `feat/profile-and-organization-settings`
mergée.

Arbitrage rendu par Novafrik le 2026-09-08 : **export JSON + audio maintenant,
PDF reporté au lot L4**, suppression définitive avec **7 jours de
rétractation**. La colonne `organizations.deletion_requested_at` existe déjà et
attend son endpoint.

Ensuite, dans cet ordre :

1. EF-02 — vérification d'email à l'inscription. Aujourd'hui l'adresse n'est
   prouvée qu'en acceptant une invitation ; une inscription directe laisse
   `email_verified_at` à NULL sans jamais rien envoyer. C'est aussi le moment
   de passer `RESEND_API_KEY` et `EMAIL_FROM` au conteneur `api` dans
   `infra/docker-compose.yml`, ce qui n'est pas fait.
2. Le brief `docs/tasks/05_L2_reunions_pipeline.md`, puis le lot L2.

---

## 3. Ce qui bloque, et sur qui

| Bloqué | Ce qu'il faut | Pour |
|---|---|---|
| Envoi à de vrais destinataires | un **domaine vérifié chez Resend** — la clé reçue utilise `onboarding@resend.dev`, qui n'écrit qu'à l'adresse du compte | mettre L1 en service |
| Staging HTTPS | un **nom de domaine** pointant sur le VPS OVHcloud | finir L0 |
| Pipeline de traitement | `ASSEMBLYAI_API_KEY`, `OPENAI_API_KEY` (Deepgram reçue le 2026-09-08) | L2 |
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

### `poolclass=None` ne désactive pas le pool

Ça signifie « le pool par défaut ». Des connexions survivaient à leur test et
remontaient au ramasse-miettes comme exceptions non levables, ce qui faisait
échouer un test différent à chaque exécution. Utiliser `NullPool`.

### L'application ne fermait pas son pool Redis

Le `lifespan` libérait le moteur de base et laissait les sockets du limiteur
ouvertes. Sur un redémarrage progressif, autant de connexions que Redis croit
encore vivantes. Trouvé en traquant l'instabilité de la suite, pas en relecture.

### Une dépendance runtime déclarée en `dev` casse l'image, pas les tests

`httpx` sert à `ResendProvider` en production mais n'était déclaré que dans
l'extra `dev`. Les tests l'avaient pour leur propre transport ASGI ; l'image
Docker ne l'installait pas et l'API ne démarrait plus. Seul le job « Docker
stack » l'a vu.

### Windows n'a pas de base de fuseaux horaires

`zoneinfo.ZoneInfo("Africa/Douala")` lève `ZoneInfoNotFoundError` sur un Python
Windows : la bibliothèque standard lit la base du système, et Windows n'en a
pas. Le paquet `tzdata` (données seules) est donc une dépendance **runtime**,
pas de développement — même piège que `httpx` plus haut.

Le fond du problème est ailleurs : un fuseau non validé est accepté à
l'inscription et n'explose que des mois plus tard, dans une tâche planifiée
(rappels d'échéance à 08:00, section 20.3), loin de qui l'a saisi. D'où le type
`Timezone` dans `packages/schemas/auth.py`, appliqué partout où le champ existe.

### `PATCH` : « absent » et « mis à vide » ne sont pas la même chose

Avec des champs `X | None = None`, un `None` peut vouloir dire « je ne touche
pas » ou « efface ». Les routes passent donc `model_dump(exclude_unset=True)` au
service, qui ne voit que ce que le client a réellement envoyé. Un `null`
explicite n'est accepté que sur `legal_id`, seule colonne nullable ; ailleurs
c'est un 422. Sans ça, un formulaire web qui renvoie tout son état écraserait
avec des valeurs vides ce que l'utilisateur n'a pas touché.

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
| 2026-09-08 | **Campagne des 200 réunions annulée.** Le pipeline L2 est construit et testé avec des doubles ; aucun appel réel aux fournisseurs d'IA. Le critère de sortie « 200 réunions en COMPLETED » et le test T-11 ne seront donc pas prononcés. |
| 2026-09-08 | Déploiement sur le VPS OVHcloud **après** la fin du lot L2 |
| 2026-09-08 | **EF-06** : export **JSON + audio** maintenant, **PDF reporté au lot L4** ; suppression définitive avec **7 jours de rétractation** |
| 2026-09-08 | Clés `RESEND_API_KEY` et `DEEPGRAM_API_KEY` fournies |

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
- **La fixture de base de données est copiée dans cinq fichiers de tests**
  (`test_auth_endpoints`, `test_auth_service`, `test_members`,
  `test_rls_isolation`, `test_organization_settings`) : 45 lignes identiques à
  chaque fois. À remonter dans `conftest.py` par une tâche dédiée, pas au
  détour d'un lot.
- **Les clés API ont transité par la conversation.** Elles vivent dans `.env`,
  ignoré par Git, mais doivent être **régénérées par Novafrik avant la mise en
  production**.
- `nb-testsignal` et `measure_drift.py` restent des squelettes : la dérive est
  mesurée par horodatage QPC, ce qui s'écarte du brief du POC #1.

# État du projet NovaBrief

> **À lire en premier au début de chaque session**, après `CLAUDE.md`.
> Ce fichier dit où en est le travail. Le *quoi* est dans
> `docs/cahier-des-charges.md`, le *pourquoi* dans `docs/adr/`, le *comment*
> dans `docs/tasks/`.
>
> Dernière mise à jour : **2026-09-10** (lot L2 déployé, puis **audit de
> cohérence** : sept défauts trouvés et corrigés, PR #22 à #24).

---

## 1. Où on en est

| Étape | État | Preuve |
|---|---|---|
| Amorçage du dépôt | **fait** | PR #1 |
| POC #1 — capture audio | **livré, No-Go en l'état** | résultats C1-C9 dans `docs/tasks/01_POC1_capture_audio.md` |
| POC #2 — benchmark transcription | **sauté** (décision Novafrik 2026-09-08) | §5 ci-dessous |
| POC #3 — extraction structurée | **sauté** (décision Novafrik 2026-09-08) | §5 ci-dessous |
| Lot L0 — socle backend | **fait**, staging inclus | PR #2 ; `https://api.novabrief.cloud/health` répond |
| Lot L1 — comptes & organisations | **terminé** (EF-01 a EF-06) | PR #2, #4, #6, #7, #8, #9, #10 |
| Lot L2 — réunions & pipeline | **terminé** (L2.1 à L2.8) | `docs/tasks/05_L2_reunions_pipeline.md` ; PR #11 à #17 |
| Lots L3 à L7 | non commencés | `docs/tasks/04_APRES_LES_POC_lots_MVP.md` |
| Audit de cohérence L1+L2 | **fait** | §2 bis ci-dessous ; PR #22, #23, #24 |

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
| EF-02 vérification d'email à l'inscription | fait — lien 24 h, `POST /auth/email/verify` et `/auth/email/resend` |
| EF-04 modification du profil (nom, langue, fuseau) | fait — `PATCH /api/v1/me` |
| EF-05 paramètres d'organisation (nom, RCCM, langue, rétention, lexique) | fait — `PATCH /api/v1/organizations/current`, Admin ou Owner |
| EF-06 export complet et suppression de l'organisation | fait — export JSON, suppression Owner avec 7 jours de rétractation, job de purge |
| Journal d'audit | écrit sur inscription, connexion, réutilisation de jeton, invitation, adhésion, révocation, réinitialisation, `profile.updated`, `organization.updated` |
| Envoi réel d'emails | **opérationnel** — domaine `novabrief.cloud` vérifié chez Resend, clé dans `.env`, service `api` branché dessus. Un envoi réel a été accepté par le fournisseur le 2026-09-09. |

---

## 2. Prochaine étape

**Lot L3 — application desktop Windows.** C'est elle qui produit l'audio que
tout le reste attend.

**À faire avant de la mettre entre les mains d'une PME pilote** : les
correctifs de capture longue durée
(`docs/tasks/02_correctifs_capture_longue_duree.md`), différés le 2026-09-07.
Sur 60 minutes, 10,25 % de l'audio disparaît et la perte est **invisible** —
le fichier a la bonne durée, le compte rendu est simplement incomplet, et le
client ne comprend jamais pourquoi.

### En production depuis le 2026-09-10

| | |
|---|---|
| `https://api.novabrief.cloud` | répond, certificat Let's Encrypt |
| `https://app.novabrief.cloud` | certificat obtenu ; répond 503 « pas encore déployée » jusqu'au lot L4 |
| Conteneurs | api, worker, beat, caddy, postgres, redis — tous sains |
| Migrations | les 10 appliquées |
| Consommation | ~374 Mo de RAM sur 3,7 Gio ; 6,4 Go de disque sur 38 |

Éprouvé de bout en bout par l'API publique : inscription, jeton signé,
isolation RLS, et **un email de vérification réellement envoyé** (Resend a
répondu 200). La donnée d'essai a été supprimée.

**Ce qui n'existe pas encore et qui compte** : les sauvegardes (lot L6). Tant
qu'elles ne tournent pas, une perte du VPS est une perte de la base — les
comptes rendus, pas l'audio qui vit chez R2. C'est le prix assumé du choix
« PostgreSQL en conteneur » plutôt que managé.

---

## 2 bis. L'audit de cohérence du 2026-09-10

Demandé par Novafrik après le déploiement : *« assure-toi que les
fonctionnalités sont okay d'un point de vue technique et aussi logique »*.

Les 313 tests passaient, `ruff` et `mypy --strict` étaient propres, et
**sept défauts réels** dormaient dessous. Aucun n'était visible en relecture :
dans chaque cas le code *disait* faire la bonne chose.

| | Défaut | Ce que ça coûtait |
|---|---|---|
| A | `is_private` appliqué nulle part | tout membre lisait les réunions privées de ses collègues, audio compris |
| B | `retry` retranscrivait puis violait `transcripts_meeting_unique` | le bouton Réessayer échouait à coup sûr, après trois transcriptions payées |
| C | la tâche partait **avant le commit** | le worker lisait l'ancien état, refusait, la réunion restait en `QUEUED` à vie |
| D | supprimer une organisation gardait ses audios | EF-06 promet l'effacement définitif ; les clés n'étaient plus référencées nulle part |
| E | `cancel` recevait un `storage` inutilisé | upload multipart facturé indéfiniment |
| F | la purge ne visait que `PUBLISHED` | un enregistrement `FAILED`, `CANCELLED` ou supprimé restait pour toujours |
| G | quota dépassé à la publication | réunion figée en `ANALYZING`, LLM repayé, hors d'atteinte du bouton Réessayer |

**Le motif commun** : B, C et G étaient déjà décrits — et évités — dans les
commentaires de `app/tasks/pipeline.py`. C'est l'API et le service qui ne
suivaient pas la règle que les workers énoncent. Un commentaire juste ne
protège que le fichier où il est écrit.

Deuxième motif, pour D, E et F : une docstring qui promet un effacement, et
aucun code dessous. Rien n'échouait, rien n'était journalisé, et la facture
montait.

**Ce qu'il faut retenir pour la suite** : la suite de tests ne prouvait rien de
tout ça parce qu'elle vérifiait *ce que le code fait*, jamais *ce que le code
promet*. Les tests ajoutés sont écrits dans l'autre sens — le plus utile ne
demande pas « la tâche a-t-elle été postée » mais **« qu'aurait vu un worker
démarrant à cet instant »**, et il répond `UPLOADING` dès qu'on retire le
correctif.

**Vérifié** : 339 tests, dont chaque correctif prouvé en retirant le correctif
et en constatant l'échec.

---

## 3. Ce qui bloque, et sur qui

| Bloqué | Ce qu'il faut | Pour |
|---|---|---|
| Second fournisseur de transcription | une **vraie clé Deepgram** : celle fournie le 2026-09-08 était en fait la clé AssemblyAI (Deepgram la rejette en 401). AssemblyAI et OpenAI sont vérifiées et en place. | L2.5 |
| Stockage des audios en production | un **Account API token** R2 (Object Read & Write, portée `novabrief-audio`) — MinIO tient le rôle en local et les tests passent contre lui | déploiement |
| Paiement | clés sandbox Flutterwave | L5 |

**Question ouverte (audit du 2026-09-10)** : faut-il **exiger un email vérifié**
avant de laisser un compte enregistrer une réunion ? Aujourd'hui non : la
vérification existe, elle est envoyée, elle fonctionne, et elle n'ouvre aucune
porte. EF-02 ne demande explicitement que le lien à usage unique, donc rien
n'est en défaut — mais c'est un arbitrage produit, pas un oubli technique, et
il revient à Novafrik. Bloquer protège d'une inscription à une adresse qu'on ne
possède pas ; ne pas bloquer évite d'arrêter net un client pilote dont l'email
est tombé en spam.

**Débloqué le 2026-09-09** : domaine `novabrief.cloud` (DNS chez OVH,
`novabrief.cloud`, `www`, `api` et `app` pointent sur le VPS), domaine vérifié
chez Resend (CNAME `send`, DKIM `resend._domainkey`, DMARC `p=none`), MX et SPF
racine OVH laissés intacts.

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

### Une tâche postée avant le commit disparaît sans bruit

Le message part vers Redis immédiatement ; un worker peut le consommer en
quelques millisecondes et lire une ligne qui porte encore l'**ancien** état. Il
fait alors la bonne chose — il refuse d'agir sur une réunion qui n'est pas là
où il l'attend — et **plus rien ne traite cette réunion**. Aucune exception,
aucun journal d'erreur, aucun réessai, et l'API a répondu 202.

Les envois passent par `deps.after_commit`. Le test ne vérifie pas qu'une tâche
a été postée : il lit la ligne **depuis une autre connexion, dans un autre
fil**, au moment exact de l'envoi.

### Un `.env` de développement peut contenir de vraies clés de production

Un test de la tâche de purge appelait `delete_prefix` sur le **vrai bucket
Cloudflare**, parce que `.env` contenait les clés R2 réelles. Il passait en
local et échouait en CI — l'inverse du signal utile.

Rien n'a été perdu (les préfixes appartenaient à des organisations inventées),
mais ce mode de panne **n'a aucun témoin** : un préfixe qui aurait
correspondu serait parti, et rien n'aurait permis de s'en apercevoir.

Le stockage vient donc de `maintenance.storage_provider()`, qu'un test
remplace. Corollaire : la suite se vérifie en **déplaçant `.env`**, ce qui est
la seule configuration fidèle à la CI.

### `session.delete()` detache les enfants au lieu de les supprimer

SQLAlchemy charge les lignes liees et emet `UPDATE users SET
organization_id = NULL` plutot que de laisser la cascade `ON DELETE CASCADE`
faire son travail. Sur la purge EF-06 ca aurait laisse des membres orphelins
tout en rapportant un succes — le critere « aucune donnee ne subsiste » aurait
ete faux en silence.

**C'est RLS qui l'a attrape** : la politique a refuse l'`UPDATE`. Sans elle le
bug passait inapercu. La relation declare maintenant `passive_deletes=True` et
la purge utilise un `delete()` Core.

Corollaire : le test compte les lignes survivantes **avec le role proprietaire**,
pas via l'API — a travers RLS, « rien » veut seulement dire que l'isolation
fonctionne, pas que les lignes ont disparu.

### Un import circulaire qu'aucun test ne pouvait voir

Construire l'application Celery avec `autodiscover_tasks(force=True)` importe
les tâches **pendant** la construction, et chaque tâche réimporte `celery_app`.
Les conteneurs worker et beat refusaient de démarrer.

En pytest ça ne se voit jamais : le fichier de test importe la tâche en premier,
donc `app.worker` est déjà chargé quand le cycle se refermerait. **C'est le
conteneur qui l'a attrapé.** Les tâches sont maintenant listées dans `include`
et importées à la finalisation, et un test les importe dans un sous-processus
nu, comme le fait un vrai worker.

### `model_copy` ne valide pas

`result.model_copy(update={"utterances": [dicts]})` accepte des dictionnaires
là où le modèle déclare des `Utterance`, sans rien dire. L'erreur ne sort que
bien plus loin, chez le premier qui lit la valeur comme un modèle.

### Un test qui saute n'est pas un test qui passe

La CI n'avait pas de MinIO : les sept tests de stockage — ceux qui prouvent
qu'une URL présignée est correctement signée, ce qu'aucun double ne peut
vérifier — **sautaient depuis le lot L2.2** en affichant du vert. Le garde-fou
anti-skip couvre maintenant `test_storage.py` en plus de la suite RLS.

### L'analyseur `env_file` de Docker Compose n'est pas celui de python-dotenv

Il **ne retire pas** un commentaire de fin de ligne : un `.env` recopié depuis
`.env.example` livre `LLM_TIMEOUT_SECONDS=  # 120` à l'application comme la
chaîne « # 120 ». Et il transmet une valeur vide comme une chaîne vide, qui
**écrase la valeur par défaut** au lieu de s'y rabattre.

Règle : une clé sans valeur est **absente**, pas vide.
`infra/deploy/generate-secrets.sh` normalise le fichier à chaque passage.

### `awk '{printf "%s\n", $0}'` n'échappe pas ce qu'on croit

awk traite l'échappement **deux fois** — à la lecture de la chaîne, puis dans
printf. La sortie contient de vraies nouvelles lignes, pas des `
` littéraux.
Le script annonçait « JWT_PRIVATE_KEY generated » en écrivant une valeur que le
fichier ne pouvait pas porter.

### `docker compose -f infra/...` lit son `.env` à côté du fichier compose

Pas à la racine du dépôt. Chaque `${VAR:?}` échoue en accusant la variable
plutôt que le chemin — pendant que les entrées `env_file:` *à l'intérieur* du
même fichier, qui sont un autre mécanisme, fonctionnent. Passer toujours par
`infra/deploy/compose.sh`.

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
| 2026-09-10 | Dépôt GitHub passé en **public** ; historique vérifié, aucune clé n'y figure |
| 2026-09-10 | Clés de développement réutilisées en production, à remplacer plus tard (décision Novafrik) |
| 2026-09-09 | **Cloudflare R2 retenu** plutôt que MinIO sur le VPS : à ce volume R2 coûte quelques centimes par mois, et sortir l'audio du disque qui porte PostgreSQL vaut plus que l'économie |
| 2026-09-09 | Domaine **`novabrief.cloud`** (OVH) ; DNS Resend configure ; redirection `contact@` vers l'adresse personnelle faute de boite OVH disponible |

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

**Et déplace `.env` le temps de la vérification.** La CI n'en a pas ; une
machine de développement en a un qui contient de vraies clés fournisseurs. Un
test de purge est ainsi passé en local **en effaçant un préfixe du vrai bucket
Cloudflare**, et n'a échoué qu'en CI — l'inverse du signal utile.

```bash
mv .env .env.hidden && python -m pytest -q ; mv .env.hidden .env
```

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
  — sept, désormais (`test_auth_endpoints`, `test_auth_service`, `test_members`,
  `test_rls_isolation`, `test_organization_settings`,
  `test_organization_lifecycle`, `test_email_verification`) : 45 lignes
  identiques à chaque fois. À remonter dans `conftest.py` par une tâche dédiée, pas au
  détour d'un lot.
- **Les clés API ont transité par la conversation.** Elles vivent dans `.env`,
  ignoré par Git, mais doivent être **régénérées par Novafrik avant la mise en
  production**.
- **Les emails transactionnels sont en français codé en dur.** `CLAUDE.md` §6
  demande que toute chaîne visible passe par i18n, et `users.locale` existe
  déjà. À reprendre quand le lot L4 apportera l'i18n côté serveur.
- **Une réunion en `QUOTA_HOLD` n'a aucune sortie.** Seul le webhook de
  paiement du lot L5 peut la relancer, et il n'existe pas. Sans effet
  aujourd'hui — aucun quota n'est assigné avant L5, donc rien n'y entre — mais
  ça devient un blocage le jour où les plans arrivent.
- **La vérification d'email n'est exigée nulle part.** Un compte non vérifié
  utilise toute l'API. EF-02 ne demande explicitement que le lien à usage
  unique, donc ce n'est pas un manquement au cahier des charges — c'est un
  arbitrage produit à trancher (question ouverte, §3).
- **L'URL présignée remise au fournisseur de transcription est celle qui a
  servi à vérifier l'empreinte.** Sur un enregistrement de deux heures, la
  vérification peut consommer une bonne part des 15 minutes de validité. Une
  seconde signature juste avant l'appel coûterait presque rien.
- **Aucun outil ne retrouve un objet orphelin dans R2.** Si un préfixe survivait
  à une purge d'organisation, plus rien en base ne le nomme.
- `nb-testsignal` et `measure_drift.py` restent des squelettes : la dérive est
  mesurée par horodatage QPC, ce qui s'écarte du brief du POC #1.

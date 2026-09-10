# Déploiement

Mode opératoire pour installer NovaBrief sur un serveur neuf, et pour le mettre
à jour ensuite. Cible actuelle : un VPS OVHcloud, Ubuntu, 2 vCPU, 3,7 Gio
(ADR-011).

## Première installation

```bash
# 1. Préparer la machine : Docker, swap, pare-feu, fail2ban.
sudo bash infra/deploy/prepare-host.sh
#    Puis se reconnecter : l'appartenance au groupe docker ne prend effet
#    qu'à la session suivante.

# 2. Récupérer le code.
git clone https://github.com/Juniorlk/novabrief.git /srv/novabrief
cd /srv/novabrief

# 3. Générer les secrets que la machine peut inventer elle-même.
bash infra/deploy/generate-secrets.sh

# 4. Compléter à la main ce qu'aucun script ne peut inventer :
#    les clés fournisseurs et les domaines. Voir plus bas.
nano .env

# 5. Démarrer.
bash infra/deploy/compose.sh up -d --build

# 6. Appliquer les migrations. Alembic lit DATABASE_ADMIN_URL tout seul :
#    le role applicatif n'a deliberement pas le DDL.
docker compose -f infra/docker-compose.prod.yml run --rm \
    --workdir /srv/apps/api api python -m alembic upgrade head
```

## Toujours passer par `compose.sh`

`docker compose` cherche le `.env` dont il tire `${VAR}` **à côté du fichier
compose**, donc dans `infra/`, pas à la racine où il se trouve. Sans
`--env-file .env`, chaque `${POSTGRES_PASSWORD:?}` échoue et le message accuse
la variable plutôt que le chemin. Les entrées `env_file:` *à l'intérieur* du
compose sont un autre mécanisme et fonctionnent dans les deux cas — c'est
précisément ce qui rend la panne déroutante.

`infra/deploy/compose.sh` passe les bons drapeaux et se place au bon endroit.

## Ce que `generate-secrets.sh` fait, et ne fait pas

Il génère ce qu'une machine peut inventer : mots de passe PostgreSQL, clé
applicative, paire RS256. Ces valeurs **ne quittent jamais le serveur** — elles
ne sont ni transmises, ni affichées.

**Il n'écrase jamais une valeur déjà présente.** Faire tourner un secret est un
acte délibéré aux conséquences réelles : une nouvelle clé JWT invalide toutes
les sessions ouvertes, un nouveau mot de passe de base coupe l'API jusqu'à ce
que PostgreSQL soit d'accord. Un script qui régénérerait à chaque passage
rendrait un redéploiement dangereux.

Il ne génère pas les clés fournisseurs. Personne ne peut les inventer.

Il **normalise aussi le fichier** à chaque passage, et c'est nécessaire :
l'analyseur `env_file` de Docker Compose n'est pas celui de python-dotenv. Il
ne retire pas un commentaire de fin de ligne, donc `LLM_TIMEOUT_SECONDS=  # 120`
arrive à l'application comme la chaîne `« # 120 »` ; et il transmet une valeur
vide comme une chaîne vide, qui écrase la valeur par défaut au lieu de s'y
rabattre. La règle est donc : **une clé sans valeur est absente, pas vide.**
Un `.env` recopié à la main depuis `.env.example` est réparé plutôt que
refusé.

## Ce qu'il faut remplir à la main dans `.env`

| Variable | Où la trouver |
|---|---|
| `API_DOMAIN` | `api.novabrief.cloud` |
| `WEB_DOMAIN` | `app.novabrief.cloud` |
| `ACME_EMAIL` | l'adresse qui reçoit les avis d'expiration Let's Encrypt |
| `RESEND_API_KEY` | console Resend |
| `ASSEMBLYAI_API_KEY`, `DEEPGRAM_API_KEY` | consoles respectives |
| `OPENAI_API_KEY` | console OpenAI |
| `R2_*` | Cloudflare R2, **Account API token**, portée `novabrief-audio` |

`ENVIRONMENT=prod` et `LOG_FORMAT=json` : les journaux de production sont
analysés et corrélés par `debug_id` (ADR-07), un format console casserait ça
silencieusement — et `config.py` refuse d'ailleurs cette combinaison.

## Les clés RS256 sont écrites sur une ligne

Un PEM est multi-lignes, et l'analyseur `env_file` de Docker Compose ne
transporte pas ça de façon fiable. Le script les écrit donc avec `\n` échappés,
et `app.config` les rétablit à la lecture. Un PEM avec de vraies nouvelles
lignes fonctionne aussi : les deux formes sont acceptées et testées.

## Mise à jour

```bash
cd /srv/novabrief
git pull
bash infra/deploy/compose.sh up -d --build
docker compose -f infra/docker-compose.prod.yml run --rm \
    --workdir /srv/apps/api api python -m alembic upgrade head
```

Les migrations sont réversibles et appliquées une par une. Une migration qui
échoue laisse la base dans son état précédent ; l'ancienne image tourne encore.

## Vérifier

```bash
bash infra/deploy/compose.sh ps
curl -fsS https://api.novabrief.cloud/health
bash infra/deploy/compose.sh logs --tail=50 worker
```

Le worker doit lister ses quatre tâches (`transcribe_meeting`,
`analyse_meeting`, `purge_organizations`, `purge_audio`) et dire `ready`.

## Ce qui n'est pas encore là

**Les sauvegardes.** Elles arrivent au lot L6 (chiffrées vers R2, RPO 6 h,
RTO 2 h). Tant qu'elles ne tournent pas, **une perte du VPS est une perte de la
base** : les comptes rendus, pas l'audio, qui vit chez R2. C'est le risque
assumé du choix « PostgreSQL en conteneur » plutôt que managé, et c'est ce qui
fait des sauvegardes une priorité et non un lot lointain.

**L'application web** (lot L4). `app.novabrief.cloud` obtient son certificat et
répond une phrase expliquant qu'elle n'est pas déployée, plutôt qu'un 502.

**L'authentification SSH par mot de passe est encore active** sur l'hôte. Le
pare-feu limite l'exposition à 22, 80 et 443, et fail2ban freine les tentatives,
mais la désactiver (`PasswordAuthentication no`) reste à faire une fois qu'on
est certain que l'accès par clé fonctionne pour tout le monde.

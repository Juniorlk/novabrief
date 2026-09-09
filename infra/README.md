# infra — pile locale et déploiement

Docker Compose pour le développement, `Caddyfile` pour le reverse proxy, script
d'initialisation PostgreSQL. L'hébergement cible est **OVHcloud** (ADR-011, qui
remplace Hetzner).

## Démarrer la pile

```bash
docker compose -f infra/docker-compose.yml up -d
cd apps/api && DATABASE_ADMIN_URL=... python -m alembic upgrade head
```

Quatre services : PostgreSQL 16, Redis, MinIO (qui tient le rôle de Cloudflare
R2 en local) et l'API.

## Configuration et secrets

Le service `api` lit le fichier **`.env` à la racine du dépôt**, ignoré par Git.
Il est déclaré `required: false` : sans lui la pile démarre quand même, et le
fournisseur d'email retombe sur `ConsoleProvider`, qui imprime les messages au
lieu de les envoyer. Un développeur sans clé voit donc quand même les liens
d'invitation.

Les valeurs du bloc `environment:` du compose l'emportent sur celles du `.env` —
c'est voulu : les URL de base de données et de Redis doivent pointer sur les
noms de services Docker (`postgres`, `redis`), pas sur le `localhost` du `.env`
qui sert aux tests lancés depuis la machine hôte.

Aucun secret n'est écrit dans ce dossier. `.env.example` documente chaque
variable.

## Le rôle applicatif

`postgres-init/01-app-role.sql` crée `novabrief_app` : `NOSUPERUSER`,
`NOBYPASSRLS`, sans DDL. L'API se connecte avec ce rôle et jamais avec le
propriétaire de la base — un superutilisateur traverse toutes les politiques
RLS et annulerait l'ADR-04 en silence. Les migrations, elles, gardent les
identifiants du propriétaire, parce qu'elles ont besoin du DDL.

## Travaux planifiés

`python -m app.jobs.purge_organizations` efface les organisations dont le délai
de rétractation de sept jours a expiré (EF-06). Il doit tourner une fois par
jour : sans lui, la promesse de suppression définitive n'est pas tenue. Le lot
L2 apporte Celery et son ordonnanceur, qui appelleront la même fonction.

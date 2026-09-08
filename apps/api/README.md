# apps/api — Backend NovaBrief

API FastAPI (Python 3.12) et, à partir du lot L2, workers Celery.

## Contenu actuel (lot L0 — socle)

| Module | Rôle |
|---|---|
| `app/config.py` | Configuration lue dans l'environnement (`.env`). Aucun prix ni quota : ils vivent en base (ADR-09). |
| `app/logging.py` | `structlog` en JSON, corrélé par `debug_id` (ADR-07). Rédaction automatique des clés sensibles. |
| `app/errors.py` | Erreurs au format Problem Details (RFC 9457), code stable + `debug_id`. |
| `app/middleware.py` | Attribue un `debug_id` à chaque requête et le renvoie dans l'en-tête `X-Debug-Id`. |
| `app/db.py` | Moteur async, sessions, et `organization_scope()` qui positionne `app.current_org_id` par transaction (ADR-04). |
| `app/routers/health.py` | `/health` (liveness, sans dépendance) et `/health/ready` (readiness, interroge la base). |

Authentification, organisations et réunions arrivent avec les lots L1 et L2.

## Démarrer

Depuis `infra/` :

```bash
docker compose up -d
curl http://localhost:8000/health
```

Documentation OpenAPI : <http://localhost:8000/api/v1/docs>

## Migrations

```bash
cd apps/api
python -m alembic revision --autogenerate -m "description"
python -m alembic upgrade head
```

L'URL de la base vient de l'environnement, jamais de `alembic.ini` : aucun
identifiant n'est versionné.

## Tests

```bash
python -m pytest apps/api/tests -q
```

Les tests n'ont besoin ni de réseau ni de base : l'application est montée en
mémoire via le transport ASGI de `httpx`.

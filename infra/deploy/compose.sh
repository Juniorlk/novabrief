#!/bin/bash
# Docker Compose for the production stack, with the two flags that are easy to
# forget and painful to diagnose.
#
#     bash infra/deploy/compose.sh up -d --build
#     bash infra/deploy/compose.sh logs -f worker
#     bash infra/deploy/compose.sh ps
#
# `--env-file .env` is the one that bites. Compose looks for the `.env` it
# interpolates `${VAR}` from **next to the compose file**, which is `infra/`,
# not the repository root where the real one lives. Without the flag every
# `${POSTGRES_PASSWORD:?}` fails and the message blames the variable rather
# than the path.
#
# The `env_file:` entries inside the compose file are a different mechanism and
# resolve relative to the file itself, which is why they work either way. That
# difference is exactly what makes the failure confusing.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

exec docker compose --env-file .env -f infra/docker-compose.prod.yml "$@"

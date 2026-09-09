#!/bin/bash
# The application role, created before anything else exists.
#
# ADR-04 says the application role must not have BYPASSRLS. That is necessary
# but not sufficient: a SUPERUSER bypasses Row-Level Security unconditionally,
# FORCE included, so connecting as the database owner leaves every policy in
# place and doing nothing. The isolation test T-07 passes only because the API
# connects as this restricted role.
#
# Migrations still run as the owner: creating tables and policies is exactly
# what this role must not be able to do.
#
# A shell script rather than plain SQL so the password comes from the
# environment. It used to be written here in clear text, which was tolerable
# while this only ever ran on a laptop and became a real problem the day it ran
# on a public server.
set -euo pipefail

: "${DATABASE_APP_PASSWORD:?DATABASE_APP_PASSWORD must be set to create the application role}"

# Passed with -v and interpolated by psql as :'name' for the literal and
# :"name" for the identifier. A password containing a quote therefore cannot
# end the string early and turn the rest of the file into SQL.
psql -v ON_ERROR_STOP=1 \
     -v app_password="${DATABASE_APP_PASSWORD}" \
     -v db_name="${POSTGRES_DB}" \
     --username "${POSTGRES_USER}" \
     --dbname "${POSTGRES_DB}" <<'EOSQL'
CREATE ROLE novabrief_app
    LOGIN
    PASSWORD :'app_password'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOBYPASSRLS
    NOINHERIT;

GRANT CONNECT ON DATABASE :"db_name" TO novabrief_app;
GRANT USAGE ON SCHEMA public TO novabrief_app;

-- Tables do not exist yet: they arrive with the migrations. Default privileges
-- grant the rights to whatever the owner creates from now on, so a new table
-- is reachable by the API without anyone remembering to grant it - and still
-- subject to its RLS policy.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO novabrief_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO novabrief_app;

-- Deliberately not granted: CREATE on the schema, and any DDL. The API changes
-- rows, never the shape of the database.
EOSQL

echo "novabrief_app created (NOSUPERUSER, NOBYPASSRLS, no DDL)"

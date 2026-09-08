-- The application role, created before anything else exists.
--
-- ADR-04 says the application role must not have BYPASSRLS. That is necessary
-- but not sufficient: a SUPERUSER bypasses Row-Level Security unconditionally,
-- FORCE included, so connecting as the database owner leaves every policy in
-- place and doing nothing. The isolation test T-07 passes only because the API
-- connects as this restricted role.
--
-- Migrations still run as the owner: creating tables and policies is exactly
-- what this role must not be able to do.

CREATE ROLE novabrief_app
    LOGIN
    PASSWORD 'novabrief-app-dev'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOBYPASSRLS
    NOINHERIT;

GRANT CONNECT ON DATABASE novabrief TO novabrief_app;
GRANT USAGE ON SCHEMA public TO novabrief_app;

-- Tables do not exist yet: they arrive with the migrations. Default privileges
-- grant the rights to whatever the owner creates from now on, so a new table
-- is reachable by the API without anyone remembering to grant it — and still
-- subject to its RLS policy.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO novabrief_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO novabrief_app;

-- Deliberately not granted: CREATE on the schema, and any DDL. The API changes
-- rows, never the shape of the database.

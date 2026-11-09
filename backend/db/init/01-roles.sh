#!/bin/sh
# Phase 4.6.1 — fresh-volume PostgreSQL role bootstrap.
#
# This script runs ONCE, via the official postgres image's docker-entrypoint-initdb.d
# hook, on an EMPTY data directory only. An EXISTING volume never runs it: for that path
# the operator runs /migrations/role_separation.sql (idempotent, same role creation).
#
# Topology (approved Phase 4.5.8 P1 / implemented Phase 4.6.1):
#   resolve_owner  NOLOGIN — owns every application table. A NOLOGIN role has no password
#                  to leak, cannot be logged into, and can only be assumed via SET ROLE
#                  from a controlled operator session (the database superuser).
#   resolve_app    LOGIN — the runtime role the backend connects as. Owns nothing.
#
# No credential is written by this file: the app-role password arrives through the
# POSTGRES_APP_PASSWORD environment variable — the same variable the backend's
# DATABASE_URL uses in docker-compose.yml. The dev default matches the repo's existing
# dev-credential convention; a real deployment sets POSTGRES_APP_PASSWORD explicitly.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'EOSQL'
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'resolve_owner') THEN
        CREATE ROLE resolve_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'resolve_app') THEN
        CREATE ROLE resolve_app LOGIN;
    END IF;
END
$$;
EOSQL

# psql does not interpolate :'var' inside -c strings — the password is expanded by the
# shell here instead (never stored by this file, never echoed).
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
ALTER ROLE resolve_app PASSWORD '${POSTGRES_APP_PASSWORD:-resolve_app}';
EOSQL

-- Phase 4.6.1 — PostgreSQL role separation (approved design: Phase 4.5.8 P1, corrected
-- and validated Phase 4.5.9 C6).
--
-- WHAT THIS IS: the operator-run, idempotent migration that moves database ownership from
-- the connected application role to a dedicated NOLOGIN owner role and grants the runtime
-- role exactly the privileges the application's normal operations need. It is pure SQL
-- (no psql meta-commands) so it can be executed verbatim by psql or by a driver.
--
-- EXECUTOR: the database superuser (in the dev deployment, `resolve`):
--   docker compose exec db psql -U resolve -d resolve -v ON_ERROR_STOP=1 \
--       -f /migrations/role_separation.sql
-- The superuser is the only controlled entry to the NOLOGIN owner role (SET ROLE).
-- Ownership-transfer statements must run before SET ROLE: PostgreSQL allows ALTER .. OWNER
-- only to the current owner or a superuser, so the one-time bootstrap exception is the
-- superuser itself; every FUTURE schema change is executed AS resolve_owner once it owns
-- the objects.
--
-- IDEMPOTENT: safe to run any number of times, on a fresh database (before the schema
-- exists — the per-object blocks are guarded) and on an existing populated volume.
-- No data is read, modified, or destroyed; no database is dropped or reset.
--
-- PASSWORDS: this file never touches credentials. Fresh volumes get resolve_app's password
-- from POSTGRES_APP_PASSWORD via backend/db/init/01-roles.sh; existing volumes set it once
-- with an operator command (documented in the deployment runbook). resolve_owner is NOLOGIN
-- and has no password at all.

BEGIN;

-- 1. Roles (idempotent). resolve_owner holds ownership and nothing can log in as it;
--    resolve_app is the only identity the backend ever connects with.
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

-- 2. The database is owned by the owner role. On PostgreSQL 15+ the public schema is owned
--    by pg_database_owner, so this also transfers the schema's ownership implicitly.
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I OWNER TO resolve_owner', current_database());
END
$$;

-- 3. Ownership transfer: every existing table and sequence moves to resolve_owner. The
--    loops are no-ops when the schema does not exist yet (fresh-install ordering:
--    bootstrap creates the schema first, then this script transfers and grants).
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER TABLE public.%I OWNER TO resolve_owner', r.tablename);
    END LOOP;
    FOR r IN SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER SEQUENCE public.%I OWNER TO resolve_owner', r.sequencename);
    END LOOP;
END
$$;

-- 4. Runtime grants — explicit, per table, only what normal application operations need.
--    DELETE is granted NOWHERE: the backend has zero delete paths (audited Phase 4.5.7).
--    audit_log is INSERT+SELECT only: it is insert-only by design and no code updates it.
--    Each block is guarded so the script also runs before the schema exists.
DO $$
BEGIN
    IF to_regclass('public.missions') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.missions TO resolve_app;
    END IF;
    IF to_regclass('public.messages') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.messages TO resolve_app;
    END IF;
    IF to_regclass('public.audit_log') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.audit_log TO resolve_app;
    END IF;
    IF to_regclass('public.users') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.users TO resolve_app;
    END IF;
    IF to_regclass('public.agent_conversation') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.agent_conversation TO resolve_app;
    END IF;
    IF to_regclass('public.resolve_orders') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.resolve_orders TO resolve_app;
    END IF;
    IF to_regclass('public.order_issues') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.order_issues TO resolve_app;
    END IF;
    IF to_regclass('public.resolution_assessments') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.resolution_assessments TO resolve_app;
    END IF;
    IF to_regclass('public.refunds') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.refunds TO resolve_app;
    END IF;
    IF to_regclass('public.replacements') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.replacements TO resolve_app;
    END IF;
    IF to_regclass('public.returns') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.returns TO resolve_app;
    END IF;
    IF to_regclass('public.resolutions') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.resolutions TO resolve_app;
    END IF;
    IF to_regclass('public.integration_events') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.integration_events TO resolve_app;
    END IF;
    IF to_regclass('public.purchase_authorizations') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.purchase_authorizations TO resolve_app;
    END IF;
END
$$;

-- 5. Sequences: serial/identity columns behind autoincrement primary keys.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO resolve_app;

-- 6. Protected revocations. The three trigger-source tables (the future event-ledger
--    sources, Phase 4.5.6) must not be deletable or truncatable by the runtime role. The
--    future ledger objects (purchase_events / ledger_state / ledger_publish_state, not
--    created in this phase) are denied EVERYTHING here so that their creation by the owner
--    in a later phase lands on an already-protected surface: with no ALTER DEFAULT
--    PRIVILEGES granting to resolve_app, a new owner-owned table denies the runtime role
--    by default anyway — these REVOKEs make that posture explicit.
DO $$
BEGIN
    IF to_regclass('public.resolve_orders') IS NOT NULL THEN
        EXECUTE 'REVOKE DELETE, TRUNCATE ON public.resolve_orders FROM resolve_app';
    END IF;
    IF to_regclass('public.purchase_authorizations') IS NOT NULL THEN
        EXECUTE 'REVOKE DELETE, TRUNCATE ON public.purchase_authorizations FROM resolve_app';
    END IF;
    IF to_regclass('public.integration_events') IS NOT NULL THEN
        EXECUTE 'REVOKE DELETE, TRUNCATE ON public.integration_events FROM resolve_app';
    END IF;
    IF to_regclass('public.purchase_events') IS NOT NULL THEN
        EXECUTE 'REVOKE ALL ON public.purchase_events FROM resolve_app';
    END IF;
    IF to_regclass('public.ledger_state') IS NOT NULL THEN
        EXECUTE 'REVOKE ALL ON public.ledger_state FROM resolve_app';
    END IF;
    IF to_regclass('public.ledger_publish_state') IS NOT NULL THEN
        EXECUTE 'REVOKE ALL ON public.ledger_publish_state FROM resolve_app';
    END IF;
END
$$;

-- 7. Structural surface: the runtime role creates no schemas and no tables, ever.
REVOKE CREATE ON SCHEMA public FROM resolve_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
DO $$
BEGIN
    EXECUTE format('REVOKE CREATE ON DATABASE %I FROM resolve_app', current_database());
END
$$;
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO resolve_app', current_database());
END
$$;
GRANT USAGE ON SCHEMA public TO resolve_app;

-- 8. Verification evidence (read-only; printed by the executor's session).
SELECT c.relname                          AS table_name,
       c.relowner::regrole::text          AS owner,
       has_table_privilege('resolve_app', c.oid, 'SELECT')    AS app_select,
       has_table_privilege('resolve_app', c.oid, 'INSERT')    AS app_insert,
       has_table_privilege('resolve_app', c.oid, 'UPDATE')    AS app_update,
       has_table_privilege('resolve_app', c.oid, 'DELETE')    AS app_delete
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
ORDER BY c.relname;

COMMIT;

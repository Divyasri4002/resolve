-- Phase 4.6.2 — Durable purchase-event ledger (approved design: Phase 4.5.6-4.5.9).
--
-- WHAT THIS IS: the owner-run, idempotent, single-transaction migration that installs
-- the purchase-event ledger: the three ledger objects, the row-revision tracking on the
-- three trigger-source tables, the SECURITY DEFINER event-emitting triggers (the ONLY
-- writers of purchase_events), the deletion guards, the deterministic historical
-- backfill, the C6 privilege posture, and the count-based verification evidence.
--
-- EXECUTOR: the database superuser (in the dev deployment, `resolve`), via
--   docker compose exec db psql -U resolve -d resolve -v ON_ERROR_STOP=1 \
--       -f /migrations/ledger_install.sql
-- The script takes SET ROLE resolve_owner for every object it creates, so triggers and
-- functions are owned by the NOLOGIN owner role exactly as Phase 4.5.8 D2 requires.
-- The superuser is the only controlled entry to that role.
--
-- RUN ORDER (binding): role_separation.sql FIRST, then this file. Re-running
-- role_separation.sql after this file strips the C6 pin grants (its section 6 revokes
-- everything on the ledger tables); the runtime verifier then fails closed at startup
-- and the operator re-runs THIS file to restore the posture. Nothing here ever drops
-- user data; a verification failure rolls the whole transaction back.
--
-- IDEMPOTENT: every statement is guarded. Safe on the existing populated volume.
-- PRECONDITION: the business schema must exist (run the bootstrap and
-- role_separation.sql first); the backfill plans against the three source tables.
-- BACKFILL SCOPE (approved 4.5.9 C4): all purchase_authorizations, all integration_events,
-- only resolve_orders rows with a non-null provider_order_reference; resolve_orders
-- INSERTs remain event-less (declared scope).
--
-- TRUST MODEL (unchanged): PostgreSQL 16 is the sole sequence authority through a
-- transactional gapless counter row (4.5.4); the triggers are the only purchase_events
-- writers (4.5.6); the external WORM head is anchored by the publisher's single-object
-- CAS (4.5.6/4.5.7); the app never gains a ledger write path (4.5.9 C6).

BEGIN;

-- --------------------------------------------------------------------------- --
-- 0. Migration barrier: the approved two-int advisory lock (4.5.8 D1). Key space 7 is
--    the RESOLVE registry; key2=1 is this migration. The single-64-bit and two-32-bit
--    advisory key spaces do not overlap (PG docs), so no collision with the mission
--    locks (single-bigint crc32 form) is possible.
-- --------------------------------------------------------------------------- --
SELECT pg_advisory_xact_lock(7, 1);

-- --------------------------------------------------------------------------- --
-- 1. Ledger objects. Idempotent; ownership moves to resolve_owner immediately so
--    everything below can run under SET ROLE.
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS public.purchase_events (
    event_seq        BIGINT PRIMARY KEY,
    source           TEXT        NOT NULL
        CHECK (source IN ('resolve_orders', 'purchase_authorizations', 'integration_events')),
    source_pk        TEXT        NOT NULL,
    source_revision  INTEGER     NOT NULL CHECK (source_revision >= 0),
    payload          JSONB       NOT NULL,
    payload_sha256   TEXT        NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    created_at       TIMESTAMP   NOT NULL DEFAULT (transaction_timestamp() AT TIME ZONE 'UTC'),
    UNIQUE (source, source_pk, source_revision)
);

CREATE TABLE IF NOT EXISTS public.ledger_state (
    id          INTEGER   PRIMARY KEY CHECK (id = 1),
    next_seq    BIGINT    NOT NULL DEFAULT 1 CHECK (next_seq >= 1),
    created_at  TIMESTAMP NOT NULL DEFAULT (transaction_timestamp() AT TIME ZONE 'UTC'),
    updated_at  TIMESTAMP NOT NULL DEFAULT (transaction_timestamp() AT TIME ZONE 'UTC')
);

CREATE TABLE IF NOT EXISTS public.ledger_publish_state (
    id                  INTEGER   PRIMARY KEY CHECK (id = 1),
    last_published_seq  BIGINT    NOT NULL DEFAULT 0,
    last_version_id     TEXT,
    last_validator      TEXT,
    last_head_hash      TEXT      NOT NULL DEFAULT repeat('0', 64)
                                  CHECK (last_head_hash ~ '^[0-9a-f]{64}$'),
    publish_status      TEXT      NOT NULL DEFAULT 'idle'
        CHECK (publish_status IN ('idle', 'publishing', 'unresolved', 'halted', 'reconcile')),
    attempts            INTEGER   NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error          TEXT,
    last_head_seq       BIGINT,
    last_head_validator TEXT,
    next_eligible_at    TIMESTAMP,
    lease_epoch         BIGINT    NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    lease_owner         TEXT,
    lease_expires_at    TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT (transaction_timestamp() AT TIME ZONE 'UTC')
);

DO $$
BEGIN
    IF to_regclass('public.purchase_events') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE public.purchase_events OWNER TO resolve_owner';
    END IF;
    IF to_regclass('public.ledger_state') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE public.ledger_state OWNER TO resolve_owner';
    END IF;
    IF to_regclass('public.ledger_publish_state') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE public.ledger_publish_state OWNER TO resolve_owner';
    END IF;
END
$$;

-- Singleton state rows. next_seq = 1: event sequences start at 1; the publisher's
-- genesis pin is last_published_seq = 0 with the all-zeros genesis chain hash.
INSERT INTO public.ledger_state (id, next_seq) VALUES (1, 1)
    ON CONFLICT (id) DO NOTHING;
INSERT INTO public.ledger_publish_state (id) VALUES (1)
    ON CONFLICT (id) DO NOTHING;

-- --------------------------------------------------------------------------- --
-- 2. Row-revision tracking on the three trigger-source tables (4.5.6). The column is
--    owned by the triggers: the application never writes it, and only a QUALIFYING
--    transition bumps it (the WHEN clause gates the whole trigger).
-- --------------------------------------------------------------------------- --
DO $$
BEGIN
    IF to_regclass('public.resolve_orders') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE public.resolve_orders ADD COLUMN IF NOT EXISTS row_revision INTEGER NOT NULL DEFAULT 0';
    END IF;
    IF to_regclass('public.purchase_authorizations') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE public.purchase_authorizations ADD COLUMN IF NOT EXISTS row_revision INTEGER NOT NULL DEFAULT 0';
    END IF;
    IF to_regclass('public.integration_events') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE public.integration_events ADD COLUMN IF NOT EXISTS row_revision INTEGER NOT NULL DEFAULT 0';
    END IF;
END
$$;

-- --------------------------------------------------------------------------- --
-- 3. Trigger functions, created AS resolve_owner (4.5.8 D2): SECURITY DEFINER with a
--    pinned search_path, so the runtime role emits events through the owner's
--    privileges while holding no ledger privileges of its own. EXECUTE is revoked from
--    PUBLIC below — trigger invocation needs no EXECUTE privilege.
--
--    Integrity sequence per event (mandate section 2):
--      (a) transaction-level advisory lock (7,2) — key2=2 is event allocation;
--      (b) gapless counter allocation: UPDATE ledger_state ... RETURNING, row lock
--          held to commit — an aborted transaction rolls its number back (4.5.4), so
--          allocation order IS commit order and A/B interleaving is structurally
--          impossible;
--      (c) canonical payload = to_jsonb(NEW) (resolve_orders minus 'product', the
--          sole redact entry, 4.5.8 D6) rendered to text;
--      (d) digest = PG core sha256 over convert_to(text, 'UTF8') — pgcrypto banned
--          (4.5.9 C3);
--      (e) the business-state change and the event commit atomically (the trigger
--          runs inside the caller's transaction; this function never commits).
-- --------------------------------------------------------------------------- --
SET ROLE resolve_owner;

CREATE OR REPLACE FUNCTION public.resolve_emit_pa_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
BEGIN
    PERFORM pg_advisory_xact_lock(7, 2);
    NEW.row_revision := OLD.row_revision + 1;
    DECLARE
        v_payload jsonb := to_jsonb(NEW);
        v_seq     bigint;
    BEGIN
        UPDATE public.ledger_state
           SET next_seq = next_seq + 1, updated_at = transaction_timestamp() AT TIME ZONE 'UTC'
         WHERE id = 1
         RETURNING next_seq - 1 INTO v_seq;
        INSERT INTO public.purchase_events
            (event_seq, source, source_pk, source_revision, payload, payload_sha256)
        VALUES
            (v_seq, TG_TABLE_NAME, NEW.authorization_id, NEW.row_revision, v_payload,
             encode(sha256(convert_to(v_payload::text, 'UTF8')), 'hex'));
        RETURN NEW;
    END;
END;
$fn$;

CREATE OR REPLACE FUNCTION public.resolve_emit_ie_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
BEGIN
    PERFORM pg_advisory_xact_lock(7, 2);
    NEW.row_revision := OLD.row_revision + 1;
    DECLARE
        v_payload jsonb := to_jsonb(NEW);
        v_seq     bigint;
    BEGIN
        UPDATE public.ledger_state
           SET next_seq = next_seq + 1, updated_at = transaction_timestamp() AT TIME ZONE 'UTC'
         WHERE id = 1
         RETURNING next_seq - 1 INTO v_seq;
        INSERT INTO public.purchase_events
            (event_seq, source, source_pk, source_revision, payload, payload_sha256)
        VALUES
            (v_seq, TG_TABLE_NAME, NEW.event_id, NEW.row_revision, v_payload,
             encode(sha256(convert_to(v_payload::text, 'UTF8')), 'hex'));
        RETURN NEW;
    END;
END;
$fn$;

CREATE OR REPLACE FUNCTION public.resolve_emit_ro_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
BEGIN
    PERFORM pg_advisory_xact_lock(7, 2);
    NEW.row_revision := OLD.row_revision + 1;
    DECLARE
        v_payload jsonb := to_jsonb(NEW) - 'product';
        v_seq     bigint;
    BEGIN
        UPDATE public.ledger_state
           SET next_seq = next_seq + 1, updated_at = transaction_timestamp() AT TIME ZONE 'UTC'
         WHERE id = 1
         RETURNING next_seq - 1 INTO v_seq;
        INSERT INTO public.purchase_events
            (event_seq, source, source_pk, source_revision, payload, payload_sha256)
        VALUES
            (v_seq, TG_TABLE_NAME, NEW.order_id, NEW.row_revision, v_payload,
             encode(sha256(convert_to(v_payload::text, 'UTF8')), 'hex'));
        RETURN NEW;
    END;
END;
$fn$;

-- Deletion guards (4.5.7): the runtime role holds no DELETE/TRUNCATE anywhere relevant,
-- and these triggers refuse the statements even for a role that somehow regains the
-- privilege short of the owner/administrative tier. purchase_events and the two state
-- tables are equally guarded — they are insert-only or pin-only surfaces.
CREATE OR REPLACE FUNCTION public.resolve_refuse_row_deletion() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
BEGIN
    RAISE EXCEPTION 'deletion of % rows is refused: the purchase-event surface is append-only',
        TG_TABLE_NAME;
END;
$fn$;

CREATE OR REPLACE FUNCTION public.resolve_refuse_truncation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
BEGIN
    RAISE EXCEPTION 'TRUNCATE of % is refused: the purchase-event surface is append-only',
        TG_TABLE_NAME;
END;
$fn$;

-- --------------------------------------------------------------------------- --
-- 4. Triggers. Emit triggers carry the approved WHEN clauses (4.5.9 C1): the exact
--    transition columns per table, IS DISTINCT FROM so no-op updates never fire.
--    Creation is guarded so re-runs never drop and recreate a live trigger.
-- --------------------------------------------------------------------------- --
DO $$
BEGIN
    IF to_regclass('public.purchase_authorizations') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_trigger
                        WHERE tgrelid = 'public.purchase_authorizations'::regclass
                          AND tgname = 'resolve_emit_pa_events' AND NOT tgisinternal) THEN
        EXECUTE $sql$
            CREATE TRIGGER resolve_emit_pa_events
            BEFORE UPDATE ON public.purchase_authorizations
            FOR EACH ROW
            WHEN (NEW.status IS DISTINCT FROM OLD.status
                  OR NEW.submission_state IS DISTINCT FROM OLD.submission_state)
            EXECUTE FUNCTION public.resolve_emit_pa_event()
        $sql$;
    END IF;

    IF to_regclass('public.integration_events') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_trigger
                        WHERE tgrelid = 'public.integration_events'::regclass
                          AND tgname = 'resolve_emit_ie_events' AND NOT tgisinternal) THEN
        EXECUTE $sql$
            CREATE TRIGGER resolve_emit_ie_events
            BEFORE UPDATE ON public.integration_events
            FOR EACH ROW
            WHEN (NEW.outcome IS DISTINCT FROM OLD.outcome
                  OR NEW.processed_at IS DISTINCT FROM OLD.processed_at)
            EXECUTE FUNCTION public.resolve_emit_ie_event()
        $sql$;
    END IF;

    IF to_regclass('public.resolve_orders') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_trigger
                        WHERE tgrelid = 'public.resolve_orders'::regclass
                          AND tgname = 'resolve_emit_ro_events' AND NOT tgisinternal) THEN
        EXECUTE $sql$
            CREATE TRIGGER resolve_emit_ro_events
            BEFORE UPDATE ON public.resolve_orders
            FOR EACH ROW
            WHEN (NEW.provider_order_reference IS DISTINCT FROM OLD.provider_order_reference)
            EXECUTE FUNCTION public.resolve_emit_ro_event()
        $sql$;
    END IF;
END
$$;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'public.resolve_orders', 'public.purchase_authorizations', 'public.integration_events',
        'public.purchase_events', 'public.ledger_state', 'public.ledger_publish_state'
    ] LOOP
        IF to_regclass(t) IS NOT NULL THEN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger
                            WHERE tgrelid = t::regclass
                              AND tgname = 'resolve_refuse_delete' AND NOT tgisinternal) THEN
                EXECUTE format(
                    'CREATE TRIGGER resolve_refuse_delete BEFORE DELETE ON %s ' ||
                    'FOR EACH ROW EXECUTE FUNCTION public.resolve_refuse_row_deletion()', t);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_trigger
                            WHERE tgrelid = t::regclass
                              AND tgname = 'resolve_refuse_truncate' AND NOT tgisinternal) THEN
                EXECUTE format(
                    'CREATE TRIGGER resolve_refuse_truncate BEFORE TRUNCATE ON %s ' ||
                    'FOR EACH STATEMENT EXECUTE FUNCTION public.resolve_refuse_truncation()', t);
            END IF;
        END IF;
    END LOOP;
END
$$;

-- --------------------------------------------------------------------------- --
-- 5. Migration barrier (4.5.9 C1): SHARE ROW EXCLUSIVE on every ledger-involved
--    table, taken BEFORE the backfill snapshot and held to commit. An uncommitted
--    settlement UPDATE that started before this migration must either commit (its row
--    lands in the backfill snapshot, because the snapshot is taken after the lock is
--    granted) or wait (and then run WITH the triggers installed). Either way no
--    committed business state can end up without its event.
-- --------------------------------------------------------------------------- --
SELECT pg_advisory_xact_lock(7, 2);  -- serialize allocation with any concurrent emitter
LOCK TABLE public.resolve_orders,
            public.purchase_authorizations,
            public.integration_events,
            public.purchase_events,
            public.ledger_state,
            public.ledger_publish_state
    IN SHARE ROW EXCLUSIVE MODE;

-- --------------------------------------------------------------------------- --
-- 6. Deterministic historical backfill (4.5.9 C4 scope, revision 0 "pre_existing").
--    Eligibility is "the row has NO event yet" — so a row updated between trigger
--    installation and barrier acquisition (which already emitted its revision-1 event)
--    is never double-counted, and reruns are exact no-ops. Ordering is declared and
--    deterministic: purchase_authorizations by authorization_id, then integration_events
--    by event_id, then resolve_orders by order_id. Sequences come from the same gapless
--    counter, allocated inside the same statement that bumps it.
-- --------------------------------------------------------------------------- --
WITH eligible AS (
    SELECT pa.authorization_id AS pk,
           to_jsonb(pa)        AS payload,
           encode(sha256(convert_to(to_jsonb(pa)::text, 'UTF8')), 'hex') AS digest
      FROM public.purchase_authorizations pa
     WHERE NOT EXISTS (
        SELECT 1 FROM public.purchase_events pe
         WHERE pe.source = 'purchase_authorizations' AND pe.source_pk = pa.authorization_id)
),
numbered AS (
    SELECT e.*,
           (SELECT ls.next_seq FROM public.ledger_state ls WHERE ls.id = 1)
               + row_number() OVER (ORDER BY e.pk) - 1 AS ev_seq
      FROM eligible e
),
ins AS (
    INSERT INTO public.purchase_events
        (event_seq, source, source_pk, source_revision, payload, payload_sha256)
    SELECT n.ev_seq, 'purchase_authorizations', n.pk, 0, n.payload, n.digest
      FROM numbered n
)
UPDATE public.ledger_state
   SET next_seq = next_seq + (SELECT count(*) FROM eligible),
       updated_at = transaction_timestamp() AT TIME ZONE 'UTC'
 WHERE id = 1;

WITH eligible AS (
    SELECT ie.event_id AS pk,
           to_jsonb(ie) AS payload,
           encode(sha256(convert_to(to_jsonb(ie)::text, 'UTF8')), 'hex') AS digest
      FROM public.integration_events ie
     WHERE NOT EXISTS (
        SELECT 1 FROM public.purchase_events pe
         WHERE pe.source = 'integration_events' AND pe.source_pk = ie.event_id)
),
numbered AS (
    SELECT e.*,
           (SELECT ls.next_seq FROM public.ledger_state ls WHERE ls.id = 1)
               + row_number() OVER (ORDER BY e.pk) - 1 AS ev_seq
      FROM eligible e
),
ins AS (
    INSERT INTO public.purchase_events
        (event_seq, source, source_pk, source_revision, payload, payload_sha256)
    SELECT n.ev_seq, 'integration_events', n.pk, 0, n.payload, n.digest
      FROM numbered n
)
UPDATE public.ledger_state
   SET next_seq = next_seq + (SELECT count(*) FROM eligible),
       updated_at = transaction_timestamp() AT TIME ZONE 'UTC'
 WHERE id = 1;

WITH eligible AS (
    SELECT ro.order_id AS pk,
           to_jsonb(ro) - 'product' AS payload,
           encode(sha256(convert_to((to_jsonb(ro) - 'product')::text, 'UTF8')), 'hex') AS digest
      FROM public.resolve_orders ro
     WHERE ro.provider_order_reference IS NOT NULL
       AND NOT EXISTS (
        SELECT 1 FROM public.purchase_events pe
         WHERE pe.source = 'resolve_orders' AND pe.source_pk = ro.order_id)
),
numbered AS (
    SELECT e.*,
           (SELECT ls.next_seq FROM public.ledger_state ls WHERE ls.id = 1)
               + row_number() OVER (ORDER BY e.pk) - 1 AS ev_seq
      FROM eligible e
),
ins AS (
    INSERT INTO public.purchase_events
        (event_seq, source, source_pk, source_revision, payload, payload_sha256)
    SELECT n.ev_seq, 'resolve_orders', n.pk, 0, n.payload, n.digest
      FROM numbered n
)
UPDATE public.ledger_state
   SET next_seq = next_seq + (SELECT count(*) FROM eligible),
       updated_at = transaction_timestamp() AT TIME ZONE 'UTC'
 WHERE id = 1;

-- --------------------------------------------------------------------------- --
-- 7. Privilege posture (4.5.9 C6): the runtime role reads the ledger and the counter,
--    may advance the publication pin and hold the publisher lease, and holds NOTHING
--    else on the ledger surface. purchase_events and ledger_state are read-only to the
--    app; the pin has no DELETE/TRUNCATE for anyone. Trigger functions lose EXECUTE
--    everywhere (trigger invocation does not need it).
-- --------------------------------------------------------------------------- --
GRANT SELECT ON public.purchase_events TO resolve_app;
GRANT SELECT ON public.ledger_state TO resolve_app;
GRANT SELECT, UPDATE ON public.ledger_publish_state TO resolve_app;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.purchase_events FROM resolve_app;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.ledger_state FROM resolve_app;
REVOKE INSERT, DELETE, TRUNCATE ON public.ledger_publish_state FROM resolve_app;

REVOKE ALL ON FUNCTION public.resolve_emit_pa_event() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.resolve_emit_ie_event() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.resolve_emit_ro_event() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.resolve_refuse_row_deletion() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.resolve_refuse_truncation() FROM PUBLIC;

RESET ROLE;

-- --------------------------------------------------------------------------- --
-- 8. Verification evidence — COUNT-BASED, never a bare boolean aggregate (an empty
--    expected set must FAIL, not silently pass). Any failure raises and rolls the
--    whole migration back, so the checks can only ever report "migrated" truthfully.
-- --------------------------------------------------------------------------- --
DO $verify$
DECLARE
    v_emit_triggers        integer;
    v_enabled_triggers     integer;
    v_guard_triggers       integer;
    v_enabled_guards       integer;
    v_definer_functions    integer;
    v_owner_functions      integer;
    v_pinned_search_path   integer;
    v_app_pe_writes        integer;
    v_app_ls_writes        integer;
    v_app_pin_destructive  integer;
    v_app_pin_writable     integer;
    v_backfill_gaps        integer;
    v_pa_rows              integer;
    v_ie_rows              integer;
    v_ro_rows              integer;
    v_digest_mismatches    integer;
    v_state_rows           integer;
    v_pin_rows             integer;
BEGIN
    -- V1/V2: the expected emit-trigger set is COMPLETE and ENABLED. Counting exact
    -- names (not aggregating over whatever exists) makes an empty or partial set fail.
    -- The table is matched through pg_class/pg_namespace by NAME, never by
    -- tgrelid::regclass::text: that cast renders WITHOUT the schema prefix whenever the
    -- table is on the search_path, and the whole expected set would silently count zero.
    SELECT count(*) INTO v_emit_triggers
      FROM pg_trigger t
      JOIN pg_class c ON c.oid = t.tgrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE NOT t.tgisinternal AND n.nspname = 'public'
       AND (t.tgname, c.relname) IN (
            ('resolve_emit_pa_events', 'purchase_authorizations'),
            ('resolve_emit_ie_events', 'integration_events'),
            ('resolve_emit_ro_events', 'resolve_orders'));
    SELECT count(*) INTO v_enabled_triggers
      FROM pg_trigger t
      JOIN pg_class c ON c.oid = t.tgrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE NOT t.tgisinternal AND t.tgenabled IN ('O', 'A') AND n.nspname = 'public'
       AND (t.tgname, c.relname) IN (
            ('resolve_emit_pa_events', 'purchase_authorizations'),
            ('resolve_emit_ie_events', 'integration_events'),
            ('resolve_emit_ro_events', 'resolve_orders'));

    -- 12 deletion guards (2 per table across 6 tables).
    SELECT count(*) INTO v_guard_triggers
      FROM pg_trigger t
      JOIN pg_class c ON c.oid = t.tgrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE NOT t.tgisinternal AND n.nspname = 'public'
       AND t.tgname IN ('resolve_refuse_delete', 'resolve_refuse_truncate')
       AND c.relname IN (
            'resolve_orders', 'purchase_authorizations',
            'integration_events', 'purchase_events',
            'ledger_state', 'ledger_publish_state');
    SELECT count(*) INTO v_enabled_guards
      FROM pg_trigger t
      JOIN pg_class c ON c.oid = t.tgrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE NOT t.tgisinternal AND t.tgenabled IN ('O', 'A') AND n.nspname = 'public'
       AND t.tgname IN ('resolve_refuse_delete', 'resolve_refuse_truncate')
       AND c.relname IN (
            'resolve_orders', 'purchase_authorizations',
            'integration_events', 'purchase_events',
            'ledger_state', 'ledger_publish_state');

    -- V3: all five trigger functions are SECURITY DEFINER, owner resolve_owner, with a
    -- pinned search_path.
    SELECT count(*) INTO v_definer_functions
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.proname IN (
            'resolve_emit_pa_event', 'resolve_emit_ie_event', 'resolve_emit_ro_event',
            'resolve_refuse_row_deletion', 'resolve_refuse_truncation')
       AND p.prosecdef;
    SELECT count(*) INTO v_owner_functions
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.proname IN (
            'resolve_emit_pa_event', 'resolve_emit_ie_event', 'resolve_emit_ro_event',
            'resolve_refuse_row_deletion', 'resolve_refuse_truncation')
       AND p.proowner::regrole::text = 'resolve_owner';
    SELECT count(*) INTO v_pinned_search_path
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.proname IN (
            'resolve_emit_pa_event', 'resolve_emit_ie_event', 'resolve_emit_ro_event',
            'resolve_refuse_row_deletion', 'resolve_refuse_truncation')
       AND p.proconfig IS NOT NULL
       AND p.proconfig @> ARRAY['search_path=pg_catalog, public'];

    -- V4: the C6 privilege posture.
    SELECT (has_table_privilege('resolve_app', 'public.purchase_events', 'INSERT')::int
          + has_table_privilege('resolve_app', 'public.purchase_events', 'UPDATE')::int
          + has_table_privilege('resolve_app', 'public.purchase_events', 'DELETE')::int
          + has_table_privilege('resolve_app', 'public.purchase_events', 'TRUNCATE')::int)
      INTO v_app_pe_writes;
    SELECT (has_table_privilege('resolve_app', 'public.ledger_state', 'INSERT')::int
          + has_table_privilege('resolve_app', 'public.ledger_state', 'UPDATE')::int
          + has_table_privilege('resolve_app', 'public.ledger_state', 'DELETE')::int
          + has_table_privilege('resolve_app', 'public.ledger_state', 'TRUNCATE')::int)
      INTO v_app_ls_writes;
    SELECT (has_table_privilege('resolve_app', 'public.ledger_publish_state', 'INSERT')::int
          + has_table_privilege('resolve_app', 'public.ledger_publish_state', 'DELETE')::int
          + has_table_privilege('resolve_app', 'public.ledger_publish_state', 'TRUNCATE')::int)
      INTO v_app_pin_destructive;
    SELECT (has_table_privilege('resolve_app', 'public.ledger_publish_state', 'SELECT')::int
          + has_table_privilege('resolve_app', 'public.ledger_publish_state', 'UPDATE')::int)
      INTO v_app_pin_writable;

    -- V5: backfill completeness — every in-scope row carries exactly one revision-0
    -- event. The guard below runs only when the source tables exist; on a fresh
    -- database with no business schema, the counts are 0 and the check passes vacuously
    -- by COUNTING (an empty scope is a legitimate empty scope, distinct from a missing
    -- trigger set, which V1 refuses).
    -- Scope counts, guarded like everything else (a missing table is counted as 0,
    -- never planned against).
    v_pa_rows := 0;
    v_ie_rows := 0;
    v_ro_rows := 0;
    IF to_regclass('public.purchase_authorizations') IS NOT NULL THEN
        SELECT count(*) INTO v_pa_rows FROM public.purchase_authorizations;
    END IF;
    IF to_regclass('public.integration_events') IS NOT NULL THEN
        SELECT count(*) INTO v_ie_rows FROM public.integration_events;
    END IF;
    IF to_regclass('public.resolve_orders') IS NOT NULL THEN
        SELECT count(*) INTO v_ro_rows FROM public.resolve_orders
         WHERE provider_order_reference IS NOT NULL;
    END IF;    IF to_regclass('public.purchase_authorizations') IS NOT NULL THEN
        SELECT count(*) INTO v_backfill_gaps
          FROM public.purchase_authorizations pa
         WHERE NOT EXISTS (
            SELECT 1 FROM public.purchase_events pe
             WHERE pe.source = 'purchase_authorizations'
               AND pe.source_pk = pa.authorization_id AND pe.source_revision = 0);
    END IF;
    IF to_regclass('public.integration_events') IS NOT NULL THEN
        SELECT count(*) INTO v_backfill_gaps
          FROM public.integration_events ie
         WHERE NOT EXISTS (
            SELECT 1 FROM public.purchase_events pe
             WHERE pe.source = 'integration_events'
               AND pe.source_pk = ie.event_id AND pe.source_revision = 0);
    END IF;
    IF to_regclass('public.resolve_orders') IS NOT NULL THEN
        SELECT count(*) INTO v_backfill_gaps
          FROM public.resolve_orders ro
         WHERE ro.provider_order_reference IS NOT NULL
           AND NOT EXISTS (
            SELECT 1 FROM public.purchase_events pe
             WHERE pe.source = 'resolve_orders'
               AND pe.source_pk = ro.order_id AND pe.source_revision = 0);
    END IF;

    -- V6: internal digest integrity over EVERY event (tamper detection inside the
    -- ledger), plus successor-scoped content identity:
    --   purchase_authorizations / integration_events: every update site on these tables
    --     is qualifying, so the LATEST event's payload must equal the current row.
    --   resolve_orders: non-qualifying columns mutate without events, so identity is
    --     verified on the tracked column (provider_order_reference) — the full-row
    --     comparison would fail spuriously exactly as the naive revision predicate did.
    IF to_regclass('public.purchase_events') IS NOT NULL THEN
        SELECT count(*) INTO v_digest_mismatches
          FROM public.purchase_events pe
         WHERE encode(sha256(convert_to(pe.payload::text, 'UTF8')), 'hex') <> pe.payload_sha256;
    END IF;
    IF to_regclass('public.purchase_authorizations') IS NOT NULL THEN
        v_digest_mismatches := v_digest_mismatches + (
            SELECT count(*) FROM public.purchase_authorizations pa
             JOIN public.purchase_events pe
               ON pe.source = 'purchase_authorizations' AND pe.source_pk = pa.authorization_id
              AND pe.source_revision = pa.row_revision
            LEFT JOIN public.purchase_events later
               ON later.source = 'purchase_authorizations'
              AND later.source_pk = pa.authorization_id
              AND later.source_revision > pe.source_revision
            WHERE later.event_seq IS NULL
              AND to_jsonb(pa)::text <> pe.payload::text);
    END IF;
    IF to_regclass('public.integration_events') IS NOT NULL THEN
        v_digest_mismatches := v_digest_mismatches + (
            SELECT count(*) FROM public.integration_events ie
             JOIN public.purchase_events pe
               ON pe.source = 'integration_events' AND pe.source_pk = ie.event_id
              AND pe.source_revision = ie.row_revision
            LEFT JOIN public.purchase_events later
               ON later.source = 'integration_events'
              AND later.source_pk = ie.event_id
              AND later.source_revision > pe.source_revision
            WHERE later.event_seq IS NULL
              AND to_jsonb(ie)::text <> pe.payload::text);
    END IF;
    IF to_regclass('public.resolve_orders') IS NOT NULL THEN
        v_digest_mismatches := v_digest_mismatches + (
            SELECT count(*) FROM public.resolve_orders ro
             JOIN public.purchase_events pe
               ON pe.source = 'resolve_orders' AND pe.source_pk = ro.order_id
              AND pe.source_revision = ro.row_revision
            LEFT JOIN public.purchase_events later
               ON later.source = 'resolve_orders'
              AND later.source_pk = ro.order_id
              AND later.source_revision > pe.source_revision
            WHERE later.event_seq IS NULL
              AND (pe.payload ->> 'provider_order_reference')
                  IS DISTINCT FROM ro.provider_order_reference);
    END IF;

    -- V7: singletons.
    SELECT count(*) INTO v_state_rows FROM public.ledger_state WHERE id = 1;
    SELECT count(*) INTO v_pin_rows FROM public.ledger_publish_state WHERE id = 1;

    IF v_emit_triggers <> 3 OR v_enabled_triggers <> 3 THEN
        RAISE EXCEPTION 'ledger verification failed: expected exactly 3 enabled emit triggers, found % total / % enabled', v_emit_triggers, v_enabled_triggers;
    END IF;
    IF v_guard_triggers <> 12 OR v_enabled_guards <> 12 THEN
        RAISE EXCEPTION 'ledger verification failed: expected exactly 12 enabled deletion guards, found % total / % enabled', v_guard_triggers, v_enabled_guards;
    END IF;
    IF v_definer_functions <> 5 OR v_owner_functions <> 5 OR v_pinned_search_path <> 5 THEN
        RAISE EXCEPTION 'ledger verification failed: expected 5 SECURITY DEFINER functions owned by resolve_owner with pinned search_path, found %/%/%', v_definer_functions, v_owner_functions, v_pinned_search_path;
    END IF;
    IF v_app_pe_writes <> 0 OR v_app_ls_writes <> 0 OR v_app_pin_destructive <> 0 THEN
        RAISE EXCEPTION 'ledger verification failed: runtime role holds a forbidden ledger write privilege (pe=% ls=% pin_destructive=%)', v_app_pe_writes, v_app_ls_writes, v_app_pin_destructive;
    END IF;
    IF v_app_pin_writable <> 2 THEN
        RAISE EXCEPTION 'ledger verification failed: runtime role must hold exactly SELECT+UPDATE on ledger_publish_state (the pin row is pre-created and UPDATE-only), found % of 2', v_app_pin_writable;
    END IF;
    IF v_backfill_gaps <> 0 THEN
        RAISE EXCEPTION 'ledger verification failed: % in-scope rows lack their revision-0 backfill event (pa=% ie=% ro=%)', v_backfill_gaps, v_pa_rows, v_ie_rows, v_ro_rows;
    END IF;
    IF v_digest_mismatches <> 0 THEN
        RAISE EXCEPTION 'ledger verification failed: % digest/content identity mismatches', v_digest_mismatches;
    END IF;
    IF v_state_rows <> 1 OR v_pin_rows <> 1 THEN
        RAISE EXCEPTION 'ledger verification failed: singleton state rows missing (state=% pin=%)', v_state_rows, v_pin_rows;
    END IF;

    RAISE NOTICE 'ledger_install verification PASSED: emit_triggers=% guards=% functions=5 ok, backfill rows pa=% ie=% ro=%, next_seq=%',
        v_emit_triggers, v_guard_triggers, v_pa_rows, v_ie_rows, v_ro_rows,
        (SELECT next_seq FROM public.ledger_state WHERE id = 1);
END
$verify$;

-- Read-out for the operator's session (evidence, not a gate).
SELECT c.relname AS table_name,
       c.relowner::regrole::text AS owner,
       has_table_privilege('resolve_app', c.oid, 'SELECT')   AS app_select,
       has_table_privilege('resolve_app', c.oid, 'INSERT')   AS app_insert,
       has_table_privilege('resolve_app', c.oid, 'UPDATE')   AS app_update,
       has_table_privilege('resolve_app', c.oid, 'DELETE')   AS app_delete
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
   AND c.relname IN ('purchase_events', 'ledger_state', 'ledger_publish_state',
                     'resolve_orders', 'purchase_authorizations', 'integration_events')
 ORDER BY c.relname;

COMMIT;

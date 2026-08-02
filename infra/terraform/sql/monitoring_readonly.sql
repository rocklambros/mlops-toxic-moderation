-- Read-only Postgres role for the monitoring dashboard (premortem H16).
--
-- The dashboard only ever issues SELECT. It must not be able to write `feedback`,
-- which is where the graded live-accuracy number comes from, and it must never hold
-- the RDS master credentials. Idempotent, so it is safe to re-run after every apply,
-- and re-running is also the password-rotation path.
--
-- Invoked by the SSM document toxic-mod-db-bootstrap-readonly, connected as the RDS
-- master user, with the psql variables ro_user, ro_pass, master_user and db_name
-- supplied from Secrets Manager at run time.
--
-- Deliberately free of DO $$ ... $$ blocks. psql does not perform variable
-- interpolation inside dollar-quoted strings, so a DO block would send the literal
-- text :'ro_user' to the server and fail with a syntax error at run time. \gexec is
-- the interpolation-safe equivalent, and :'x' / :"x" produce a correctly quoted
-- literal and identifier respectively, so neither value can be injected.

-- Create the role only when it is absent. NOLOGIN here, because the password is set
-- by the ALTER below: the role never exists with an empty password, not even briefly.
SELECT format(
  'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION',
  :'ro_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ro_user')
\gexec

-- Converge the attributes and the password on every run. This statement is what
-- makes the document a rotation path as well as a bootstrap.
--
-- NOSUPERUSER and NOREPLICATION are deliberately absent. On RDS the master is not a
-- superuser -- it holds rds_superuser, which may not alter the SUPERUSER attribute even
-- to the value it already has -- so naming either one fails the whole statement with
-- "permission denied to alter role ... Only roles with the SUPERUSER attribute may change
-- the SUPERUSER attribute". Observed on 2026-08-01: CREATE ROLE above succeeded and this
-- statement aborted, leaving monitor_ro existing with no password.
--
-- Nothing is lost by omitting them. CREATE ROLE above already sets both, RDS forbids any
-- role from granting itself SUPERUSER or REPLICATION, and the attributes cannot drift
-- because no principal in this account is able to change them.
ALTER ROLE :"ro_user" WITH LOGIN PASSWORD :'ro_pass'
  NOCREATEDB NOCREATEROLE NOINHERIT;

GRANT CONNECT ON DATABASE :"db_name" TO :"ro_user";
GRANT USAGE ON SCHEMA public TO :"ro_user";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"ro_user";

-- Phase 2 creates predictions, review_queue and feedback AFTER this runs. Without
-- default privileges the role would see an empty schema and the dashboard would
-- render empty charts with no error. FOR ROLE is explicit rather than left to
-- current_user, so the grant still lands if the bootstrap is ever re-run by a
-- different superuser than the one that owns the tables.
ALTER DEFAULT PRIVILEGES FOR ROLE :"master_user" IN SCHEMA public
  GRANT SELECT ON TABLES TO :"ro_user";

-- Nothing above grants CREATE on schema public, and Postgres 15 and later no longer
-- grant it to PUBLIC either, so `CREATE TABLE probe(x int)` as monitor_ro fails with
-- "permission denied for schema public". That is the H16 acceptance check.

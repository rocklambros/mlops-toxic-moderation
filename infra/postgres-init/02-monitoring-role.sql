-- The read-only role the monitoring dashboard connects as (premortem H16). The same two
-- grants run against RDS in Phase A2; this file is what makes the local stack match.
--
-- Numbered 02 so it runs after Postgres has created the `toxic` database. The role holds
-- SELECT and nothing else: `tests/unit/test_dashboard_guards.py` proves the dashboard's
-- code issues no write statement, and this proves the credential could not carry one out
-- even if it did. A dashboard that can write the graded metric is one leaked DSN away from
-- being the accuracy number it is supposed to be reporting.
--
-- ALTER DEFAULT PRIVILEGES covers tables created after this script runs, which is all of
-- them: the backend's `init_db` creates the schema on first start, well after the database
-- container has finished initialising.

CREATE ROLE monitoring_ro LOGIN PASSWORD 'monitoring_ro';

\connect toxic

GRANT CONNECT ON DATABASE toxic TO monitoring_ro;
GRANT USAGE ON SCHEMA public TO monitoring_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO monitoring_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO monitoring_ro;

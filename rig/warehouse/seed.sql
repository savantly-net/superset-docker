-- Warehouse fixture: one mart table that the fixture dataset reads.
CREATE SCHEMA marts;
CREATE TABLE marts.calls_daily (
  location_key text NOT NULL,
  day          date NOT NULL,
  calls        integer NOT NULL
);
INSERT INTO marts.calls_daily VALUES
  ('loc-001', '2026-11-01', 10),
  ('loc-001', '2026-11-02', 12),
  ('loc-002', '2026-11-01', 7),
  ('loc-002', '2026-11-02', 9);

-- The scorecard fixture of the guest-token rig tests (fran-ops S6.2). The location keys are
-- cuids, because the FranOps broker refuses any other key. Franchisee A has 3 rows on 2
-- locations, B has 2 rows on 2 locations. The fran-ops rig test creates locations with these IDs.
-- A schema of its own: the S5.5 rig tests assert the tables of marts.
CREATE SCHEMA embed;
CREATE TABLE embed.scorecard_fixture (
  franchisee_label text NOT NULL,
  location_key     text NOT NULL,
  calls            integer NOT NULL
);
INSERT INTO embed.scorecard_fixture VALUES
  ('A', 'crigloca00000000000000001', 10),
  ('A', 'crigloca00000000000000001', 11),
  ('A', 'crigloca00000000000000002', 20),
  ('B', 'criglocb00000000000000001', 40),
  ('B', 'criglocb00000000000000002', 50);

-- A raw table (S7.1): the BI must not reach it. A deployed warehouse keeps it from the BI with
-- grants (franops-data-agent warehouse/grants.sql); the rig connects as the owner, so the S7.1
-- check proves the Superset half only: HQ has no grant on a dataset of this table.
CREATE SCHEMA raw_callrail;
CREATE TABLE raw_callrail.calls (
  id            text PRIMARY KEY,
  location_key  text NOT NULL,
  caller_number text NOT NULL
);
INSERT INTO raw_callrail.calls VALUES
  ('CAL1', 'loc-001', '+15550000001'),
  ('CAL2', 'loc-002', '+15550000002');

-- The source fixture of the S4.11 rig tests: tests/fixtures/sources/bi/callrail/ reads it. A
-- schema of its own, because the S5.5 rig tests assert the tables of marts.
CREATE SCHEMA source_fixture;
CREATE TABLE source_fixture.callrail_calls (
  location_key text NOT NULL,
  day          date NOT NULL,
  calls        integer NOT NULL
);
INSERT INTO source_fixture.callrail_calls VALUES
  ('loc-001', '2026-11-01', 4),
  ('loc-002', '2026-11-01', 3);

-- The real CallRail mart of franops_sources (fran-ops S12.4), with the columns and types that
-- dbt builds (franops-data-dbt sources/models/shared/fct_calls_location_day.sql). The certified
-- scorecard in franops-data-dbt sources/bi/callrail/ reads it. Franchisee A owns the crigloca
-- locations, B the criglocb ones (cuids, as the broker requires). The last row is a call that
-- only a franchisee rule maps: its location_key is null, so no guest token clause matches it.
CREATE TABLE marts.fct_calls_location_day (
  location_key     text,
  franchisee_key   text,
  date             date,
  calls            bigint,
  answered         bigint,
  answerable       bigint,
  first_time_calls bigint,
  missed           bigint,
  baseline_calls   numeric
);
INSERT INTO marts.fct_calls_location_day VALUES
  ('crigloca00000000000000001', 'crigfrana0000000000000001', '2026-11-01', 10, 8, 9, 4, 1, 12),
  ('crigloca00000000000000001', 'crigfrana0000000000000001', '2026-11-02', 12, 11, 12, 5, 1, 12),
  ('crigloca00000000000000002', 'crigfrana0000000000000001', '2026-11-01', 6, 5, 6, 2, 1, NULL),
  ('criglocb00000000000000001', 'crigfranb0000000000000001', '2026-11-01', 40, 30, 38, 20, 8, 35),
  ('criglocb00000000000000002', 'crigfranb0000000000000001', '2026-11-02', 50, 45, 49, 21, 4, 44),
  (NULL, 'crigfranb0000000000000001', '2026-11-02', 3, 3, 3, 1, 0, NULL);

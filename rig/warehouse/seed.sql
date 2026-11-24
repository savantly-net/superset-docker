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
-- A schema of its own: the S5.5 rig tests assert that marts holds calls_daily only.
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

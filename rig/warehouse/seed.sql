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

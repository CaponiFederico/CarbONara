-- I dati di rete per le app

ALTER TABLE grid_requests ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'past';

-- Le zone che le app chiedono
CREATE TABLE IF NOT EXISTS grid_zone_demand (
    zone TEXT PRIMARY KEY,
    first_requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_requested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS grid_snapshots (
    zone TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('latest', 'forecast', 'recent')),
    points JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (zone, kind)
);

REVOKE ALL ON grid_zone_demand, grid_snapshots FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON grid_zone_demand TO ecomonitor_api;
GRANT SELECT ON grid_snapshots, grid_hourly TO ecomonitor_api;
GRANT SELECT ON grid_zone_demand TO ecomonitor_grid;
GRANT SELECT, INSERT, UPDATE ON grid_snapshots TO ecomonitor_grid;
GRANT SELECT, INSERT, UPDATE, DELETE ON grid_zone_demand, grid_snapshots TO ecomonitor_admin;

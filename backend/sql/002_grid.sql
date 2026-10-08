-- Intensita' della rete ora per ora, per ogni zona in cui ci sono partecipanti

CREATE TABLE IF NOT EXISTS grid_hourly (
    zone TEXT NOT NULL,
    hour_start TIMESTAMPTZ NOT NULL,
    g_per_kwh DOUBLE PRECISION CHECK (g_per_kwh IS NULL OR g_per_kwh >= 0),
    first_g_per_kwh DOUBLE PRECISION,
    revisions INTEGER NOT NULL DEFAULT 0,
    is_estimated BOOLEAN,
    estimation_method TEXT,
    source_updated_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (zone, hour_start)
);

-- Ogni richiesta fatta a ElectricityMaps
CREATE TABLE IF NOT EXISTS grid_requests (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    zone TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    status INTEGER NOT NULL,
    points INTEGER NOT NULL
);

-- Il raccoglitore gira con un utente suo
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ecomonitor_grid') THEN
        CREATE ROLE ecomonitor_grid LOGIN;
    END IF;
END
$$;

REVOKE ALL ON grid_hourly, grid_requests FROM PUBLIC;
GRANT SELECT (zone, hour_start) ON hourly_usage TO ecomonitor_grid;
GRANT SELECT, INSERT, UPDATE ON grid_hourly TO ecomonitor_grid;
GRANT SELECT, INSERT ON grid_requests TO ecomonitor_grid;
GRANT SELECT, INSERT, UPDATE, DELETE ON grid_hourly, grid_requests TO ecomonitor_admin;

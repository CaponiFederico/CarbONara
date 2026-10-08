-- Schema del database dello studio
--
-- nessun dato personale: il partecipante e' solo l'impronta SHA-256
-- di un token casuale da 32 byte. Nome, cognome, eta' e mail stanno nell'anagrafica
-- sulla macchina locale
-- L'impronta di un token casuale non si inverte

CREATE TABLE IF NOT EXISTS participants (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token_sha256 CHAR(64) NOT NULL UNIQUE CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

-- Cancellare un partecipante (ritiro dallo studio) cancella anche le sue ore.
CREATE TABLE IF NOT EXISTS hourly_usage (
    participant_id BIGINT NOT NULL REFERENCES participants (id) ON DELETE CASCADE,
    hour_start TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_version INTEGER NOT NULL,
    app_version TEXT NOT NULL,
    os_family TEXT NOT NULL CHECK (os_family IN ('windows', 'linux', 'macos')),
    device_class TEXT NOT NULL CHECK (device_class IN ('laptop', 'desktop')),
    country_code CHAR(2) NOT NULL,
    zone TEXT NOT NULL,
    covered_seconds DOUBLE PRECISION NOT NULL CHECK (covered_seconds > 0),
    energy_wh DOUBLE PRECISION NOT NULL CHECK (energy_wh >= 0),
    gco2 DOUBLE PRECISION NOT NULL CHECK (gco2 >= 0),
    estimated_wh DOUBLE PRECISION NOT NULL CHECK (estimated_wh >= 0),
    battery_wh DOUBLE PRECISION NOT NULL CHECK (battery_wh >= 0),
    fallback_wh DOUBLE PRECISION NOT NULL CHECK (fallback_wh >= 0),
    cpu_wh DOUBLE PRECISION NOT NULL CHECK (cpu_wh >= 0),
    PRIMARY KEY (participant_id, hour_start)
);

-- Due ruoli per protezione in caso di compromissione API
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ecomonitor_api') THEN
        CREATE ROLE ecomonitor_api LOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ecomonitor_admin') THEN
        CREATE ROLE ecomonitor_admin NOLOGIN;
    END IF;
END
$$;

REVOKE ALL ON participants, hourly_usage FROM PUBLIC;
GRANT SELECT (id, token_sha256, joined_at, revoked_at) ON participants TO ecomonitor_api;
GRANT SELECT, INSERT, UPDATE ON hourly_usage TO ecomonitor_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON participants, hourly_usage TO ecomonitor_admin;

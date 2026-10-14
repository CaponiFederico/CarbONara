-- Studio a due fasi e schema 2 degli invii
--
-- Fase 1 (baseline): l'app raccoglie senza mostrare consigli. Fase 2 (advice):
-- l'app completa. Il calendario parte da started_at, la prima ora inviata in
-- modalita' studio, e la durata delle fasi si cambia per partecipante senza
-- reinstallare niente.
--
-- Le colonne nuove di hourly_usage sono nullabili: le ore dello schema 1 restano.

ALTER TABLE participants ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE participants ADD COLUMN IF NOT EXISTS baseline_hours INTEGER NOT NULL DEFAULT 168
    CHECK (baseline_hours > 0);
ALTER TABLE participants ADD COLUMN IF NOT EXISTS advice_hours INTEGER NOT NULL DEFAULT 168
    CHECK (advice_hours > 0);

ALTER TABLE hourly_usage ADD COLUMN IF NOT EXISTS install_id UUID;
-- NULL: in quell'ora l'app girava senza token, completa
ALTER TABLE hourly_usage ADD COLUMN IF NOT EXISTS phase TEXT
    CHECK (phase IS NULL OR phase IN ('baseline', 'advice'));
ALTER TABLE hourly_usage ADD COLUMN IF NOT EXISTS dashboard_visible_seconds DOUBLE PRECISION
    CHECK (dashboard_visible_seconds >= 0);
ALTER TABLE hourly_usage ADD COLUMN IF NOT EXISTS advice_wait_seconds DOUBLE PRECISION
    CHECK (advice_wait_seconds >= 0);
ALTER TABLE hourly_usage ADD COLUMN IF NOT EXISTS advice_now_seconds DOUBLE PRECISION
    CHECK (advice_now_seconds >= 0);
ALTER TABLE hourly_usage ADD COLUMN IF NOT EXISTS plugged_seconds DOUBLE PRECISION
    CHECK (plugged_seconds >= 0);
ALTER TABLE hourly_usage ADD COLUMN IF NOT EXISTS charge_percent DOUBLE PRECISION
    CHECK (charge_percent >= 0);
ALTER TABLE hourly_usage ADD COLUMN IF NOT EXISTS native_wh DOUBLE PRECISION
    CHECK (native_wh >= 0);
ALTER TABLE hourly_usage ADD COLUMN IF NOT EXISTS partial_wh DOUBLE PRECISION
    CHECK (partial_wh >= 0);
ALTER TABLE hourly_usage ADD COLUMN IF NOT EXISTS calibrated_wh DOUBLE PRECISION
    CHECK (calibrated_wh >= 0);
ALTER TABLE hourly_usage ADD COLUMN IF NOT EXISTS generic_wh DOUBLE PRECISION
    CHECK (generic_wh >= 0);


GRANT SELECT (started_at, baseline_hours, advice_hours) ON participants TO ecomonitor_api;
GRANT UPDATE (started_at) ON participants TO ecomonitor_api;

-- Piu' dispositivi per partecipante
--
-- La chiave di un'ora diventa (partecipante, installazione, ora): due computer con
-- lo stesso token mandano ore distinte invece di sostituirsi a vicenda. Il
-- calendario delle fasi resta per partecipante.


UPDATE hourly_usage
SET install_id = '00000000-0000-0000-0000-000000000000'
WHERE install_id IS NULL;

ALTER TABLE hourly_usage ALTER COLUMN install_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.conrelid = 'hourly_usage'::regclass
          AND c.contype = 'p'
          AND a.attname = 'install_id'
    ) THEN
        ALTER TABLE hourly_usage DROP CONSTRAINT hourly_usage_pkey;
        ALTER TABLE hourly_usage
            ADD CONSTRAINT hourly_usage_pkey PRIMARY KEY (participant_id, install_id, hour_start);
    END IF;
END
$$;

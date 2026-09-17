CREATE TABLE instruments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), identity_key text NOT NULL UNIQUE
);
INSERT INTO instruments(id,identity_key)
SELECT md5('qmt:' || code)::uuid,'qmt:' || code FROM (
    SELECT code FROM securities UNION SELECT code FROM bars
) existing;
ALTER TABLE securities ADD COLUMN instrument_id uuid REFERENCES instruments(id);
UPDATE securities SET instrument_id=md5('qmt:' || code)::uuid;
ALTER TABLE securities ALTER COLUMN instrument_id SET NOT NULL;
ALTER TABLE securities DROP CONSTRAINT securities_pkey;
ALTER TABLE securities ADD PRIMARY KEY(source,code);
CREATE INDEX securities_instrument_idx ON securities(instrument_id,source);

UPDATE securities SET metadata=metadata || jsonb_build_object('delivery_month',left(metadata->>'expiry',6))
WHERE kind='future' AND subtype IN ('','contract') AND metadata->>'expiry' ~ '^\d{8}$'
AND upper(split_part(code,'.',1)) IN (
    upper((metadata->>'product') || substr(metadata->>'expiry',3,4)),
    upper((metadata->>'product') || substr(metadata->>'expiry',4,3))
);
WITH candidates AS (
    SELECT instrument_id,'future:' || market || ':' || upper(metadata->>'product') || ':' || (metadata->>'delivery_month') AS key
    FROM securities WHERE kind='future' AND subtype IN ('','contract') AND metadata->>'delivery_month' ~ '^\d{6}$'
    AND coalesce(metadata->>'product','') <> ''
), unique_keys AS (SELECT key FROM candidates GROUP BY key HAVING count(*)=1)
UPDATE instruments i SET identity_key=c.key FROM candidates c JOIN unique_keys u USING(key) WHERE i.id=c.instrument_id;

ALTER TABLE bars ADD COLUMN instrument_id uuid REFERENCES instruments(id);
UPDATE bars SET instrument_id=md5('qmt:' || code)::uuid;
ALTER TABLE bars ALTER COLUMN instrument_id SET NOT NULL;
ALTER TABLE bars ADD COLUMN normalization_version text NOT NULL DEFAULT 'qmt-raw-v1';
ALTER TABLE bars DROP CONSTRAINT bars_pkey;
ALTER TABLE bars ADD PRIMARY KEY(instrument_id,source,period,time);
CREATE INDEX bars_source_code_time_idx ON bars(source,code,period,time);

CREATE FUNCTION resolve_legacy_instrument() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.instrument_id IS NULL THEN
        SELECT instrument_id INTO NEW.instrument_id FROM securities WHERE source=NEW.source AND code=NEW.code;
        IF NEW.instrument_id IS NULL THEN
            INSERT INTO instruments(identity_key) VALUES(NEW.source || ':' || NEW.code)
            ON CONFLICT(identity_key) DO UPDATE SET identity_key=EXCLUDED.identity_key
            RETURNING id INTO NEW.instrument_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER bars_legacy_identity BEFORE INSERT ON bars FOR EACH ROW EXECUTE FUNCTION resolve_legacy_instrument();
CREATE TRIGGER securities_legacy_identity BEFORE INSERT ON securities FOR EACH ROW EXECUTE FUNCTION resolve_legacy_instrument();

ALTER TABLE trading_dates ADD COLUMN source text NOT NULL DEFAULT 'qmt';
ALTER TABLE trading_dates DROP CONSTRAINT trading_dates_pkey;
ALTER TABLE trading_dates ADD PRIMARY KEY(source,market,day);
ALTER TABLE datasets ADD COLUMN source text NOT NULL DEFAULT 'qmt' CHECK(source IN ('qmt','tushare'));
ALTER TABLE datasets ADD COLUMN account_id text;
ALTER TABLE datasets ADD COLUMN endpoint text;
ALTER TABLE datasets ADD COLUMN schedule_time time NOT NULL DEFAULT '17:00';
CREATE TABLE contract_mappings (
    source text NOT NULL, code text NOT NULL, trading_day date NOT NULL,
    member_code text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(source,code,trading_day)
);
CREATE INDEX jobs_provider_queue_idx ON jobs(kind,state,(coalesce(payload->>'source','qmt')),created_at);

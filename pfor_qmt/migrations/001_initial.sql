CREATE TABLE IF NOT EXISTS schema_version (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS securities (
    code text PRIMARY KEY, name text NOT NULL, kind text NOT NULL CHECK (kind IN ('stock','index','etf')),
    details jsonb NOT NULL DEFAULT '{}', source text NOT NULL DEFAULT 'qmt', updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS index_mapping (
    code text PRIMARY KEY, sector text NOT NULL, name text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS constituent_snapshots (
    id uuid PRIMARY KEY, index_code text NOT NULL, observed_at timestamptz NOT NULL DEFAULT now(), members jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS trading_dates (market text NOT NULL, day date NOT NULL, PRIMARY KEY(market,day));
CREATE TABLE IF NOT EXISTS bars (
    code text NOT NULL, period text NOT NULL CHECK(period IN ('1d','1m','5m')), time timestamptz NOT NULL,
    open numeric, high numeric, low numeric, close numeric, volume numeric, amount numeric,
    source text NOT NULL DEFAULT 'qmt', updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(code,period,time)
);
CREATE TABLE IF NOT EXISTS factors (
    code text PRIMARY KEY, raw jsonb NOT NULL, observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS datasets (
    id uuid PRIMARY KEY, name text NOT NULL, members jsonb NOT NULL, periods jsonb NOT NULL,
    index_code text, snapshot_id uuid REFERENCES constituent_snapshots(id), scheduled boolean NOT NULL DEFAULT false,
    schedule_from date NOT NULL DEFAULT CURRENT_DATE, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS jobs (
    id uuid PRIMARY KEY, kind text NOT NULL CHECK(kind IN ('download','export')), state text NOT NULL DEFAULT 'queued',
    payload jsonb NOT NULL, checkpoint integer NOT NULL DEFAULT 0, attempts integer NOT NULL DEFAULT 0,
    cancel_requested boolean NOT NULL DEFAULT false, result jsonb NOT NULL DEFAULT '{}', error text,
    schedule_key text UNIQUE, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS coverage (
    job_id uuid NOT NULL REFERENCES jobs(id), code text NOT NULL, period text NOT NULL,
    requested_start date NOT NULL, requested_end date NOT NULL, actual_start timestamptz, actual_end timestamptz,
    row_count integer NOT NULL, gaps jsonb NOT NULL DEFAULT '[]', PRIMARY KEY(job_id,code,period,requested_start)
);
INSERT INTO schema_version(version) VALUES(1) ON CONFLICT DO NOTHING;

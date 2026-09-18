UPDATE jobs SET state='succeeded' WHERE state='completed';
ALTER TABLE jobs ADD COLUMN parent_id uuid REFERENCES jobs(id);
ALTER TABLE jobs ADD COLUMN error_code text;
ALTER TABLE jobs ADD COLUMN action text;
ALTER TABLE jobs ADD COLUMN run_number integer NOT NULL DEFAULT 0;
CREATE INDEX jobs_parent_idx ON jobs(parent_id);
CREATE INDEX jobs_queue_idx ON jobs(kind,state,created_at);
CREATE TABLE job_units (
    job_id uuid NOT NULL REFERENCES jobs(id), unit_index integer NOT NULL,
    request jsonb NOT NULL, state text NOT NULL, quality_state text NOT NULL,
    row_count integer NOT NULL DEFAULT 0, issues jsonb NOT NULL DEFAULT '[]',
    error_code text, retryable boolean NOT NULL DEFAULT false, stats jsonb NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(job_id,unit_index)
);
CREATE TABLE job_events (
    id bigserial PRIMARY KEY, job_id uuid REFERENCES jobs(id), run_number integer NOT NULL DEFAULT 0,
    unit_index integer, level text NOT NULL, code text NOT NULL, message text NOT NULL,
    context jsonb NOT NULL DEFAULT '{}', sample jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX job_events_job_idx ON job_events(job_id,id);
CREATE INDEX job_events_time_idx ON job_events(created_at);
CREATE TABLE maintenance_plans (
    id uuid PRIMARY KEY, name text NOT NULL, source text NOT NULL,
    kind text NOT NULL CHECK(kind IN ('catalog','download')), payload jsonb NOT NULL,
    enabled boolean NOT NULL DEFAULT true, schedule_time time NOT NULL,
    lookback_days integer NOT NULL DEFAULT 5 CHECK(lookback_days BETWEEN 1 AND 365),
    last_date date, last_error text, updated_at timestamptz NOT NULL DEFAULT now()
);

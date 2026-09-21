ALTER TABLE trading_dates ADD COLUMN evidence text NOT NULL DEFAULT 'legacy';
ALTER TABLE trading_dates ADD COLUMN observed_at timestamptz;
ALTER TABLE trading_dates ADD COLUMN source_fields jsonb;
ALTER TABLE contract_mappings ADD COLUMN source_fields jsonb;
CREATE TABLE contract_mapping_snapshots (
    source text NOT NULL, code text NOT NULL, member_code text NOT NULL,
    observed_at timestamptz NOT NULL, trading_day date,
    job_id uuid NOT NULL REFERENCES jobs(id), unit_index integer NOT NULL,
    source_fields jsonb NOT NULL,
    PRIMARY KEY(job_id,unit_index,code)
);
CREATE INDEX mapping_snapshots_latest_idx ON contract_mapping_snapshots(source,code,observed_at DESC);
CREATE VIEW current_contract_mappings AS
SELECT DISTINCT ON(source,code) *,xmin AS row_version FROM contract_mapping_snapshots
ORDER BY source,code,observed_at DESC,job_id DESC;

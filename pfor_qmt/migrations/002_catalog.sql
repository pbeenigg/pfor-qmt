ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK(kind IN ('download','export','catalog'));
CREATE TABLE catalog_sectors (
    name text PRIMARY KEY, observed_at timestamptz NOT NULL DEFAULT now()
);

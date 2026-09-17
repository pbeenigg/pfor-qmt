ALTER TABLE securities DROP CONSTRAINT securities_kind_check;
ALTER TABLE securities ADD COLUMN subtype text NOT NULL DEFAULT '';
UPDATE securities SET kind='fund',subtype='etf' WHERE kind='etf';
ALTER TABLE securities ADD CONSTRAINT securities_kind_check CHECK(kind IN ('stock','index','future','option','fund','bond'));
ALTER TABLE securities ADD COLUMN market text NOT NULL DEFAULT '';
UPDATE securities SET market=split_part(code,'.',2);
ALTER TABLE securities ADD COLUMN metadata jsonb NOT NULL DEFAULT '{}';
CREATE INDEX securities_kind_market_idx ON securities(kind,market,code);
ALTER TABLE catalog_sectors ADD COLUMN category text NOT NULL DEFAULT 'other';
ALTER TABLE catalog_sectors ADD COLUMN path jsonb NOT NULL DEFAULT '[]';
CREATE TABLE board_snapshots (
    id uuid PRIMARY KEY, name text NOT NULL, category text NOT NULL,
    members jsonb NOT NULL, observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX board_snapshots_latest_idx ON board_snapshots(name,observed_at DESC);
ALTER TABLE datasets ADD COLUMN board_name text;
ALTER TABLE datasets ADD COLUMN board_snapshot_id uuid REFERENCES board_snapshots(id);
ALTER TABLE bars ADD COLUMN open_interest numeric;
ALTER TABLE bars ADD COLUMN settlement numeric;
ALTER TABLE bars ADD COLUMN previous_settlement numeric;
ALTER TABLE bars ADD COLUMN trading_day date;

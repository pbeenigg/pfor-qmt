ALTER TABLE bars DROP CONSTRAINT bars_period_check;
ALTER TABLE bars ADD CONSTRAINT bars_period_check CHECK (
    period IN ('1d','1m','5m') OR source='tushare' AND period IN ('1w','1mo','15m','30m','60m')
);
ALTER TABLE bars ADD COLUMN as_of_date date;
ALTER TABLE bars ADD COLUMN source_fields jsonb NOT NULL DEFAULT '{}';
ALTER TABLE trading_dates ADD COLUMN is_open boolean NOT NULL DEFAULT true;
ALTER TABLE trading_dates ADD COLUMN pretrade_date date;

CREATE TABLE futures_warehouse_receipts (
    source text NOT NULL, exchange text NOT NULL, symbol text NOT NULL, trade_date date NOT NULL,
    row_key text NOT NULL, fut_name text, warehouse text, wh_id text,
    pre_vol numeric, vol numeric, vol_chg numeric, area text, year text, grade text,
    brand text, place text, pd numeric, is_ct text, unit text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(source,exchange,symbol,trade_date,row_key)
);
CREATE TABLE futures_holdings (
    source text NOT NULL, exchange text NOT NULL, symbol text NOT NULL, trade_date date NOT NULL,
    broker text NOT NULL, vol numeric, vol_chg numeric, long_hld numeric, long_chg numeric,
    short_hld numeric, short_chg numeric, updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(source,exchange,symbol,trade_date,broker)
);

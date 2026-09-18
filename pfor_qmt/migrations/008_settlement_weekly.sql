CREATE TABLE futures_settlements (
    source text NOT NULL, ts_code text NOT NULL, exchange text NOT NULL, trade_date date NOT NULL,
    settle numeric, trading_fee_rate numeric, trading_fee numeric, delivery_fee numeric,
    b_hedging_margin_rate numeric, s_hedging_margin_rate numeric, long_margin_rate numeric,
    short_margin_rate numeric, offset_today_fee numeric,
    updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(source,ts_code,trade_date)
);
CREATE TABLE futures_weekly_details (
    source text NOT NULL, exchange text NOT NULL, prd text NOT NULL, week_date date NOT NULL,
    week text NOT NULL, name text, vol numeric, vol_yoy numeric, amount numeric, amout_yoy numeric,
    cumvol numeric, cumvol_yoy numeric, cumamt numeric, cumamt_yoy numeric, open_interest numeric,
    interest_wow numeric, mc_close numeric, close_wow numeric, original_amount numeric, original_cumamt numeric,
    normalization_version text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(source,exchange,prd,week_date)
);

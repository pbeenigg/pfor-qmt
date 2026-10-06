CREATE TABLE exchange_notices (
    id bigserial PRIMARY KEY,
    exchange text NOT NULL,
    url text NOT NULL,
    content_hash text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    published_at date,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    checked_at timestamptz NOT NULL DEFAULT now(),
    is_current boolean NOT NULL DEFAULT true,
    parse_state text NOT NULL CHECK(parse_state IN ('parsed','pending_verification')),
    parse_detail text NOT NULL,
    parser_version text NOT NULL,
    UNIQUE(exchange,url,content_hash)
);
CREATE UNIQUE INDEX exchange_notices_current_idx ON exchange_notices(exchange,url) WHERE is_current;
CREATE TABLE exchange_holiday_events (
    notice_id bigint NOT NULL REFERENCES exchange_notices(id),
    ordinal integer NOT NULL,
    start_day date NOT NULL,
    end_day date NOT NULL CHECK(end_day>=start_day),
    kind text NOT NULL CHECK(kind IN ('closed','open','night_closed','night_open')),
    evidence text NOT NULL,
    PRIMARY KEY(notice_id,ordinal)
);
CREATE TABLE exchange_holiday_sync (
    exchange text NOT NULL,
    year integer NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT now(),
    succeeded_at timestamptz,
    state text NOT NULL,
    detail text NOT NULL,
    notice_count integer NOT NULL DEFAULT 0,
    PRIMARY KEY(exchange,year)
);
COMMENT ON TABLE exchange_notices IS '交易所休市公告原文与版本，独立于行情源日历';
COMMENT ON COLUMN exchange_notices.id IS '公告版本编号';
COMMENT ON COLUMN exchange_notices.exchange IS '公告所属交易所';
COMMENT ON COLUMN exchange_notices.url IS '官方公告原文地址';
COMMENT ON COLUMN exchange_notices.content_hash IS '标题、发布时间及正文的内容摘要';
COMMENT ON COLUMN exchange_notices.title IS '公告原始标题';
COMMENT ON COLUMN exchange_notices.body IS '公告正文，不保存脚本与样式';
COMMENT ON COLUMN exchange_notices.published_at IS '明确的公告发布日期，未提供时为空';
COMMENT ON COLUMN exchange_notices.fetched_at IS '首次保存此版本的时间';
COMMENT ON COLUMN exchange_notices.checked_at IS '最近确认此版本的时间';
COMMENT ON COLUMN exchange_notices.is_current IS '是否为此地址最近读到的版本';
COMMENT ON COLUMN exchange_notices.parse_state IS '日期安排是否完整识别，未确认版本不用于自动判定';
COMMENT ON COLUMN exchange_notices.parse_detail IS '解析结果或待核验原因';
COMMENT ON COLUMN exchange_notices.parser_version IS '生成日期安排的解析规则版本';
COMMENT ON TABLE exchange_holiday_events IS '从公告明确语句提取的开休市与夜盘安排';
COMMENT ON COLUMN exchange_holiday_events.notice_id IS '所属公告版本编号';
COMMENT ON COLUMN exchange_holiday_events.ordinal IS '公告内安排顺序';
COMMENT ON COLUMN exchange_holiday_events.start_day IS '安排开始自然日';
COMMENT ON COLUMN exchange_holiday_events.end_day IS '安排结束自然日';
COMMENT ON COLUMN exchange_holiday_events.kind IS '全天开休市或当晚夜盘开停，不能据此猜测行情交易日';
COMMENT ON COLUMN exchange_holiday_events.evidence IS '日期安排对应的原文语句';
COMMENT ON TABLE exchange_holiday_sync IS '交易所及年度范围最近同步结果，失败保留成功缓存';
COMMENT ON COLUMN exchange_holiday_sync.exchange IS '请求同步的交易所';
COMMENT ON COLUMN exchange_holiday_sync.year IS '用户选择的公告安排年度';
COMMENT ON COLUMN exchange_holiday_sync.attempted_at IS '最近同步尝试时间';
COMMENT ON COLUMN exchange_holiday_sync.succeeded_at IS '最近读到且解析成功的同步时间';
COMMENT ON COLUMN exchange_holiday_sync.state IS '最近同步结果，不表示完整交易日历';
COMMENT ON COLUMN exchange_holiday_sync.detail IS '访问、解析或发现范围的结果说明';
COMMENT ON COLUMN exchange_holiday_sync.notice_count IS '此次确认的公告数';

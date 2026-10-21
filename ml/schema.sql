-- ml_state：时序解码模型输出（追加表，不改动既有 21 表）
CREATE TABLE IF NOT EXISTS ml_state (
    date TEXT PRIMARY KEY,
    state TEXT CHECK (state IN ('fatigue', 'adaptation', 'overtraining_risk')),
    p_fatigue REAL,
    p_adaptation REAL,
    p_overtraining REAL,
    model_version TEXT,
    feature_version TEXT,
    features TEXT,             -- JSON 快照，便于回溯
    baseline_readiness REAL,   -- 旧 pillar 准备度（对比用）
    created_at TEXT
);

-- activity_labels：活动 FIT 里的佳明手动评价（workout_rpe / workout_feel）
CREATE TABLE IF NOT EXISTS activity_labels (
    start_time TEXT PRIMARY KEY,   -- 本地时间，唯一标识一次活动
    date TEXT,
    sport_id TEXT,
    rpe INTEGER,
    feel INTEGER,
    source TEXT
);

-- weather_daily：每日环境（热应激/湿度是 HRV 的独立混杂）
CREATE TABLE IF NOT EXISTS weather_daily (
    date TEXT PRIMARY KEY,
    t_max REAL, t_min REAL, humidity REAL, apparent_max REAL, precip REAL,
    source TEXT
);

-- sleep_weekly：佳明睡眠报表是「周聚合」，不能当每日值用；单独存，作为慢变量上下文
CREATE TABLE IF NOT EXISTS sleep_weekly (
    date_start TEXT PRIMARY KEY,   -- 周首日
    date_end TEXT,                 -- 周末日
    avg_hours REAL,
    score REAL,
    quality TEXT,
    source TEXT
);

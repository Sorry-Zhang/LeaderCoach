-- ═══════════════════════════════════════════════════════════
-- 佳明 AI 教练 · schema v1.7（DDL 唯一源，禁止修订，只许追加）
-- 21 张表 · 七个域
-- ═══════════════════════════════════════════════════════════

-- ═══ A 档案与注册 ═══

CREATE TABLE meta (
key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE athlete_profile (
id INTEGER PRIMARY KEY CHECK (id=1),
birth_year INTEGER, sex TEXT NOT NULL,
height_cm REAL,
hr_max INTEGER, hr_max_source TEXT,
hr_rest INTEGER, hr_rest_source TEXT,
sleep_need_h REAL DEFAULT 7.5,
hr_zones TEXT NOT NULL,
hr_zones_source TEXT,
contraception TEXT CHECK (contraception IN ('none','hormonal','hormonal_continuous','nonhormonal')),
calendar_grid TEXT,
goal_text TEXT, race_date TEXT,
updated_at TEXT NOT NULL);

CREATE TABLE device_registry (
model TEXT NOT NULL, fw TEXT NOT NULL,
first_seen TEXT, last_seen TEXT,
expected TEXT,
PRIMARY KEY (model, fw));

CREATE TABLE sport_registry (
sport_id TEXT PRIMARY KEY,
display_name TEXT NOT NULL,
family TEXT NOT NULL CHECK (family IN ('run_family','climb_family','strength','skill_fixed','unclassified')),
role TEXT CHECK (role IN ('primary','secondary','support','fixed')),
load_path TEXT NOT NULL CHECK (load_path IN ('srpe','srpe+hr_cross','srpe+power')),
pattern TEXT NOT NULL,
priority INTEGER CHECK (priority BETWEEN 1 AND 3),
goal_text TEXT, goal_date TEXT,
weekly_sessions_min INTEGER, weekly_sessions_max INTEGER,
maintenance_floor_sessions INTEGER,
target_load_share REAL,
mesocycle_state TEXT DEFAULT 'maintenance' CHECK (mesocycle_state IN ('progression','maintenance','peak','recovery')),
obs_state TEXT DEFAULT 'observation' CHECK (obs_state IN ('observation','active')),
active INTEGER DEFAULT 1, created_at TEXT);

-- ═══ B 活动与生理 ═══

CREATE TABLE activities (
id INTEGER PRIMARY KEY AUTOINCREMENT,
garmin_activity_id TEXT UNIQUE,
fit_path TEXT,
start_time TEXT NOT NULL,
sport_id TEXT NOT NULL REFERENCES sport_registry(sport_id),
duration_min REAL NOT NULL,
distance_km REAL, avg_hr INTEGER, max_hr INTEGER,
hr_zones_pct TEXT,
avg_pace_min_km REAL, elevation_gain_m REAL,
power_avg_w INTEGER, power_max_w INTEGER,
dynamics TEXT,
te_aerobic REAL, te_anaerobic REAL, official_load REAL,
indoor INTEGER,
temp_start_c REAL, humidity_pct REAL, wind_kph REAL,
weather_condition TEXT,
weather_status TEXT CHECK (weather_status IN ('ok','unavailable','indoor','skipped')),
climb_style TEXT CHECK (climb_style IN ('bouldering','route')),
venue TEXT CHECK (venue IN ('indoor','outdoor')),
grade_system TEXT CHECK (grade_system IN ('V','YDS')),
grade_top TEXT, grade_typical TEXT,
routes_total INTEGER, routes_sent INTEGER, falls INTEGER,
annotation TEXT,
rpe INTEGER CHECK (rpe BETWEEN 1 AND 10),
srpe_au REAL,
hr_trimp REAL,
load_final REAL,
load_path_used TEXT CHECK (load_path_used IN ('srpe','bridge_trimp','manual')),
intensity_domain TEXT CHECK (intensity_domain IN ('low','moderate','high')),
mismatch_flag TEXT,
calc_version TEXT, param_version TEXT,
created_at TEXT, updated_at TEXT);

CREATE INDEX idx_act_date ON activities(start_time);
CREATE INDEX idx_act_sport ON activities(sport_id, start_time);

CREATE TABLE activity_splits (
activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
km INTEGER NOT NULL,
pace_min_km REAL, avg_hr INTEGER, elevation_gain_m REAL,
PRIMARY KEY (activity_id, km));

CREATE TABLE daily_metrics (
date TEXT PRIMARY KEY,
sleep_hours REAL, sleep_deep_h REAL, sleep_light_h REAL,
sleep_rem_h REAL, sleep_awake_h REAL,
sleep_start TEXT, sleep_end TEXT, sleep_mid TEXT,
hrv_last_night REAL, hrv_baseline REAL, hrv_dev_pct REAL,
resting_hr INTEGER, rhr_baseline REAL,
stress_daily_avg REAL,
data_status TEXT DEFAULT 'ok' CHECK (data_status IN ('ok','partial','missing')));

CREATE TABLE official_snapshot (
date TEXT PRIMARY KEY,
training_readiness REAL, recovery_time_h REAL,
load_7d REAL, load_ratio_4w1w REAL,
training_status TEXT, raw TEXT);

CREATE TABLE body_metrics (
measured_at TEXT PRIMARY KEY,
weight_kg REAL NOT NULL,
weight_7d_avg REAL,
body_fat_pct REAL);

-- ═══ C 力量 ═══

CREATE TABLE strength_sessions (
id INTEGER PRIMARY KEY AUTOINCREMENT,
date TEXT NOT NULL,
exercise_id TEXT NOT NULL,
pattern TEXT NOT NULL CHECK (pattern IN ('squat','hinge','push','pull','core','hang','carry')),
sets INTEGER, reps INTEGER, duration_s REAL,
weight_kg REAL, added_kg REAL,
rpe INTEGER CHECK (rpe BETWEEN 1 AND 10),
srpe_au REAL NOT NULL,
vi REAL, vi_ref REAL,
mismatch_flag TEXT CHECK (mismatch_flag IN ('cap_high_feel_low','cap_low_feel_high')),
notes TEXT, created_at TEXT);

CREATE INDEX idx_str_date ON strength_sessions(date);
CREATE INDEX idx_str_ex ON strength_sessions(exercise_id, date);

CREATE TABLE strength_baseline (
exercise_id TEXT PRIMARY KEY,
volume_proxy_mean REAL, n_sessions INTEGER, updated_at TEXT);

-- ═══ D 周期 ═══

CREATE TABLE cycle_periods (
id INTEGER PRIMARY KEY AUTOINCREMENT,
start_date TEXT NOT NULL UNIQUE,
end_date TEXT,
source TEXT CHECK (source IN ('garmin','user')),
created_at TEXT);

CREATE TABLE cycle_daily (
date TEXT PRIMARY KEY,
flow TEXT CHECK (flow IN ('none','light','moderate','heavy')),
symptoms TEXT,
bbt_c REAL,
note TEXT, created_at TEXT);

CREATE TABLE cycle_stats (
key TEXT PRIMARY KEY,
value TEXT, updated_at TEXT);

-- ═══ E 主观与伤病 ═══

CREATE TABLE subjective (
date TEXT PRIMARY KEY,
soreness INTEGER CHECK (soreness BETWEEN 0 AND 10),
sleep_feel INTEGER CHECK (sleep_feel BETWEEN 1 AND 5),
note TEXT, injury_flag INTEGER DEFAULT 0,
created_at TEXT);

-- ═══ 追加列（v1.7 → v1.8）：时变混杂日志 ═══
-- alcohol_units: 标准杯数（1 杯 ≈ 14 g 纯酒精）
-- caffeine_mg:   当日咖啡因摄入（毫克）
-- stress:        主观压力 1-5（1 很低，5 很高）
ALTER TABLE subjective ADD COLUMN alcohol_units REAL;
ALTER TABLE subjective ADD COLUMN caffeine_mg REAL;
ALTER TABLE subjective ADD COLUMN stress INTEGER;

CREATE TABLE injury_history (
id INTEGER PRIMARY KEY AUTOINCREMENT,
body_part TEXT NOT NULL, side TEXT,
onset_date TEXT NOT NULL, resolved_date TEXT,
status TEXT CHECK (status IN ('active','restricted','resolved')),
constraint_json TEXT,
note TEXT, created_at TEXT);

-- ═══ F 计划与引擎 ═══

CREATE TABLE weekly_plans (
id INTEGER PRIMARY KEY AUTOINCREMENT,
week_start TEXT NOT NULL,
template_id TEXT,
plan TEXT NOT NULL,
constraint_check TEXT,
validation_warnings TEXT,
llm_deviation_pct REAL,
status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','archived')),
created_at TEXT, activated_at TEXT);

CREATE TABLE engine_daily (
date TEXT PRIMARY KEY,
ctl REAL, atl REAL, tsb REAL,
acwr REAL, ewma7 REAL, ewma28 REAL,
readiness_score REAL,
readiness_confidence TEXT CHECK (readiness_confidence IN ('high','medium','low')),
pillars TEXT, inputs_used TEXT,
gates_triggered TEXT, flags TEXT,
param_version TEXT, calc_version TEXT);

-- ═══ G 治理 ═══

CREATE TABLE feedback (
id INTEGER PRIMARY KEY AUTOINCREMENT,
ts TEXT NOT NULL, suggestion_ref TEXT NOT NULL,
action TEXT NOT NULL CHECK (action IN ('accept','modify','reject')),
note TEXT);

CREATE TABLE audit_log (
id INTEGER PRIMARY KEY AUTOINCREMENT,
ts TEXT NOT NULL, tool_name TEXT NOT NULL,
caller_context TEXT,
params TEXT, result_status TEXT, result_summary TEXT);

CREATE TABLE learning_state (
param_key TEXT PRIMARY KEY,
scope TEXT,
value TEXT NOT NULL,
source TEXT CHECK (source IN ('default','calibration')),
evidence TEXT,
updated_at TEXT);

CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;

CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;

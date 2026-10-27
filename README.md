# LeaderCoach

[![中文](https://img.shields.io/badge/%E4%B8%AD%E6%96%87-current-red)](README.md)
[![English](https://img.shields.io/badge/English-switch-blue)](README.en.md)

本地优先的多运动训练引擎：把 Garmin 数据读入本地数据库，用确定性代码计算负荷、
恢复状态与当天的训练预测，再给出当天的训练安排。LLM 只做调整与解释，不参与运行时计算。

> 术语说明：代码与文档里的 **训练预测**（`training_forecast`，编号 G1–G10）指"今天可能
> 需要调整的情况"，不是安全审查；**主观记录**（`logging`）指每日主观状态与补录节奏；
> **训练安排**代替了早期的"处方"说法。数据库列名 `gates_triggered` 属于历史字段，含义
> 与训练预测相同，保留只为兼容旧数据。

![Training load vs HRV deviation](assets/load_vs_hrv.png)

> 单被试真实数据；横轴为相对天数，纵轴为标准化值，日期与绝对值已隐去。

## 版本能力演进

本项目的采集能力是**分阶段开放**的，不是一开始就全量：

- 起初为了先把链路跑通、减少单次任务量，只读取了活动摘要和少量日指标（睡眠时长、HRV、
  静息心率、压力均值）。这一阶段的目标是验证"数据 → 引擎 → 训练安排"是否闭合。
- 随着引擎侧的表结构、解析器和校验逻辑逐步完善，采集范围才扩展到插件全部只读工具，
  并补上睡眠阶段时间线、夜间序列、运动分段和官方训练指标。
- 因此 `docs/` 里早期文档会写"只有运动数据""睡眠 0 天"，那是当时的真实状态；现在以
  `README` 的数据表和 `store_ext.stats()` 的覆盖度为准。

## 架构

| 层   | 位置                                   | 作用                               |
| --- | ------------------------------------ | -------------------------------- |
| 采集  | `ingest/`                            | Garmin 插件 MCP、FIT/CSV 解析、天气      |
| 观测  | `ml/features.py`、`core/daily.py`     | 日面板、HRV 偏离、CTL/ATL/TSB、ACWR      |
| 模型  | `ml/`                                | Ridge / GBM、walk-forward；当前为研究原型 |
| 控制  | `core/engine.py`、`core/templates.py` | G1–G10 训练预测、T1–T6 模板               |
| 解释  | `coach/`                             | LLM 只做调整与解释                      |

## 数据

- 活动：Garmin FIT / CSV；
- 每日生理：HRV、睡眠、静息心率、压力；
- 标签：每日主观 + 运动后 RPE/Feel；
- 天气：Open-Meteo（可选）。

### Garmin 插件全量采集（v1.8）

`python main.py sync` 与 MCP 的 `sync_garmin` 默认走 **`ingest/plugin_full.py`**，
调用插件的全部只读工具，原始返回 gzip 存档到 `archive/plugin_raw/`：

| 类别 | 工具 | 落到哪里 |
| --- | --- | --- |
| 活动 | `get_garmin_activities`（full 详情） | `activities`（心率区间、训练效果、官方负荷、步态）+ `garmin_activities` |
| 睡眠 | `get_garmin_sleep` | `daily_metrics`（时长/分期/入睡与醒来时刻）+ `garmin_metrics.sleep_score` |
| 睡眠阶段 | SDK 直读 `getSleepData`（`ingest/_sdk_sleep_probe.js`） | `sleep_stages`（阶段时间线）+ `sleep_series`（夜间心率/压力/身体电量/HRV/呼吸） |
| HRV | `get_garmin_hrv` / `get_garmin_hrv_summaries` | `hrv_readings`（每 5 分钟读数）+ `garmin_metrics`（5 min 峰值、周均值、平衡中心） |
| 压力 / 身体电量 | `get_garmin_stress` / `_summaries` / `get_garmin_body_battery` | `stress_series`、`body_battery_series`、`daily_metrics.stress_daily_avg` |
| 训练 | `get_garmin_training_readiness` / `daily_training_status` / `training_status` / `training_load_balance` / `max_metrics(_range)` / `race_predictions(_range)` | `garmin_metrics` + `official_snapshot` |
| 其它 | 心率、步数、体重、训练库、个人资料 | `garmin_metrics` / 原始存档 |
| 运动分段 | 本地 FIT 的 lap / split / 1 km 自动分段 | `activity_splits`（圈序号 + 配速/心率/爬升；明细存 `activities.annotation`） |

- 原始存档按 `tool/日期.json.gz` 保存（同名时更新为本轮返回）；解析器有变更时可
  `python -m ingest.plugin_full --replay` 用同一份存档重新解析回填。
- 历史回填按月分段跑（单次跨度要 ≤31 天，这是插件接口的限制）：
  `python -m ingest.plugin_full --start 2026-06-01 --end 2026-06-30`。
- `get_daily_brief` 的 `recovery` 新增 `sleep_window`、`sleep_detail`、`overnight_series`、
  `garmin_official`；`get_sleep_detail` 可拉取单日的完整阶段时间线与夜间序列。
- 旧的最小采集路径仍可用：`python main.py sync --legacy` 或 `sync_garmin(full=False)`。
- Garmin 官方指标是**隔离层**，只作参考，不参与自算准备度；`config.toml` 的
  `[garmin_plugin]` 可调 `raw_dir` / `full_sync` / `replay_raw_each_sync`。

### 数据可用性随设备设置变化（以本账号实测为准）

| 数据 | 实测覆盖 | 说明 |
| --- | --- | --- |
| 睡眠汇总 + 入睡/醒来时刻 | 2026-03-11 起完整 | 早期接口不带阶段时间线，但有窗口和分期小时数 |
| 睡眠阶段时间线 + 夜间序列 | **2026-06-01 起** | SDK 只对近期数据返回 `sleepLevels` / 心率 / 压力序列 |
| HRV 每 5 分钟读数 | 约 6 月起，125/149 天 | 部分夜晚接口返回空数组 |
| 压力 / 身体电量时间序列 | **2026-06-01 起** | 更早的日期 `stressValuesArray` 为空，只有日均值 |
| 训练状态 / 负荷平衡 / 比赛预测 | 2026-03-11 起完整 | |
| 活动完整详情 | 仅最近约 100 条 | 插件不按日期过滤活动，回填不会补到更早的活动 |

### 睡眠状态评分（v1.9）

睡眠模块的目标不是预测睡眠，而是**用昨晚的睡眠推断今天恢复到什么程度**，再决定当天练什么。
实现见 `core/sleep_health.py`，输出写入 `garmin_metrics`（`sleep_state_score` 及各维度）。

六个维度（0–100，加权合成 SSHI）：

| 维度 | 依据 | 权重 |
| --- | --- | --- |
| 时长 duration | 相对**个人睡眠需要量**的偏差 + 7 天睡眠债 | 0.25 |
| 规律 regularity | 相邻两天的睡眠规律指数（SRI，5 分钟 epoch 一致率）；缺阶段时用睡眠中点离散度 | 0.25 |
| 时序 timing | 睡眠中点相对个人 28 天中点的偏移 | 0.10 |
| 效率 efficiency | 总睡眠 / 卧床时间 | 0.15 |
| 稳定 stability | 夜间清醒时长、觉醒次数、阶段切换频率 | 0.15 |
| 主观 subjective | `sleep_feel` 1–5；缺省时按客观项推算并标记 `derived` | 0.10 |

**个人睡眠需要量是初始化时设定的定量**：在运动档案里写 `athlete_profile.sleep_need_h`
（本项目当前设为 **7.5 h**），每名运动员按自己的情况设定；`config.toml` 的
`sleep.personal_need_h` 只在该字段为空时兜底。AASM 的 7–9 h 仅作为下限参考，
**不覆盖个人设定**。

接入方式：

- 睡眠支柱改用 `sleep_state_score`，替代原来"睡了几小时"的绝对插值（缺该分数时自动退回）。
- 观察结果（6–10 月 149 天）：按 `min(p10, 60)` 共有 **11 天**满足条件，
  0 天真正拦截（`sleep_g8_enforced` 全为 0）。这些天将作为后续验证的候选样本。
- 训练预测 G8 默认处于**观察模式**（`sleep.g8_mode = "observe"`）：触发条件是
  `睡眠状态分 < min(个人 28 天 p10, 绝对下限 60)`，命中时只把 `sleep_g8_triggered=1`
  及阈值写进 `garmin_metrics` 和简报，**不改变当天训练安排**。要真正拦截，需显式把
  `g8_mode` 改成 `"enforce"`；没有睡眠分时才退回 `sleep_hours_floor` 的绝对时长判断。
  设为观察模式的原因：现有数据里睡眠分对当天表现几乎没有预测力（见下），
  先积累“若启用会命中哪些天”的证据，再决定是否值得砍质量课。
- 分期（深/浅/REM）只用于夜间稳定与解释，**不单独触发调整**——手腕设备的分期误差较大。

### 主观记录与"默认值填写"约定

- `log_subjective` 的 `sleep_feel`（1–5）是睡眠模块的**主观项**，保留独立权重。
- **允许默认值落库**：用户没有对"醒来后的身体状态"提出异议时，按睡眠引擎预测的感受
  （由睡眠时长/规律性/效率与个人基准推算）填写即可，`note` 里注明 `source: derived`；
  用户明确提出异议时再覆盖，并保留原预测值便于事后比对。
- 这样不会因为漏填而让主观支柱长期缺失；但**主观项永远是修正项，不替代客观睡眠指标**，
  也不作为训练预测的唯一依据（与 `sleep_detail` 一起解释）。

### 当前验证结果（描述性，不作因果结论）

6–10 月共 149 天有睡眠状态分（均值 79.1，SD 10.7）。与当天/次日状态的关系：

| 对照 | 相关系数 | n |
| --- | --- | --- |
| 同日活动 RPE | 0.09 | 130 |
| 同日 Feel | −0.12 | 128 |
| 跑步速度/心率 | −0.09 | 175 |
| 次日 HRV | 0.05 | 124 |

**结论：在这份数据里，睡眠状态分对当天主观用力与表现几乎没有线性预测力。**
这与本项目此前的负结果一致（训练负荷对睡眠的增量 R²≈0），也和文献中"睡眠与次日
HRV 只有弱相关"相符。因此当前只把睡眠用作**保守的排除条件**（极差时不给质量课），
不把它当作训练强度的正向依据；后续需要更多"同配速下的心率漂移/计时测试"这类
表现锚点，才谈得上验证它是否真的有用。

## 隐私

本仓库**只含框架代码**，不含任何个人数据：

- 不上传：FIT、CSV、数据库、备份、个人档案、成绩、日期与生理数值；
- 图示为单被试真实数据，但已隐去日期轴与绝对值；
- 完整数据可在与作者沟通后单独提供。

## 结果

数据截至 **2026-10-20**，**n=175**。  
若后续数字有差异，以最新一次 `python -m ml.transition` 的输出为准。

| 模型    | MAE    | R²     |
| ----- | ------ | ------ |
| Ridge | 13.711 | −0.248 |
| GBM   | 15.316 | −0.454 |

- 分类基线 macro-F1 ≈ 0.29–0.31（chance 水平）；
- 长窗口暴露对后续睡眠的增量 R² ≈ 0；
- 结论：当前模型**不能用于决策**，瓶颈在因果识别，不在模型容量。

## 快速开始

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-ml.txt

# 1) 安装 Garmin 插件（MIT，单独安装，不随本仓库分发）
npm i -g dsh-plugin-garmin-connect

# 2) 用环境变量指定插件路径（不要写进代码）
$env:GARMIN_PLUGIN_MCP_JS  = "<插件目录>\lib\mcp.js"
$env:GARMIN_PLUGIN_SESSION = "<插件会话文件>"
$env:GARMIN_PLUGIN_NODE    = "node"

# 3) 初始化、同步、今日安排
python main.py init
python main.py sync --days 60
python main.py today
```

## 目录

```
core/       引擎：负荷、恢复、训练预测、模板选择
ingest/     采集：插件桥、FIT/CSV、天气
ml/         模型：特征、基线、转移、暴露、锚点
templates/  T1–T6 模板
db/         schema.sql / seed.sql
mcp_server/ MCP 工具
coach/      提示词模板
tests/      引擎与 schema 测试
```

## 更新

- 2026-10-27（v1.10）：G8 改为观察模式——触发条件 `睡眠状态分 < min(个人 28 天 p10, 60)`，
  只写入 `garmin_metrics` 与简报、不改变训练；`sleep.g8_mode="enforce"` 才真正拦截。
- 2026-10-27（v1.9）：新增睡眠状态评分 `core/sleep_health.py`（六维、个人需要量 7.5 h 可配）；
  睡眠支柱改用状态分；G8 改为按状态分判断；术语统一（训练预测 / 主观记录 / 训练安排）。
- 2026-10-27（v1.8）：`sync_garmin` / `main.py sync` 默认启用插件**全量只读采集**
  （23 个工具除写接口外的全部调用、原始 gzip 存档、睡眠阶段时间线、HRV/压力/身体电量时序、
  运动分段）；扩展表 `core/store_ext.py`；新增 `get_sleep_detail`。
- 2026-10-23：新增 `sync_garmin`（MCP 内同步）；HRV 单日骤降 G4b/G4c，动作由用户选择；双基线（轮动 14 天 + Garmin 参考）。
- 2026-10-21：首个公开版本。

## 许可证

- 本项目代码：MIT，见 `LICENSE`；
- 第三方组件与引用：见 `THIRD_PARTY_NOTICES.md`；
- Garmin 原始数据不随仓库分发，需使用者自行采集。

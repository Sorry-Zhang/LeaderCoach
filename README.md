# LeaderCoach

[![中文](https://img.shields.io/badge/%E4%B8%AD%E6%96%87-current-red)](README.md)
[![English](https://img.shields.io/badge/English-switch-blue)](README.en.md)

本地优先的多运动训练引擎：把 Garmin 数据读入本地数据库，用确定性代码计算负荷、
恢复与闸门，再给出当天的训练安排。LLM 只做调整与解释，不参与运行时计算。

![Training load vs HRV deviation](assets/load_vs_hrv.png)

> 单被试真实数据；横轴为相对天数，纵轴为标准化值，日期与绝对值已隐去。

## 架构

| 层   | 位置                                   | 作用                               |
| --- | ------------------------------------ | -------------------------------- |
| 采集  | `ingest/`                            | Garmin 插件 MCP、FIT/CSV 解析、天气      |
| 观测  | `ml/features.py`、`core/daily.py`     | 日面板、HRV 偏离、CTL/ATL/TSB、ACWR      |
| 模型  | `ml/`                                | Ridge / GBM、walk-forward；当前为研究原型 |
| 控制  | `core/engine.py`、`core/templates.py` | G1–G10 闸门、T1–T6 模板               |
| 解释  | `coach/`                             | LLM 只做调整与解释                      |

## 数据

- 活动：Garmin FIT / CSV；
- 每日生理：HRV、睡眠、静息心率、压力；
- 标签：每日主观 + 运动后 RPE/Feel；
- 天气：Open-Meteo（可选）。

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

# 3) 初始化、同步、今日处方
python main.py init
python main.py sync --days 60
python main.py today
```

## 目录

```
core/       引擎：负荷、恢复、闸门、模板选择
ingest/     采集：插件桥、FIT/CSV、天气
ml/         模型：特征、基线、转移、暴露、锚点
templates/  T1–T6 模板
db/         schema.sql / seed.sql
mcp_server/ MCP 工具
coach/      提示词模板
tests/      引擎与 schema 测试
```

## 许可证

- 本项目代码：MIT，见 `LICENSE`；
- 第三方组件与引用：见 `THIRD_PARTY_NOTICES.md`；
- Garmin 原始数据不随仓库分发，需使用者自行采集。

from __future__ import annotations

import argparse
from datetime import date

from core import db as dbmod
from core.config import load_config

_STATE_CN = {"adaptation": "适应良好", "fatigue": "疲劳累积", "overtraining_risk": "过度训练风险"}
_STATE_ACT = {"adaptation": "按计划推进", "fatigue": "减量 / 轻松课",
              "overtraining_risk": "停训评估，必要时就医"}

def daily_report(conn, day: str) -> str:
    row = conn.execute("SELECT * FROM ml_state WHERE date=?", (day,)).fetchone()
    if not row:
        return _descriptive(conn, day)
    state = row["state"]
    prob = {"adaptation": row["p_adaptation"], "fatigue": row["p_fatigue"],
            "overtraining_risk": row["p_overtraining"]}.get(state)
    prob = float(prob) if prob is not None else float("nan")
    base = row["baseline_readiness"]
    lines = [
        f"# {day} 状态报告（确定性输出）",
        "",
        f"- **状态**：{_STATE_CN.get(state, state)}（置信 {prob:.2f}）",
        f"- 概率：适应 {_f(row['p_adaptation'])} / 疲劳 {_f(row['p_fatigue'])} / "
        f"过度训练 {_f(row['p_overtraining'])}",
        f"- 建议：{_STATE_ACT.get(state, '')}",
        f"- 旧 pillar 准备度：{base if base is not None else '无'}",
        f"- 模型版本：{row['model_version']}（特征 {row['feature_version']}）",
        "",
        "> 本报告由确定性模型生成，不含 LLM 推断；仅供训练参考，伤病/持续疼痛请就医。",
    ]
    return "\n".join(lines)

def _descriptive(conn, day: str) -> str:
    from ml.features import build_panel
    df = build_panel(conn, day, day)
    if df.empty:
        return f"# {day} 状态报告\n\n该日无数据。\n"
    r = df.iloc[-1]

    def g(key, fmt="{:.1f}"):
        v = r.get(key)
        try:
            if v is None or v != v:
                return "—"
            return fmt.format(v)
        except Exception:
            return "—"

    return "\n".join([
        f"# {day} 状态报告（尚无模型，描述性输出）",
        "",
        f"- 夜间 HRV：{g('hrv', '{:.0f}')} ms（相对个人基线 {g('hrv_dev', '{:+.1f}')}%）",
        f"- 周睡眠均值：{g('sleep_week_avg')} h",
        f"- 当日负荷：{g('load')} AU",
        f"- TSB：{g('tsb')}（冷启动时不可信）",
        f"- ACWR：{g('acwr', '{:.2f}')}",
        "",
        "> 尚未训练三分类模型（主观标签不足）。以上为确定性描述，不含推断。",
    ])

def _f(v) -> str:
    return f"{float(v):.2f}" if v is not None else "—"

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=date.today().isoformat())
    args = p.parse_args()
    conn = dbmod.get_conn(load_config())
    print(daily_report(conn, args.date))

from __future__ import annotations

import json
from pathlib import Path

def capability_matrix(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM sport_registry WHERE active=1 ORDER BY priority").fetchall()
    out = []
    for r in rows:
        out.append({
            "sport_id": r["sport_id"],
            "display_name": r["display_name"],
            "role": r["role"],
            "family": r["family"],
            "load_path": r["load_path"],
            "pattern": r["pattern"],
            "mesocycle": r["mesocycle_state"],
            "floor_sessions": r["maintenance_floor_sessions"],
            "goal": r["goal_text"],
            "goal_date": r["goal_date"],
        })
    return out

def athlete_profile(conn) -> dict:
    r = conn.execute("SELECT * FROM athlete_profile WHERE id=1").fetchone()
    if not r:
        return {}
    return {k: r[k] for k in r.keys() if k != "id"}

def gates_table(cfg: dict) -> str:
    rows = [
        ("G1", "TSB ≤ −100", "当日强制轻松课"),
        ("G2", "TSB ≤ −170", "休息日"),
        ("G3", "ACWR > 1.5", "质量课冻结（当日+次日）"),
        ("G4", "HRV dev ≤ −15% 连续 2 天", "强度降级一档"),
        ("G5", "RHR dev ≥ +10% 连续 2 天 或发热", "疾病协议（休息）"),
        ("G6", "伤病旗/active 约束", "禁跑 + 按伤位限制"),
        ("G7", "readiness < 40", "默认轻松日"),
        ("G8", "睡眠 < 5h", "当日无质量课"),
        ("G9", "RED-S active", "负荷−20~30% + 蛋白质 1.8–2.2 g/kg"),
        ("G10", "痛经 cramps ≥7/10", "当日强制轻松课"),
    ]
    lines = ["| 闸门 | 条件 | 动作 |", "|---|---|---|"]
    for g, c, a in rows:
        lines.append(f"| {g} | {c} | {a} |")
    return "\n".join(lines)

def interference_matrix() -> str:
    return """- 下肢大重量（squat/hinge，RPE≥7）→ 跑步质量课：间隔 24h
- 长距离 ≥90min（run 族）→ 下肢力量：间隔 24h
- 指力板（hang）↔ 攀岩：间隔 48h（双向）
- 高强度攀岩 → 上肢拉（pull/push）：间隔 24–48h
- 同日轻松跑 + 力量：力量在后，间隔 ≥6h
- 同日攀强度 + 跑：攀在前（落地缓冲，安全顺序）
- 减脂模式激活：上述间隔 ×1.5
- 同日双课 RPE 之和 ≥15 → 次日不排质量课"""

def weekly_constraints() -> str:
    return """- 校验器 R1–R17 三档判定（REJECT 拒绝 / WARN 告警 / PASS），证据标签随行。
- 周预算 = 7 × CTL × 模板 modifier，上限 ≤ 7×CTL×1.08。
- 每周 ≥1 个零负荷日（day_type=rest_zero）。
- 质量课 ≤3/周（减脂 ≤2）；低强度占比 ≥75%。
- 完成率口径：闸门豁免后口径（撤课不入分母）。"""

def build_system_prompt(cfg: dict, conn) -> str:
    tpl = (Path(cfg["_root"]) / "coach" / "system_prompt.md.j2").read_text(encoding="utf-8")
    ap = athlete_profile(conn)
    cm = capability_matrix(conn)
    return (tpl
            .replace("{{ENGINE_VERSION}}", "1.3")
            .replace("{{SCHEMA_VERSION}}", "1.7")
            .replace("{{PARAM_VERSION}}", "p2026-10")
            .replace("{{ATHLETE_PROFILE_JSON}}", json.dumps(ap, ensure_ascii=False, indent=2))
            .replace("{{CAPABILITY_MATRIX_JSON}}", json.dumps(cm, ensure_ascii=False, indent=2))
            .replace("{{GATES_TABLE}}", gates_table(cfg))
            .replace("{{INTERFERENCE_MATRIX}}", interference_matrix())
            .replace("{{WEEKLY_CONSTRAINTS}}", weekly_constraints()))

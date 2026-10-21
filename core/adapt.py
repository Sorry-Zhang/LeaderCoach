from __future__ import annotations

CORE_SPORT = "run"
SECONDARY_SHARE = 0.20
ROTATION_WEEKS = 4
EASY_AU = 180
QUALITY_PCT = 0.15

def classify(hist: dict, sports: set) -> dict:
    total = sum(hist.values()) or 1
    out = {}
    for s in sports:
        c = hist.get(s, 0)
        if s == CORE_SPORT:
            out[s] = "core"
        elif c / total >= SECONDARY_SHARE:
            out[s] = "secondary"
        else:
            out[s] = "occasional"
    return out

def _easy_session(note: str, sport: str = CORE_SPORT) -> dict:
    return {"slot": "primary_easy", "sport_id": sport,
            "pattern": "lower_cardio" if sport == "other" else "lower_impact",
            "planned_au": EASY_AU, "planned_rpe": 3, "planned_min": 60,
            "structure": None, "notes": note}

def adapt_week(week: dict, hist: dict, focus: str | None = None,
               week_index: int = 0) -> dict:
    sports = set(hist) | {s.get("sport_id") for d in week["days"].values()
                          for s in d["sessions"] if s.get("sport_id")}
    cls = classify(hist, sports)
    notes = []
    rotation_week = (week_index % ROTATION_WEEKS == 0)

    for day, d in week["days"].items():
        kept = []
        for s in d["sessions"]:
            sport = s.get("sport_id")
            if cls.get(sport) == "occasional":
                if not rotation_week:
                    notes.append(f"{day}: {sport} 轮空（每 {ROTATION_WEEKS} 周一次）")
                    kept.append(_easy_session(f"{sport} 轮空 → 骑行有氧", sport="other"))
                    continue
                if (s.get("planned_rpe") or 0) >= 7:
                    s["planned_rpe"] = 5
                    s["notes"] = (s.get("notes") or "") + \
                        " 轮换周：强度 7→5，避免与质量课冲突"
                    notes.append(f"{day}: {sport} 轮换周强度 7→5")
            kept.append(s)
        d["sessions"] = kept

    if cls.get("other") == "secondary":
        replaced = False
        for d in week["days"].values():
            for s in d["sessions"]:
                if (not replaced and s.get("slot") == "primary_easy"
                        and s.get("sport_id") == CORE_SPORT):
                    s["sport_id"] = "other"
                    s["pattern"] = "lower_cardio"
                    s["notes"] = (s.get("notes") or "") + " 次要项目补位：轻松跑→骑行"
                    notes.append("轻松跑→骑行（次要项目补位）")
                    replaced = True

    if focus == "endurance":
        budget = week.get("week_budget_au") or 0
        for d in week["days"].values():
            for s in d["sessions"]:
                if s.get("slot") == "primary_quality" and s.get("structure") == "intervals":
                    s["structure"] = "threshold"
                    s["planned_rpe"] = 7
                    if budget:
                        s["planned_au"] = int(round(budget * QUALITY_PCT / 5.0) * 5)
                    s["notes"] = (s.get("notes") or "") + \
                        " 诊断=耐力短板：intervals→threshold，AU 降至 15%"
                    notes.append("质量课 intervals→threshold")

    week["adapt_notes"] = notes
    week["sport_classes"] = cls
    return week

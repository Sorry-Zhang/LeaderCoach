from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    ".git", ".venv", "__pycache__", ".pytest_cache", ".ipynb_checkpoints",
    "data", "Dataset", "archive", "backup", "reference", "reports",
    "_discuss", "领航", "docs",
}
SKIP_FILES = {"config.toml", "coach_prompt.md", "check_privacy.py"}
SKIP_REL = {"ml/README.md", "使用教程.md", "MCP接入教程.md"}
SKIP_PREFIX = (
    "docs/套磁材料",
    "docs/anchor_profile.md",
    "docs/route_a_findings.md",
    "docs/RESULTS.md",
    "docs/plan_audit.md",
)

CRITICAL = {
    "邮箱": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "Windows 用户路径": re.compile(r"C:\\Users\\[^\\\s\"']+"),
    "密码环境变量": re.compile(r"GARMIN_PASSWORD"),
    "会话文件": re.compile(r"\.session\.json"),
}

_extra = os.environ.get("PRIVACY_EXTRA_TERMS", "").strip()
if _extra:
    CRITICAL["自定义敏感词"] = re.compile(
        "|".join(re.escape(x) for x in _extra.split(",") if x.strip())
    )
WARN = {
    "本地绝对路径": re.compile(r"\bD:\\[^\s\"']+"),
}
TEXT_EXT = {".py", ".md", ".toml", ".json", ".sql", ".j2", ".txt",
            ".yml", ".yaml", ".cff"}
BINARY_EXT = {".db", ".fit", ".zip", ".pkl", ".joblib", ".png", ".jpg",
              ".jpeg", ".pdf", ".docx", ".xlsx", ".pyc"}
ALLOW_BINARY = ("assets/",)

def iter_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.name in SKIP_FILES:
            continue
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        if rel in SKIP_REL:
            continue
        if any(rel.startswith(x) for x in SKIP_PREFIX):
            continue
        yield p

def main() -> int:
    critical, warnings = [], []
    for p in iter_files():
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        if p.suffix.lower() == ".db":
            continue
        if p.suffix.lower() in BINARY_EXT and not rel.startswith(ALLOW_BINARY):
            critical.append((rel, "二进制/数据文件", "不应提交"))
            continue
        if p.suffix.lower() not in TEXT_EXT:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pat in CRITICAL.items():
            for m in pat.finditer(text):
                line = text[:m.start()].count("\n") + 1
                critical.append((f"{rel}:{line}", label, m.group(0)[:60]))
        for label, pat in WARN.items():
            for m in pat.finditer(text):
                line = text[:m.start()].count("\n") + 1
                warnings.append((f"{rel}:{line}", label, m.group(0)[:60]))

    if warnings:
        print(f"警告 {len(warnings)} 处（本地绝对路径，不影响隐私，但不可移植）：")
        for loc, label, detail in warnings[:20]:
            print(f"  [{label}] {loc} -> {detail}")
        if len(warnings) > 20:
            print(f"  ... 其余 {len(warnings) - 20} 处省略")
        print()

    if critical:
        print(f"阻断 {len(critical)} 处：")
        for loc, label, detail in critical:
            print(f"  [{label}] {loc} -> {detail}")
        print("\n请删除、改为环境变量，或加入 .gitignore。")
        return 1

    print("OK：未发现个人标识。")
    return 0

if __name__ == "__main__":
    sys.exit(main())

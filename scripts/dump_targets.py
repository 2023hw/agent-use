# -*- coding: utf-8 -*-
"""把语料里所有目标条文打印出来，用于校准领域分类器的关键词。"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lpa.profile import build_targets  # noqa: E402
from lpa.taxonomy import classify_domain  # noqa: E402

ROOT = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent")
LESSONS = json.loads((ROOT / "data" / "structured" / "lessons.json").read_text(encoding="utf-8"))

pieces = Counter()
for lesson in LESSONS:
    texts = {c["goal_text"] for c in lesson["children"] if c["goal_text"]}
    for t in texts:
        for spec in build_targets(t):
            pieces[spec.text] += 1

lines = ["# 目标条文清单（用于校准领域分类器）", ""]
lines.append(f"去重后共 {len(pieces)} 条")
lines.append("")
lines.append("| 出现次数 | 当前判定领域 | 目标条文 |")
lines.append("|---|---|---|")
for p, n in pieces.most_common():
    lines.append(f"| {n} | {classify_domain(p)} | {p[:120]} |")

target = ROOT / "data" / "extracted" / "TARGET_TEXTS.md"
target.write_text("\n".join(lines), encoding="utf-8")
print("wrote", target, len(pieces))

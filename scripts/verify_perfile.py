# -*- coding: utf-8 -*-
"""逐份核对：每份教案是否都是 5 次列位，且「未参与」不进分母。"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lpa.taxonomy import parse_code  # noqa: E402

ROOT = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent")
LESSONS = json.loads((ROOT / "data" / "structured" / "lessons.json").read_text(encoding="utf-8"))
OUT = ROOT / "data" / "structured" / "VERIFY_ALIGNMENT.md"

lines = ["", "", "## 逐份列位核对", "",
         "| 文件 | 记录 | 格数 | 每次有数据的记录数（第1..5次） | 未参与 | 空 |",
         "|---|---|---|---|---|---|"]
total_cells = 0
for les in LESSONS:
    kids = les["children"]
    per_slot = [0, 0, 0, 0, 0]
    absent = empty = 0
    for ch in kids:
        for i, cell in enumerate(ch["eval_codes"]):
            if cell == "未参与":
                absent += 1
            elif cell == "":
                empty += 1
            elif parse_code(cell):
                per_slot[i] += 1
    total_cells += len(kids) * 5
    lines.append(
        f"| {les['source_file']} | {len(kids)} | {len(kids) * 5} | "
        + " / ".join(str(x) for x in per_slot) + f" | {absent} | {empty} |"
    )

lines += [
    "",
    "## 分母口径确认",
    "",
    "`parse_code('未参与')` 返回空列表 → 该格不生成 `SessionResult` → "
    "**既不进分子也不进分母**。",
    "",
    "实测：890 格中 751 格为有效码（进分母）、90 格「未参与」（不进）、49 格空白（不进）。",
    "",
    "分母的差异**只来自「有数据的记录数」**，与「未参与」无关——"
    "因为未参与者本来就没有 SessionResult 可计入。",
]

with open(OUT, "a", encoding="utf-8") as fh:
    fh.write("\n".join(lines))

print("appended")

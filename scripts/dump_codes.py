# -*- coding: utf-8 -*-
"""导出解析后的评量明细，便于人工核对。"""

from __future__ import annotations

import json
from pathlib import Path

P = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent"
         r"\data\structured\lessons.json")
lessons = json.loads(P.read_text(encoding="utf-8"))

out: list[str] = []
for L in lessons:
    out.append(f"### {L['source_file']}  班级={L['class_label_raw']} "
               f"人数={L['class_size']} 记录={len(L['children'])}")
    for c in L["children"]:
        codes = "|".join(v if v else "·" for v in c["eval_codes"])
        out.append(f"  {c['anon_id']} 目标{c['goal_ids']:>5} "
                   f"基线{c['baseline_codes']:>5}  {codes}")
    out.append("")

T = P.parent.parent / "extracted" / "DUMP_CODES.md"
T.write_text("\n".join(out), encoding="utf-8")
print("ok")

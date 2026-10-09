# -*- coding: utf-8 -*-
"""列出所有含 F 的评量单元格，附完整行上下文，供判断 F 的含义。"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document

SRC = Path(r"C:\Users\17093\Desktop\homework\agent use idea")
NAME_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")
NON_NAME = {"儿童姓名", "个别化差异目标", "康复目标", "康复基线", "评量效果",
            "重度", "多重障碍", "轻度", "中度"}

out = ["# 所有含 F 的评量单元格", ""]

for f in sorted(SRC.glob("*.docx")):
    if f.name.startswith("~$"):
        continue
    doc = Document(str(f))
    hits = []
    for table in doc.tables:
        for row in table.rows:
            raw = [c.text.strip() for c in row.cells]
            if len(raw) < 12:
                continue
            nm = re.sub(r"\s+", "", raw[8])
            if not (NAME_RE.match(nm) and nm not in NON_NAME):
                continue
            cells = [re.sub(r"\s+", "", raw[i] or "") for i in range(10, min(len(raw), 16))]
            if any("F" in c for c in cells):
                hits.append((nm, re.sub(r"\s+", "", raw[9]), cells))
    if hits:
        out.append(f"## {f.name}")
        out.append("")
        for nm, base, cells in hits:
            out.append(f"- **{nm}** 基线=`{base}`　评量格={cells}")
        out.append("")

Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent\data\extracted\F_CASES.md").write_text(
    "\n".join(out), encoding="utf-8"
)
print("ok")

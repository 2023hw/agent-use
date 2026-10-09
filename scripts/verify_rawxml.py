# -*- coding: utf-8 -*-
"""反向核查：直接从原始 docx 的 <w:tc> XML 重读个别化表，
把每一行**完整原样打印**，人工核对「评量 5 格」是否真的对上「第一次~第五次」。

不做任何解析、不做任何假设，只输出下标 -> 原文。
"""

from __future__ import annotations

import sys
from pathlib import Path
from docx import Document
from docx.oxml.ns import qn

SRC = Path(r"C:\Users\17093\Desktop\homework\agent use idea")
ROOT = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent")
OUT = ROOT / "data" / "structured" / "VERIFY_ALIGNMENT.md"

TARGETS = [
    "第一周-快乐六一(1).docx",
    "第一周-劳动最快乐(1).docx",
    "闪电班4月21日主题教案(1).docx",
]


def tc_cells(row):
    out = []
    for tc in row._tr.findall(qn("w:tc")):
        paras = []
        for p in tc.iter(qn("w:p")):
            txt = "".join(t.text or "" for t in p.iter(qn("w:t"))).strip()
            if txt:
                paras.append(txt)
        out.append("\\n".join(paras))
    return out


lines = ["", "", "## 原始 XML 反向核对（表格逐行原样）", "",
         "下面把个别化表的**每一行每个 tc 下标**原样列出，不做任何解析。",
         "核对点：下标 3=姓名、4=目标编号、5=基线、**6~10=第一次~第五次评量**。",
         ""]

for name in TARGETS:
    p = SRC / name
    if not p.exists():
        lines.append(f"### {name} —— 文件不存在")
        continue
    doc = Document(str(p))
    lines += [f"### {name}", ""]
    if len(doc.tables) < 2:
        lines += ["只有 1 张表，跳过", ""]
        continue
    t = doc.tables[1]
    lines += ["| 行 | " + " | ".join(f"tc[{i}]" for i in range(11)) + " |",
              "|" + "---|" * 12]
    for ri, row in enumerate(t.rows):
        cells = tc_cells(row)
        if len(cells) < 4:
            continue
        padded = (cells + [""] * 11)[:11]
        disp = []
        for c in padded:
            c = c.replace("\n", "⏎")
            if len(c) > 14:
                c = c[:14] + "…"
            disp.append(c if c else "·")
        lines.append(f"| {ri} | " + " | ".join(disp) + " |")
    lines.append("")

with open(OUT, "a", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("ok")

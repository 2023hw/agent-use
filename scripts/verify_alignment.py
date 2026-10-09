# -*- coding: utf-8 -*-
"""对齐自检：确认 lessons.json 里每一格都是「评量数据」，没有把左侧内容混进来。

四项断言：
  A. eval_codes 长度恒为 5
  B. 每格非空值时，内容只能是 [PE]+ 或「未参与」，不含数字/汉字
  C. goal_ids 只含数字，baseline_codes 只含 P/E，且长度一致
  D. eval_codes 与 goal_ids 位数不强制相等（缺席会造成长短不一），
     但**若有非空格，其字母数应 <= 目标条数上限**

额外输出：所有非空格的形态直方图，肉眼扫一遍。
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent")
LESSONS = json.loads((ROOT / "data" / "structured" / "lessons.json").read_text(encoding="utf-8"))
OUT = ROOT / "data" / "structured" / "VERIFY_ALIGNMENT.md"

problems: list[str] = []
shape_counter: Counter = Counter()
n_cells = 0
n_empty = 0
n_absent = 0
n_code = 0

CODE_RE = re.compile(r"^[PE]+$")
NUM_RE = re.compile(r"^\d+$")

for les in LESSONS:
    for ch in les["children"]:
        sid = ch["anon_id"]
        f = les["source_file"]

        # A. 长度
        if len(ch["eval_codes"]) != 5:
            problems.append(f"[A] {f} {sid}: eval_codes 长度 {len(ch['eval_codes'])} != 5")

        # C. 编号与基线
        gid, base = ch["goal_ids"], ch["baseline_codes"]
        if not gid:
            problems.append(f"[C] {f} {sid}: goal_ids 为空")
        elif not NUM_RE.match(gid):
            problems.append(f"[C] {f} {sid}: goal_ids 含非数字 -> {gid!r}")
        if not base:
            problems.append(f"[C] {f} {sid}: baseline_codes 为空")
        elif not CODE_RE.match(base):
            problems.append(f"[C] {f} {sid}: baseline_codes 含非 P/E -> {base!r}")
        elif gid and NUM_RE.match(gid) and len(gid) != len(base):
            problems.append(f"[C] {f} {sid}: 编号位数 {len(gid)} != 基线长度 {len(base)}")

        # B. 逐格内容
        for k, cell in enumerate(ch["eval_codes"], start=1):
            n_cells += 1
            if cell == "":
                n_empty += 1
                shape_counter["(空)"] += 1
                continue
            if cell == "未参与":
                n_absent += 1
                shape_counter["未参与"] += 1
                continue
            n_code += 1
            shape_counter[cell] += 1
            if not CODE_RE.match(cell):
                problems.append(f"[B] {f} {sid} 第{k}次: 非 P/E 内容 -> {cell!r}")
            # D. 字母数不应超过目标条数
            if gid and NUM_RE.match(gid) and len(cell) > len(gid):
                problems.append(
                    f"[D] {f} {sid} 第{k}次: 字母数 {len(cell)} > 目标条数 {len(gid)} -> {cell!r}"
                )

# 额外：检查有没有把「目标文本」误当评量（长汉字串）
suspicious = [s for s in shape_counter if len(s) > 10]
for s in suspicious:
    problems.append(f"[B] 可疑长内容当作评量: {s[:40]!r}")

lines = [
    "# 对齐自检（未参与不计分母·五次评量固定位）",
    "",
    f"- 检查文件：**{len(LESSONS)}** 份",
    f"- 检查记录：**{sum(len(l['children']) for l in LESSONS)}** 条",
    f"- 检查格子：**{n_cells}**",
    "",
    "## 格子构成",
    "",
    "| 类型 | 数量 | 占比 |",
    "|---|---|---|",
    f"| P/E 有效码 | {n_code} | {n_code / n_cells:.1%} |",
    f"| 「未参与」 | {n_absent} | {n_absent / n_cells:.1%} |",
    f"| 真空格 | {n_empty} | {n_empty / n_cells:.1%} |",
    "",
    "## 断言结果",
    "",
]
if problems:
    lines.append(f"**发现问题 {len(problems)} 项：**")
    lines.append("")
    lines.extend(f"- {p}" for p in problems)
else:
    lines.append("**全部通过。** A（长度恒 5）/ B（只含 P/E 或未参与）/ "
                 "C（编号纯数字、基线纯 P/E、长度一致）/ D（字母数不超目标条数）均无违例。")

lines += ["", "## 非空格形态分布（前 30）", "", "| 形态 | 出现次数 | 说明 |", "|---|---|---|"]
for shape, n in shape_counter.most_common(30):
    if shape == "(空)":
        note = "该次未填，不计入任何分母"
    elif shape == "未参与":
        note = "缺席，不计入分母"
    else:
        note = f"{len(shape)} 条目标：P {shape.count('P')} / E {shape.count('E')}"
    lines.append(f"| `{shape}` | {n} | {note} |")

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"problems={len(problems)} cells={n_cells} code={n_code} absent={n_absent} empty={n_empty}")
for p in problems[:40]:
    print(p)

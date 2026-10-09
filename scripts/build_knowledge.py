# -*- coding: utf-8 -*-
"""从语料**统计**教师的支架梯级模式，作为 A/B/C 分层目标的结构先验。

只抽"结构"（辅助由重到轻的序列），不抄文本 —— 避免把语料的重复内容
带进生成层。产出 knowledge/ladder_patterns.json。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lpa.knowledge import DOMAIN_LIBRARY  # noqa: E402
from lpa.profile import build_targets  # noqa: E402
from lpa.taxonomy import (  # noqa: E402
    SUPPORT_RANK,
    classify_domain,
    is_classroom_behavior_goal,
    ladder_of,
)

ROOT = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent")
LESSONS = json.loads((ROOT / "data" / "structured" / "lessons.json").read_text(encoding="utf-8"))
OUT_KN = ROOT / "knowledge"
OUT_KN.mkdir(exist_ok=True)

ladders: Counter = Counter()
ladder_by_domain: Counter = Counter()
n_targets = 0
domain_counter: Counter = Counter()
verb_counter: Counter = Counter()

for les in LESSONS:
    children = les["children"]
    for ch in children:
        text = ch["goal_text"] or les["class_objectives"] or ""
        targets = build_targets(text)
        rungs = []
        for t in targets:
            n_targets += 1
            if is_classroom_behavior_goal(t.text):
                continue
            d = t.domain
            domain_counter[d] += 1
            for v in DOMAIN_LIBRARY.get(d, {}).get("verbs", []):
                if v in t.text:
                    verb_counter[v] += 1
            lv = ladder_of(t.text)
            if lv:
                rungs.append(lv[0])
        rungs_sorted = sorted(set(rungs), key=lambda x: SUPPORT_RANK.get(x, 99))
        if len(rungs_sorted) >= 2:
            seq = " → ".join(rungs_sorted)
            ladders[seq] += 1
            main_d = max((t.domain for t in targets
                          if not is_classroom_behavior_goal(t.text)
                          and t.domain != "未分类"),
                         key=lambda d: sum(1 for t in targets if t.domain == d),
                         default="未分类")
            ladder_by_domain[f"{main_d} || {seq}"] += 1

payload = {
    "note": "从 16 份语料统计得到的教师辅助梯级模式；只取结构，不取文本。",
    "n_targets_scanned": n_targets,
    "ladder_patterns": [
        {"seq": s, "count": n,
         "rungs": [x.strip() for x in s.split("→")]}
        for s, n in ladders.most_common()
    ],
    "ladder_by_domain": [
        {"domain": k.split(" || ")[0], "seq": k.split(" || ")[1], "count": n}
        for k, n in ladder_by_domain.most_common(30)
    ],
    "domain_target_counts": dict(domain_counter.most_common()),
    "observable_verb_counts": dict(verb_counter.most_common(40)),
}

(OUT_KN / "ladder_patterns.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

lines = ["# 从语料统计的支架梯级与领域分布", "",
         f"扫描目标条数：{n_targets}", "",
         "## 辅助梯级模式（结构先验，用于 A/B/C 分层）", "",
         "| 出现次数 | 梯级序列 |", "|---|---|"]
for s, n in ladders.most_common():
    lines.append(f"| {n} | {s} |")
lines += ["", "## 主领域分布", "", "| 领域 | 目标条数 |", "|---|---|"]
for d, n in domain_counter.most_common():
    lines.append(f"| {d} | {n} |")
lines += ["", "## 出现频次最高的可观测动词", "", "| 动词 | 次数 |", "|---|---|"]
for v, n in verb_counter.most_common(40):
    lines.append(f"| {v} | {n} |")
lines += ["", "> **说明**：本文件只抽「结构」，不抄语料文本。",
          "> 生成层的目标文本来自 `knowledge.py` 的领域库 + 大模型改写，",
          "> 梯级模式只用来决定「一条目标要拆成几档、顺序如何」。", ""]

(ROOT / "data" / "structured" / "KNOWLEDGE_MINING.md").write_text(
    "\n".join(lines), encoding="utf-8")

print(f"ok ladders={len(ladders)} domains={len(domain_counter)} verbs={len(verb_counter)}")

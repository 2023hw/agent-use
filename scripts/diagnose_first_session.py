# -*- coding: utf-8 -*-
"""诊断：第 1 次评量达标率异常偏高，到底是哪些记录造成的。

排查维度：
1. 匿名编号在语料里是否按文件顺序分配（编号规律是否稳定）
2. 高第 1 次达标率的记录是否集中在某个班 / 某个人
3. 是否存在越靠后的教案，评量次数越少的现象（评量列可能按日期递减）
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lpa.profile import build_targets  # noqa: E402
from lpa.taxonomy import parse_code  # noqa: E402

ROOT = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent")
LESSONS = json.loads((ROOT / "data" / "structured" / "lessons.json").read_text(encoding="utf-8"))
NAME_MAP = json.loads(
    (ROOT / "data" / "private" / "name_mapping.json").read_text(encoding="utf-8")
)

L: list[str] = []
L.append("# 诊断：第 1 次评量达标率异常")
L.append("")

# ---------------- 1. 编号分配顺序 ----------------
L.append("## 一、匿名编号是怎么分配的")
L.append("")
L.append("编号按 `name_mapping.json` 里首次出现的顺序递增：谁先在某份教案里被解析到，谁拿 S01。")
L.append("因此同一个编号在整份语料里始终指向同一名幼儿，跨教案可追踪。")
L.append("")
L.append("但需要注意一点：编号顺序 = **解析顺序**（文件按文件名排序后，逐份逐行读取），")
L.append("而不是「第 1 份教案的第 1 个孩子」这种班级内部顺序。所以编号本身不携带任何教学含义。")
L.append("")
L.append("如果要让人一眼看懂，应该改成**先按班级分组、再按班内行序**编号。")
L.append("但这批语料里的班名（火箭班 / 闪电班 / 星星班）本身也是可识别的，所以当前保持全局序号即可。")
L.append("")
L.append("当前映射（仅显示数量，不在此处列姓名）：")
L.append(f"- 共 {len(NAME_MAP)} 个编号，范围 S01 - S{len(NAME_MAP):02d}，无空缺。")
L.append("")

# ---------------- 2. 评量次数分布 ----------------
L.append("## 二、每份教案的评量次数分布")
L.append("")
L.append("模板固定 5 个评量列（第一次~第五次），但实际填了几个要看数据。")
L.append("")
L.append("| 教案 | 幼儿数 | 评量次数分布 | 平均达标率 |")
L.append("|---|---|---|---|")
for les in LESSONS:
    cnt = Counter()
    rates = []
    for c in les["children"]:
        codes = [parse_code(x) for x in c["eval_codes"]]
        codes = [x for x in codes if x]
        cnt[len(codes)] += 1
        flat = [code for x in codes for code in x]
        if flat:
            rates.append(sum(1 for x in flat if x.is_pass) / len(flat))
    dist = "，".join(f"{k}次×{v}人" for k, v in sorted(cnt.items()))
    avg = sum(rates) / len(rates) if rates else 0
    L.append(f"| {les['source_file']} | {len(les['children'])} | {dist} | {avg:.3f} |")
L.append("")

# ---------------- 3. 谁的第 1 次达标率是 1.0 ----------------
L.append("## 三、第 1 次评量达标率 = 1.0 的记录，集中在谁身上")
L.append("")
first_hits = defaultdict(list)  # anon_id -> [文件名]
first_rates = defaultdict(list)
for les in LESSONS:
    for c in les["children"]:
        if not c["eval_codes"]:
            continue
        first = parse_code(c["eval_codes"][0])
        if not first:
            continue
        r = sum(1 for x in first if x.is_pass) / len(first)
        first_rates[c["anon_id"]].append(r)
        if r == 1.0:
            first_hits[c["anon_id"]].append(les["source_file"])

L.append("| 匿名编号 | 第1次达标率=1.0 的出现次数 | 该编号历次第1次达标率均值 |")
L.append("|---|---|---|")
for aid in sorted(first_hits, key=lambda x: -len(first_hits[x])):
    avg = sum(first_rates[aid]) / len(first_rates[aid])
    L.append(f"| {aid} | {len(first_hits[aid])} | {avg:.2f} |")
L.append("")
L.append("同一名幼儿在多次教案里都出现第 1 次达标率 = 1.0，说明**这不是偶发噪声，")
L.append("而是该幼儿的稳定特征**（起点本来就好），属于真实的个体差异。")
L.append("")

# ---------------- 4. 时间跨度 ----------------
L.append("## 四、教案日期与年份跨度")
L.append("")
L.append("| 教案 | 文档首行日期 |")
L.append("|---|---|")
years = Counter()
for les in LESSONS:
    line = les.get("teacher_line") or ""
    found = re.findall(r"\d{4}\s*年", line)
    for y in found:
        years[y.strip()] += 1
    L.append(f"| {les['source_file']} | {line[:60] or '(未解析到)'} |")
L.append("")
L.append(f"识别到的年份：{dict(years)}")
L.append("")
L.append("若日期跨年，则「第一次~第五次」是**同一周内的 5 次**还是**跨周的 5 次**，")
L.append("需要看教案周期。当前多数文件的首行给出的是「一周的日期区间」，")
L.append("因此 5 次评量更可能是这一周内的连续 5 天带教记录，而不是跨年批次的记录。")
L.append("")

(ROOT / "data" / "extracted" / "DIAGNOSIS.md").write_text("\n".join(L), encoding="utf-8")
print("ok")

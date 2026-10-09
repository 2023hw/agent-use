# -*- coding: utf-8 -*-
"""最终一致性总检：三份报告 + 两份 JSON 的数字必须互相对得上。"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lpa.analysis import pairwise, gains, stability  # noqa: E402
from lpa.profile import SessionResult  # noqa: E402
from lpa.taxonomy import parse_code  # noqa: E402

ROOT = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent")
D = ROOT / "data" / "structured"
LESSONS = json.loads((D / "lessons.json").read_text(encoding="utf-8"))

checks: list[tuple[str, bool, str]] = []


def ck(name: str, ok: bool, detail: str) -> None:
    checks.append((name, ok, detail))


# ---- 1. lessons.json 规模 ----
n_rec = sum(len(l["children"]) for l in LESSONS)
n_cell = sum(len(c["eval_codes"]) for l in LESSONS for c in l["children"])
ck("lessons.json 记录数=178", n_rec == 178, f"实际 {n_rec}")
ck("lessons.json 格数=890", n_cell == 890, f"实际 {n_cell}")

all_len5 = all(len(c["eval_codes"]) == 5 for l in LESSONS for c in l["children"])
ck("所有 eval_codes 长度=5", all_len5, "有非 5 长度")

# ---- 2. 未参与不进分母 ----
abs_cells = sum(1 for l in LESSONS for c in l["children"]
                for v in c["eval_codes"] if v == "未参与")
ck("未参与格数=90", abs_cells == 90, f"实际 {abs_cells}")
ck("parse_code('未参与') 为空（不进分母）", parse_code("未参与") == [], "返回非空！")

# ---- 3. 重建配对分析 ----
by_date: dict = {}
for les in LESSONS:
    k = les["date_range"] or les["source_file"]
    by_date.setdefault(k, les)
seqs = []
for les in by_date.values():
    for ch in les["children"]:
        s = []
        for i, raw in enumerate(ch["eval_codes"], start=1):
            pc = parse_code(raw)
            if pc:
                s.append(SessionResult(session_index=i, codes=pc))
        if s:
            seqs.append(s)

pw = pairwise(seqs)
ck("配对样本=103", pw.n_records_complete == 103, f"实际 {pw.n_records_complete}")
expect = {1: 0.141, 2: 0.559, 3: 0.831, 4: 0.896, 5: 0.907}
for k, v in expect.items():
    got = pw.by_session.get(k, 0)
    ck(f"配对第{k}次={v}", abs(got - v) < 0.0005, f"实际 {got:.3f}")

# ---- 4. gain 主指标 ----
gn = gains([("x", str(i), s) for i, s in enumerate(seqs)])
ck("进步样本=154", len(gn.records) == 154, f"实际 {len(gn.records)}")
ck("平均进步=+0.611", abs(gn.mean_gain - 0.611) < 0.0005, f"实际 {gn.mean_gain:+.3f}")
ck("进步占比=84%", abs(gn.improved_ratio - 0.84) < 0.005, f"实际 {gn.improved_ratio:.0%}")
ck("退步占比=0%", gn.declined_ratio == 0.0, f"实际 {gn.declined_ratio:.0%}")

# ---- 5. stability ----
st = stability([("x", str(i), s) for i, s in enumerate(seqs)])
ck("稳定达标=8", st.counts.get("稳定达标", 0) == 8, f"实际 {st.counts.get('稳定达标',0)}")
ck("波动已收敛=145", st.counts.get("波动已收敛", 0) == 145,
   f"实际 {st.counts.get('波动已收敛',0)}")

# ---- 6. 报告文本里的数字对得上 ----
an = (D / "ANALYSIS_REPORT.md").read_text(encoding="utf-8")
pr = (D / "PROFILE_REPORT.md").read_text(encoding="utf-8")
ps = (D / "PARSE_SUMMARY.md").read_text(encoding="utf-8")
va = (D / "VERIFY_ALIGNMENT.md").read_text(encoding="utf-8")

for frag, src, doc in [
    ("0.141", an, "ANALYSIS"), ("0.907", an, "ANALYSIS"),
    ("+0.611", an, "ANALYSIS"), ("103 / 156", an, "ANALYSIS"),
    ("| 5 列 | 156 |", an, "ANALYSIS"),
    ("164 条", pr, "PROFILE"), ("156 / 164", pr, "PROFILE"),
    ("178** 条", ps, "PARSE"), ("890 格", ps, "PARSE"),
    ("21** 处", ps, "PARSE"),
    ("problems", va, "VERIFY") if False else ("全部通过", va, "VERIFY"),
    ("751", va, "VERIFY"), ("90", va, "VERIFY"),
]:
    ck(f"{doc} 含 {frag!r}", frag in src, "未找到")

# ---- 7. 形态分布一致性 ----
shapes = defaultdict(int)
for l in LESSONS:
    for c in l["children"]:
        for v in c["eval_codes"]:
            shapes[v if v else "(空)"] += 1
ck("PPP=232", shapes.get("PPP", 0) == 232, f"实际 {shapes.get('PPP',0)}")
ck("PPPP=141", shapes.get("PPPP", 0) == 141, f"实际 {shapes.get('PPPP',0)}")

# ---- 输出 ----
out = ["# 最终一致性总检", ""]
bad = [c for c in checks if not c[1]]
out.append(f"- 检查项：**{len(checks)}**")
out.append(f"- 通过：**{len(checks) - len(bad)}**")
out.append(f"- 失败：**{len(bad)}**")
out.append("")
out.append("| 检查项 | 结果 | 备注 |")
out.append("|---|---|---|")
for name, ok, detail in checks:
    out.append(f"| {name} | {'✅' if ok else '❌'} | {detail} |")

(D / "FINAL_CONSISTENCY.md").write_text("\n".join(out), encoding="utf-8")
print(f"checks={len(checks)} passed={len(checks)-len(bad)} failed={len(bad)}")
for name, ok, detail in bad:
    print("FAIL:", name, "|", detail)

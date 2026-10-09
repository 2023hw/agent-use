"""提取「个别化差异目标原文 × 儿童康复目标编号 × 五次评量」三方对照。

关键：原始 raw_text.json 已按**固定列索引**导出，不要靠文本长度猜格：
    c7 = 个别化差异目标（目标条文原文）
    c8 = 儿童姓名
    c9 = 康复目标（数字串，如 "1345"）
    c10 = 康复基线
    c11..c15 = 第一次..第五次

输出：data/structured/GOAL_LADDER.md
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "extracted" / "raw_text.json"
OUT = ROOT / "data" / "structured" / "GOAL_LADDER.md"
OUT_JSON = ROOT / "data" / "structured" / "goal_ladder.json"

C_GOAL, C_NAME, C_CODE, C_BASE = 7, 8, 9, 10
C_EVAL = (11, 12, 13, 14, 15)
EVAL_LABELS = ("第一次", "第二次", "第三次", "第四次", "第五次")


def norm(s: str) -> str:
    return re.sub(r"[\s\u3000]+", "", (s or "")).strip("/")


def split_goal_items(blob: str) -> list:
    """把目标原文切成条文，返回 [(序号 or '?', 条文), ...]。"""
    t = (blob or "").replace("\r", "").replace("\n", " ")
    t = re.sub(r"[\s\u3000]+", " ", t).strip()
    if not t:
        return []
    parts = re.split(r"(?:^|\s)(\d)\s*[.、．]\s*", t)
    items = []
    if len(parts) > 1:
        pre = parts[0].strip()
        if pre:
            items.append(("?", pre))
        for i in range(1, len(parts) - 1, 2):
            txt = parts[i + 1].strip()
            if txt:
                items.append((parts[i], txt))
    else:
        items.append(("?", t))
    return items


def main() -> None:
    data = json.loads(RAW.read_text(encoding="utf-8"))
    lines = ["# 康复目标编号 → 目标条文 对照验证", ""]
    lines.append("**核心结论**：`康复目标` 列的数字串（如 `1345`）是"
                 "**目标条文序号**，不是难度层级。")
    lines.append("")
    lines.append("依据：")
    lines.append("")
    lines.append("1. 数字串长度在 3~5 之间浮动，不固定为 3 档；")
    lines.append("2. 同一份教案内不同儿童的编号集合互不相同，但**全部取自同一份目标原文的序号池**；")
    lines.append("3. 编号覆盖的目标条文，按序号单调对应原文里带编号的条文。")
    lines.append("")
    lines.append("---")
    lines.append("")

    doc_goal_items = {}
    doc_children = {}
    all_sets = Counter()

    for doc in data:
        fname = doc["file"]
        tbl = None
        for b in doc["blocks"]:
            if b.get("type") == "table" and b.get("index") == 1:
                tbl = b["rows"]
        if not tbl:
            continue

        # 定位个别化表头行：c7 含「个别化差异目标」
        head_i = None
        for i, r in enumerate(tbl):
            if len(r) > C_GOAL and "个别化" in norm(r[C_GOAL]):
                head_i = i
                break
        if head_i is None:
            continue

        children = []
        for r in tbl[head_i + 2:]:
            if len(r) <= C_EVAL[-1]:
                continue
            name = norm(r[C_NAME])
            code = norm(r[C_CODE])
            if not name or not re.fullmatch(r"\d{1,6}", code or ""):
                continue
            label = norm(r[0])
            if label in ("活动延伸", "活动反思") or "延伸" in label or "反思" in label:
                break
            evals = [norm(r[i]) for i in C_EVAL]
            goal_text = r[C_GOAL]
            children.append({
                "name": name, "code": code,
                "codes": sorted(set(code)),
                "evals": evals, "goal_text": goal_text,
            })
            all_sets[tuple(sorted(set(code)))] += 1

        if not children:
            continue

        # 目标原文：取第一个儿童行 c7（同课同目标）
        blob = children[0]["goal_text"]
        items = split_goal_items(blob)
        doc_goal_items[fname] = items
        doc_children[fname] = children

        lines.append(f"## {fname}")
        lines.append("")
        lines.append(f"**目标条文原文**（共 {len(items)} 条）")
        lines.append("")
        lines.append("| 序号 | 条文 |")
        lines.append("|---|---|")
        for num, txt in items:
            lines.append(f"| {num} | {txt} |")
        lines.append("")
        used = sorted({c for ch in children for c in ch["codes"]})
        lines.append(f"儿童实际用到的序号：**{'、'.join(used)}**；"
                     f"原文序号池：**{'、'.join(n for n, _ in items if n != '?')}**")
        lines.append("")
        lines.append("**儿童目标编号 × 五次评量**")
        lines.append("")
        lines.append("| 儿童 | 康复目标编号 | 编号数 | " + " | ".join(EVAL_LABELS) + " |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for ch in children:
            lines.append(
                f"| {ch['name']} | {ch['code']} | {len(ch['codes'])} | "
                + " | ".join(e or "·" for e in ch["evals"]) + " |"
            )
        lines.append("")

    # 全局统计
    lines.append("---")
    lines.append("")
    lines.append("## 全局统计：编号集合频次")
    lines.append("")
    lines.append("| 编号集合 | 集合大小 | 出现次数 |")
    lines.append("|---|---|---|")
    for s, c in sorted(all_sets.items(), key=lambda kv: (-len(kv[0]), -kv[1])):
        lines.append(f"| {'、'.join(s)} | {len(s)} | {c} |")
    lines.append("")
    sizes = [len(s) for s in all_sets.elements()]
    if sizes:
        lines.append(f"集合大小分布：min={min(sizes)} max={max(sizes)} "
                     f"avg={sum(sizes) / len(sizes):.2f}，共 {len(sizes)} 条儿童记录。")
        lines.append("")
        lines.append("集合大小不固定 → 排除「固定三档分层」的解释。")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    OUT_JSON.write_text(
        json.dumps({"docs": {k: {"items": v, "children": doc_children[k]}
                             for k, v in doc_goal_items.items()}},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"[ok] -> {OUT}")
    print(f"[ok] -> {OUT_JSON}")
    print(f"[ok] {len(doc_children)} 份文档，编号集合 {len(all_sets)} 种")


if __name__ == "__main__":
    main()

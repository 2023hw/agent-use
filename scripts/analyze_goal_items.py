"""诊断 + 定档依据：从范例「个别化差异目标」原文恢复条文，统计**组合规律**。

## 为什么需要

源表条文编号**经常缺失或粘连**：

    第二周-动物的家    "能够在课堂上安坐不走出位置2.能看图片认识动物3.能在全辅助下…"
                        ↑ 第 1 条没有 "1." 前缀（老师漏写）
    第五周-童心向党    "能够在课堂上安坐不走出位置 能在全辅助下指认党旗。 能在少量辅助下…"
                        ↑ 完全没有编号，序号只能是**文本顺序位**
    第四周-能干的我    "整节课堂能安坐好…不闹情绪能在身体辅助下完成看图指出事项名称能…"
                        ↑ 连空格都没有，全靠"能在/能自己/能看…"起首词切

「康复目标」列里的 `1456` 指的就是**文本位置** 1/4/5/6。

## 产出

1. 每课恢复出的条文列表（1..N）；
2. 每条条文在「支持轴 S / 发展轴 A / 常规 R」上的位置；
3. 每个幼儿的**轴位签名**（如 `R+S1+S2`）→ 跨课可比的「组合规律」；
4. 全局签名频次 → **分层数应当取几**。

跑法：
    python scripts/analyze_goal_items.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lpa.layering import build_ladder  # noqa: E402

LADDER = ROOT / "data" / "structured" / "goal_ladder.json"

# 条文起首词（用于在缺编号/粘连时切句）
START_RE = re.compile(
    r"(?=整节课堂)|(?=能够?在课堂)|(?=能在)|(?=能自己)|(?=能看图)|(?=能看)|"
    r"(?=能听令)|(?=能参与)|(?=能表达)|(?=会)"
)


def split_goals(raw: str) -> list:
    """把差异目标原文切成条文列表（按文本顺序 = 序号顺序）。

    优先按**写出来的编号** `N.` 切；编号不全时，按**起首词**切。
    """
    s = (raw or "").strip()
    if not s:
        return []
    s = re.sub(r"[\u3000]+", " ", s)

    # 路径 A：编号切分
    # ⚠ 用 lambda 替换，不要写 `r"\x00\1\x00"` —— re 模板不认 `\x` 转义会抛
    #   `bad escape \x`。
    MARK = "\uE000"
    marked = re.sub(r"(?<!\d)(\d{1,2})[.、]\s*",
                    lambda m: MARK + m.group(1) + MARK, s)
    if marked.count(MARK) >= 6:
        segs = [p.strip() for p in marked.split(MARK) if p.strip()]
        out, i = [], 0
        while i < len(segs):
            if re.fullmatch(r"\d{1,2}", segs[i]) and i + 1 < len(segs):
                out.append(segs[i + 1])
                i += 2
            else:
                out.append(segs[i])
                i += 1
        if len(out) >= 3:
            return [o for o in out if o]

    # 路径 B：起首词切分
    parts = [p.strip() for p in START_RE.split(s) if p.strip()]
    if len(parts) >= 3:
        return parts

    # 路径 C：按句末标点切
    return [p.strip() for p in re.split(r"[。！；;]", s) if p.strip()] or [s]


def _rank_in(items, index: int) -> int:
    for i, it in enumerate(items, start=1):
        if it.index == index:
            return i
    return 0


def signature(ladder, ids: str) -> str:
    """编号串 → 轴位签名（`R` 常规 / `S1..Sn` 支持档 / `A1..An` 发展档）。"""
    by = ladder.by_index()
    sup, adv = ladder.support_items(), ladder.advanced_items()
    parts = []
    for c in ids:
        it = by.get(int(c))
        if it is None:
            parts.append(f"?{c}")
        elif it.is_routine:
            parts.append("R")
        else:
            r = _rank_in(sup, it.index)
            parts.append(f"S{r}" if r else f"A{_rank_in(adv, it.index)}")
    return "+".join(parts)


def main() -> None:
    docs = json.loads(LADDER.read_text(encoding="utf-8"))["docs"]
    sig_cnt: Counter = Counter()
    sig_docs: dict = defaultdict(set)
    sig_ex: dict = defaultdict(list)
    n_rec = 0
    bad = []

    print("=" * 78)
    print("【一】每课恢复出的条文（1..N）")
    for fn, d in docs.items():
        raw = "".join(str(b) for _, b in (d.get("items") or []))
        goals = split_goals(raw)
        codes = ["".join(c for c in str(ch.get("code") or "") if c.isdigit())
                 for ch in (d.get("children") or [])]
        codes = [c for c in codes if c]
        mx = max((max(int(c) for c in k) for k in codes), default=0)
        ok = len(goals) >= mx
        if not ok:
            bad.append((fn, len(goals), mx))
        print("-" * 78)
        print(f"{fn}   条文 {len(goals)} 条 / 最大序号 {mx}  {'ok' if ok else '⚠ 缺条文'}")
        for i, g in enumerate(goals, start=1):
            print(f"    {i}. {g[:76]}")

    print("\n" + "=" * 78)
    print("【二】轴位签名（跨课可比）")
    for fn, d in docs.items():
        raw = "".join(str(b) for _, b in (d.get("items") or []))
        goals = split_goals(raw)
        if not goals:
            continue
        lad = build_ladder([(str(i), g) for i, g in enumerate(goals, start=1)])
        for ch in d.get("children") or []:
            ids = "".join(c for c in str(ch.get("code") or "") if c.isdigit())
            if not ids:
                continue
            n_rec += 1
            s = signature(lad, ids)
            sig_cnt[s] += 1
            sig_docs[s].add(fn)
            if len(sig_ex[s]) < 3:
                sig_ex[s].append((fn.replace("(1).docx", "")[:12], ids))

    print(f"\n{'签名':22s} {'次数':>4s} {'占比':>6s} {'份数':>4s}  例子")
    for s, n in sig_cnt.most_common():
        ex = "；".join(f"{t}·{i}" for t, i in sig_ex[s])
        print(f"{s:22s} {n:4d} {n / n_rec:6.1%} {len(sig_docs[s]):4d}  {ex[:44]}")

    ln = Counter(len(s.split("+")) for s in sig_cnt.elements())
    print(f"\n{n_rec} 条记录 / {len(sig_cnt)} 种签名；长度分布 "
          f"{ {k: ln[k] for k in sorted(ln)} }")
    if bad:
        print("\n⚠ 条文切分不足的课：")
        for fn, a, b in bad:
            print(f"    {fn}: 切出 {a} 条，但最大序号 {b}")


if __name__ == "__main__":
    main()

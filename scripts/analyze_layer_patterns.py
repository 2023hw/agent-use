"""从范例语料反推「康复目标组合」的规律 —— 决定分层应分几层。

用户要求：**不要固定 3 层**，按案例里康复目标的组合规律分层，有几种规律就分几层。

## 方法

`data/structured/goal_ladder.json` 的 `docs` 里，每份范例都带着：

- `items` —— 该课「个别化差异目标」的条文清单 `[(序号, 文本), ...]`
- `children` —— 每位幼儿的 `code`（如 `1345`）与五次 `evals`

于是可以：

1. 用 `build_ladder()` 把每课条文解析成「支持轴 / 发展轴」两轴档位；
2. 把每位幼儿的编号映射成**轴位签名**：

       常规 + 支持第1档 + 支持第2档   → "R+S1+S2"
       常规 + 支持第2档 + 发展第1档   → "R+S2+A1"

   签名用**轴位**而非原始序号 —— 各课序号都从 1 开始但条文内容不同，
   只有轴位才跨课可比。

3. 统计签名频次：频次靠前的签名 = 案例里真实存在的组合规律。

跑法：
    python scripts/analyze_layer_patterns.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lpa.layering import build_ladder  # noqa: E402

LADDER = ROOT / "data" / "structured" / "goal_ladder.json"


def _rank_in(items, index: int) -> int:
    for i, it in enumerate(items, start=1):
        if it.index == index:
            return i
    return 0


def signature(ladder, ids: str) -> str:
    """把 `1345` 映射成轴位签名，如 `R+S1+A2`。"""
    by = ladder.by_index()
    sup_items = ladder.support_items()
    adv_items = ladder.advanced_items()
    parts = []
    for c in ids:
        it = by.get(int(c))
        if it is None:
            parts.append(f"?{c}")
            continue
        if it.is_routine:
            parts.append("R")
        else:
            r = _rank_in(sup_items, it.index)
            if r:
                parts.append(f"S{r}")
            else:
                parts.append(f"A{_rank_in(adv_items, it.index)}")
    return "+".join(parts)


def main() -> None:
    docs = json.loads(LADDER.read_text(encoding="utf-8"))["docs"]
    sig_cnt = Counter()
    sig_examples = defaultdict(list)
    sig_docs = defaultdict(set)
    n_rec = 0

    print(f"扫描 {len(docs)} 份范例阶梯\n")
    for fn, d in docs.items():
        items = [(str(a), str(b)) for a, b in (d.get("items") or [])]
        if not items:
            continue
        lad = build_ladder(items)
        for ch in d.get("children") or []:
            ids = "".join(c for c in str(ch.get("code") or "") if c.isdigit())
            if not ids:
                continue
            n_rec += 1
            s = signature(lad, ids)
            sig_cnt[s] += 1
            sig_docs[s].add(fn)
            if len(sig_examples[s]) < 3:
                sig_examples[s].append((fn.replace("(1).docx", ""), ids))

    print("=== 轴位签名频次（降序）===")
    for s, n in sig_cnt.most_common():
        ex = "；".join(f"{t}·{i}" for t, i in sig_examples[s])
        print(f"  {n:4d}  {n / n_rec:5.1%}  {s:16s} {len(sig_docs[s]):2d}份  例：{ex[:56]}")

    print(f"\n{n_rec} 条记录 → {len(sig_cnt)} 种轴位签名")

    ln = Counter(len(s.split("+")) for s in sig_cnt.elements())
    print("\n=== 组合长度分布 ===")
    for k in sorted(ln):
        print(f"   {k} 条：{ln[k]} 条记录")


if __name__ == "__main__":
    main()

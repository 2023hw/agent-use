"""定档依据：把范例里的「康复目标组合」按**实测能力**排序并分档。

## 为什么能用基线验证

「康复基线」是幼儿**接手本课时**的能力水平（P=达标 / E=未达标）。
若组合 A 确实比组合 B "更高"，那么持有 A 的孩子基线里 **P 的比例应更高**。
于是可以：

1. 用**实测 P 密度**给每个组合排序（数据说话，不靠关键词猜）；
2. 看这批组合是否自然聚成几档 —— **档数就是分层数**；
3. 再用条文的两轴位置（支持档 / 发展档）解释每一档，作为可复用的判据。

跑法：
    python scripts/analyze_layer_tiers.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_goal_items import split_goals  # noqa: E402
from lpa.layering import build_ladder  # noqa: E402
from roster_from_corpus import load_lessons  # noqa: E402

LADDER = ROOT / "data" / "structured" / "goal_ladder.json"


def marks(s: str) -> str:
    return "".join(c for c in (s or "").upper() if c in "PE")


def main() -> None:
    ladders = json.loads(LADDER.read_text(encoding="utf-8"))["docs"]

    # ---- 每课的条文两轴位置 ----
    axis = {}      # (file, index) -> GoalItem
    for fn, d in ladders.items():
        raw = "".join(str(b) for _, b in (d.get("items") or []))
        goals = split_goals(raw)
        if not goals:
            continue
        lad = build_ladder([(str(i), g) for i, g in enumerate(goals, start=1)])
        axis[fn] = lad

    stat = defaultdict(lambda: {"rec": 0, "docs": set(), "p": 0, "n": 0,
                                "max_s": 0, "max_d": 0, "has_adv": False})

    for L in load_lessons():
        fn = L["file"]
        lad = axis.get(fn)
        if lad is None:
            continue
        by = lad.by_index()
        adv_idx = {it.index for it in lad.advanced_items()}
        for r in L["rows"]:
            goal = "".join(c for c in str(r.get("goal") or "") if c.isdigit())
            base = marks(r.get("base") or "")
            if not goal:
                continue
            e = stat[goal]
            e["rec"] += 1
            e["docs"].add(fn)
            if base:
                e["p"] += base.count("P")
                e["n"] += len(base)
            for c in goal:
                it = by.get(int(c))
                if it is None:
                    continue
                e["max_s"] = max(e["max_s"], it.support_rank)
                e["max_d"] = max(e["max_d"], it.dev_rank)
                if it.index in adv_idx:
                    e["has_adv"] = True

    print(f"{'组合':7s} {'记录':>4s} {'份数':>4s} {'最高支持':>7s} {'最高发展':>7s} "
          f"{'含进阶条':>7s} {'基线P密度':>9s}")
    rows = sorted(stat.items(),
                  key=lambda kv: (kv[1]["p"] / kv[1]["n"] if kv[1]["n"] else -1))
    for combo, e in rows:
        dens = f"{e['p'] / e['n']:.2f}" if e["n"] else "—"
        print(f"{combo:7s} {e['rec']:4d} {len(e['docs']):4d} {e['max_s']:7d} "
              f"{e['max_d']:7d} {str(e['has_adv']):>7s} {dens:>9s}")


if __name__ == "__main__":
    main()

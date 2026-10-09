"""验证规则：组合变化后，**新加入的目标**在下一份教案的基线里是不是 `E`。

用户给出的规则：
> 升级后的康复目标发生变化后，康复基线应针对新加入的康复目标来确定，
> 即取该新增康复目标对应的未达标（不达标）等级 E。

## 怎么验

同一名幼儿按日期排成时间线，取相邻两份记录：

- **保留的目标**（旧组合 ∩ 新组合）→ 记号应**沿用**上一份；
- **新增的目标**（新组合 − 旧组合）→ 记号应是 `E`。

记号按**序号升序**逐位对齐（`134` 的第 1 位对应序号 1，第 2 位对应 3…）。

跑法：
    python scripts/analyze_baseline_rule.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from roster_from_corpus import load_lessons  # noqa: E402


def marks(s: str) -> str:
    return "".join(c for c in (s or "").upper() if c in "PE")


def main() -> None:
    per_child = defaultdict(list)
    for L in load_lessons():
        for r in L["rows"]:
            g = "".join(c for c in str(r.get("goal") or "") if c.isdigit())
            b = marks(r.get("base") or "")
            if not g or not b:
                continue
            per_child[r["name"]].append(
                (L["start"], L["date_range"][:14], g, b, sorted(int(c) for c in g)))

    pairs = []
    for nm, recs in per_child.items():
        recs.sort(key=lambda x: (x[0], x[1]))
        for (d1, dt1, g1, b1, ix1), (d2, dt2, g2, b2, ix2) in zip(recs, recs[1:]):
            if g1 == g2 or len(b1) != len(ix1) or len(b2) != len(ix2):
                continue
            pairs.append((nm, dt1, g1, b1, dt2, g2, b2,
                          dict(zip(ix1, b1)), dict(zip(ix2, b2))))

    print(f"可比「组合发生变化」的相邻记录：{len(pairs)} 对\n")
    print(f"{'幼儿':6s} {'上一份':14s} {'组合':6s} {'基线':9s} → "
          f"{'本份':14s} {'组合':6s} {'基线':9s}  新增目标记号")
    keep_ok = new_e = new_p = 0
    keep_tot = 0
    for nm, dt1, g1, b1, dt2, g2, b2, m1, m2 in pairs:
        added = [i for i in m2 if i not in m1]
        kept = [i for i in m2 if i in m1]
        tag = " ".join(f"{i}:{m2[i]}" for i in added) or "—"
        for i in kept:
            keep_tot += 1
            if m1[i] == m2[i]:
                keep_ok += 1
        for i in added:
            if m2[i] == "E":
                new_e += 1
            else:
                new_p += 1
        print(f"{nm:6s} {dt1:14s} {g1:6s} {b1:9s} → {dt2:14s} {g2:6s} {b2:9s}  {tag}")

    print()
    if keep_tot:
        print(f"保留目标沿用原记号：{keep_ok}/{keep_tot} = {keep_ok / keep_tot:.0%}")
    tot_new = new_e + new_p
    if tot_new:
        print(f"新增目标记号为 E    ：{new_e}/{tot_new} = {new_e / tot_new:.0%}"
              f"（P {new_p} 例）")


if __name__ == "__main__":
    main()

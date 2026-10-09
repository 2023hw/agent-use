"""挖掘「康复目标组合」的规律 → 产出 `data/structured/layer_tiers.json`。

用户要求：分层**不固定 3 层**，按案例里康复目标的组合规律分层，
**有几种组合规律就分几层**。

## 做法

1. 从范例恢复每课的条文（`analyze_goal_items.split_goals` —— 源表编号
   经常缺失/粘连，必须先还原成 1..N）；
2. 用 `lpa.layering.combo_tier()` 把每条儿童记录判到某一层
   （判据：离哪条**组合规律**最近）；
3. 按层汇总 **基线 P 密度**（`P` 占基线的比例）—— 这是分层的**独立证据**：
   层越高，"孩子接手本课时已达标"的条目应当越多；
4. 写出层数、每层规律、语料实例、高频组合、P 密度，
   供 `lpa.layering` 加载（`LAYER_TIERS_FILE`）。

跑法：
    python scripts/mine_layer_tiers.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_goal_items import split_goals                       # noqa: E402
from lpa.layering import (DEFAULT_LAYER_SPECS, build_ladder,     # noqa: E402
                          combo_tier, layer_combos)
from roster_from_corpus import load_lessons                      # noqa: E402

LADDER = ROOT / "data" / "structured" / "goal_ladder.json"
OUT = ROOT / "data" / "structured" / "layer_tiers.json"


def marks(s: str) -> str:
    return "".join(c for c in (s or "").upper() if c in "PE")


def lesson_ladders() -> dict:
    docs = json.loads(LADDER.read_text(encoding="utf-8"))["docs"]
    out = {}
    for fn, d in docs.items():
        raw = "".join(str(b) for _, b in (d.get("items") or []))
        goals = split_goals(raw)
        if goals:
            out[fn] = build_ladder(
                [(str(i), g) for i, g in enumerate(goals, start=1)])
    return out


def main() -> None:
    lads = lesson_ladders()

    # 只用「四条规律互不撞车」的教案做归因 —— 此时每条儿童记录落在哪一层
    # 是**唯一确定**的（组合恰好等于该层的规律实例），没有"最近邻"的模糊。
    canon_by_doc = {}
    for fn, lad in lads.items():
        c = layer_combos(lad)
        strs = {L: "".join(str(i) for i in v) for L, v in c.items()}
        if len(set(strs.values())) == len(strs):
            canon_by_doc[fn] = strs

    agg = defaultdict(lambda: {"rec": 0, "p": 0, "n": 0, "docs": set(),
                               "combos": defaultdict(int)})
    inst = defaultdict(set)

    for L in load_lessons():
        cf = canon_by_doc.get(L["file"])
        if not cf:
            continue
        inv = {v: k for k, v in cf.items()}
        for lvl, c in cf.items():
            inst[lvl].add(c)
        for r in L["rows"]:
            g = "".join(c for c in str(r.get("goal") or "") if c.isdigit())
            b = marks(r.get("base") or "")
            if g not in inv:
                continue
            e = agg[inv[g]]
            e["rec"] += 1
            e["docs"].add(L["file"])
            e["combos"][g] += 1
            if b:
                e["p"] += b.count("P")
                e["n"] += len(b)

    tiers = []
    for lvl in sorted(agg):
        e = agg[lvl]
        spec = next((s for s in DEFAULT_LAYER_SPECS if s["level"] == lvl), {})
        tiers.append({
            "level": lvl,
            "name": spec.get("name", f"第{lvl}层"),
            "support": spec.get("support", ""),
            "pattern": spec.get("pattern", []),
            "exemplars": sorted(inst[lvl]),
            "records": e["rec"],
            "docs": len(e["docs"]),
            "p_density": round(e["p"] / e["n"], 2) if e["n"] else None,
            "combos": dict(sorted(e["combos"].items(),
                                  key=lambda kv: (-kv[1], kv[0]))),
        })

    dens = [t["p_density"] for t in tiers]
    mono = all(a is not None and b is not None and a <= b
               for a, b in zip(dens, dens[1:]))
    data = {
        "source": f"{len(lads)} 份范例教案、{sum(t['records'] for t in tiers)} "
                  f"条可归因记录（只用四条规律互不撞车的教案）",
        "rule": "层 = 组合规律：R+S1+S2 / R+S1+S3 / R+S3+A1 / R+S3+A1+A2",
        "validation": f"各层基线 P 密度 {dens}，"
                      + ("单调递增，分层成立" if mono else "非单调（需复核）"),
        "derivation": "python scripts/mine_layer_tiers.py",
        "layer_count": len(tiers),
        "tiers": tiers,
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"写入 {OUT.relative_to(ROOT)}   层数 = {len(tiers)}\n")
    print(f"{'层':4s} {'名称':7s} {'规律':16s} {'记录':>4s} {'份数':>4s} "
          f"{'P密度':>6s}  规律实例")
    for t in tiers:
        print(f"L{t['level']:<3d} {t['name']:7s} {'+'.join(t['pattern']):16s} "
              f"{t['records']:4d} {t['docs']:4d} {str(t['p_density']):>6s}  "
              f"{t['exemplars']}")
    print(f"\nP 密度序列 {dens} —— "
          f"{'单调递增 ✅ 分层成立' if mono else '非单调 ⚠'}")


if __name__ == "__main__":
    main()

"""把范例的评量结果**回灌**到生成链路：算出每位幼儿「本课」的层级 / 目标 / 基线。

## 配置逻辑（本轮用户确认后的完整口径）

```
┌ 1. 分层 ────────────────────────────────────────────────────────┐
│ 层数不预设。语料 13 份范例、170 条记录里的「康复目标」组合只落在 4 │
│ 种形态上（组合规律），所以分 4 层：                              │
│   L1  R+S1+S2        高支持层  （组合实例 123/134）              │
│   L2  R+S1+S3        中支持层  （组合实例 124/1234/1346）        │
│   L3  R+S3+A1        低支持层  （组合实例 145/125/135）          │
│   L4  R+S3+A1+A2     泛化层    （组合实例 1456/1345/156）        │
│ 验证：各层「康复基线」的 P 密度 = 0.48 / 0.56 / 0.80 / 0.92，    │
│       单调递增 → 分层与实际能力一致。                           │
│ 产出：data/structured/layer_tiers.json（重算：mine_layer_tiers） │
└─────────────────────────────────────────────────────────────────┘
┌ 2. 定层 ────────────────────────────────────────────────────────┐
│ 幼儿的层级 = 他/她**最近一次「可解释」的记录**里的组合落在哪条规律 │
│ 上（在该课自己的阶梯上算，跨课可比）。                          │
│ · 可解释 = 编号**全部**落在该课阶梯内且条数 ≥3（各课编号含义不同，│
│   6 号在 A 课是独立档、在 B 课可能是进阶条，不可直接搬）。        │
│ · 最近一条不可解释就往前回退；一条都没有 → 回落**画像判定**层。  │
│ · 判定依据与层级同源输出（`basis`），附录表三不再自相矛盾。      │
└─────────────────────────────────────────────────────────────────┘
┌ 3. 调层（阈值由用户指定）────────────────────────────────────────┐
│ 上一份的五次评量：**连续 2 次 P 率 ≥ 85%** → 上调一层；          │
│                   单次 P 率 < 40%          → 下调一层；          │
│                   「未参与」不计分母、不触发调整；              │
│                   上限 = 层数（4），下限 L1。                   │
└─────────────────────────────────────────────────────────────────┘
┌ 4. 康复目标 ────────────────────────────────────────────────────┐
│ 换成**本课阶梯上该层的规律实例**（跨课复用规律、换课换条文）。   │
└─────────────────────────────────────────────────────────────────┘
┌ 5. 康复基线 ────────────────────────────────────────────────────┐
│ 逐位对齐本课的新组合：                                          │
│   · 旧组合里**保留**的目标 → 沿用原记号；                       │
│   · **新加入**的目标 → E（未达标，因为还没评估过）。            │
│ 整轮未参与 → 统一写「未参与」。                                 │
└─────────────────────────────────────────────────────────────────┘
```

跑法（自检）：
    python scripts/lesson_flow.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_goal_items import split_goals                     # noqa: E402
from lpa.layering import (LAYER_META, build_ladder,            # noqa: E402
                          combo_tier, compose_baseline,
                          layer_count, marks_by_index, next_state,
                          pick_goal_indices)
from roster_from_corpus import (ABSENT_MARK, build_history,    # noqa: E402
                                normalize_baseline)

LADDER = ROOT / "data" / "structured" / "goal_ladder.json"


def lesson_ladders() -> dict:
    """{范例文件名: GoalLadder}（条文用 `split_goals` 从原文恢复）。"""
    docs = json.loads(LADDER.read_text(encoding="utf-8"))["docs"]
    out = {}
    for fn, d in docs.items():
        raw = "".join(str(b) for _, b in (d.get("items") or []))
        goals = split_goals(raw)
        if goals:
            out[fn] = build_ladder(
                [(str(i), g) for i, g in enumerate(goals, start=1)])
    return out


def interpretable(goal: str, ladder) -> list:
    """记录里的组合能否在**该课阶梯**上解释。

    跨课定层必须读懂上一份教案的组合，而各课编号的含义不同（本课「4」是
    独立档，别课「4」可能是进阶条），所以只有编号**全部**落在该课阶梯内、
    且条数 ≥3（语料硬约束：组合恒为 3~4 条）时才算可解释。
    """
    if ladder is None:
        return []
    raw = [int(c) for c in str(goal or "") if c.isdigit()]
    ok = [i for i in raw if i in ladder.by_index()]
    if len(ok) < 3 or len(ok) != len(raw):
        return []
    return ok


def targets_for(anon2name: dict, ladder, hist: dict = None,
                ladders: dict = None) -> dict:
    """→ {anon_id: {name, layer, goal, baseline, absent, source, why, basis}}。

    `ladder` 是**本课**的阶梯；`hist` 是各幼儿的历史记录序列。

    层级取自「**最近一次可解释的记录**」（`interpretable`），逐条往前回退；
    一条都没有就返回 `layer=None`，由渲染层回落画像判定 —— 这样「层级」
    与附录表三的「判定依据」永远同源，不会出现依据写 L4、层级却是 L1。
    """
    hist = hist if hist is not None else build_history()
    ladders = ladders if ladders is not None else lesson_ladders()
    out: dict = {}

    for aid, nm in anon2name.items():
        seq = hist.get(nm) or []
        if not seq:
            out[aid] = {"name": nm, "layer": None, "goal": "", "baseline": "",
                        "absent": False, "source": "", "basis": "",
                        "why": "无历史记录"}
            continue

        # 往前找最近一条**能在其所在课的阶梯上解释**的记录
        pick, prev_idx = None, []
        for r in reversed(seq):
            idx = interpretable(r.get("goal"), ladders.get(r.get("file")))
            if idx:
                pick, prev_idx = r, idx
                break

        # ---- 情况 A：整轮未参与（缺勤）------------------------------------
        # ⚠ 优先于「阶梯是否可解释」判定：缺勤是事实，不因上一课阶梯没恢复好
        #   就把这名幼儿当成有数据的人去调层。
        if bool(seq[-1].get("all_absent")):
            if pick is None:            # 阶梯不全 → 回落画像层，但基线仍记「未参与」
                out[aid] = {"name": nm, "layer": None, "prev_layer": None,
                            "goal": "", "baseline": ABSENT_MARK,
                            "absent": True,
                            "basis": "整轮未参与；所在课阶梯不全，层级由画像判定"
                                     " → 基线记「未参与」、不调层",
                            "source": seq[-1].get("date_range", ""),
                            "why": "整轮未参与；所在课阶梯不全，层由画像判定"}
                continue
            prev_layer = combo_tier(prev_idx, ladders[pick["file"]])
            new_idx = pick_goal_indices(ladder, prev_layer)
            out[aid] = {
                "name": nm, "layer": prev_layer, "prev_layer": prev_layer,
                "prev_goal": "".join(str(i) for i in prev_idx),
                "goal": "".join(str(i) for i in new_idx),
                "baseline": ABSENT_MARK, "absent": True,
                "source": pick.get("date_range", ""), "evals": [],
                "history": [],
                "basis": (f"本人最近记录（{pick.get('date_range', '')}）组合 "
                          f"{''.join(str(i) for i in prev_idx)} 属 "
                          f"{LAYER_META[prev_layer]['pattern']} 规律 → "
                          f"L{prev_layer}；整轮未参与 → 目标沿用、"
                          f"基线记「未参与」、不调层"),
                "why": f"整轮未参与 → 层级与目标沿用 L{prev_layer}、"
                       f"基线记「未参与」、不调层",
            }
            continue

        # ---- 情况 B：无可解释记录 → 回落画像判定 --------------------------
        if pick is None:
            out[aid] = {"name": nm, "layer": None, "prev_layer": None,
                        "goal": "", "baseline": "", "absent": False,
                        "source": seq[-1].get("date_range", ""), "basis": "",
                        "why": "历史记录的组合无法在所在课的阶梯上解释，"
                               "回落画像判定"}
            continue

        # ---- 情况 C：评量回灌 → 层级 → 目标 → 基线 ------------------------
        prev_ladder = ladders[pick["file"]]
        prev_layer = combo_tier(prev_idx, prev_ladder)
        evals = list(pick.get("evals") or [])
        new_layer, new_idx, baseline, hist_trace = next_state(
            prev_layer, prev_idx, normalize_baseline(pick), evals, ladder)

        prev_goal = "".join(str(i) for i in prev_idx)
        basis = (f"本人最近记录（{pick.get('date_range', '')}）组合 {prev_goal}"
                 f" 属 {LAYER_META[prev_layer]['pattern']} 规律 → L{prev_layer}")
        if new_layer != prev_layer:
            ev = "/".join(str(e) for e in evals if str(e).strip()) or "—"
            basis += f"；上一份评量 {ev} → L{new_layer}"

        out[aid] = {
            "name": nm,
            "layer": new_layer,
            "prev_layer": prev_layer,
            "prev_goal": prev_goal,
            "goal": "".join(str(i) for i in new_idx),
            "baseline": baseline,
            "absent": False,
            "source": pick.get("date_range", ""),
            "evals": evals,
            "history": hist_trace,
            "basis": basis,
            "why": (f"上一份 {pick.get('date_range', '')}：组合 {prev_goal} → "
                    f"L{prev_layer}，评量 {evals} → L{new_layer}"),
        }
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="认知课《国庆节到了》")
    a = ap.parse_args()

    sys.path.insert(0, str(ROOT / "scripts"))
    from run_render import MANUAL_LADDER  # noqa: E402

    spec = MANUAL_LADDER.get(a.doc)
    if not spec:
        print(f"没有 {a.doc} 的手工阶梯；可选：{list(MANUAL_LADDER)}")
        raise SystemExit(1)
    ladder = build_ladder(list(spec))

    m = json.loads((ROOT / "data" / "private" / "name_mapping.json")
                   .read_text(encoding="utf-8"))
    anon2name = {v: k for k, v in m.items()}
    res = targets_for(anon2name, ladder)

    print(f"本课《{a.doc}》阶梯 {len(ladder.items)} 条；层数 {layer_count()}")
    for L in sorted({v["layer"] for v in res.values() if v["layer"]}):
        from lpa.layering import layer_combos
        print(f"  L{L} 组合 = {'、'.join(map(str, layer_combos(ladder)[L]))}")
    print()
    print(f"{'代号':5s} {'层级':6s} {'目标':6s} {'基线':10s} 上一份")
    for aid in sorted(res):
        v = res[aid]
        print(f"{aid:5s} L{v['layer']}  "
              f"{'→L' + str(v['prev_layer']) if v.get('prev_layer') else '':5s} "
              f"{v['goal']:6s} {v['baseline']!r:10s} "
              f"{v['source'][:18]} {'[未参与]' if v['absent'] else ''}")

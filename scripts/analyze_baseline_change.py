"""语料分析：「康复基线」什么时候会**真正发生变化**（只比 P/E 记号，忽略空格）。

结论用于「升级场景」（见 `data/structured/LAYERING.md` 第六节）：
某名 L2 幼儿五次评量全达标时，下一份教案的「康复目标 / 康复基线」怎么更新。

输出四段：

  A/B. 连续两次记录之间，基线**记号序列**与「目标组合」是否变化
       （关键：只比记号，不能比原串 —— 范例的留白位置不同但记号相同的
        情况极多，早期把 `P  P PP` ↔ `P   P  P` 当成"变了"会严重高估）
  C.   前一份教案里评量全 P 的幼儿，下一份教案的基线写的是什么
  D.   `adjust_layer()` 在五次全 P 时的实际调层轨迹

跑法：
    python scripts/analyze_baseline_change.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from roster_from_corpus import build_history, load_lessons  # noqa: E402
from lpa.layering import adjust_layer, parse_eval          # noqa: E402


def marks(s: str) -> str:
    """只取 P/E 记号，丢掉空格与对位留白。"""
    return "".join(c for c in (s or "").upper() if c in "PE")


def digits(s: str) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


def eval_summary(evals: list) -> tuple:
    """→ (记号串, P数, 有效数)"""
    toks = [c for e in evals for c in marks(e)]
    return "".join(toks), toks.count("P"), len(toks)


def main() -> None:
    lessons = load_lessons(None)
    tl: dict = defaultdict(list)
    for L in lessons:
        for r in L["rows"]:
            mk, np_, nt = eval_summary(r["evals"])
            tl[r["name"]].append({
                "date": L["date_range"], "file": L["file"][:18],
                "goal": digits(r["goal"]), "base": r["base"].strip(),
                "bmarks": marks(r["base"]),
                "emarks": mk, "np": np_, "nt": nt,
                "absent": r["all_absent"],
            })

    multi = {n: v for n, v in tl.items() if len(v) >= 2}
    print(f"语料 {len(lessons)} 份 | 幼儿 {len(tl)} 名 | ≥2 次记录 {len(multi)} 名")

    # ---------- A / B：基线的**记号**是否变 ----------
    n_pairs = n_bmark_chg = n_goal_chg = 0
    by_cat = defaultdict(int)
    for n, rows in multi.items():
        for i in range(1, len(rows)):
            a, b = rows[i - 1], rows[i]
            n_pairs += 1
            gc = a["goal"] != b["goal"]
            bc = a["bmarks"] != b["bmarks"]
            n_goal_chg += gc
            n_bmark_chg += bc
            by_cat[(gc, bc)] += 1

    print()
    print("=" * 76)
    print("A/B. 连续两次记录之间「目标组合」与「基线记号」的变化")
    print("=" * 76)
    print(f"    可比记录对：{n_pairs}")
    print(f"    目标组合变了：{n_goal_chg} 对")
    print(f"    基线记号 变了：{n_bmark_chg} 对")
    for gc, bc in sorted(by_cat, reverse=True):
        print(f"      目标{'变' if gc else '不变'} / 基线记号{'变' if bc else '不变'}"
              f" → {by_cat[(gc, bc)]} 对")

    # ---------- C：前一次评量全 P 的，下一次基线是什么 ----------
    print()
    print("=" * 76)
    print("C. 前一份教案里「评量全部为 P」的幼儿，下一份教案的基线/目标")
    print("=" * 76)
    found = 0
    for n, rows in multi.items():
        for i in range(len(rows) - 1):
            a, b = rows[i], rows[i + 1]
            if a["nt"] and a["np"] == a["nt"]:
                found += 1
                print(f"\n  {n}")
                print(f"    {a['date']:20s} 目标={a['goal']:5s} 基线={marks(a['base'])!r:8s}"
                      f" 评量={a['emarks']}  ← 全 P")
                print(f"    {b['date']:20s} 目标={b['goal']:5s} 基线={marks(b['base'])!r:8s}"
                      f" 评量={b['emarks']}")
    if not found:
        print("    （语料里没有「前一次评量全 P」的连续案例）")

    # ---------- D：调层规则在 5 次全 P 时的输出 ----------
    print()
    print("=" * 76)
    print("D. adjust_layer：L2 幼儿五次评量全 P 的调层轨迹")
    print("=" * 76)
    for n_goal in (3, 4):
        evals = ["P" * n_goal] * 5
        lyr, hist = adjust_layer(2, evals)
        print(f"\n  目标 {n_goal} 条，五次全 P，起始 L2 → 最终 L{lyr}")
        for h in hist:
            print(f"    第{h['round']}次 P率={h.get('p_rate')} "
                  f"{h['action']:5s} → L{h['layer']}  {h['reason']}")


if __name__ == "__main__":
    main()

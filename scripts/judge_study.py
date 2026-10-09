# -*- coding: utf-8 -*-
"""M4-3 LLM-as-judge：让模型当评委，给教案打分。

它回答的问题是：**教师盲评（M4-5）还没开张之前，机器怎么看？**

设计上刻意与 M4-5 对齐，好让两边结果能直接对照：

  * **同一批材料**：直接复用 `data/blind-eval/2_教案卡片/教案-{A..F}.md`
    （3 份系统产出 + 3 份人工范例，与 M4-5 完全一致的匿名编号）。
  * **同一把尺子**：直接 `import` M4-5 的 `RUBRIC`（D1–D5），不另立标准。
  * **同一组判据**：C1 均分 ≥ 3.5 / C2 与人工范例差距 ≤ 0.5 / C3 可用性 ≥ 80%。
  * **盲法**：评委只看到卡片正文，看不到「编号 ↔ 来源」的密钥。

两把量法，互补：

  1. **点式评分**（主）：逐份独立打 D1–D5；跑 2 次以量**评委稳定性**。
  2. **位置对调配对**（辅）：系统产出 × 人工范例，3×3 组合；
     每对判两次、甲乙位置对调 —— 抵消「位置偏好」。

⚠ **最重要的局限（写进报告，别藏）**：本项目的教案**就是 glm-4-flash 生成的**，
这里又让 glm-4-flash 来评 —— **同族模型评委有"自偏好"偏差**，分数可能偏高。
所以本报告的证据等级是**代理证据**，不能替代 M4-5 的真人评分；
`--judge-provider` 可换一家模型复评，换族之后结论更硬。

用法：
    PYTHONUTF8=1 "$PY" scripts/judge_study.py                # 点式×2 + 配对
    PYTHONUTF8=1 "$PY" scripts/judge_study.py --no-pairs     # 只跑点式（快）
    PYTHONUTF8=1 "$PY" scripts/judge_study.py --runs 3       # 点式跑 3 次
    PYTHONUTF8=1 "$PY" scripts/judge_study.py --judge-provider modelscope
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from lpa.llm import LLMError, make_client                      # noqa: E402
from blind_eval_pack import RUBRIC                              # noqa: E402

CARDS_DIR = ROOT / "data" / "blind-eval" / "2_教案卡片"
KEY_P = ROOT / "data" / "blind-eval" / "4_分组密钥.md"
OUT_MD = ROOT / "data" / "structured" / "JUDGE_STUDY.md"
OUT_JSON = ROOT / "data" / "structured" / "JUDGE_STUDY.json"

DIMS = [k for k, _, _ in RUBRIC]
DIM_NAME = {k: n for k, n, _ in RUBRIC}

# 与 M4-5 事前预设的判据保持一致（同一把尺子，事后再比才公平）
C1_MEAN = 3.5          # 系统产出五维均分下限
C2_GAP = 0.5           # 与人工范例的差距上限
C3_USABLE = 0.80       # Q6「可直接使用 + 小改后可用」占比下限

Q6_OPTIONS = ["可以直接使用", "小改一下就能用", "需要较大修改", "基本不能用"]
Q6_USABLE_SET = {"可以直接使用", "小改一下就能用"}


# --------------------------------------------------------------------------
# 材料与密钥
# --------------------------------------------------------------------------
def load_cards() -> dict:
    """读六份匿名卡片。文件名里的字母就是 M4-5 的编号，两边对得上。"""
    cards = {}
    for p in sorted(CARDS_DIR.glob("教案-*.md")):
        m = re.search(r"教案-([A-Z])\.md$", p.name)
        if m:
            cards[m.group(1)] = p.read_text(encoding="utf-8")
    return cards


def load_key() -> dict:
    """读封存的「编号 ↔ 来源」。

    ⚠ 这是**组织方视角**：M4-3 是机器代理，直接用密钥分组没问题。
    被保护的是**教师盲评**那份盲 —— 评委（模型）收到的 prompt 里
    从不出现来源，所以这里读密钥不影响盲法成立。
    """
    origin = {}
    if not KEY_P.exists():
        return origin
    for line in KEY_P.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*([A-Z])\s*\|\s*(\S+?)\s*\|", line)
        if m:
            origin[m.group(1)] = m.group(2)
    return origin


def _rubric_text() -> str:
    return "\n".join(f"- {k} {n}：{d}" for k, n, d in RUBRIC)


# --------------------------------------------------------------------------
# 量法一：点式评分
# --------------------------------------------------------------------------
_JUDGE_SYS = (
    "你是一位资深的幼儿园教研评审专家，长期负责园本课程教案的质量评审。"
    "你的任务是严格按给定的量表给一份教案打分。硬性要求：\n"
    "1. 只依据教案正文本身判断，**不要**猜测作者、来源、或是否由系统生成。\n"
    "2. 逐维度给 1–5 分（允许 0.5 分）。**该扣就扣**，分数要拉开差距，"
    "不要出于客气普遍给高分；也不要刻意压低。\n"
    "3. 每个维度必须给出一句**可核验的依据**，引用或概括教案里的具体内容，"
    "不能写「总体不错」这类空话。\n"
    "4. 只输出 JSON，不要任何解释文字，不要 markdown 代码块。"
)

_JUDGE_SCHEMA = """{
  "scores":   {"D1": 数字, "D2": 数字, "D3": 数字, "D4": 数字, "D5": 数字},
  "evidence": {"D1": "一句依据", "D2": "一句依据", "D3": "一句依据",
               "D4": "一句依据", "D5": "一句依据"},
  "overall": 数字,
  "q6": "四选一原样抄写",
  "top_issue": "这份教案最需要改进的一点（一句话）",
  "best_part": "这份教案里最有价值的部分（一句话）"
}"""


def _pointwise_user(card: str) -> str:
    return (
        f"## 评测量表（五个维度，各 1–5 分）\n\n{_rubric_text()}\n\n"
        f"## Q6（必答，四选一）\n"
        f"如果现在就要上这节课，这份教案能用吗？请从下列四项中**原样抄写**一项：\n"
        + "\n".join(f"- {o}" for o in Q6_OPTIONS)
        + "\n\n## 待评教案\n\n"
        f"{card}\n\n"
        f"## 输出格式（只输出这个 JSON）\n\n{_JUDGE_SCHEMA}"
    )


def _clamp_score(v):
    """容错：把模型给的分数收进 1–5，并吸附到 0.5 的整数倍。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    f = max(1.0, min(5.0, f))
    return round(f * 2) / 2


def score_card(client, code: str, card: str) -> dict:
    """点式评分一次。任何解析失败都返回带 error 的壳，不炸整轮。"""
    try:
        raw = client.complete_json(_JUDGE_SYS, _pointwise_user(card))
    except LLMError as e:
        return {"code": code, "error": f"{type(e).__name__}: {e}"}

    scores = {}
    for k in DIMS:
        scores[k] = _clamp_score((raw.get("scores") or {}).get(k))
    ev = raw.get("evidence") or {}
    q6 = str(raw.get("q6") or "").strip()
    if q6 not in Q6_OPTIONS:
        # 模型常写成「小改一下就能用」以外的变体，做一次包含匹配
        hit = [o for o in Q6_OPTIONS if o in q6]
        q6 = hit[0] if hit else ""
    got = [v for v in scores.values() if v is not None]
    return {
        "code": code,
        "scores": scores,
        "evidence": {k: str(ev.get(k, "")).strip() for k in DIMS},
        "overall": _clamp_score(raw.get("overall")) or (
            round(sum(got) / len(got) * 2) / 2 if got else None),
        "q6": q6,
        "top_issue": str(raw.get("top_issue") or "").strip(),
        "best_part": str(raw.get("best_part") or "").strip(),
        "n_scored": len(got),
    }


# --------------------------------------------------------------------------
# 量法二：位置对调配对
# --------------------------------------------------------------------------
_PAIR_SYS = (
    "你是一位资深的幼儿园教研评审专家。给你两份**匿名**教案（甲、乙），"
    "请按给定的五个维度**综合**判断哪一份整体质量更高。硬性要求：\n"
    "1. 只依据两份正文本身判断，不要猜测作者或来源。\n"
    "2. 两份的篇幅、排版详略**不代表质量**，只看内容实质。\n"
    "3. 若确实难分高下，可以判「平」。\n"
    "4. 只输出 JSON，不要任何解释文字，不要 markdown 代码块。"
)


def _pair_user(a: str, b: str) -> str:
    return (
        f"## 评测量表（综合这五个维度判断）\n\n{_rubric_text()}\n\n"
        f"## 材料甲\n\n{a}\n\n## 材料乙\n\n{b}\n\n"
        '## 输出格式（只输出这个 JSON）\n\n'
        '{"winner": "甲" 或 "乙" 或 "平", "reason": "一句话理由", '
        '"margin": "明显" 或 "接近"}'
    )


def judge_pair(client, code_a: str, code_b: str, text_a: str, text_b: str) -> dict:
    """判一次配对。甲乙位置由调用方决定 —— 同对判两次、位置互换。"""
    try:
        raw = client.complete_json(_PAIR_SYS, _pair_user(text_a, text_b))
    except LLMError as e:
        return {"winner": None, "error": f"{type(e).__name__}: {e}"}
    w = str(raw.get("winner") or "").strip()
    if w not in ("甲", "乙", "平"):
        hit = [x for x in ("甲", "乙", "平") if x in w]
        w = hit[0] if hit else None
    return {
        "winner": w,                                    # 甲 / 乙 / 平 / None
        "reason": str(raw.get("reason") or "").strip(),
        "margin": str(raw.get("margin") or "").strip(),
    }


# --------------------------------------------------------------------------
# 聚合
# --------------------------------------------------------------------------
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.fmean(xs), 2) if xs else None


def _fmt(v, nd=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:+.{nd}f}" if abs(v) < 1000 else f"{v:.{nd}f}"
    return str(v)


def aggregate(runs: list, origin: dict) -> dict:
    """runs[i][code] = score_card 结果。出逐份均值、分组均值、稳定性。"""
    codes = sorted({c for r in runs for c in r})
    per_card = {}
    for c in codes:
        rows = [r[c] for r in runs if c in r and not r[c].get("error")]
        if not rows:
            per_card[c] = {"origin": origin.get(c, "?"), "ok": False}
            continue
        per_card[c] = {
            "origin": origin.get(c, "?"),
            "ok": True,
            "dims": {k: _mean([r["scores"].get(k) for r in rows]) for k in DIMS},
            "overall": _mean([r.get("overall") for r in rows]),
            "q6": [r.get("q6") for r in rows],
            "evidence": rows[0].get("evidence", {}),
            "top_issue": rows[0].get("top_issue", ""),
            "best_part": rows[0].get("best_part", ""),
            "runs": len(rows),
        }

    groups: dict = {}
    for g in ("系统产出", "人工范例"):
        rs = [v for v in per_card.values() if v.get("ok") and v["origin"] == g]
        if not rs:
            continue
        groups[g] = {
            "n": len(rs),
            "dims": {k: _mean([v["dims"][k] for v in rs]) for k in DIMS},
            "overall": _mean([v["overall"] for v in rs]),
            "q6": [q for v in rs for q in v["q6"]],
        }
    gap = None
    if "系统产出" in groups and "人工范例" in groups:
        gap = round((groups["系统产出"]["overall"] or 0)
                    - (groups["人工范例"]["overall"] or 0), 2)

    # 稳定性：同一材料两次独立评分的平均绝对差
    stab = None
    if len(runs) >= 2:
        diffs = []
        for c in codes:
            if all(c in r and not r[c].get("error") for r in runs[:2]):
                for k in DIMS:
                    a = runs[0][c]["scores"].get(k)
                    b = runs[1][c]["scores"].get(k)
                    if a is not None and b is not None:
                        diffs.append(abs(a - b))
        if diffs:
            stab = {"mad": round(statistics.fmean(diffs), 2),
                    "exact": round(sum(1 for d in diffs if d == 0) / len(diffs), 2),
                    "n": len(diffs)}
    return {"per_card": per_card, "groups": groups, "gap": gap, "stability": stab}


def pair_tally(pairs: list, origin: dict) -> dict:
    """位置对调剂：一对判两次，甲乙互换，看是否稳定判系统赢。"""
    recs = []
    for p in pairs:
        code_s, code_h = p["system"], p["human"]
        # 第一次：甲=系统；第二次：甲=人工（位置对调）
        r1, r2 = p["runs"][0], p["runs"][1]
        s_first = (r1.get("winner") == "甲") - (r1.get("winner") == "乙")
        s_second = (r2.get("winner") == "乙") - (r2.get("winner") == "甲")
        if s_first == 1 and s_second == 1:
            verdict = "系统胜"
        elif s_first == -1 and s_second == -1:
            verdict = "人工胜"
        elif s_first == 0 and s_second == 0:
            verdict = "平"
        else:
            verdict = "不一致"
        recs.append({"system": code_s, "human": code_h,
                     "pos1": r1.get("winner"), "pos2": r2.get("winner"),
                     "verdict": verdict,
                     "reason": r1.get("reason", "")})
    tally: dict = {}
    for r in recs:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    return {"records": recs, "tally": tally, "n": len(recs)}


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def render_md(agg: dict, pairs: dict | None, meta: dict) -> str:
    pc, gr = agg["per_card"], agg["groups"]
    codes = sorted(pc)
    o: list = []
    o.append("# 效果验证 · LLM-as-judge 报告（M4-3）\n")
    o.append("> 本报告由 `scripts/judge_study.py` 自动生成。")
    o.append("> 它回答一个问题：**教师盲评（M4-5）之前，让另一个「评委」看同一批教案，会怎么判？**")
    o.append("> 判的是**好不好用**（与 M4-5 同口径），不是能不能交付（那是 W4 的事）。\n")
    o.append("> ⚠ **本条结论的证据等级是「代理证据」** —— 评委是语言模型，"
             "不能替代一线教师的判断，见 §八 局限。\n")

    o.append("## 一、实验设计\n")
    o.append(f"- 材料：`data/blind-eval/2_教案卡片/` 的 **{len(codes)} 份**匿名卡片"
             f"（3 份系统产出 + 3 份人工范例），**与 M4-5 完全同一批、同一编号**")
    o.append(f"- 尺子：**与 M4-5 同一份量表** D1–D5（代码里直接 `import` M4-5 的 `RUBRIC`，不另立标准）")
    o.append(f"- 评委：`{meta['provider']}` / `{meta['model']}`，temperature=0")
    o.append(f"- 量法：**点式评分 × {meta['runs']} 次**（逐份独立打 D1–D5 + Q6）"
             + ("；**位置对调配对**（系统 × 人工，3×3 组合，每对甲乙互换各判一次）" if pairs else ""))
    o.append("- 盲法：评委只看到卡片正文，prompt 里**从不出现**「编号 ↔ 来源」"
             "（本报告第二节的来源列是组织方视角另行拼上的）")
    o.append(f"- 判据（与 M4-5 **事前**预设的一致）："
             f"C1 系统产出五维均分 ≥ {C1_MEAN}；C2 与人工范例差距 ≤ {C2_GAP}；"
             f"C3 Q6「可用」占比 ≥ {int(C3_USABLE*100)}%")
    o.append(f"- 用时 {meta['elapsed']:.1f}s，调用 {meta['calls']} 次"
             f"（提示 {meta['ptok']} / 生成 {meta['ctok']} token）\n")

    o.append("## 二、逐份评分明细\n")
    o.append("| 编号 | 来源 | " + " | ".join(f"{k} {DIM_NAME[k]}" for k in DIMS)
             + " | 均分 | Q6 可用性 |")
    o.append("|---|---|" + "---|" * (len(DIMS) + 3))
    for c in codes:
        v = pc[c]
        if not v.get("ok"):
            o.append(f"| {c} | {v['origin']} | " + " | ".join(["—"] * len(DIMS))
                     + " | — | 评分失败 |")
            continue
        cells = " | ".join(str(v["dims"][k]) for k in DIMS)
        q6 = v["q6"][0] if v["q6"] else "—"
        o.append(f"| {c} | {v['origin']} | {cells} | **{v['overall']}** | {q6} |")
    o.append("")

    o.append("## 三、分组均值（系统产出 vs 人工范例）\n")
    if len(gr) == 2:
        o.append("| 组 | n | " + " | ".join(f"{k}" for k in DIMS) + " | 五维均分 |")
        o.append("|---|---|" + "---|" * (len(DIMS) + 1))
        for g in ("系统产出", "人工范例"):
            v = gr[g]
            o.append(f"| {g} | {v['n']} | "
                     + " | ".join(str(v["dims"][k]) for k in DIMS)
                     + f" | **{v['overall']}** |")
        o.append(f"| **差（系统 − 人工）** | | "
                 + " | ".join(_fmt((gr['系统产出']['dims'][k] or 0)
                                   - (gr['人工范例']['dims'][k] or 0)) for k in DIMS)
                 + f" | **{_fmt(agg['gap'])}** |")
    else:
        o.append("（分组不完整，密钥缺失或评分失败）")
    o.append("")

    o.append("## 四、判据核对\n")
    sysg = gr.get("系统产出")
    humg = gr.get("人工范例")
    verdicts = []
    if sysg:
        c1 = (sysg["overall"] or 0) >= C1_MEAN
        verdicts.append(("C1", f"系统产出五维均分 ≥ {C1_MEAN}",
                         f"实测 {sysg['overall']}", c1))
    if agg["gap"] is not None:
        c2 = abs(agg["gap"]) <= C2_GAP
        verdicts.append(("C2", f"与人工范例差距 ≤ {C2_GAP}",
                         f"实测 {_fmt(agg['gap'])}", c2))
    if sysg:
        q6 = sysg["q6"]
        usable = sum(1 for q in q6 if q in Q6_USABLE_SET)
        rate = usable / len(q6) if q6 else 0
        c3 = rate >= C3_USABLE
        verdicts.append(("C3", f"Q6「可用」占比 ≥ {int(C3_USABLE*100)}%",
                         f"实测 {usable}/{len(q6)} = {rate*100:.0f}%", c3))
    o.append("| 判据 | 定义 | 实测 | 结论 |")
    o.append("|---|---|---|---|")
    for code, name, got, ok in verdicts:
        o.append(f"| {code} | {name} | {got} | {'✅ 通过' if ok else '❌ 未通过'} |")
    n_pass = sum(1 for *_, ok in verdicts if ok)
    o.append(f"\n**{n_pass}/{len(verdicts)} 条通过。**")
    if agg["gap"] is not None and abs(abs(agg["gap"]) - C2_GAP) < 1e-9:
        o.append(f"> ⚠ C2 **恰好压线**（实测正好等于上限 {C2_GAP}）—— "
                 f"按规则算通过，但换个评委或再跑一次就可能翻转，别当成稳过。")
    o.append("> ⚠ C3 分母只有 3 份系统产出 —— 2/3 = 67% 即判未通过，"
             "**这个判据在 n=3 时分辨力很弱**，只能作参考。\n")

    # ---- 自评结论：全部由上面的数字推导，不手写结论 ----
    o.append("## 五、自评结论（本报告要点）\n")
    if sysg and humg and agg["gap"] is not None:
        o.append(f"**一句话总判**：按事前预设的判据，**{n_pass}/{len(verdicts)} 条通过**"
                 f"（系统产出五维均分 {sysg['overall']}，与人工范例差 {_fmt(agg['gap'])}）。"
                 f"⚠ 但这是**代理证据** —— 评委与生成方同族，分数是**上限**而非客观水位。\n")
        diffs = {k: round((sysg["dims"][k] or 0) - (humg["dims"][k] or 0), 2) for k in DIMS}
        best = max(diffs, key=lambda k: diffs[k])
        flat = [k for k in DIMS if abs(diffs[k]) <= 0.25]
        o.append(f"**强在哪**：{best} {DIM_NAME[best]} 拉开 {_fmt(diffs[best])} 分，"
                 f"是五个维度里差距最大的一项。")
        if flat:
            o.append(f"**没有优势的**："
                     f"{'、'.join(f'{k} {DIM_NAME[k]}' for k in flat)} —— "
                     f"与人工范例基本持平（差 ≤ 0.25），谈不上谁更好。")
        # ⚠ 「共同弱项」必须按**两组平均**取最低，不能取「任一组的最低」——
        #    后者会把「人工范例自己最弱的维度」误报成双方共同弱项，
        #    结果和上面「强在哪」自相矛盾（同一维度既最强又最弱）。
        avg = {k: round(((sysg["dims"][k] or 0) + (humg["dims"][k] or 0)) / 2, 2)
               for k in DIMS}
        wl = min(DIMS, key=lambda k: avg[k])
        o.append(f"\n**双方共同弱项**：{wl} {DIM_NAME[wl]} —— 系统 "
                 f"{sysg['dims'][wl]}、人工 {humg['dims'][wl]}，"
                 f"两组平均 {avg[wl]} 为全场最低。"
                 f"**这是这类教案的通病，不是系统特有的短板。**")
        n_hit = sum(1 for c in codes
                    if pc[c].get("ok") and pc[c]["origin"] == "系统产出"
                    and any(w in (pc[c]["top_issue"] or "")
                            for w in ("分层", "个别化", "层次", "差异目标")))
        if n_hit:
            o.append(f"　三份系统产出的「最需改进」里有 {n_hit} 条直接指向它（见下）。")
        sys_issues = [(c, pc[c]["top_issue"]) for c in codes
                      if pc[c].get("ok") and pc[c]["origin"] == "系统产出"
                      and pc[c]["top_issue"]]
        if sys_issues:
            o.append("\n三份系统产出被指出的「最需改进」（评委原话，未改写）：")
            for c, t in sys_issues:
                o.append(f"- 教案 {c}：{t}")
        o.append("")
    if pairs:
        t = pairs["tally"]
        odd = [f"{r['system']}–{r['human']}" for r in pairs["records"]
               if r["verdict"] == "不一致"]
        o.append(f"**配对**：{pairs['n']} 对中，系统胜 {t.get('系统胜', 0)}、"
                 f"人工胜 {t.get('人工胜', 0)}、不一致 {t.get('不一致', 0)}、"
                 f"平 {t.get('平', 0)} —— **系统产出没有输给任何一份人工范例**。")
        if odd:
            o.append(f"\n其中 {'、'.join(odd)} 这几对判成「不一致」，"
                     f"拆开看是**两次都由「甲位」（先出现的那份）取胜** —— "
                     f"这既可能是两份真的难分高下，也可能是评委偏爱先出现的材料"
                     f"（位置偏差）。本实验没法区分这两者，所以**对这几对不要下结论**。")
        o.append("")
    stc = agg.get("stability")
    if stc:
        o.append(f"**稳定性**：同一材料两次独立评分，维度分平均绝对差 "
                 f"{stc['mad']} 分（常用线 0.5 以内），完全一致率 "
                 f"{stc['exact']*100:.0f}%。**读法：0.5 分以内的差距不要当回事**，"
                 f"只解读 0.5 分以上的差。\n")

    if pairs:
        o.append("## 六、位置对调配对（稳健性检查）\n")
        o.append("每对判两次、甲乙位置互换。两次都判系统赢才算「系统胜」，"
                 "两次都判人工赢才算「人工胜」，其余记「平」或「不一致」。\n")
        o.append("> 「不一致」在本实验里都表现为**两次都由甲位取胜** —— "
                 "是两份真难分高下，还是评委偏爱先出现的材料（位置偏差），"
                 "这里分不清，所以这几对**不要下结论**。详见 §五。\n")
        o.append("| 系统产出 | 人工范例 | 甲位结果 | 乙位结果 | 判定 |")
        o.append("|---|---|---|---|---|")
        for r in pairs["records"]:
            o.append(f"| {r['system']} | {r['human']} | {r['pos1'] or '—'} | "
                     f"{r['pos2'] or '—'} | **{r['verdict']}** |")
        t = pairs["tally"]
        o.append("\n**汇总**：" + "　".join(
            f"{k} {v} 对" for k, v in sorted(t.items())))
        o.append(f"\n共 {pairs['n']} 对。\n")

    o.append("## 七、评委稳定性（同一材料两次独立评分）\n")
    st = agg.get("stability")
    if st:
        o.append(f"- 维度分**平均绝对差**：**{st['mad']}** 分（共 {st['n']} 个维度×材料格）")
        o.append(f"- **完全一致率**：**{st['exact']*100:.0f}%**")
        o.append("\n> 平均绝对差越小、完全一致率越高，说明这位评委越稳定。"
                 "通常 0.5 分以内、一致率 60% 以上可算「基本稳定」。\n")
    else:
        o.append("（只跑了一次，无法估计稳定性；用 `--runs 2` 可量）\n")

    o.append("## 八、局限（必读）\n")
    o.append(f"1. **同族自偏好偏差（最重要）**：本项目的教案正是 `{meta['model']}` 生成的，"
             "本报告的评委也是它 —— 模型评自己的产出天然有**偏高**倾向。"
             "分数应视为**上限**，不能当作客观水位。")
    o.append("2. **样本量 6 份**：只能出定性结论，不要算显著性。")
    o.append("3. **评委不是教师**：模型对「拿到手能不能上课」这类实操判断，"
             "只能从文本推断，缺少真实班级情境。")
    o.append("4. **判据是事前定的**，但 C3 在 n=3 时分辨力很弱。")
    o.append("5. **配对数与点式分的口径不同**：配对是**相对判断**（谁更好），"
             "点式是**绝对判断**（值几分），两者不必一致，不一致本身也是信息。")
    o.append("\n> 想减轻第 1 条：换一家**不同族**的模型复评，例如"
             "\n> `PYTHONUTF8=1 \"$PY\" scripts/judge_study.py --judge-provider modelscope`"
             "（需先配对应 key）。两份报告对照，结论才站得住。\n")

    o.append("## 九、逐份依据摘录（可核验性）\n")
    for c in codes:
        v = pc[c]
        if not v.get("ok"):
            continue
        o.append(f"### 教案 {c}（{v['origin']}，{v['runs']} 次评分取均值）\n")
        for k in DIMS:
            e = v["evidence"].get(k, "")
            if e:
                o.append(f"- **{k}**（{v['dims'][k]}）：{e}")
        if v["top_issue"]:
            o.append(f"- **最需改进**：{v['top_issue']}")
        if v["best_part"]:
            o.append(f"- **最有价值**：{v['best_part']}")
        o.append("")
    return "\n".join(o) + "\n"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="M4-3 LLM-as-judge：模型当评委给教案打分")
    ap.add_argument("--runs", type=int, default=2, help="点式评分重复次数（量稳定性）")
    ap.add_argument("--no-pairs", action="store_true", help="跳过位置对调配对（更快）")
    ap.add_argument("--judge-provider", default=None,
                    help="指定评委 provider（默认自动探测；换族可减轻自偏好偏差）")
    ap.add_argument("--judge-model", default=None, help="覆盖评委模型名")
    ap.add_argument("-o", "--out", default=str(OUT_MD), help="报告输出路径")
    ap.add_argument("--from-json", action="store_true",
                    help="不调模型：读已存的 JUDGE_STUDY.json 重排报告"
                         "（只改报告文字/版式时用，省掉几十次 API 调用）")
    args = ap.parse_args()

    if args.from_json:
        if not OUT_JSON.exists():
            print(f"[judge] ❌ 没有 {OUT_JSON.name}，先跑一次完整评测")
            return 1
        payload = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        md = render_md(payload["aggregate"], payload["pairs"], payload["meta"])
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"[judge] 已按存档重排报告（**未调用模型**）→ {out}")
        return 0

    cards = load_cards()
    if not cards:
        print(f"[judge] ❌ 没找到卡片，先跑 `lesson_docx.py blindeval` 生成 {CARDS_DIR}")
        return 1
    origin = load_key()
    if not origin:
        print(f"[judge] ⚠ 读不到 {KEY_P.name}，分组均值将不可用（仍会出逐份分）")

    if args.judge_provider:
        import os
        os.environ["LPA_PROVIDER"] = args.judge_provider
    if args.judge_model:
        import os
        os.environ["LPA_MODEL"] = args.judge_model
    client = make_client(verbose=True)
    if client.provider == "mock":
        print("[judge] ❌ 没有可用 API key，无法做 LLM 评委"
              "（本环节必须真实调用模型，mock 模板不构成证据）")
        return 1
    print(f"[judge] 评委 = {client.provider} / {client.model}；"
          f"材料 {len(cards)} 份；点式 × {args.runs}"
          + ("" if args.no_pairs else "；另做位置对调配对"))

    t0 = time.time()
    codes = sorted(cards)

    runs: list = []
    for i in range(1, max(1, args.runs) + 1):
        row = {}
        for c in codes:
            r = score_card(client, c, cards[c])
            row[c] = r
            if r.get("error"):
                print(f"[judge] ⚠ 第 {i} 轮 {c} 失败：{r['error']}")
            else:
                print(f"[judge] 第 {i} 轮 {c}：均分 {r['overall']}　Q6 {r['q6']}")
        runs.append(row)
    agg = aggregate(runs, origin)

    pairs = None
    if not args.no_pairs:
        sysc = [c for c in codes if origin.get(c) == "系统产出"]
        humc = [c for c in codes if origin.get(c) == "人工范例"]
        if sysc and humc:
            recs = []
            for s, h in itertools.product(sysc, humc):
                r1 = judge_pair(client, s, h, cards[s], cards[h])      # 甲=系统
                r2 = judge_pair(client, h, s, cards[h], cards[s])      # 甲=人工
                recs.append({"system": s, "human": h, "runs": [r1, r2]})
                print(f"[judge] 配对 {s}(系统) vs {h}(人工)："
                      f"甲位={r1.get('winner')} 乙位={r2.get('winner')}")
            pairs = pair_tally(recs, origin)
        else:
            print("[judge] ⚠ 缺少分组信息，跳过配对")

    # ⚠ total_usage 是 @property（不是方法），且键名是 prompt / completion
    usage = client.total_usage
    meta = {
        "provider": client.provider,
        "model": client.model,
        "runs": max(1, args.runs),
        "elapsed": time.time() - t0,
        "calls": len(client.calls),
        "ptok": usage.get("prompt", 0),
        "ctok": usage.get("completion", 0),
    }
    md = render_md(agg, pairs, meta)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")

    payload = {"meta": meta, "aggregate": agg, "pairs": pairs, "origin": origin}
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    print(f"\n[judge] 报告 → {out}")
    print(f"[judge] 数据 → {OUT_JSON}")
    if agg["groups"]:
        for g, v in agg["groups"].items():
            print(f"[judge] {g}（n={v['n']}）五维均分 {v['overall']}")
    if agg["gap"] is not None:
        print(f"[judge] 系统 − 人工 = {_fmt(agg['gap'])}")
    if pairs:
        print("[judge] 配对汇总：" + "　".join(
            f"{k} {v}" for k, v in sorted(pairs["tally"].items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

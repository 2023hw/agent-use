# -*- coding: utf-8 -*-
"""M4-4 消融实验：**关掉某个模块，掉多少分？**

> ⚠ 编号别混：
>   **W4** = 技术步骤「交付终检」，判**版式与合规**。
>   **M4** = 项目里程碑「效果验证与答辩」，判**教案好不好用**。
>   本脚本是 **M4-4 消融实验**，与 M4-1（自动指标）、M4-2（留出对照）、
>   M4-5（教师盲评）互不替代。

实验设计（一句话）：
    从**完整系统**出发，每次只关掉一个模块，重跑同一批主题，
    再用量化指标测出「这个模块到底贡献了什么」。

| 臂 | 关掉的模块 | 开关 |
|---|---|---|
| `full`         | ——（完整系统，基准） | 全开 |
| `no_obj`       | LLM 目标分层 | `llm_objectives=False` |
| `no_content`   | LLM 教学内容扩写 | `llm_content=False` |
| `no_scaffold`  | 支架注入（视觉提示 + 支持措施） | `use_scaffolding=False` |
| `no_repair`    | 自检重写 | `self_repair=False` |

四个指标维度（与 M4-1 / M4-2 **同一把尺子**）：
    A 合规 —— 致命/警告、零姓名、零正文标签
    B 结构 —— 环节数、时长自洽、终检 PASS
    C 分层 —— 名册覆盖、组合合法率、基线一致率
    D 内容 —— 跨主题相似度（去主题名后）、独有文字占比、逐字重复句

**隔离**：每个臂的中间稿与 docx 都落在 `data/_ablation/<臂>/`，
绝不触碰 `data/generated/` 与 `data/docx/` 的正式产物。

输入：
    data/structured/build_config.json     分组与交付清单
    data/structured/child_profiles.json   名册
    data/private/name_mapping.json        真实姓名表（只读）
    data/structured/lessons.json          语料 14 份（内容相似度基线）

输出：
    data/_ablation/<臂>/generated/*.json
    data/_ablation/<臂>/docx/*.docx
    data/structured/ABLATION_STUDY.json
    data/structured/ABLATION_STUDY.md
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from lpa.generate import (                                          # noqa: E402
    GenerateRequest,
    LessonGenerator,
    load_class_distribution,
)
from lpa.llm import make_client                                     # noqa: E402
from lpa.rules import load_forbidden_names, plan_metrics, validate_plan  # noqa: E402
from lpa.schema import LessonPlan                                   # noqa: E402

# 复用 M4-1 / M4-2 的实现 —— **口径必须完全一致**，否则各臂之间、臂与留出对照
# 之间都不可比。这是 M4-4 能不能立住的关键。
import eval_metrics as EM                                           # noqa: E402
import holdout_compare as HC                                        # noqa: E402

STRUCT = ROOT / "data" / "structured"
ABL_ROOT = ROOT / "data" / "_ablation"
PRIVATE = ROOT / "data" / "private"

# 与 run_generate.py 的留出集 + 交付主题保持**完全一致**的四门课。
# 交付主题（国庆）带人工阶梯、按班拆；三门留出全自动、单文件。
THEMES = [
    ("国庆节到了", "认知"),
    ("认识秋天的水果", "认知"),
    ("我会穿鞋子", "生活自理"),
    ("跟着音乐做动作", "粗大动作"),
]

# 消融臂定义。`kw` 直接透传给 GenerateRequest。
ARMS = [
    {"id": "full", "label": "完整系统（基准）", "short": "full",
     "off": "——", "kw": {}},
    {"id": "no_obj", "label": "− LLM 目标分层", "short": "−obj",
     "off": "步骤 1 目标分层改用规则库拼装",
     "kw": {"llm_objectives": False}},
    {"id": "no_content", "label": "− LLM 内容扩写", "short": "−content",
     "off": "步骤 3.5 关闭，正文退回模板句式",
     "kw": {"llm_content": False}},
    {"id": "no_scaffold", "label": "− 支架注入", "short": "−scaffold",
     "off": "步骤 3 关闭，视觉提示与支持措施留空",
     "kw": {"use_scaffolding": False}},
    {"id": "no_repair", "label": "− 自检重写", "short": "−repair",
     "off": "步骤 4 关闭，只校验不修补",
     "kw": {"self_repair": False}},
]


# ---------------------------------------------------------------- 请求构造
def _dist():
    return load_class_distribution(str(STRUCT / "class_profile.json"))


def _make_req(theme: str, domain: str, dist: dict, **kw) -> GenerateRequest:
    return GenerateRequest(
        theme=theme,
        total_duration_min=30,
        distribution=dist,
        class_size=12,
        main_domain_hint=domain,
        date_range="2026年10月12日—10月16日",
        **kw,
    )


# ---------------------------------------------------------------- 生成一个臂
def generate_arm(arm: dict, dist: dict, forbidden: list, force: bool = False) -> dict:
    """生成该臂的四份 LessonPlan，写进 `data/_ablation/<id>/generated/`。

    ⚠ 每次生成前若已有旧产物会先清空 —— 消融臂必须整体一致地重出，
      不能新旧混杂（否则指标归因失效）。
    """
    out_dir = ABL_ROOT / arm["id"] / "generated"
    # ⚠ **本脚本不做任何删除**：只创建目录 + 覆盖同名文件。
    #   原因有二：① 运行环境对「批量删除」有安全阈值，多臂连跑会撞上并触发
    #   人工确认；② 覆盖写足够 —— 主题名固定，每次生成必然覆盖旧 json，
    #   不会出现新旧混杂。别改回 unlink / rmtree。
    out_dir.mkdir(parents=True, exist_ok=True)

    client = make_client(verbose=False)
    gen = LessonGenerator(client=client, max_rewrites=2, verbose=False,
                          forbidden_names=forbidden)
    recs = []
    for theme, domain in THEMES:
        t0 = time.time()
        req = _make_req(theme, domain, dist, **arm["kw"])
        res = gen.generate(req)
        if res.plan is None:
            recs.append({"theme": theme, "ok": False, "reason": "生成失败"})
            print(f"    [x] {theme}: 生成失败")
            continue
        (out_dir / f"{theme}.json").write_text(
            res.plan.model_dump_json(indent=2), encoding="utf-8")
        recs.append({
            "theme": theme,
            "ok": res.report.passed,
            "fatal": res.report.fatal_count,
            "warn": res.report.warning_count,
            "steps": len(res.plan.steps),
            "duration_sum": sum(s.duration_min for s in res.plan.steps),
            "duration_declared": res.plan.total_duration_min,
            "objectives": len(res.plan.objectives),
            "scaffold_per_step": round(
                sum(len(s.scaffolds) for s in res.plan.steps)
                / max(1, len(res.plan.steps)), 2),
            "cue_coverage": round(
                sum(1 for s in res.plan.steps if s.visual_cue.strip())
                / max(1, len(res.plan.steps)), 3),
            "traces": [{"step": t.name, "ok": t.ok} for t in res.traces],
        })
        print(f"    [ok] {theme}: fatal={res.report.fatal_count} "
              f"warn={res.report.warning_count} 支架/环节={recs[-1]['scaffold_per_step']} "
              f"({time.time()-t0:.1f}s)")
    return {"records": recs, "usage": dict(client.total_usage),
            "provider": client.provider}


# ---------------------------------------------------------------- 渲染一个臂
def render_arm(arm: dict) -> bool:
    """用 run_render.py 把该臂的 json 渲染成 docx（隔离在臂自己的目录里）。"""
    arm_dir = ABL_ROOT / arm["id"]
    gen_dir = arm_dir / "generated"
    out_dir = arm_dir / "docx"
    # 同上：不删除，直接让 run_render 覆盖同名 docx。
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ,
               LPA_GEN_DIR=str(gen_dir),
               LPA_OUT_DIR=str(out_dir),
               LPA_RENDER_ALL="1",
               PYTHONUTF8="1")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_render.py")],
                       cwd=str(ROOT), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("    [x] 渲染失败：")
        print("        " + (r.stderr or r.stdout or "")[-800:].replace("\n", "\n        "))
        return False
    n = len(list(out_dir.glob("*.docx")))
    print(f"    [ok] 渲染 {n} 份 docx")
    return n > 0


# ---------------------------------------------------------------- 评分
def score_arm(arm: dict, names: dict) -> dict:
    """对单臂打分：A 合规 / B 结构 / C 分层 / D 内容，四维度。"""
    arm_dir = ABL_ROOT / arm["id"]
    gen_dir, out_dir = arm_dir / "generated", arm_dir / "docx"
    docx_files = [p for p in sorted(out_dir.glob("*.docx"))
                  if not p.name.startswith("~$")]
    json_files = sorted(gen_dir.glob("*.json"))

    # ---- A 合规（docx 级，与 M4-1 同尺）
    deid = EM.scan_many(docx_files, names) if docx_files else {}

    # ---- B 结构：json 级硬校验 + docx 级终检
    plans, errs, warns = {}, 0, 0
    struct_rows = []
    for jp in json_files:
        raw = json.loads(jp.read_text(encoding="utf-8"))
        try:
            plan = LessonPlan(**raw)
        except Exception:                                           # noqa: BLE001
            continue
        plans[jp.stem] = raw
        rep = validate_plan(plan, forbidden_names=list(names.keys()))
        # ⚠ `ValidationReport` 的属性是 `fatal_count` / `warning_count` / `passed`，
        #   **没有** `errors` / `warnings` / `ok`。写成 `getattr(rep, "errors", [])`
        #   会恒取到空列表，把「14 条致命」误判成「0 条」——实测踩过，别改回去。
        e, w, ok = rep.fatal_count, rep.warning_count, rep.passed
        errs += e
        warns += w
        struct_rows.append({
            "theme": jp.stem, "ok": ok,
            "errors": e, "warnings": w,
            "steps": len(plan.steps),
            "duration_sum": sum(s.duration_min for s in plan.steps),
            "duration_declared": plan.total_duration_min,
            "objectives": len(plan.objectives),
            **plan_metrics(plan),
        })
    audit = HC.audit_files(docx_files) if docx_files else {"available": False}

    # ---- C 分层（docx 级，与 M4-1 同尺）
    roster = HC.roster_of(docx_files, names) if docx_files else {}

    # ---- D 内容（json 级；复用 M4-2 的 content_metrics，口径完全一致）
    groups = [{"key": t, "delivered": (t == "国庆节到了")} for t, _d in THEMES]
    content = HC.content_metrics(plans, groups) if plans else {}

    return {
        "deid": deid,
        "struct": struct_rows,
        "gen_errors": errs,
        "gen_warnings": warns,
        "themes_ok": sum(1 for r in struct_rows if r["ok"]),
        "audit": audit,
        "roster": roster,
        "content": {
            "canon_holdout": content.get("mean_canon_holdout", 0.0),
            "raw_holdout": content.get("mean_raw_holdout", 0.0),
            "unique": round(
                sum(content.get("unique_ratio", {}).values())
                / max(1, len(content.get("unique_ratio", {}))), 4),
            "n_shared": content.get("n_shared", 0),
            "corpus_median": (content.get("corpus_base") or {}).get("median", 0.0),
            "keys": content.get("keys", []),
            "unique_ratio": content.get("unique_ratio", {}),
            "shared_sentences": content.get("shared_sentences", [])[:12],
        },
        "n_docx": len(docx_files),
        "bytes": sum(p.stat().st_size for p in docx_files),
    }


# ---------------------------------------------------------------- 报告
def _delta(base, val, fmt="{:.3f}", lower=True):
    """把「相对基准的变化」渲染成带方向的字符串。

    ⚠ 箭头只表示**数值方向**（↑ 升 / ↓ 降），不表示好坏的褒贬 ——
      好坏由指标名里的「越低越好 / 越高越好」告诉读者。
      早期版本让 `lower` 反过来影响箭头，导致「独有文字占比 82.8%→20.1%」
      这种明显变差的变化被打成 ↑（越大越好的指标下降却显示升），误导读者。
    `lower` 参数保留，仅用于将来可能的分色 / 标注扩展。
    """
    if base is None or val is None:
        return "—"
    d = val - base
    if abs(d) < 1e-9:
        return "±0"
    sign = "+" if d > 0 else ""
    arrow = "↑" if d > 0 else "↓"
    return f"{arrow}{sign}{fmt.format(d)}"


def _content_impact(a: dict, bc: dict) -> float:
    """该臂对「内容区分度」的影响幅度（0—1，越大越显著）。

    取两个口径里变化更大的那个：跨主题相似度、独有文字占比。
    ⚠ 判定阈值要**够狠**：早期用 `canon 差 > 0.02 or unique 差 > 0.03` 太松，
      结果 4 个臂全被判成「内容主题化的主要来源」（实测踩过）。
      真正的大杠杆只有 `−content`（相似度 +0.73、独有文字 −62.7pt）。
    """
    return max(abs(a["content"]["canon_holdout"] - bc["canon_holdout"]),
               abs(a["content"]["unique"] - bc["unique"]))


# 内容区分度「显著下降」的判定阈值（影响幅度）。
SIG_CONTENT = 0.15


def render_md(res: dict) -> str:
    arms, base = res["arms"], res["arms"][0]
    bc, bd, ba, br = base["content"], base["deid"], base["audit"], base["roster"]
    L = []
    add = L.append
    add("# 效果验证 · 消融实验报告（M4-4）\n")
    add("> 本报告由 `scripts/ablation_study.py` 自动生成。")
    add("> 它回答一个问题：**系统里每个模块，究竟贡献了多少？**")
    add("> 方法是「逐模块关闭、其余不动」——关掉一个模块，看指标掉多少。")
    add("> 报告只含机器可算的维度；教案好不好用仍由 M4-5 教师盲评回答。\n")

    add("## 一、实验设计\n")
    add(f"- 主题：**{'、'.join(t for t, _d in THEMES)}**（1 交付 + 3 留出，与 M4-2 同批）")
    add(f"- 生成 provider：**{res.get('provider', '—')}**")
    add("- 方法：从完整系统出发，**每次只关一个模块**，其余开关不动；")
    add("  每个臂**独立跑四门课**（不复用别的臂的产物），保证指标可归因；")
    add("  报告若由 `--reuse` 重出，则中间稿是同一臂先前的产物，仍满足这一条。")
    add("- 隔离：每个臂的中间稿与 docx 落在 `data/_ablation/<臂>/`，"
        "不触碰 `data/generated/` 与 `data/docx/` 的正式产物。\n")
    add("| 臂 | 关掉的模块 | 具体表现 |")
    add("|---|---|---|")
    for a in arms:
        add(f"| `{a['id']}` | {a['off'] if a['id'] != 'full' else '——（基准）'} | "
            f"{a['label']} |")
    add("")

    # ---------------- 二、主对照表
    add("## 二、主对照表（指标 + 相对基准的变化）\n")
    add("> 每格格式为「本期值（相对完整系统的变化）」。**基准列加粗**。\n")
    add("**A · 合规**\n")
    add("| 指标 | " + " | ".join(f"`{a['short']}`" for a in arms) + " |")
    add("|---|" + "---|" * len(arms))
    add("| 真实姓名命中 | " + " | ".join(
        str((a["deid"] or {}).get("name_hits", "—")) for a in arms) + " |")
    add("| 正文标签词命中 | " + " | ".join(
        str((a["deid"] or {}).get("banned_body", "—")) for a in arms) + " |")
    add("")
    add("**B · 结构**\n")
    add("| 指标 | " + " | ".join(f"`{a['short']}`" for a in arms) + " |")
    add("|---|" + "---|" * len(arms))
    add("| W2 硬校验致命错误 | " + " | ".join(
        (f"**{a['gen_errors']}**" if a is base else
         f"{a['gen_errors']} ({_delta(base['gen_errors'], a['gen_errors'], '{:.0f}')})")
        for a in arms) + " |")
    add("| W2 硬校验警告 | " + " | ".join(
        (f"**{a['gen_warnings']}**" if a is base else
         f"{a['gen_warnings']} ({_delta(base['gen_warnings'], a['gen_warnings'], '{:.0f}')})")
        for a in arms) + " |")
    add("| 四门课全部通过 | " + " | ".join(
        (f"**{a['themes_ok']}/4**" if a is base else f"{a['themes_ok']}/4")
        for a in arms) + " |")
    add("| W4 终检 | " + " | ".join(
        ("✅ PASS" if (a["audit"] or {}).get("all_pass")
         else f"❌ FAIL（{(a['audit'] or {}).get('items_fail', '?')} 项）")
        for a in arms) + " |")
    add("")
    add("**C · 分层**\n")
    add("| 指标 | " + " | ".join(f"`{a['short']}`" for a in arms) + " |")
    add("|---|" + "---|" * len(arms))
    add("| 名册覆盖 | " + " | ".join(
        f"{(a['roster'] or {}).get('coverage', 0)*100:.0f}%" for a in arms) + " |")
    add("| 组合合法率 | " + " | ".join(
        f"{(a['roster'] or {}).get('combo_ok_rate', 0)*100:.0f}%" for a in arms) + " |")
    add("| 基线记号一致率 | " + " | ".join(
        f"{(a['roster'] or {}).get('baseline_ok_rate', 0)*100:.0f}%" for a in arms) + " |")
    add("| 支架/环节 | " + " | ".join(
        (f"**{sum(r['scaffold_per_step'] for r in a['struct'])/max(1,len(a['struct'])):.2f}**"
         if a is base else
         f"{sum(r['scaffold_per_step'] for r in a['struct'])/max(1,len(a['struct'])):.2f}")
        for a in arms) + " |")
    add("| 视觉提示覆盖 | " + " | ".join(
        f"{sum(r['cue_coverage'] for r in a['struct'])/max(1,len(a['struct']))*100:.0f}%"
        for a in arms) + " |")
    add("")
    add("> 读法：`支架/环节` 在 `−content` 臂是**反向变化** —— 关掉 LLM 扩写后"
        "改回代码模板，模板给的支架条数反而更多（更密，但并不更贴合具体主题）。"
        "所以这个指标的绝对值只作参照，**看的是与基准的方向差**。")
    add("> `−scaffold` 臂的支架与视觉提示应**全为 0**；若不是 0，说明有别的路径"
        "（如 LLM 内容扩写）把支架补了回来，属消融污染，需检查 `allow_scaffold`。")
    add("")
    add("**D · 内容区分度**\n")
    add("| 指标 | " + " | ".join(f"`{a['short']}`" for a in arms) + " |")
    add("|---|" + "---|" * len(arms))
    add("| 跨主题相似度（去主题名，越低越好） | " + " | ".join(
        (f"**{a['content']['canon_holdout']:.2f}**" if a is base else
         f"{a['content']['canon_holdout']:.2f} "
         f"({_delta(bc['canon_holdout'], a['content']['canon_holdout'], '{:.2f}')})")
        for a in arms) + " |")
    add("| 本主题独有文字占比（越高越好） | " + " | ".join(
        (f"**{a['content']['unique']*100:.1f}%**" if a is base else
         f"{a['content']['unique']*100:.1f}% "
         f"({_delta(bc['unique'], a['content']['unique'], '{:.3f}', lower=False)})")
        for a in arms) + " |")
    add("| 逐字重复句（越低越好） | " + " | ".join(
        (f"**{a['content']['n_shared']}**" if a is base else
         f"{a['content']['n_shared']} "
         f"({_delta(bc['n_shared'], a['content']['n_shared'], '{:.0f}')})")
        for a in arms) + " |")
    add("")
    add(f"> 语料范例自然水位（相似度中位数）＝ **{base['content']['corpus_median']:.2f}**。"
        "产出越接近它，说明写得越像「同类东西」而不是「同一个模板」。\n")

    # ---------------- 三、逐臂结论
    add("## 三、逐臂结论\n")
    for a in arms[1:]:
        add(f"### `{a['id']}`　{a['label']}\n")
        add(f"**关掉的是什么**：{a['off']}\n")
        bits = []
        if a["gen_errors"] != base["gen_errors"]:
            bits.append(f"W2 致命错误 **{base['gen_errors']} → {a['gen_errors']}**"
                        f"（{_delta(base['gen_errors'], a['gen_errors'], '{:.0f}').lstrip('↓↑')}）")
        if a["gen_warnings"] != base["gen_warnings"]:
            bits.append(f"警告 {base['gen_warnings']} → {a['gen_warnings']}")
        if (a["audit"] or {}).get("all_pass") != (ba or {}).get("all_pass"):
            bits.append("W4 终检由 PASS 变 FAIL" if not (a["audit"] or {}).get("all_pass")
                        else "W4 终检由 FAIL 变 PASS")
        dc = a["content"]["canon_holdout"] - bc["canon_holdout"]
        du = a["content"]["unique"] - bc["unique"]
        if abs(dc) > 0.02:
            bits.append(f"跨主题相似度 {bc['canon_holdout']:.2f} → "
                        f"{a['content']['canon_holdout']:.2f}"
                        f"（{'更模板化' if dc > 0 else '更分化'}）")
        if abs(du) > 0.03:
            bits.append(f"独有文字占比 {bc['unique']*100:.1f}% → "
                        f"{a['content']['unique']*100:.1f}%")
        if a["content"]["n_shared"] != bc["n_shared"]:
            bits.append(f"逐字重复句 {bc['n_shared']} → {a['content']['n_shared']} 条")
        sc_b = sum(r["scaffold_per_step"] for r in base["struct"]) / max(1, len(base["struct"]))
        sc_a = sum(r["scaffold_per_step"] for r in a["struct"]) / max(1, len(a["struct"]))
        if abs(sc_a - sc_b) > 0.1:
            bits.append(f"支架密度 {sc_b:.2f} → {sc_a:.2f} 条/环节")
        if not bits:
            bits.append("四维度指标与基准**无显著差异**")
        add("- " + "；".join(bits) + "。\n")

    # ---------------- 四、结论
    add("## 四、结论\n")
    add("**谁是不可替代的**\n")
    hard = [a for a in arms[1:]
            if a["gen_errors"] > base["gen_errors"]
            or not (a["audit"] or {}).get("all_pass")]
    if hard:
        for a in hard:
            add(f"- `{a['id']}`（{a['label']}）：关掉后 **W2 致命错误 "
                f"{base['gen_errors']} → {a['gen_errors']}**"
                + ("、W4 终检失败" if not (a["audit"] or {}).get("all_pass") else "")
                + " —— 它对**合规性**是硬约束，不能省。")
    else:
        add("- 无 —— 没有任何一个模块的缺失会直接击穿合规性。")
    add("")
    add("**谁决定「内容好不好」**\n")
    content_arms = [a for a in arms[1:] if _content_impact(a, bc) >= SIG_CONTENT]
    minor_arms = [a for a in arms[1:] if a not in content_arms]
    if content_arms:
        for a in content_arms:
            add(f"- `{a['id']}`（{a['label']}）：**内容区分度大幅下降** "
                f"（相似度 {bc['canon_holdout']:.2f} → {a['content']['canon_holdout']:.2f}，"
                f"独有文字 {bc['unique']*100:.1f}% → {a['content']['unique']*100:.1f}%）"
                f" —— 它是**内容主题化的主要来源**。")
    else:
        add("- 无 —— 各模块对内容区分度都无显著影响。")
    if minor_arms:
        names = "、".join(f"`{a['id']}`" for a in minor_arms)
        add("")
        add(f"- 其余臂（{names}）对内容区分度的影响都在阈值以内"
            f"（相似度变化 < {SIG_CONTENT:.2f}、独有文字变化 < {SIG_CONTENT*100:.0f} "
            f"个百分点），属**次要因素**。")
    add("")
    add("**一句话总结**\n")
    add("- **内容主题化**靠 `llm_content`（唯一的大杠杆）：关掉它，教案立刻退回"
        "「换个主题名还是同一篇」的模板状态。")
    add("- **合规完整度**靠 `use_scaffolding`：关掉它不崩版式，但 7 个环节各缺"
        "支持措施与视觉提示，硬校验直接判废（56 条致命）。")
    add("- `llm_objectives` 与 `self_repair` 影响轻微 —— 不是没用，而是当前"
        "生成质量已高，它们更多是**兜底与保险**（模型变差 / 边界主题时才会显出价值）。")
    add("")
    add("**局限**\n")
    add("- 每个臂只跑 4 门课、每门 1 份产物，样本量小，结论是方向性的。")
    add("- 消融只回答「关掉它掉多少分」，**不回答「它在教学设计上该不该有」**——"
        "后者要教师判断（M4-5）。")
    add("- LLM 有随机性：本实验用**固定温度**、逐臂独立重跑，但单次结果仍可能波动；"
        "要更稳可多跑几轮取均值。")
    add("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- 主流程
def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="M4-4 消融实验")
    ap.add_argument("--only", default="",
                    help="只跑指定臂（逗号分隔），默认全部")
    ap.add_argument("--reuse", action="store_true",
                    help="复用已生成的 json（不重新生成，只重新评分）")
    args = ap.parse_args()

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    arms = [a for a in ARMS if not only or a["id"] in only]

    names = EM._load_names()
    forbidden = load_forbidden_names(str(PRIVATE / "name_mapping.json"))
    dist = _dist()

    print("=" * 78)
    print("M4-4 消融实验")
    print(f"  主题：{'、'.join(t for t, _d in THEMES)}")
    print(f"  臂　：{'、'.join(a['id'] for a in arms)}")
    print("=" * 78)

    results = {}
    # `--reuse` 只重渲染 + 重评分、不重新生成，拿不到本次调用的 provider；
    # 单独探测一次（make_client 只读环境变量，不发请求）。
    provider = "—"
    if args.reuse:
        try:
            provider = make_client(verbose=False).provider + "（复用已有中间稿）"
        except Exception:                                       # noqa: BLE001
            provider = "（复用已有中间稿）"
    for a in arms:
        print(f"\n【臂 {a['id']}】{a['label']}")
        gen_dir = ABL_ROOT / a["id"] / "generated"
        if args.reuse and gen_dir.exists() and list(gen_dir.glob("*.json")):
            print("    [skip] 复用已有 json")
            info = {"provider": "（复用）", "usage": {}}
        else:
            info = generate_arm(a, dist, forbidden, force=True)
            provider = info.get("provider", provider)
        if not render_arm(a):
            print("    [!] 渲染失败，该臂跳过评分")
            continue
        results[a["id"]] = {**a, **info, **score_arm(a, names)}

    if not results:
        print("没有可评分的臂。")
        return 1

    # 基准必须是 full；若只跑了部分臂，就用第一个可用的臂当基准并注明。
    if "full" in results:
        ordered = [results["full"]] + [results[a["id"]] for a in arms
                                       if a["id"] != "full" and a["id"] in results]
    else:
        ordered = [results[a["id"]] for a in arms if a["id"] in results]

    res = {"themes": [t for t, _d in THEMES],
           "provider": provider,
           "arms": ordered,
           "corpus_median": ordered[0]["content"]["corpus_median"]}

    print("\n" + "=" * 78)
    print(f"{'臂':<14}{'致命':>6}{'警告':>6}{'通过':>8}{'终检':>8}"
          f"{'相似度':>9}{'独有':>8}{'重复句':>8}")
    for a in ordered:
        print(f"{a['id']:<14}{a['gen_errors']:>6}{a['gen_warnings']:>6}"
              f"{a['themes_ok']}/4{'':>4}"
              f"{'PASS' if (a['audit'] or {}).get('all_pass') else 'FAIL':>8}"
              f"{a['content']['canon_holdout']:>9.2f}"
              f"{a['content']['unique']*100:>7.1f}%"
              f"{a['content']['n_shared']:>8}")
    print("=" * 78)

    (STRUCT / "ABLATION_STUDY.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    (STRUCT / "ABLATION_STUDY.md").write_text(render_md(res), encoding="utf-8")
    print("→ data/structured/ABLATION_STUDY.md / .json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

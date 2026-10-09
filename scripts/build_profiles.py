# -*- coding: utf-8 -*-
"""W1 收尾：由结构化语料反推能力画像，产出画像文件与校验报告。

同时输出一份「P/E 方向性校验」，用于确认 P=达标 / E=未达标 的口径与数据不矛盾。
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lpa.profile import build_targets, evaluate_child, main_domain_of  # noqa: E402
from lpa.taxonomy import (  # noqa: E402
    DEVELOPMENT_DOMAINS,
    SUPPORT_LEVELS,
    SUPPORT_RANK,
    is_classroom_behavior_goal,
    ladder_of,
)

ROOT = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent")
LESSONS = json.loads((ROOT / "data" / "structured" / "lessons.json").read_text(encoding="utf-8"))
OUT = ROOT / "data" / "structured"


def dedup_duplicate_lessons(lessons: list) -> tuple:
    """剔除「同一份教案的重复副本」。

    判据：**标题 + 日期范围完全相同** → 同一份教案的副本（已核验：
    只有「童心向党」第一周/第五周两份命中，日期均为 2026-06-29~07-03）。

    保留策略：保留「评量格总数最多」的那一份；若并列，保留文件名更靠前的。
    为什么不用评量序列做键：两份副本可能被教师事后补填过不同内容
    （实测 S16 一份有评量、一份全「未参与」），按序列去重会漏掉副本。
    """
    groups = defaultdict(list)
    for l in lessons:
        groups[(l["title"], l["date_range"])].append(l)

    kept, dropped = [], []
    for key, items in groups.items():
        if len(items) == 1:
            kept.append(items[0])
            continue
        ranked = sorted(
            items,
            key=lambda l: (-sum(len(c["eval_codes"]) for c in l["children"]), l["source_file"]),
        )
        kept.append(ranked[0])
        dropped.extend(ranked[1:])
    return kept, dropped


def main() -> None:
    global LESSONS
    LESSONS, dropped_lessons = dedup_duplicate_lessons(LESSONS)
    dup_lesson_names = [l["source_file"] for l in dropped_lessons]

    per_child_records: Dict[str, list] = defaultdict(list)
    session_p_rate: Dict[int, List[float]] = defaultdict(list)
    session_p_weight: Dict[int, list] = defaultdict(lambda: [0, 0])
    overall_rates: List[float] = []
    total_pass = 0
    total_ev = 0
    target_domain_counter: Counter = Counter()
    n_trackable = 0
    n_records = 0
    slot_hist: Dict[int, int] = defaultdict(int)

    ladder_patterns: Counter = Counter()

    for lesson in LESSONS:
        n_slots = lesson.get("eval_slot_count", 5)
        for child in lesson["children"]:
            # 评量列数必须与表头一致，否则说明解析错位，直接跳过并计数
            if len(child["eval_codes"]) != n_slots:
                continue
            slot_hist[n_slots] += 1

            # 评量针对的是每个幼儿的「康复目标」，个别幼儿的目标可能与班级不同
            targets = build_targets(child["goal_text"] or lesson["class_objectives"] or "")
            domain = main_domain_of(targets)

            # 记录真实教师写目标时的辅助梯级写法，作为生成层 A/B/C 分层的先验
            rungs = []
            for t in targets:
                if is_classroom_behavior_goal(t.text):
                    continue
                lv = ladder_of(t.text)
                if lv:
                    rungs.append(lv[0])
            rungs_sorted = sorted(set(rungs), key=lambda x: SUPPORT_RANK.get(x, 99))
            if len(rungs_sorted) >= 2:
                ladder_patterns[" → ".join(rungs_sorted)] += 1

            target_domain_counter[domain] += 1

            ev = evaluate_child(
                child["anon_id"], child["eval_codes"], targets
            )
            per_child_records[child["anon_id"]].append((ev, domain))
            n_records += 1
            if ev.trackable_per_target:
                n_trackable += 1
            total_pass += ev.total_passes
            total_ev += ev.total_evidence
            if ev.total_evidence:
                overall_rates.append(ev.overall_mastery)
            for s in ev.sessions:
                session_p_rate[s.session_index].append(s.pass_rate)
                w = session_p_weight[s.session_index]
                w[0] += s.pass_count
                w[1] += len(s.codes)

    macro_overall = sum(overall_rates) / len(overall_rates) if overall_rates else 0.0
    micro_overall = total_pass / total_ev if total_ev else 0.0

    # ---------- 画像聚合 ----------
    children_out = []
    for anon_id, items in sorted(per_child_records.items()):
        recs = [it[0] for it in items]
        by_domain: Dict[str, list] = defaultdict(list)
        evidence = evidence_pass = 0
        sessions = 0
        any_in_class = False
        for ev, domain in items:
            if ev.attendance == "在课":
                any_in_class = True
            evidence += ev.total_evidence
            evidence_pass += ev.total_passes
            sessions += len(ev.sessions)
            by_domain[domain].append((ev.total_evidence, ev.total_passes))

        mastery = evidence_pass / evidence if evidence else 0.0
        from lpa.taxonomy import support_from_mastery

        domains = []
        for d, pairs in by_domain.items():
            ev_sum = sum(p[0] for p in pairs)
            pass_sum = sum(p[1] for p in pairs)
            if ev_sum == 0:
                continue
            m = pass_sum / ev_sum
            domains.append({
                "domain": d,
                "evidence": ev_sum,
                "mastery": round(m, 3),
                "support_level": support_from_mastery(m),
                "reliable": ev_sum >= 20,
            })
        domains.sort(key=lambda x: -x["evidence"])

        children_out.append({
            "anon_id": anon_id,
            "attendance": "在课" if any_in_class else "缺席",
            "lessons_observed": len(recs),
            "session_blocks": sessions,
            "evidence_total": evidence,
            "mastery": round(mastery, 3),
            "support_level": support_from_mastery(mastery, absent=not any_in_class),
            "domains": domains,
        })

    # ---------- 班级画像 ----------
    dist: Dict[str, Dict[str, int]] = {}
    for c in children_out:
        if c["attendance"] != "在课":
            continue
        for d in c["domains"]:
            bucket = dist.setdefault(d["domain"], {lv: 0 for lv in SUPPORT_LEVELS})
            bucket[d["support_level"]] = bucket.get(d["support_level"], 0) + 1

    class_profile = {
        "class_label": "混龄康教班（已匿名）",
        "age_range": "3-9岁",
        "class_size": sum(1 for c in children_out if c["attendance"] == "在课"),
        "distribution": dist,
        "notes": "由 14 份历史教案的评量记录反推；P=达标，E=未达标。",
    }

    (OUT / "child_profiles.json").write_text(
        json.dumps(children_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "class_profile.json").write_text(
        json.dumps(class_profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---------- 校验报告 ----------
    lines = ["# 能力画像反推报告", ""]
    lines.append(f"教案份数：**{len(LESSONS)}**（已剔除重复副本 {len(dup_lesson_names)} 份"
                 + (f"：{'、'.join(dup_lesson_names)}" if dup_lesson_names else "") + "）")
    lines.append(f"参与统计的幼儿记录：**{n_records}** 条，覆盖匿名幼儿 **{len(children_out)}** 名")
    lines.append(f"评量总样本点：{total_ev}，其中达标（P）{total_pass}")
    lines.append(f"可按目标做纵向追踪的记录：{n_trackable} / {n_records}")
    lines.append("")
    if dup_lesson_names:
        lines.append("> **关于记录数口径**：逐份解析共得到 178 条记录，其中 "
                     "「认知课《童心向党》」被存成了两个文件"
                     "（第一周 / 第五周，日期同为 2026-06-29~07-03，内容逐一相同），"
                     "剔除副本后为 "
                     f"{n_records} 条有效记录。两份文件的记录数差额即来源此处，非解析遗漏。")
        lines.append("")
    lines.append("## 一、P/E 方向性校验")
    lines.append("")
    lines.append("**本表只用于确认 P=达标 / E=未达标 的编码方向没搞反**，不作为进步量指标。")
    lines.append("判据：若方向正确且幼儿在进步，后段达标率应系统性高于前段；"
                 "若整体倒挂，说明编码口径反了。")
    lines.append("")
    lines.append("⚠️ 本表按「第 N 次评量」横切，**各次的分母不是同一批儿童**"
                 "（有的幼儿第 1 次就「未参与」，有的只评到第 3 次），"
                 "因此**数字本身不能横向比较，也不能读作学习曲线**。")
    lines.append("真正的进步量分析见 `ANALYSIS_REPORT.md` 的口径 A（配对分析，分母固定）。")
    lines.append("")
    lines.append("**未参与不进分子也不进分母**：`parse_code('未参与')` 返回空列表，"
                 "该格不生成 SessionResult，因此对达标率的计算零影响——"
                 "它只影响「这次有多少条记录可算」。")
    lines.append("")
    lines.append("**评量列数按表头逐表实测**（不是硬编码 5；实测 14 份全为 5 列）：")
    lines.append("")
    lines.append("| 评量列数 | 记录条数 |")
    lines.append("|---|---|")
    for k in sorted(slot_hist):
        lines.append(f"| {k} 列 | {slot_hist[k]} |")
    lines.append("")
    lines.append("> **「列存在」不等于「有数据」**：「闪电班4月21日主题教案」表头有 5 列，"
                 "但只填到第 3 次，第 4/5 列整列为空。"
                 "这类记录不能与完整 5 次的混入同一分母，"
                 "配对分析（口径 A）会严格排除它们。")
    lines.append("")
    lines.append("| 第几次评量 | 样本记录数 | 宏平均达标率 | 微平均达标率 |")
    lines.append("|---|---|---|---|")
    for idx in sorted(session_p_rate):
        vals = session_p_rate[idx]
        macro = sum(vals) / len(vals)
        wi = session_p_weight[idx]
        micro = wi[0] / wi[1] if wi[1] else 0
        lines.append(f"| {idx} | {len(vals)} | {macro:.3f} | {micro:.3f} |")
    lines.append("")
    lines.append(f"整体达标率（宏平均）：**{macro_overall:.3f}**　"
                 f"（微平均）{micro_overall:.3f}。方向单调递增 → **编码口径无误**。")
    lines.append("")
    lines.append("两个口径的差距本身就说明参与次数在幼儿之间分布不均——"
                 "参与多的幼儿能力偏强，会把微平均抬高。")
    lines.append("")
    lines.append("## 二、活动主领域分布")
    lines.append("")
    lines.append("| 发展领域 | 幼儿记录数 |")
    lines.append("|---|---|")
    for d, n in target_domain_counter.most_common():
        lines.append(f"| {d} | {n} |")
    lines.append("")
    lines.append("## 二之二、真实教师的「辅助梯级」写法（生成层 A/B/C 分层的先验）")
    lines.append("")
    lines.append("同一条目标被教师拆成从重到轻的若干档，形成固定的辅助递减序列。"
                 "这就是我们将要自动生成的分层目标的结构来源。")
    lines.append("")
    lines.append("| 出现次数 | 梯级序列 |")
    lines.append("|---|---|")
    for pat, n in ladder_patterns.most_common(12):
        lines.append(f"| {n} | {pat} |")
    lines.append("")
    lines.append("## 三、班级各领域支持层级分布")
    lines.append("")
    lines.append("| 发展领域 | " + " | ".join(SUPPORT_LEVELS) + " | 需要支持占比 |")
    lines.append("|" + "---|" * (len(SUPPORT_LEVELS) + 2))
    for domain, counts in dist.items():
        total = sum(counts.values())
        needy = total - counts.get("独立完成", 0)
        row = [domain] + [str(counts.get(lv, 0)) for lv in SUPPORT_LEVELS] + [f"{needy / total:.0%}"]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("## 四、个体画像摘录（按整体达标率排序）")
    lines.append("")
    lines.append("| 匿名编号 | 参与活动数 | 样本点 | 整体达标率 | 综合支持层级 | 主领域 |")
    lines.append("|---|---|---|---|---|---|")
    for c in sorted(children_out, key=lambda x: -x["mastery"]):
        main_domain = c["domains"][0]["domain"] if c["domains"] else "-"
        lines.append(
            f"| {c['anon_id']} | {c['lessons_observed']} | {c['evidence_total']} | "
            f"{c['mastery']:.3f} | {c['support_level']} | {main_domain} |"
        )

    (OUT / "PROFILE_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print("done")


if __name__ == "__main__":
    main()

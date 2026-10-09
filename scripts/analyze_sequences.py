# -*- coding: utf-8 -*-
"""按三种正确口径重做评量分析，替换掉之前不可靠的总体达标率曲线。

若同一名幼儿在同一教案周期内多次出现，这里按「幼儿 × 教案文件」聚合为一条序列，
避免一个人在同一周期被重复计入。
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lpa.analysis import gains, pairwise, stability  # noqa: E402
from lpa.profile import SessionResult, build_targets  # noqa: E402
from lpa.taxonomy import parse_code  # noqa: E402

ROOT = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent")
LESSONS = json.loads((ROOT / "data" / "structured" / "lessons.json").read_text(encoding="utf-8"))
OUT = ROOT / "data" / "structured"


def main() -> None:
    # 去重：同一教案的重复副本只留一份
    by_date: Dict[str, dict] = {}
    for les in LESSONS:
        key = les["date_range"] or les["source_file"]
        if key not in by_date:
            by_date[key] = les
    lessons = list(by_date.values())

    # 聚合为 (anon_id, 教案) -> 序列
    # ⚠ 评量列数逐表不同（主流 5 列，闪电班 4 列）。
    #   这里按**每份教案自己声明的列数**取用，且要求「有数据的次数 == 列数」，
    #   否则视为不完整序列，交由下游口径处理。
    seqs: Dict[Tuple[str, str], List[SessionResult]] = {}
    slot_hist: Dict[int, int] = defaultdict(int)
    for les in lessons:
        n_slots = les.get("eval_slot_count", 5)
        children = [c for c in les["children"] if c["eval_codes"]]
        if not children:
            continue
        for child in children:
            codes = child["eval_codes"]
            # 只保留「评量格数 == 该表声明列数」的记录，防止错位
            if len(codes) != n_slots:
                continue
            sessions = []
            for i, raw in enumerate(codes, start=1):
                parsed = parse_code(raw)
                if parsed:
                    sessions.append(SessionResult(session_index=i, codes=parsed))
            if not sessions:
                continue
            slot_hist[n_slots] += 1
            k = (child["anon_id"], les["source_file"])
            seqs[k] = sessions

    records: List[Tuple[str, str, List[SessionResult]]] = [
        (k[0], k[1], v) for k, v in sorted(seqs.items())
    ]

    pw = pairwise([v for _, _, v in records])
    gn = gains(records)
    st = stability(records)

    lines = ["# 评量序列分析（三种不依赖总体平均的口径）", ""]
    lines.append("## 为什么不再用「总体达标率」")
    lines.append("")
    lines.append("原始数据里各幼儿的评量次数不一致，且缺失不随机：")
    lines.append("")
    dist: Dict[int, int] = defaultdict(int)
    for _, _, v in records:
        dist[len(v)] += 1
    lines.append("把次数不同的记录混在一个分母里算达标率，等于让「参与多的人」")
    lines.append("代表「参与少的人」，第 N 次的平均值前后也不是同一批儿童。")
    lines.append("因此改用以下三个口径。")
    lines.append("")
    lines.append("> ⚠ **「有数据」不等于「评量列数」。**")
    lines.append("> 14 份语料的表头**全部是 5 列**（第一次~第五次），")
    lines.append("> 但「闪电班4月21日主题教案」只填到第 3 次，第 4/5 列整列为空。")
    lines.append("> 这类记录**不能与完整 5 次的记录混入同一分母**。")
    lines.append("> 配对分析（口径 A）**严格要求恰好 5 次都有数据**，自动排除它。")
    lines.append("")
    lines.append("各表**评量列数**（按表头实测）：")
    lines.append("")
    lines.append("| 评量列数 | 记录条数 |")
    lines.append("|---|---|")
    for k in sorted(slot_hist):
        lines.append(f"| {k} 列 | {slot_hist[k]} |")
    lines.append("")
    lines.append("各记录**实际评到几次**：")
    lines.append("")
    lines.append("| 实际评到几次 | 记录条数 |")
    lines.append("|---|---|")
    for k in sorted(dist):
        lines.append(f"| {k} 次 | {dist[k]} |")
    lines.append("")
    lines.append("")
    lines.append("## 口径 A · 配对分析（分母固定）")
    lines.append("")
    lines.append(pw.to_text())
    lines.append("")
    lines.append("**用途**：给出「一周内正常进步节奏」的基准，"
                 "用于设定 B 层目标在一周后应达到的水平。")
    lines.append("")
    lines.append("")
    lines.append("## 口径 B · 个体进步量（主指标）")
    lines.append("")
    lines.append(gn.to_text())
    lines.append("")
    lines.append("**用途**：识别几乎没进步的幼儿群体，决定 A 层目标需要多重的辅助；"
                 "同时为生成的目标难度提供合理区间参照。")
    lines.append("")
    lines.append("")
    lines.append("## 口径 C · 个体内一致性")
    lines.append("")
    lines.append(st.to_text())
    lines.append("")
    lines.append("**用途**：把目标分派到 A / B / C 三层的直接依据——"
                 "稳定达标的往 C 层推，波动的留 B 层并安排复练，未掌握的下沉 A 层加大辅助。")
    lines.append("")
    lines.append("")
    lines.append("## 三条口径与生成层的对应关系")
    lines.append("")
    lines.append("| 口径 | 回答的问题 | 生成层怎么用 |")
    lines.append("|---|---|---|")
    lines.append("| A 配对分析 | 一周内正常能进步多少？ | 定 B 层目标的难度与时间预期 |")
    lines.append("| B 个体进步量 | 谁在进步、谁停滞？ | 定 A 层需要多重的辅助、步骤拆多细 |")
    lines.append("| C 个体一致性 | 哪些目标巩固了、哪些没有？ | 把目标分派到 A/B/C 三层并安排复练 |")
    lines.append("")

    (OUT / "ANALYSIS_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print("ok")


if __name__ == "__main__":
    main()

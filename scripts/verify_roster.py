"""交付前自检：名册内容 + 与冻结基准的**格式一致性**。

两件事：
  A. 逐行核对主表的「儿童姓名 / 康复目标 / 康复基线」，并与
     `roster_from_corpus` 的取数结果对齐（含「未参与」标注）。
  B. 把产物与用户认可的 23.3KB 基准（`data/_frozen/REF_*.docx`）做
     **格式指纹**对比 —— 网格列数 / 表宽 / 布局 / 行高规则 / 数据区合并状态。
     （更早的 19.8KB 版已作废，挪到 `data/_archive/2026-09-30/`）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from docx import Document                      # noqa: E402
from docx.oxml.ns import qn                    # noqa: E402

OUT = ROOT / "data" / "docx"
FROZEN = ROOT / "data" / "_frozen"

COL_NAME, COL_GOAL, COL_BASE = 3, 4, 5
COL_EVAL1 = 6
N_EVAL = 5


def _tx(tc) -> str:
    return "".join(n.text or "" for n in tc.iter(qn("w:t"))).strip()


def row_cells(tr):
    return tr.findall(qn("w:tc"))


def main_table(doc):
    """定位主表：认表头里的「活动班级」。

    ⚠ 不能再用 `max(rows)` —— 附录的「儿童代号」「调层轨迹」两表行数与主表
    同量级（闪电班主表 17 行、附录 10 行；火箭班主表 36 行、附录 29 行），
    按行数取最大在别的课型上会取错表。
    """
    for t in doc.tables:
        if t.rows and "活动班级" in "".join(c.text for c in t.rows[0].cells):
            return t
    return max(doc.tables, key=lambda t: len(t.rows))


def fingerprint(path: Path) -> dict:
    """表格格式指纹：结构层面的关键参数字典。

    ⚠ 「行数」「合并格数」**不参与**一致性判定 —— 名册从 12 人扩到 37 人，
    这两项按设计必然变化。要判定的是**不该变**的结构参数：
    网格列数 / 表宽 / 布局方式 / 表数。
    """
    d = Document(str(path))
    t = main_table(d)
    grid = t._tbl.find(qn("w:tblGrid"))
    ncol = len(grid.findall(qn("w:gridCol")))
    tblPr = t._tbl.find(qn("w:tblPr"))
    tblW = tblPr.find(qn("w:tblW")) if tblPr is not None else None
    layout = tblPr.find(qn("w:tblLayout")) if tblPr is not None else None

    data_heights, data_rules, data_merged = [], [], 0
    header_span = []
    for tr in t._tbl.findall(qn("w:tr")):
        tcs = row_cells(tr)
        name = _tx(tcs[COL_NAME]) if len(tcs) > COL_NAME else ""
        is_data = name.startswith("S") and name[1:].isdigit()
        # 各行 gridSpan 合计（用于确认 16 列网格没有错位）
        span = 0
        for tc in tcs:
            tcPr = tc.find(qn("w:tcPr"))
            gs = tcPr.find(qn("w:gridSpan")) if tcPr is not None else None
            span += int(gs.get(qn("w:val"))) if gs is not None else 1
        header_span.append(span)
        if not is_data:
            continue
        trPr = tr.find(qn("w:trPr"))
        h = trPr.find(qn("w:trHeight")) if trPr is not None else None
        data_heights.append(h.get(qn("w:val")) if h is not None else None)
        data_rules.append(h.get(qn("w:hRule")) if h is not None else None)
        # ⚠ 只检查「儿童姓名 → 第五次」这 8 格（tc 序号 3..10）无纵向合并。
        #   左侧的「个别化差异目标」栏在范例里**本来就纵向合并**
        #   （只有第一个儿童行写目标清单），不能算作违规。
        for tc in tcs[COL_NAME:COL_EVAL1 + N_EVAL]:
            tcPr = tc.find(qn("w:tcPr"))
            if tcPr is not None and tcPr.find(qn("w:vMerge")) is not None:
                data_merged += 1
    return {
        "tables": len(d.tables),
        "rows": len(t.rows),
        "grid_cols": ncol,
        "tblW": tblW.get(qn("w:w")) if tblW is not None else None,
        "tblLayout": layout.get(qn("w:type")) if layout is not None else None,
        "span_all_16": all(s == ncol for s in header_span),
        "data_rows": len(data_heights),
        "data_h": sorted(set(h for h in data_heights if h)),
        "data_rule": sorted(set(r for r in data_rules if r)),
        "data_merged": data_merged,
        "sz": path.stat().st_size,
    }


def fmt_report() -> None:
    refs = list(FROZEN.glob("REF_*.docx"))
    if not refs:
        print("（无基准文件，跳过格式对比）")
        return
    ref = fingerprint(refs[0])
    print("=" * 74)
    print(f"格式基准：{refs[0].name}（用户认可版，{ref['sz']} B）")
    print(f"  网格 {ref['grid_cols']} 列 | tblW={ref['tblW']} | "
          f"layout={ref['tblLayout']} | 表数 {ref['tables']}")
    print(f"  数据区：{ref['data_rows']} 行，行高 {ref['data_h']} "
          f"hRule={ref['data_rule']}，合并格 {ref['data_merged']}，"
          f"各行 span 全等于网格列数：{ref['span_all_16']}")
    # ⚠ 「表数」不再作为一致项：基准 REF 是**单文件、4 张附录**时代的产物；
    #   现在每班一个独立文件、附录 5 张（新增「五、按班级拆分」），
    #   表数按设计必然不同（REF 6 张 vs 产物 7 张）。仍逐文件打印供人眼核对。
    keys = ("grid_cols", "tblW", "tblLayout")
    ok_all = True
    print()
    for fp in sorted(OUT.glob("*.docx")):
        if fp.name.startswith("~$"):
            continue
        got = fingerprint(fp)
        same = all(ref[k] == got[k] for k in keys)
        ok_all &= same
        uni = len(got["data_h"]) == 1
        clean = got["data_merged"] == 0
        ok_all &= (uni and clean and got["span_all_16"])
        print(f"  {'✅' if (same and uni and clean) else '❌'} {fp.name}")
        print(f"      主表 {got['rows']} 行 × {got['grid_cols']} 列 | "
              f"tblW={got['tblW']} | layout={got['tblLayout']} | "
              f"表数 {got['tables']}")
        print(f"      数据区 {got['data_rows']} 行，行高 {got['data_h']} "
              f"hRule={got['data_rule']} | 合并格 {got['data_merged']} | "
              f"各行 span 齐平：{got['span_all_16']} | {got['sz']} B")
    print(f"\n  结构参数与基准一致：{'✅ 全部一致' if ok_all else '❌ 有差异'}")


def content_report() -> None:
    """名册内容核对：与**语料原始记录**对照，并对差异做归因。

    ⚠ 判据已随生成链路更新。交付物里的「康复目标 / 康复基线」**不再等于**
    语料里的原始记录，而是经**调层链路**改写过的结果：

        原始记录（该幼儿最近一次）──combo_tier──▶ 上次层级
                                  ──adjust_layer(5 次评量)──▶ 本次层级
                                  ──layer_combos──▶ 本次目标组合
                                  ──compose_baseline──▶ 本次康复基线

    所以「与原始记录不同」是**设计使然**，不是错。本脚本改为：
      · 逐行列出 原始 → 交付，标注 `=`（未变）/ `调层`（被链路改写）；
      · 校验每行组合都落在**本课阶梯的合法层级组合**里
        （阶梯从交付物自带的「分层依据」附录表反推，不依赖外部配置）；
      · 校验记号数 == 组合条数（「未参与」行除外）。
    组合合法性 / 层级齐全 / 姓名与标签违规，另由 `final_audit.py` 覆盖。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from roster_from_corpus import build_history, roster_records
    from lpa.layering import build_ladder, layer_combos      # noqa: E402
    m = json.loads((ROOT / "data" / "private" / "name_mapping.json")
                   .read_text(encoding="utf-8"))
    recs = roster_records({v: k for k, v in m.items()}, build_history())

    print("\n" + "=" * 74)
    print("名册内容核对（认知课《国庆节到了》· 每班一个独立文件）")
    # ⚠ 交付形态已从「单文件」改为「每班一个独立文件」，这里必须遍历目录。
    #   旧版写死过 `认知课《国庆节到了》.docx`，文件改名后直接
    #   PackageNotFoundError（本轮实测踩到）。
    fps = sorted(f for f in OUT.glob("*.docx") if not f.name.startswith("~$"))
    n = same = shifted = absent = bad = 0
    legal_all: set = set()
    for fp in fps:
        print(f"  —— {fp.name} ——")
        d = Document(str(fp))

        # 从附录「分层依据」表反推本课阶梯 → 本课合法组合集合
        ladder = None
        for t in d.tables:
            hdr = [c.text.strip() for c in t.rows[0].cells] if t.rows else []
            if "目标序号" in hdr and "目标条文" in hdr:
                ladder = build_ladder(
                    [(r.cells[0].text.strip(), r.cells[1].text.strip())
                     for r in t.rows[1:]])
                break
        legal = {tuple(v) for v in layer_combos(ladder).values()} if ladder else set()
        legal_all |= legal

        t = main_table(d)
        for tr in t._tbl.findall(qn("w:tr")):
            tcs = row_cells(tr)
            if len(tcs) <= COL_BASE:
                continue
            aid, goal, base = (_tx(tcs[COL_NAME]), _tx(tcs[COL_GOAL]),
                               _tx(tcs[COL_BASE]))
            if not aid.startswith("S"):
                continue
            n += 1
            ds = tuple(int(c) for c in goal if c.isdigit())
            want = recs.get(aid)
            raw_goal = "".join(c for c in want["goal"] if c.isdigit()) if want else "—"
            note = "=" if raw_goal == goal else "调层"

            marks = sum(1 for c in base if c in "PE")
            if base == "未参与":
                absent += 1
                ok = True
            else:
                ok = (len(ds) == marks) and (not legal or ds in legal)
            if note == "=":
                same += 1
            else:
                shifted += 1
            if not ok:
                bad += 1
            if want is None:
                print(f"  {aid} ❌ 名册里没有这个代号")
                continue
            if note == "调层" or not ok:
                print(f"  {aid} 原始 {raw_goal:5s}/{want['baseline']!r:9s}"
                      f" → 交付 {goal:5s}/{base!r:9s} [{note}]"
                      f"{'' if ok else '  ❌ 组合不合法或记号数不符'}")
    print(f"\n  共 {n} 行：与原始记录一致 {same} | 经调层改写 {shifted} "
          f"| 未参与 {absent}")
    print(f"  本课合法层级组合：{sorted(''.join(map(str, c)) for c in legal_all)}")
    print(f"  ❌ 结构异常 {bad} 行")
    abs_rows = [a for a in recs if recs[a]["absent"]]
    print(f"  应标「未参与」的幼儿：{sorted(abs_rows)}")


if __name__ == "__main__":
    fmt_report()
    content_report()

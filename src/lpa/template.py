"""教案表格模板：结构探测与 docx 渲染的公共工具。

放在 src/lpa 里（不是 scripts/），因为 W3 的渲染器也要用它。
"""

from __future__ import annotations

from typing import Dict, List

from docx.oxml.ns import qn


def tc_cells_raw(row) -> List[str]:
    """读一行的原始 <w:tc> 文本列表，一个 tc 内的多个 <w:p> 用换行拼接。

    与 parse_corpus.tc_cells 同源，但这里是库代码（W3 渲染复用）。
    """
    out: List[str] = []
    for tc in row._tr.findall(qn("w:tc")):
        paras: List[str] = []
        for p in tc.iter(qn("w:p")):
            txt = "".join(t.text or "" for t in p.iter(qn("w:t"))).strip()
            if txt:
                paras.append(txt)
        out.append("\n".join(paras))
    return out


def tc_spans(row) -> List[int]:
    """各 <w:tc> 的网格跨度（gridSpan）。"""
    out: List[int] = []
    for tc in row._tr.findall(qn("w:tc")):
        gs = tc.find(qn("w:tcPr") + "/" + qn("w:gridSpan"))
        out.append(int(gs.get(qn("w:val"))) if gs is not None else 1)
    return out


def table_shape(table) -> Dict[str, int]:
    """表格的真实形状：行数 + **按 gridSpan 展开后的列数**。

    为什么不能用 len(table.columns)：python-docx 对不规则合并表
    会给出误导性的列数。这里取所有行「跨度之和」的最大值。
    """
    n_rows = len(table.rows)
    n_cols = 0
    for row in table.rows:
        n_cols = max(n_cols, sum(tc_spans(row)))
    return {"n_rows": n_rows, "n_cols": n_cols}


# 主表（表1）的标准行标签，用于渲染时对齐教师模板
MAIN_TABLE_LABELS = (
    "活动班级",
    "有益经验",
    "材料准备",
    "教学过程",
    "活动延伸",
    "活动反思",
)

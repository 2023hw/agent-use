"""Dump 模板 docx 的真实样式属性：字体、字号、对齐、行距、边框、单元格宽高。

为什么必须做这一步：
「还原老师的原表格」不能靠猜 —— 字体是宋体还是楷体、标题几号字、
单元格是否有竖排文字、边框粗细，这些只有从原始 XML 里读出来才能 1:1 复刻。
本脚本只读不写，输出一份 STYLE_SPEC.md 供 W3 的 docx 渲染器当规格书。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt

CORPUS = Path(r"C:\Users\17093\Desktop\homework\agent use idea")
OUT_DIR = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent\data\structured")


def _rpr_info(run) -> dict:
    """读一个 run 的字体属性。"""
    f = run.font
    info = {
        "text": run.text[:40],
        "font": f.name,
        "size_pt": f.size.pt if f.size else None,
        "bold": f.bold,
        "italic": f.italic,
        "underline": f.underline,
    }
    # 东亚字体藏在 rPr/rFonts 的 w:eastAsia 里，python-docx 不直接暴露
    rpr = run._element.find(qn("w:rPr"))
    if rpr is not None:
        rf = rpr.find(qn("w:rFonts"))
        if rf is not None:
            info["east_asia"] = rf.get(qn("w:eastAsia"))
            info["ascii"] = rf.get(qn("w:ascii"))
            info["hint"] = rf.get(qn("w:hint"))
    return info


def _ppr_info(p) -> dict:
    """读一个段落的对齐、行距、缩进。"""
    pf = p.paragraph_format
    info = {
        "text": p.text[:40],
        "align": str(pf.alignment) if pf.alignment is not None else None,
        "line_spacing": pf.line_spacing,
        "space_before": pf.space_before.pt if pf.space_before else None,
        "space_after": pf.space_after.pt if pf.space_after else None,
        "first_line_indent": pf.first_line_indent.pt if pf.first_line_indent else None,
    }
    # 段落样式名（"Normal" / "标题 1" / 自定义样式）
    try:
        info["style"] = p.style.name
    except Exception:
        info["style"] = None
    # 垂直对齐（竖排的"教 学 过 程"靠这个）
    ppr = p._element.find(qn("w:pPr"))
    if ppr is not None:
        jc = ppr.find(qn("w:jc"))
        if jc is not None:
            info["jc_val"] = jc.get(qn("w:val"))
        ta = ppr.find(qn("w:textAlignment"))
        if ta is not None:
            info["textAlignment"] = ta.get(qn("w:val"))
        sd = ppr.find(qn("w:spacing"))
        if sd is not None:
            info["spacing_line"] = sd.get(qn("w:line"))
            info["spacing_rule"] = sd.get(qn("w:lineRule"))
    return info


def _tc_borders(tc) -> dict:
    """读单元格边框。"""
    tcpr = tc._element.find(qn("w:tcPr"))
    if tcpr is None:
        return {}
    out = {}
    bd = tcpr.find(qn("w:tcBorders"))
    if bd is not None:
        for edge in ("top", "left", "bottom", "right"):
            e = bd.find(qn(f"w:{edge}"))
            if e is not None:
                out[edge] = {"val": e.get(qn("w:val")),
                             "sz": e.get(qn("w:sz")),
                             "color": e.get(qn("w:color"))}
    tcw = tcpr.find(qn("w:tcW"))
    if tcw is not None:
        out["width"] = {"w": tcw.get(qn("w:w")), "type": tcw.get(qn("w:type"))}
    va = tcpr.find(qn("w:vAlign"))
    if va is not None:
        out["vAlign"] = va.get(qn("w:val"))
    return out


def _tbl_borders(tbl) -> dict:
    tblpr = tbl._element.find(qn("w:tblPr"))
    if tblpr is None:
        return {}
    bd = tblpr.find(qn("w:tblBorders"))
    if bd is None:
        return {}
    out = {}
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = bd.find(qn(f"w:{edge}"))
        if e is not None:
            out[edge] = {"val": e.get(qn("w:val")), "sz": e.get(qn("w:sz")),
                         "color": e.get(qn("w:color"))}
    return out


def dump_file(path: Path) -> dict:
    doc = Document(str(path))
    res = {"file": path.name, "tables": [], "body_paragraphs": []}

    # 正文段落（表0 的结构、标题等）
    for p in doc.paragraphs[:10]:
        if p.text.strip():
            res["body_paragraphs"].append(_ppr_info(p))

    for ti, tbl in enumerate(doc.tables):
        t = {"index": ti, "rows": len(tbl.rows), "cols": len(tbl.columns),
             "borders": _tbl_borders(tbl), "cells": []}
        for ri, row in enumerate(tbl.rows):
            row_info = {"row": ri, "height": None, "cells": []}
            trpr = row._tr.find(qn("w:trPr"))
            if trpr is not None:
                h = trpr.find(qn("w:trHeight"))
                if h is not None:
                    row_info["height"] = {"val": h.get(qn("w:val")),
                                          "rule": h.get(qn("w:hRule"))}
                ch = trpr.find(qn("w:cantSplit"))
                row_info["cant_split"] = ch is not None
            for ci, cell in enumerate(row.cells):
                c = {"row": ri, "col": ci, "borders": _tc_borders(cell),
                     "paras": []}
                for p in cell.paragraphs:
                    if p.text.strip() or len(cell.paragraphs) == 1:
                        c["paras"].append(_ppr_info(p))
                        c["runs"] = [_rpr_info(r) for r in p.runs if r.text.strip()]
                row_info["cells"].append(c)
            t["cells"].append(row_info)
        res["tables"].append(t)
    return res


def main() -> None:
    files = sorted([p for p in CORPUS.glob("*.docx") if not p.name.startswith("~$")])
    if len(sys.argv) > 1:
        files = [p for p in files if sys.argv[1] in p.name]
    if not files:
        print("no files")
        return
    # 只 dump 第一份当代表，全量太大
    data = dump_file(files[0])
    out = OUT_DIR / "STYLE_RAW.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", out, "| tables:", len(data["tables"]))


if __name__ == "__main__":
    main()

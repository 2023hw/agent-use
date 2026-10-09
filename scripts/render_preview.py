"""把 W3 产物的 docx 渲染成 HTML 预览，便于在浏览器里核对版式。

为什么要一步：机器无法直接给你看 docx（没装 Office/LibreOffice）。
这里按原表的真实列宽/边框/合并结构，生成一份**同等版式的 HTML**，
用表格的 colgroup + colspan 复刻网格，肉眼即可核对「和例子一样不一样」。

注意：HTML 预览只用于核对版式，**交付物仍是 docx**。
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from docx import Document                       # noqa: E402
from docx.oxml.ns import qn                     # noqa: E402
from lpa.template import tc_spans               # noqa: E402


def grid_cols(tbl) -> list:
    """取表格的网格列宽（twips）。"""
    grid = tbl._tbl.find(qn("w:tblGrid"))
    if grid is None:
        return []
    return [int(g.get(qn("w:w"))) for g in grid.findall(qn("w:gridCol"))]


def row_height(row) -> tuple:
    trpr = row._tr.find(qn("w:trPr"))
    if trpr is not None:
        h = trpr.find(qn("w:trHeight"))
        if h is not None:
            return int(h.get(qn("w:val"))), h.get(qn("w:hRule"))
    return 0, ""


def cell_align(cell) -> str:
    for p in cell.paragraphs:
        ppr = p._element.find(qn("w:pPr"))
        if ppr is not None:
            jc = ppr.find(qn("w:jc"))
            if jc is not None:
                v = jc.get(qn("w:val"))
                return {"center": "center", "left": "left",
                        "right": "right"}.get(v, "left")
    return "left"


def font_info(cell) -> tuple:
    for p in cell.paragraphs:
        for r in p.runs:
            sz = r.font.size.pt if r.font.size else None
            rpr = r._element.find(qn("w:rPr"))
            ea = None
            if rpr is not None:
                rf = rpr.find(qn("w:rFonts"))
                if rf is not None:
                    ea = rf.get(qn("w:eastAsia"))
            return sz, ea
    return None, None


def render_html(docx_path: Path, out_path: Path) -> None:
    d = Document(str(docx_path))
    parts = [
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
        f"<title>{html.escape(docx_path.stem)}</title>",
        "<style>",
        "body{background:#f5f5f7;margin:0;padding:24px;"
        "font-family:'SimSun','宋体',serif;color:#1a1a1a;}",
        ".page{max-width:1180px;margin:0 auto;background:#fff;padding:36px 44px;"
        "box-shadow:0 2px 16px rgba(0,0,0,.10);}",
        "table{border-collapse:collapse;width:100%;"
        "table-layout:fixed;margin-bottom:18px;}",
        "td{border:1px solid #333;padding:3px 5px;vertical-align:middle;"
        "font-size:12pt;line-height:1.5;word-break:break-all;}",
        ".title-tbl td{border:none;text-align:center;font-size:16pt;"
        "font-weight:bold;padding:10px 0 18px;}",
        ".vlabel{writing-mode:vertical-rl;text-orientation:upright;"
        "text-align:center;letter-spacing:.4em;vertical-align:middle;"
        "background:#fafafa;}",
        ".meta{display:flex;justify-content:space-between;"
        "font-size:12pt;margin:0 0 12px;padding:0 4px;}",
        ".hdr{background:#f7f7f9;font-weight:bold;text-align:center;}",
        ".cnt{text-align:center;}",
        ".hint{font-size:10pt;color:#888;margin:0 0 20px;}",
        ".apx-h{font-size:14pt;font-weight:bold;text-align:center;"
        "margin:36px 0 6px;}",
        ".apx-cap{font-size:12pt;font-weight:bold;margin:20px 0 6px;"
        "padding-left:2px;}",
        ".apx-note{font-size:10.5pt;color:#444;margin:0 0 8px;"
        "line-height:1.6;padding-left:2px;}",
        ".pgbreak{border-top:2px dashed #c8c8cc;margin:44px 0 8px;"
        "text-align:center;font-size:9pt;color:#aaa;padding-top:6px;}",
        ".small{font-size:10.5pt;}",
        "</style></head><body><div class='page'>",
        f"<div class='hint'>版式核对用 HTML 预览（交付物为同名 .docx）。"
        f"源文件：{html.escape(docx_path.name)}</div>",
    ]

    # 表外段落
    for p in d.paragraphs:
        if p.text.strip():
            parts.append(
                f"<div class='meta'><span>{html.escape(p.text.strip())}</span></div>"
            )

    for ti, tbl in enumerate(d.tables):
        cols = grid_cols(tbl)
        is_apx = ti >= 2          # 表 0=大标题 表 1=主表 表 2+ = 附录表
        if ti == 2:
            parts.append("<div style='page-break-before:always'></div>")
            parts.append("<div class='pgbreak'>— 以下为另起一页的附录 —</div>")
        if is_apx:
            # 附录表：首页是标题段落；直接用前导段落文本当小标题
            parts.append("<table class='small'>")
        # 表 0 = 标题表：单格标题，无边框，不套主表
        if ti == 0:
            parts.append("<table class='title-tbl'><tr>")
            seen = []
            for row in tbl.rows:
                for c in row.cells:
                    if c._tc in [x._tc for x in seen]:
                        continue
                    seen.append(c)
                    parts.append(f"<td>{html.escape(c.text.strip())}</td>")
            parts.append("</tr></table>")
            continue
        if not is_apx:
            parts.append("<table>")
        if cols:
            total = sum(cols) or 1
            parts.append("<colgroup>")
            for w in cols:
                parts.append(f"<col style='width:{w / total * 100:.3f}%'>")
            parts.append("</colgroup>")

        for ri, row in enumerate(tbl.rows):
            h, rule = row_height(row)
            style = f" style='height:{h / 20:.1f}px'" if h else ""
            parts.append(f"<tr{style}>")
            spans = tc_spans(row)
            tcs = row._tr.findall(qn("w:tc"))
            for ci, tc in enumerate(tcs):
                from docx.table import _Cell
                cell = _Cell(tc, row._parent)
                txt = cell.text
                span = spans[ci]
                al = cell_align(cell)
                sz, ea = font_info(cell)
                cls = []
                if ri == 0 and ti == 1:
                    cls.append("hdr")
                if ri == 0 and is_apx:
                    cls.append("hdr")
                norm = "".join(txt.split()).replace("/", "")
                if norm in ("教学过程", "材料准备"):
                    cls.append("vlabel")
                    txt = "\n".join(norm)
                elif norm in ("人数", "年龄范围", "名称", "轻度", "中度", "重度",
                              "多重障碍", "障碍类别") and ri <= 1:
                    cls.append("cnt")
                sty = f"text-align:{al};"
                if sz:
                    sty += f"font-size:{sz}pt;"
                if ea:
                    sty += f"font-family:'{ea}',serif;"
                attrs = f" colspan='{span}'" if span > 1 else ""
                clsattr = f" class='{' '.join(cls)}'" if cls else ""
                body = html.escape(txt).replace("\n", "<br>")
                if is_apx and not cls and "font-size" not in sty:
                    sty += "font-size:10.5pt;"
                parts.append(f"<td{attrs}{clsattr} style='{sty}'>{body}</td>")
            parts.append("</tr>")
        parts.append("</table>")

    # 附录页的标题/说明段落（在 docx 里位于主表之后、附录表之前）
    apx_ps = []
    if len(d.tables) > 2:
        for p in d.paragraphs:
            t = p.text.strip()
            if not t:
                continue
            if t.startswith("附录："):
                apx_ps.append(f"<div class='apx-h'>{html.escape(t)}</div>")
            elif t.startswith("说明"):
                apx_ps.append(f"<div class='apx-note'>{html.escape(t)}</div>")
        parts.append("".join(apx_ps))
    parts.append("</div></body></html>")
    out_path.write_text("".join(parts), encoding="utf-8")


def main() -> None:
    src = ROOT / "data" / "docx"
    dst = ROOT / "data" / "preview"
    dst.mkdir(parents=True, exist_ok=True)
    files = sorted(src.glob("*.docx"))
    if not files:
        print("no docx")
        return
    for f in files:
        o = dst / f"{f.stem}.html"
        render_html(f, o)
        print(f"[preview] {f.name} -> {o.name}")


if __name__ == "__main__":
    main()

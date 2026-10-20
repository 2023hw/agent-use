"""W3：把 LessonPlan 渲染成**与教师原模板格式一致**的 docx。

## 核心策略：以原 docx 为父本（父本克隆）

不重新搭表格，而是：
  1. 复制一份原始语料 docx 当父本 → styles.xml / theme / 页面设置全部继承
  2. 掏空 body（保留末尾 sectPr）
  3. deepcopy 原始的表0（标题单格）与表1（主表）
  4. **只改文本、不动任何 XML 格式属性** → 字体/字号/行距/边框/列宽天然一致

为什么不用"从零建表 + 硬编码样式"：
- 硬编码要复刻 17 列宽度、每行 trHeight、tcBorders、gridSpan，极易漏
- 原模板换个班级就变（火箭班 15 人 / 闪电班人数不同），硬编码必然失真

## 硬约束：单元格文本替换不能碰 run 的属性

`_set_cell_text()` 保留单元格里的**第一个 run**，只改它的文字，
并删除多余的 run。这样 run 上的 rPr（字体/字号/加粗）原样留在 XML 里。
**绝不能用 `cell.text = "..."`** —— python-docx 会把整格推倒重建，样式全丢。

## 个别化差异目标表：匿名占位行

用户要求"表头 + 匿名占位行"。但本项目的核心约束是**文本层面零身份**：
- 匿名编号（S01/S02）本身合规：不含姓名、不含诊断
- 但**康复目标列不得引用真实目标编号（如 1456）** —— 那等于把某个孩子的
  评量结果搬进教案。因此该列改写为**分层通用目标描述**，评量五列留空。
"""
from __future__ import annotations

import copy
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from .layering import ChildLayer, LAYER_META
from .schema import LessonPlan
from .template import tc_cells_raw, tc_spans

# 语料里可当父本的文件（排版最规整的一份）
DEFAULT_PARENT = Path(r"C:\Users\17093\Desktop\homework\agent use idea"
                      r"\第一周-劳动最快乐(1).docx")

# 表1 中固定尾行的栏目名（活动延伸 / 活动反思），用于定位尾部。
# 注意：模板用「活 动 延 伸」这种字间加空格的写法，故比对前先过 `_norm()`。
TAIL_LABELS = ("活动延伸", "活动反思")
# 教学过程栏目的行首名（同样会被 `_norm()` 规范成无空格形式）
PROC_LABEL = "教学过程"
# 个别化表在表1 中的列下标
COL_IND_GOAL = 7      # 个别化差异目标
COL_CHILD = 8         # 儿童姓名
COL_TARGET = 9        # 康复目标
COL_BASELINE = 10     # 康复基线
COL_EVAL1 = 11        # 第一次 … 第五次 = 11..15

# 数据行统一行高（twips）。
#
# ⚠ 为什么写死而不是取「区域内现有行高的中位数」：
#   父本数据区的行高五花八门（540/786/865/1058/1262/1306），中位数会随
#   **行数**漂移 —— 12 人名册时算出 837，扩到 37 人后就变成 786，同一种
#   格式在两版之间行高不一致。固定成用户已认可的那一版实测值（837
#   twips ≈ 1.48cm）后，名册人数怎么变，行高都稳定不变。
DATA_ROW_TWIPS = 837

# 「任课老师：…时间：…」段落里右对齐制表位的位置（twips）。
# A4 纵向正文宽约 8306 twips（21cm − 左右各 3.17cm 边距），取接近右边界处。
META_TAB_POS = 8300

# 正文字体规格 —— 必须与范例父本的 docDefaults 逐项一致：
#   西文 / 数字 → Times New Roman，中文 → 宋体；
#   `hint="eastAsia"` 让中英混排时各归其位。
#
# ⚠ 历史 bug（2026-10-08 修复）：原先用 python-docx 的
#   `run.font.name = "宋体"` 设字体，它只写 `w:ascii` / `w:hAnsi`
#   —— 等于**把西文字体也设成了中文衬线字体「宋体」**，而且不写
#   `w:eastAsia` / `w:hint`。结果同一份 docx 里并存两套字体档：
#     主表/大标题（继承父本）→ Times New Roman + 宋体
#     附录页 + 任课老师行      → 宋体（西文也是宋体）
#   现在统一走 `_apply_template_font()`，并由 `final_audit.py`
#   的「字体统一」判据拦住回退。
TEMPLATE_LATIN_FONT = "Times New Roman"
TEMPLATE_CJK_FONT = "宋体"
# 正文（主表 / 标题）字号，与范例父本一致。
TEMPLATE_BODY_PT = 12.0
# 附录页字号：与主表/范例同号（范例正文全是 12pt）。
# 想让附录更紧凑，把这里改成 10.5 即可 —— 只影响附录，不动正文。
APPENDIX_FONT_PT = 12.0


# ---------------------------------------------------------------------------
# 低层工具
# ---------------------------------------------------------------------------


def _norm(s: str) -> str:
    """规范化单元格文本，用于栏目名匹配。

    模板里竖排栏目（「教 学 过 程」「材 料 准 备」）的实现方式是：
    **每个字之间插入换行/空格**，有些格还会被填充 `/` 字符。
    实测父本 r4 的第 0 格 `_norm` 后是 `////////////教学过程` ——
    如果用 `== "教学过程"` 匹配会失败，教学过程正文就填不进去。
    所以这里一并剥掉：空白、全角空格、以及作为填充物的 `/`。
    """
    if not s:
        return ""
    s = re.sub(r"[\s\u3000]+", "", s)
    s = s.strip("/")            # 剥掉首尾的填充斜杠
    return s


def _set_cell_text(cell, text: str) -> None:
    """把单元格文本换成 text，**保留原有 run 格式**。

    ⚠ 这是本文件最关键的函数。做法：
      1. 取格内第一个带 rPr 的 run，深拷贝它的 rPr 作为格式模板
      2. 删掉格内所有 run
      3. 新建一个 run 写入第一行文本，把模板 rPr 装回去
      4. 其余行用 <w:br/> 挂在同一 run 里换行（原表单元格就是单段落多换行）
      5. 删掉除第一段以外的段落（原表单元格基本是单段落）

    为什么不直接 `cell.text = ...`：python-docx 会把整格推倒重建，
    rPr（字体/字号）全部丢失，渲染出来字体就不对了。
    """
    lines = (text or "").split("\n")
    paras = cell.paragraphs
    if not paras:
        return
    p0 = paras[0]

    # 1) 取格式模板
    rpr_template = None
    for p in paras:
        for r in p.runs:
            rpr = r._element.find(qn("w:rPr"))
            if rpr is not None:
                rpr_template = copy.deepcopy(rpr)
                break
        if rpr_template is not None:
            break

    # 2) 清掉本格所有 run
    for p in paras:
        for r in list(p.runs):
            r._element.getparent().remove(r._element)

    # 3) 写第一行
    run = p0.add_run(lines[0])

    # 4) 其余行用 <w:br/> + <w:t>
    for ln in lines[1:]:
        run._element.append(run._element.makeelement(qn("w:br"), {}))
        t = run._element.makeelement(qn("w:t"), {})
        t.text = ln
        t.set(qn("xml:space"), "preserve")
        run._element.append(t)

    # 5) 把模板 rPr 装到 run 最前面（rPr 必须是 run 的第一个子元素）
    if rpr_template is not None:
        old = run._element.find(qn("w:rPr"))
        if old is not None:
            run._element.remove(old)
        run._element.insert(0, copy.deepcopy(rpr_template))
    else:
        # ⚠ 父本有个别格是空的、里面一个 run 都没有，取不到格式模板。
        #   这时若放任新 run 不带 rPr，它会落到 python-docx 的默认样式
        #   （Calibri + 10.5pt），与相邻格不一致 —— 实测漏出过
        #   「S16 / S17 / 123」这四格。补上范例字体档兜底。
        _apply_template_font(run, TEMPLATE_BODY_PT)

    # 6) 删掉多余段落
    for p in paras[1:]:
        p._element.getparent().remove(p._element)


_EMPH_RE = re.compile(r"\*\*(.+?)\*\*", re.S)


def _split_emphasis(text: str):
    """把 `**加粗**` 切成 [(片段, 是否加粗), ...]。

    docx 没有 markdown 语法 —— 直接把 `**` 写进文本会**原样显示成星号**。
    兜底：落单的 `**` 直接清掉，免得又漏一个星星。
    """
    out = []
    pos = 0
    for m in _EMPH_RE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return [(seg.replace("**", ""), b) for seg, b in out if seg]


def _add_rich_run(paragraph, text: str, size_pt=None) -> None:
    """把带 `**` 标记的文本写成**真加粗**的多个 run（字体仍走模板档）。"""
    for seg, bold in _split_emphasis(text):
        run = paragraph.add_run(seg)
        _apply_template_font(run, size_pt)
        if bold:
            run.font.bold = True


def _apply_template_font(run, size_pt=None) -> None:
    """把 run 的字体刷成与范例父本（docDefaults）同款。

    ⚠ 不要用 python-docx 的 `run.font.name = "宋体"`：它只写
      `w:ascii` / `w:hAnsi`，会把西文字体也设成中文字体名。
      这里显式写全四个属性 + hint，中英各归其位。
    ⚠ 用 `get_or_add_rFonts()` 而不是手写 `insert(0, ...)`：
      `w:rPr` 的子元素顺序在 OOXML 里是有约束的（rFonts 必须在 b/sz 之前），
      由 python-docx 负责插到正确位置，免得 Word 报「文件已损坏」。
    """
    # `run` 既可以是 python-docx 的 `Run`，也可以是底层的 `CT_R` 元素
    # （附录整页刷字体时是直接遍历 XML 拿到的元素）。
    r = getattr(run, "_element", run)
    rpr = r.get_or_add_rPr()
    rf = rpr.get_or_add_rFonts()
    rf.set(qn("w:hint"), "eastAsia")
    rf.set(qn("w:ascii"), TEMPLATE_LATIN_FONT)
    rf.set(qn("w:hAnsi"), TEMPLATE_LATIN_FONT)
    rf.set(qn("w:eastAsia"), TEMPLATE_CJK_FONT)
    rf.set(qn("w:cs"), TEMPLATE_LATIN_FONT)
    if size_pt is not None:
        half = str(int(round(size_pt * 2)))       # w:sz 的单位是半磅
        rpr.get_or_add_sz().set(qn("w:val"), half)
        if hasattr(rpr, "get_or_add_szCs"):
            rpr.get_or_add_szCs().set(qn("w:val"), half)


def _restyle_appendix_fonts(doc, start_idx: int) -> None:
    """把附录页（body 第 `start_idx` 个元素之后）的 run 全部刷成范例字体档。

    附录是范例里**没有**的教学管理页，由代码从零建表 / 建段，
    因此没有可从父本继承的格式 —— 不显式写就会落到 python-docx 的
    默认样式（Calibri + 宋体），与主表的 Times New Roman 不一致。

    分页前记下 `len(doc.element.body)`，分页后从这里往后扫，
    就能精确圈出「附录页」这一段，不会误伤正文。
    """
    for el in list(doc.element.body)[start_idx:]:
        if el.tag == qn("w:p"):
            for r in el.findall(qn("w:r")):
                txt = "".join(t.text or "" for t in r.findall(qn("w:t")))
                # 附录大标题保持 14pt（与范例大标题同号）
                _apply_template_font(
                    r, None if txt.startswith("附录：") else APPENDIX_FONT_PT)
        elif el.tag == qn("w:tbl"):
            for r in el.iter(qn("w:r")):
                _apply_template_font(r, APPENDIX_FONT_PT)


def _style_appendix_table(tbl) -> None:
    """给附录表加边框（字体由 `_restyle_appendix_fonts()` 统一刷）。

    ⚠ 不能用 `tbl.style = "Table Grid"`：父本的 styles.xml 是教师原文档的，
    里面**没有** `Table Grid` 这个样式定义，会 KeyError。
    因此这里手写 tblBorders，规格照抄父本主表（四边 + 内部 single/sz=4）。
    """
    tblPr = tbl._tbl.tblPr
    if tblPr is None:
        tblPr = OxmlElement("w:tblPr")
        tbl._tbl.insert(0, tblPr)
    for old in tblPr.findall(qn("w:tblBorders")):
        tblPr.remove(old)
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement(f"w:{edge}")
        e.set(qn("w:val"), "single")
        e.set(qn("w:sz"), "4")
        e.set(qn("w:space"), "0")
        e.set(qn("w:color"), "auto")
        borders.append(e)
    tblPr.append(borders)

    # 字体不在这里刷 —— 附录整页建完后由 `_restyle_appendix_fonts()`
    # 统一按范例字体档处理（含表外段落），避免两处口径不一致。


def _fill_table_from_rows(tbl, header: Sequence[str],
                          rows: Sequence[Sequence[str]]) -> None:
    """往附录表里填数据（表已有 header 行）。"""
    for r in rows:
        cells = tbl.add_row().cells
        for i, v in enumerate(r):
            if i < len(cells):
                cells[i].text = str(v)


def _short_goal(text: str, limit: int = 26) -> str:
    """把一条目标条文压成短标签，用于个别化表的「目标描述」列。

    范例里那一格写的是简短描述（如「整节课堂能安坐好不走出位置」），
    不是把整段条文搬进去。这里去掉冗语后截断。

    ⚠ 必须**保留辅助量词**，只把「在…下」的框架压成短标记：
      「能在全辅助下通过障碍物把动物送回家」 → 「能全辅助通过障碍物把动物送回家」
      「能在少量辅助通过障碍物把动物送回家」   → 「能少量辅助通过障碍物把动物送回家」
      「能自己通过障碍物把动物送回家」         → 「能独立通过障碍物把动物送回家」
    如果连辅助量一起剥掉，同一支持轴上三条会压成完全相同的文本，
    表格里就看不出分层了 —— 那正是这张表存在的意义。
    """
    t = re.sub(r"[\s\u3000]+", "", text or "")
    # 「在…（辅助|提示|示范）下」→ 只去掉「在」和「下」，留下辅助量词
    t = re.sub(r"在([^，。；]{0,12}?(?:辅助|提示|示范))。?(?:下)?", r"\1", t)
    # 「能全辅助」「能少量辅助」→ 补「在…下」框架缺失后的通顺写法
    t = re.sub(r"能(全辅助|少量辅助|大量辅助)", r"能在\1下", t)
    # 「能自己」统一成「能独立」，与 L3「独立完成」的命名保持一致
    t = t.replace("能自己", "能独立")
    # 去重复词（范例原文常出现「独立独立」「表达表达」这类手写重复）
    t = re.sub(r"(.{2})\1", r"\1", t)
    # 剥掉「如：…」这类举例尾巴
    t = re.sub(r"[如例][：:].*$", "", t)
    # 剥掉括号补充
    t = re.sub(r"（.*?）|\(.*?\)", "", t)
    t = t.rstrip("，。、；：.")
    t = t.replace("。。", "。")
    # 截断时避免砍掉成对的引号（`送“回家` 会读成残句）
    if len(t) > limit:
        head = t[:limit]
        for lq, rq in (("“", "”"), ("「", "」"), ("（", "）")):
            if head.count(lq) > head.count(rq):
                head += rq
        t = head + "…"
    return t


def _simulate_evals(cl) -> List[str]:
    """按层级推演一组五次评量的演示序列（仅用于附录页展示调层机制）。

    推演依据层级 → 能力画像的对应关系，并显式覆盖三类典型走向，
    让附录页能同时看到「上调」与「下调」两种调整：

      · L1 高支持层 —— 起点低、缓慢进步，第 4~5 次出现连续达标 → 上调
      · L2 中支持层 —— 起点中等、稳定进步，第 3~5 次连续达标 → 上调
      · L3 低支持层 —— 起点高但中途滑坡 → 触发一次下调，再回升
    另随机安排一次「未参与」以体现该情况不计入分母、不触发调整。
    """
    if cl.layer == 1:
        seq = ["EEE", "PEE", "PEE", "PPP", "PPP"]
    elif cl.layer == 2:
        seq = ["PEE", "PPE", "PPP", "PPP", "PPP"]
    else:
        # 上限层没有上调空间，安排一次滑坡展示「下调」分支
        seq = ["PPP", "PPP", "EEE", "PPP", "PPP"]
    # 第二次安排未参与（范例里很常见）
    if cl.layer >= 2:
        seq[1] = "未参与"
    return seq


def _rebuild_meta_paragraph(p_el) -> None:
    """重建「任课老师：……时间：……」段落，改用**右对齐制表位**。

    父本原写法是 `任课老师：` + 40 个半角空格 + `时间：2025年 5月6日—5月9日`。
    靠空格凑对齐有两个问题：
      1. 换字体 / 改字号就错行；
      2. 日期长度一变，「时间：」的横向位置就跟着漂。
    用户明确要求「任课老师」与日期**各自位于正确位置且不得错行**。

    做法：清空原有 run，改成
        [左对齐] 任课老师：____        [右对齐制表位 → 行尾] 时间：xxxx
    制表位落在页面正文宽度处，日期恒贴右边界，与左标签互不干扰。
    """
    pPr = p_el.find(qn("w:pPr"))
    if pPr is None:
        pPr = OxmlElement("w:pPr")
        p_el.insert(0, pPr)

    # 清掉原有制表位定义，重建一个右对齐制表位
    for tabs in pPr.findall(qn("w:tabs")):
        pPr.remove(tabs)
    tabs = OxmlElement("w:tabs")
    tab = OxmlElement("w:tab")
    tab.set(qn("w:val"), "right")          # 右对齐制表位
    tab.set(qn("w:leader"), "none")
    tab.set(qn("w:pos"), str(META_TAB_POS))  # twips，正文宽度处
    tabs.append(tab)
    pPr.insert(0, tabs)

    # 段落标记（`w:pPr/w:rPr`）的字体也要刷成范例档。
    # ⚠ 父本这一段的段落标记原本写的是 `w:ascii="宋体"`（西文也用宋体），
    #   与正文其余部分不一致；`final_audit.py` 的「字体统一」判据会判 FAIL。
    prpr = pPr.find(qn("w:rPr"))
    if prpr is None:
        prpr = OxmlElement("w:rPr")
        _after = None
        for _tag in ("w:sectPr", "w:pPrChange"):
            _el = pPr.find(qn(_tag))
            if _el is not None:
                _after = _el
                break
        if _after is not None:
            _after.addprevious(prpr)
        else:
            pPr.append(prpr)
    prf = prpr.find(qn("w:rFonts"))
    if prf is None:
        prf = OxmlElement("w:rFonts")
        _style = prpr.find(qn("w:rStyle"))
        # `w:rFonts` 是 `w:rPr` 里 rStyle 之后、其余属性之前的首个子元素
        if _style is not None:
            _style.addnext(prf)
        else:
            prpr.insert(0, prf)
    prf.set(qn("w:hint"), "eastAsia")
    prf.set(qn("w:ascii"), TEMPLATE_LATIN_FONT)
    prf.set(qn("w:hAnsi"), TEMPLATE_LATIN_FONT)
    prf.set(qn("w:eastAsia"), TEMPLATE_CJK_FONT)
    prf.set(qn("w:cs"), TEMPLATE_LATIN_FONT)

    # 清掉所有 run / bookmark，只留一个干净的段落
    for ch in list(p_el):
        if ch.tag in (qn("w:r"), qn("w:bookmarkStart"), qn("w:bookmarkEnd")):
            p_el.remove(ch)

    # 保留原有 rPr（宋体 12pt），没有就建一个
    rpr = OxmlElement("w:rPr")
    rf = OxmlElement("w:rFonts")
    rf.set(qn("w:hint"), "eastAsia")
    rf.set(qn("w:ascii"), TEMPLATE_LATIN_FONT)
    rf.set(qn("w:hAnsi"), TEMPLATE_LATIN_FONT)
    rf.set(qn("w:eastAsia"), TEMPLATE_CJK_FONT)
    rf.set(qn("w:cs"), TEMPLATE_LATIN_FONT)
    rpr.append(rf)
    for tag, val in (("w:sz", "24"), ("w:szCs", "24")):
        e = OxmlElement(tag)
        e.set(qn("w:val"), val)
        rpr.append(e)

    def _run(text: str, with_tab: bool = False):
        r = OxmlElement("w:r")
        r.append(copy.deepcopy(rpr))
        if with_tab:
            r.append(OxmlElement("w:tab"))
        t = OxmlElement("w:t")
        t.set(qn("xml:space"), "preserve")
        t.text = text
        r.append(t)
        p_el.append(r)

    _run("任课老师：")
    _run("时间：", with_tab=True)
    # 具体日期由 _fill_meta() 回填（此时还不知道 plan.date_range）


def _format_date_range(raw: str) -> str:
    """把日期范围整理成模板口吻：2026年10月12日—10月16日。"""
    s = (raw or "").strip()
    if not s:
        return ""
    return s


def _find_main_table(doc) -> Optional[object]:
    """在父本里找出主表（行数最多、含「有益经验」的那张）。"""
    best = None
    for t in doc.tables:
        txt = _norm("".join(c.text for c in t.rows[0].cells))
        if "有益经验" in _norm("".join(c.text for row in t.rows[:5] for c in row.cells)) or \
           "活动班级" in txt:
            if best is None or len(t.rows) > len(best.rows):
                best = t
    return best


def _row_label(row) -> str:
    """取一行的行首栏目名（第一个 <w:tc> 的文本）。"""
    return _norm(tc_cells_raw(row)[0] if tc_cells_raw(row) else "")


def _cell_at(row, tc_index: int):
    """按**原始 <w:tc> 下标**取单元格。

    ⚠ 关键区别：python-docx 的 `row.cells` 会**按 gridSpan 展开**，
    合并格会在列表里重复出现。父本的教学过程行第 0 格 `gridSpan=6`，
    于是 `row.cells[1]` 仍然指向第 0 格那个合并单元格本身，
    而真正的正文格在 `cells[6]`。用 `cells[1]` 写内容会写错位置
    （实测表现为"教学过程正文丢失"）。

    这里改用 lxml 层面的 `<w:tc>` 顺序索引，与 `tc_cells_raw()` 对齐。
    """
    tcs = row._tr.findall(qn("w:tc"))
    if tc_index < 0 or tc_index >= len(tcs):
        return None
    from docx.table import _Cell
    return _Cell(tcs[tc_index], row._parent)


def _logical_col_to_tc(row, target_col: int):
    """把「展开后的逻辑列号」映射到「原始 <w:tc> 下标」。

    父本表格不规则合并（gridSpan），`row.cells` 是按逻辑列展开的，
    而 lxml 的 `<w:tc>` 是压缩的。要按逻辑列精确写格，必须先做映射。
    返回 tc 下标；越界返回 None。
    """
    spans = tc_spans(row)
    col = 0
    for ti, sp in enumerate(spans):
        if col <= target_col < col + sp:
            return ti
        col += sp
    return None


def _cell_by_logical_col(row, target_col: int):
    """按逻辑列号取单元格（自动处理 gridSpan）。"""
    ti = _logical_col_to_tc(row, target_col)
    return _cell_at(row, ti) if ti is not None else None


def _clone_row(tbl, src_tr):
    """克隆一行并追加到表尾。"""
    new_tr = copy.deepcopy(src_tr)
    tbl._tbl.append(new_tr)
    return new_tr


def set_row_height(tr, twips: int, rule: str = "atLeast") -> None:
    """设置行的 trHeight。

    ⚠ 必须显式设 `hRule`：父本有些行只写 `w:val` 不写 `w:hRule`，
    Word 默认按 `auto` 处理 —— 内容少时行会被压到近零高（实测父本
    数据区存在 h=99 / h=90 / h=184 的行，渲出来那一行的幼儿"消失"）。
    这里统一用 `atLeast`（至少这么高），既不压行、又能随内容长高。
    """
    trPr = tr.find(qn("w:trPr"))
    if trPr is None:
        trPr = OxmlElement("w:trPr")
        tr.insert(0, trPr)
    h = trPr.find(qn("w:trHeight"))
    if h is None:
        h = OxmlElement("w:trHeight")
        trPr.append(h)
    h.set(qn("w:val"), str(int(twips)))
    h.set(qn("w:hRule"), rule)


def unmerge_cell(tc) -> None:
    """把一个 `<w:tc>` 的纵向合并标记去掉，使其成为独立格子。

    `w:vMerge` 有两个形态：
      - `<w:vMerge w:val="restart"/>` 合并起点
      - `<w:vMerge/>`（无 val）             合并续行（视觉上是同一个格）
    两种都要删，否则该格仍被 Word 当作合并区域的一部分，
    不会显示成"一个个独立的格子"。
    """
    tcPr = tc.find(qn("w:tcPr"))
    if tcPr is None:
        return
    vm = tcPr.find(qn("w:vMerge"))
    if vm is not None:
        tcPr.remove(vm)


def _set_vmerge(tc, restart: bool) -> None:
    """给一个 `<w:tc>` 打纵向合并标记。

      · `restart=True`  → `<w:vMerge w:val="restart"/>`（合并区起点，带文字）
      · `restart=False` → `<w:vMerge/>`（续行，视觉上与起点是同一个格）

    ⚠ `w:tcPr` 的子元素**有固定顺序**（cnfStyle / tcW / gridSpan / hMerge /
      vMerge / tcBorders / shd / …）。`vMerge` 必须插在 `gridSpan` / `hMerge`
      之后、`tcBorders` 之前；图省事直接 `append` 会排到 `vAlign` 后面，
      Word 打开时报「文件已损坏，是否修复」。
    """
    tcPr = tc.find(qn("w:tcPr"))
    if tcPr is None:
        tcPr = OxmlElement("w:tcPr")
        tc.insert(0, tcPr)
    vm = tcPr.find(qn("w:vMerge"))
    if vm is None:
        vm = OxmlElement("w:vMerge")
        before = ("cnfStyle", "tcW", "gridSpan", "hMerge")
        pos = 0
        for i, ch in enumerate(tcPr):
            if ch.tag.split("}")[-1] in before:
                pos = i + 1
        tcPr.insert(pos, vm)
    if restart:
        vm.set(qn("w:val"), "restart")
    elif vm.get(qn("w:val")) is not None:
        del vm.attrib[qn("w:val")]


def merge_header_cols(tbl, top_idx: int, bot_idx: int,
                      cols: Sequence[int]) -> None:
    """把表头**两行**的指定逻辑列做纵向合并（范例的写法）。

    范例实测（每一份都如此）：

        r4: … | 个别化差异目标(R) | 儿童姓名(R) | 康复目标(R) | 康复基线(R) | 评量效果(gs5)
        r5: … |      (c)        |    (c)      |    (c)      |    (c)      | 第一次…第五次

    即「个别化差异目标 / 儿童姓名 / 康复目标 / 康复基线」四格各占**一整块**，
    「评量效果」在第二行才拆成五次列。本项目的 `normalize_region` 会把
    数据区所有 `vMerge` 拆掉（用户早前要求「独立等高格」），表头两行也
    在被拆的范围内，于是变成「上格有字 + 下格空白」两个格 ——
    与范例不符。此函数在 `normalize_region` **之后**把表头重新合回去。
    """
    rows = list(tbl.rows)
    top, bot = rows[top_idx], rows[bot_idx]
    for col in cols:
        ti_t = _logical_col_to_tc(top, col)
        ti_b = _logical_col_to_tc(bot, col)
        if ti_t is None or ti_b is None:
            continue
        tc_t = top._tr.findall(qn("w:tc"))[ti_t]
        tc_b = bot._tr.findall(qn("w:tc"))[ti_b]
        _set_vmerge(tc_t, True)
        _set_vmerge(tc_b, False)
        # 续行格必须是空的，否则合并后会盖住起点格的文字
        for t in tc_b.iter(qn("w:t")):
            t.text = ""


def unify_column_vmerge(rows, col: int, keep_text_row: int = 0) -> None:
    """把一个逻辑列在**多行**上合并成一整格（首行 restart、其余续行）。

    用于「个别化差异目标」列：范例里这格是**全班共用的目标池**，
    只在第一个幼儿行写一次，向下一直合并到最后一个幼儿行。
    """
    for i, row in enumerate(rows):
        ti = _logical_col_to_tc(row, col)
        if ti is None:
            continue
        tc = row._tr.findall(qn("w:tc"))[ti]
        _set_vmerge(tc, i == keep_text_row)
        if i != keep_text_row:
            for t in tc.iter(qn("w:t")):
                t.text = ""


def normalize_region(tbl, r0: int, r1: int,
                     col_start: int, col_end: int,
                     row_twips: int) -> None:
    """把 `[r0, r1]` 行 × `[col_start, col_end]` 逻辑列的矩形区域规整为：
    每个格都是**独立等高**的单元格（不合并、不改变格高）。

    用户诉求（原文）：
      「使儿童姓名往右的 8 个格以及向下到活动延伸之前的所有单元格
        都呈现为一个个等高的独立格子，若无人填写则保持空白，
        不要合并或改变格子高度。」

    因此这里做三件事：
      1. 拆掉该区域内所有 `w:vMerge`（纵向合并）—— 让每行每列都是独立格；
      2. 把区域内每一行设为**同一个行高**（`atLeast`，不会压行）；
      3. 不动文本内容（空的就是空的，不填占位符）。
    """
    rows = list(tbl.rows)
    for ri in range(max(0, r0), min(r1 + 1, len(rows))):
        row = rows[ri]
        for col in range(col_start, col_end + 1):
            ti = _logical_col_to_tc(row, col)
            if ti is None:
                continue
            tc = row._tr.findall(qn("w:tc"))[ti]
            unmerge_cell(tc)
        set_row_height(row._tr, row_twips, "atLeast")


def marks_and_gaps(s: str) -> tuple:
    """把 P/E 记号串拆成 (记号表, 相邻记号之间的空格数表)。

    范例的「康复基线」是**记号与目标编号逐位对齐**的写法，所以记号之间
    带空格：目标 `1456` 配 `P  P PP`（4 个记号），目标 `145` 配 `P  EE`
    （3 个记号）。空格只是书写时的对位留白，语义全靠记号的**个数与顺序**。

        marks_and_gaps("P  P PP") -> (['P','P','P','P'], [2, 1, 0])
        marks_and_gaps("PEEE")    -> (['P','E','E','E'], [0, 0, 0])
    """
    marks: List[str] = []
    gaps: List[int] = []
    last = None
    for i, ch in enumerate(s or ""):
        if ch in "PE":
            if last is not None:
                gaps.append(i - last - 1)
            marks.append(ch)
            last = i
    return marks, gaps


def fit_baseline(s: str, n: int, layer: int) -> str:
    """把「康复基线」串的**记号个数**调到 n（= 本课该幼儿的目标条数）。

    ## 为什么必须调

    实测 178 条可比记录，**记号个数 == 目标编号个数，178/178 无例外**
    （目标 `1456` 配 4 个记号、`145` 配 3 个记号）。所以「康复基线」是
    **逐条目标的评量**：本课要评 3 条，基线就只能有 3 个记号。

    跨教案承接时，上一课的目标组合与本课未必同长度（如上一课评 4 条、
    本课只评 3 条），直接照搬会得到「4 个记号对 3 条目标」的错位。

    ## 调法

    - 长度相同 → **原样返回**（连原表那套空格写法一起保留，最贴近范例）；
    - 记号多了 → 取前 n 个（顺序不变）；
    - 记号少了 → 末尾补：L2/L3 补 `P`、L1 补 `E`（低支持层多未达标）；
    - 补出来的串沿用原串的**间隔模式**，视觉风格与范例一致；
    - 串里**没有 P/E 记号**（如缺勤标记 `未参与`）→ 原样保留，不要清空。
    """
    marks, gaps = marks_and_gaps(s)
    # ⚠ 先判「没有记号」：范例里的 `未参与` 是缺勤标记，不含 P/E。
    #   早期版本把它当成空串返回，导致该幼儿的基线格变成空白。
    if not marks:
        return (s or "").strip()
    if n <= 0:
        return s
    if len(marks) == n:
        return s
    if len(marks) > n:
        marks = marks[:n]
    else:
        fill = "P" if layer >= 2 else "E"
        marks = marks + [fill] * (n - len(marks))
    out = marks[0]
    for i, m in enumerate(marks[1:], start=1):
        g = gaps[i - 1] if i - 1 < len(gaps) else (gaps[-1] if gaps else 1)
        out += " " * max(0, g) + m
    return out


def normalize_grid(tbl) -> None:
    """修掉父本表格「网格列数 ≠ 各行 span 合计」的错位。

    ## 问题现象

    父本 `第一周-劳动最快乐(1).docx` 的 `tblGrid` 有 **17** 个 `gridCol`：

        [785, 1106, 1106, 971, 1291, 1291, 189, 1102, 441, 369, 369,
         369, 369, 369, 369, 356, 813]

    但每一行的 `gridSpan` 加起来只有 **16**。表头 7 个 tc：

        教学过程(1) + 正文(6) + 个别化(1) + 儿童姓名(1) + 康复目标(1)
        + 康复基线(1) + 评量效果(5) = 16

    也就是最后那个 `813` 宽度的网格列**没有被任何单元格占用**。
    Word/LibreOffice 渲染时它仍然占位 → 所有单元格被挤窄，
    数字/文字被迫逐字换行（实测 `S01`、`123` 被拆成一列一个字）。

    对照正确范例 `第五周-童心向党(1).docx`：网格 **16** 列、span 合计 **16**，
    完全一致，渲染正常 —— 这正是用户截图 2 的"正常样子"。

    ## 修法

    以「**多数行**的 span 合计」为准（用众数，不用最大值）裁 `tblGrid`。

    ⚠ 为什么不用最大值：父本 `活动延伸` 那一行是 `sp1 + sp15 + sp1 = 17`，
    比其它所有行（16）多出一个多余的尾格。若取最大值就永远修不掉。
    取众数则得到 16（20 行里有 19 行是 16），正好匹配正确范例的结构。

    同时把那个"多出来的尾格"（仅该行 span 超标的行）一并删掉，
    使 `活动延伸` 行也回到 16 —— 否则该行仍会比别人宽一格。
    """
    grid = tbl._tbl.find(qn("w:tblGrid"))
    if grid is None:
        return
    cols = grid.findall(qn("w:gridCol"))

    def _span(tr) -> int:
        s = 0
        for tc in tr.findall(qn("w:tc")):
            tcPr = tc.find(qn("w:tcPr"))
            gs = tcPr.find(qn("w:gridSpan")) if tcPr is not None else None
            s += int(gs.get(qn("w:val"))) if gs is not None else 1
        return s

    trs = tbl._tbl.findall(qn("w:tr"))
    if not trs:
        return
    from collections import Counter
    cnt = Counter(_span(tr) for tr in trs)
    need = cnt.most_common(1)[0][0]      # 众数 = 表的主体逻辑列数
    if need <= 0 or need >= len(cols):
        return

    # 把 span 超标的行裁到 need（删掉末尾多余的空白 tc）
    for tr in trs:
        while _span(tr) > need:
            tcs = tr.findall(qn("w:tc"))
            # 从后往前找可以删的「空白尾格」
            dropped = False
            for tc in reversed(tcs):
                tx = "".join(n.text or "" for n in tc.iter(qn("w:t"))).strip()
                if not tx:
                    tr.remove(tc)
                    dropped = True
                    break
            if not dropped:
                break

    # 裁掉末尾多余的 gridCol
    for c in cols[need:]:
        grid.remove(c)
    # 同步 tblW
    tblPr = tbl._tbl.find(qn("w:tblPr"))
    if tblPr is not None:
        tblW = tblPr.find(qn("w:tblW"))
        if tblW is not None:
            total = sum(int(g.get(qn("w:w"))) for g in cols[:need])
            tblW.set(qn("w:w"), str(total))
            tblW.set(qn("w:type"), "dxa")


# ---------------------------------------------------------------------------
# 渲染器
# ---------------------------------------------------------------------------


def _center_table_horizontally(doc, tbl) -> None:
    """让整张表格在版心内水平居中。

    OOXML 里表格居中靠 `w:tblPr/w:jc val="center"`；但部分渲染器
    （在线预览、旧版 WPS）只认 `w:tblInd`（左缩进）。两处都写，
    计算结果一致，不会互相打架。

      twips 换算：1 twip = 635 EMU；python-docx 的长度都返回 EMU。
      居中缩进 = (版心宽 - 表宽) / 2
    """
    tblPr = tbl._tbl.tblPr
    if tblPr is None:
        return
    # ① 表格对齐 = center（CT_TblPr 自带 API，会自动保序插到 tblW 之后）
    jc = tblPr.get_or_add_jc()
    jc.set(qn("w:val"), "center")
    # ② 同时写入精确的居中缩进（供不认 tblJc 的渲染器兜底）
    sec = doc.sections[0]
    usable_emu = sec.page_width - sec.left_margin - sec.right_margin
    usable_tw = int(usable_emu / 635)
    tbl_w = 0
    tw = tblPr.find(qn("w:tblW"))
    if tw is not None and (tw.get(qn("w:type")) or "dxa") == "dxa":
        try:
            tbl_w = int(tw.get(qn("w:w")) or 0)
        except ValueError:
            tbl_w = 0
    if tbl_w <= 0:
        # 兜底：把各列宽加起来
        grid = tbl._tbl.find(qn("w:tblGrid"))
        if grid is not None:
            tbl_w = sum(int(g.get(qn("w:w")) or 0)
                        for g in grid.findall(qn("w:gridCol")))
    if tbl_w <= 0 or tbl_w >= usable_tw:
        return
    ind_tw = (usable_tw - tbl_w) // 2
    ind = tblPr.find(qn("w:tblInd"))
    if ind is None:
        # ⚠ 必须按 OOXML 顺序插到 tblCellSpacing 之后、tblBorders 之前，
        #   直接 append 会排到末尾 → Word 报「文件已损坏」。
        ind = OxmlElement("w:tblInd")
        tblPr.insert_element_before(
            ind,
            "w:tblBorders", "w:shd", "w:tblLayout", "w:tblCellMar",
            "w:tblLook", "w:tblCaption", "w:tblDescription", "w:tblPrChange",
        )
    ind.set(qn("w:w"), str(ind_tw))
    ind.set(qn("w:type"), "dxa")


class DocxRenderer:
    """把 LessonPlan 渲染成模板格式 docx。"""

    def __init__(self, parent_path: Optional[Path] = None,
                 class_size: int = 12, class_label: str = "",
                 child_prefix: str = "S", date_range: str = "",
                 teacher_line: str = "", teacher_name: str = "",
                 children: Optional[List[dict]] = None,
                 ladder_spec: Optional[List] = None,
                 prev_baselines: Optional[List[str]] = None,
                 child_records: Optional[Dict[str, Dict[str, str]]] = None,
                 age_range: str = "",
                 severity_mark: str = "★",
                 disability_label: str = "",
                 renumber: bool = True):
        self.parent_path = Path(parent_path or DEFAULT_PARENT)
        self.class_size = class_size
        self.class_label = class_label
        # 班级信息行的「年龄范围」。留空时回落 `plan.age_range`。
        # 按班拆分时，每个班取**该班最新日期范例**里的写法（如 `3----10岁`）。
        self.age_range = age_range
        # 「轻度/中度/重度/多重障碍」四格的记号。范例实测 14 份**全是** `★`，
        # 它描述的是本班包含的程度构成，不是个体诊断，且四个栏目名本就是
        # 模板固有栏目（见 `check_docx_compliance` 的 struct_ok 白名单）。
        self.severity_mark = severity_mark
        # 班级信息行「障碍类别」一格的内容。
        #   范例 14 份里这一格**都写了**该班整体的障碍名称与类别
        #   （实测：火箭班系「智力障碍、自闭症★」，闪电班系「智力障碍、自闭症   ★」）。
        #   用户本轮明确要求「范例里面有每个班的，也要填」，故照范例填写。
        #   值由 `run_render.py` 从**该班日期最新那份范例**读出后传入。
        #   留空则退回「清空该格」的旧行为（匿名化优先时用）。
        self.disability_label = disability_label
        # 是否重新编号。按班拆分时**不能**重编 —— 每个班的编号必须保持
        # 全局唯一（S01…S28 / S29…S37），否则两个班都会出现 S01。
        self.renumber = renumber
        self.child_prefix = child_prefix
        self.date_range = date_range
        self.teacher_line = teacher_line
        self.teacher_name = teacher_name
        # 匿名儿童画像（用于分层）；为空时退化为按人数平均分层
        self.children = children or []
        # 目标阶梯条文 [(序号, 文本)]；为空时退化为用 plan.objectives
        self.ladder_spec = ladder_spec or []
        # 「康复基线」列：**上一教案同一位幼儿**的评量结果（P/E 串），
        # 按本份教案的行序对齐。次选来源；首选是 `child_records`。
        # 为空时按层级推演（见 `_baseline_for`）。
        self.prev_baselines = prev_baselines or []
        # ⭐ 个别化延续表：`{anon_id: {"goal": "1456", "baseline": "P  P PP"}}`
        # 每位幼儿的「康复目标 + 康复基线」取自**他/她最近一次有记录的教案**
        # （见 scripts/roster_from_corpus.py）。有值时优先于分层推演的编号组合。
        self.child_records = child_records or {}
        # 本份教案的分层结果（由 _fill_main 计算后缓存，供附录页用）
        self._layers: List[ChildLayer] = []
        # 与 _layers 同序的个别化延续表（由 _anon_entries 填充）
        self._overrides: List[Dict[str, str]] = []
        # 按班拆分时的各班结果：[(班级信息 dict, [ChildLayer…]), …]
        # 单班渲染时为空，附录页自动退回只用 `_layers`。
        self._layer_groups: List[tuple] = []

    # ---------- 主入口 ----------

    def render(self, plan: LessonPlan, out_path: Path) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # 1) 克隆父本骨架（styles / theme / 页面设置全部继承）
        shutil.copy(self.parent_path, out_path)
        doc = Document(str(out_path))
        parent = Document(str(self.parent_path))
        body = doc.element.body
        sect = body.find(qn("w:sectPr"))

        # 2) 掏空 body，但保住 sectPr
        for child in list(body):
            if child is sect:
                continue
            body.remove(child)

        # 3) 按**父本原顺序**重建：
        #       表0（大标题） → 表外段落（任课老师/时间） → 表1（主表）
        #
        #    ⚠ 顺序不能乱。父本 body 实测为：
        #         <tbl 表0> / <p 任课老师…时间…> / <tbl 表1>
        #      早期版本先 add_paragraph 再 append 两张表，结果变成
        #      「段落 → 表0 → 表1」，大标题被挤到第二行。
        #      用户明确要求「大标题须置于第一行」，所以这里严格照抄父本顺序。
        ptbls = list(parent.tables)          # [表0, 表1]
        p_meta = None
        for el in parent.element.body:
            if el.tag == qn("w:p"):
                if p_meta is None:
                    p_meta = el
                break

        def _insert(el):
            (sect.addprevious(el) if sect is not None else body.append(el))

        # 3a) 表0 = 大标题
        if ptbls:
            _insert(copy.deepcopy(ptbls[0]._tbl))

        # 3b) 表外段落：任课老师 / 时间
        if p_meta is not None:
            new_p = copy.deepcopy(p_meta)
            _rebuild_meta_paragraph(new_p)
            _insert(new_p)
        else:
            _insert(doc.add_paragraph("")._element)

        # 3c) 表1 = 主表
        for t in ptbls[1:]:
            _insert(copy.deepcopy(t._tbl))

        doc.save(str(out_path))

        # 4) 重新打开填内容
        doc = Document(str(out_path))
        self._fill(doc, plan)
        doc.save(str(out_path))
        return out_path

    def render_multi(self, plan: LessonPlan, out_path: Path,
                     groups: List[dict]) -> Path:
        """按**班级**各出一套教案表（大标题只出一次）。

        用户本轮要求：「按班级拆分『认知课《国庆节到了》』的内容，分别列出
        各班的儿童姓名代号，并依据每个班级中最新日期的范例，为其填写
        『轻度』等相关信息项」——落成形式确认为 **每班各一套教案表**。

        骨架与单班版完全一致（大标题 → 任课老师/时间 → 主表×N → 附录），
        只是主表复制 N 份，每份用**自己班**的：

          · `class_label`      火箭班 / 闪电班
          · `age_range`        取自该班**最新日期范例**的写法（`3----10岁`）
          · `class_size`       本课名册里该班**实际人数**（28 / 9）
          · 名册与康复目标/基线 只列本班幼儿

        ⚠ 编号**不重编**：两个班各自的 S01 会撞号，必须保持全局唯一
          （S01…S28 / S29…S37），否则附录里的「儿童代号」不再唯一。

        `groups` 每项的键：
          label / age_range / size / children / child_records / severity_mark
        """
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not groups:
            return self.render(plan, out_path)

        # 1) 克隆骨架
        shutil.copy(self.parent_path, out_path)
        doc = Document(str(out_path))
        parent = Document(str(self.parent_path))
        body = doc.element.body
        sect = body.find(qn("w:sectPr"))
        for child in list(body):
            if child is sect:
                continue
            body.remove(child)

        ptbls = list(parent.tables)          # [标题表, 主表]
        p_meta = None
        for el in parent.element.body:
            if el.tag == qn("w:p"):
                if p_meta is None:
                    p_meta = el
                break

        def _insert(el):
            (sect.addprevious(el) if sect is not None else body.append(el))

        # 2) 大标题（只出一次） → 任课老师/时间 → 每班一张主表
        if ptbls:
            _insert(copy.deepcopy(ptbls[0]._tbl))
        if p_meta is not None:
            new_p = copy.deepcopy(p_meta)
            _rebuild_meta_paragraph(new_p)
            _insert(new_p)
        else:
            _insert(doc.add_paragraph("")._element)
        for _ in groups:
            _insert(copy.deepcopy(ptbls[1]._tbl))
        doc.save(str(out_path))

        # 3) 逐班填内容
        doc = Document(str(out_path))
        self._fill_title(doc, doc.tables[0], plan)
        self._fill_meta(doc, plan)
        mains = [t for t in doc.tables if len(t.rows) > 3]

        keep_renumber = self.renumber
        self.renumber = False                # 保持全局编号，两个班不撞号
        self._layer_groups = []
        n_rec_total = 0
        for tbl, grp in zip(mains, groups):
            self.class_label = grp.get("label", "")
            self.age_range = grp.get("age_range", "")
            self.class_size = int(grp.get("size") or 0)
            self.children = grp.get("children") or []
            self.child_records = grp.get("child_records") or {}
            if grp.get("severity_mark"):
                self.severity_mark = grp["severity_mark"]
            if grp.get("disability_label") is not None:
                self.disability_label = grp.get("disability_label") or ""
            normalize_grid(tbl)
            self._fill_main(tbl, plan)
            n_rec_total += getattr(self, "_n_from_record", 0)
            self._layer_groups.append((grp, list(self._layers)))
        self.renumber = keep_renumber

        # 4) 附录页：两个班合起来统计
        self._layers = [cl for _g, ls in self._layer_groups for cl in ls]
        self._n_from_record = n_rec_total
        self.append_layer_appendix(doc, plan)
        doc.save(str(out_path))
        return out_path

    def _fill(self, doc, plan: LessonPlan) -> None:
        main = max(doc.tables, key=lambda t: len(t.rows))
        # ⚠ 先修网格错位：父本的 tblGrid 比各行 span 合计多一列，
        #   多出的空列会把所有单元格挤窄（表现为数字逐字换行）。
        #   必须在填内容之前修，否则列宽已经算错。
        normalize_grid(main)
        others = [t for t in doc.tables if t is not main]
        if others:
            self._fill_title(doc, others[0], plan)
        self._fill_meta(doc, plan)
        self._fill_main(main, plan)
        # 分层结果不写进教案正文，另附一页
        self.append_layer_appendix(doc, plan)

    # ---------- 表0：标题 ----------

    def _fill_title(self, doc, tbl, plan: LessonPlan) -> None:
        """大标题（表0 是 body 的第一个元素，天然在第一行）。

        用户要求：大标题**居中**。范例原文这一格是 `w:jc` 缺省（左对齐），
        所以「居中」是本方案相对范例的**有意差异**，其余格式
        （14pt / 加粗 / 行高 480 atLeast / 单元格宽 3336 twips）与范例一致。

        ⚠ 居中要做**两层**，少一层都会看起来左对齐：
          ① 格内文本居中（段落 `w:jc`）；
          ② **表格本身**水平居中（`tblPr/w:jc` + 算出来的 `tblInd`）。
          标题表只有一列、宽 3336 twips，而页面可用宽约 10466 twips，
          若只做 ①，表格仍贴在左缩进 2943 处 —— 视觉上就是左对齐。
        """
        cell = tbl.rows[0].cells[0]
        _set_cell_text(cell, plan.title)
        for p in cell.paragraphs:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _center_table_horizontally(doc, tbl)

    # ---------- 表外段落：任课老师 / 时间 ----------

    def _fill_meta(self, doc, plan: LessonPlan) -> None:
        """回填「任课老师：　时间：xxxx」。

        段落结构已由 `_rebuild_meta_paragraph()` 改成「左标签 + 右对齐制表位」，
        这里只需把两个 run 的占位文字补上，**不要动 run 里的 <w:tab/>**。

        ⚠ 必须直接改 `<w:t>` 的文本，不能用 `run.text = ...`。
        python-docx 的 `run.text` setter 会**清空 run 的所有子元素**后只留一个
        `<w:t>`，那会把右对齐制表位 `<w:tab/>` 一起干掉，日期就贴不到右边界。
        ⚠ 教师名留空时也不要塞全角空格 —— 制表位已经负责定位，
        塞空格反而把左标签撑歪。
        """
        for p in doc.paragraphs:
            txt = _norm(p.text)
            if not txt.startswith("任课老师"):
                continue
            runs = p.runs
            if not runs:
                return

            def _set_t(run, text: str) -> None:
                """只替换 run 内的 <w:t>，保留 <w:tab/> 等其它子元素。"""
                ts = run._element.findall(qn("w:t"))
                if ts:
                    ts[0].text = text
                    ts[0].set(qn("xml:space"), "preserve")
                    for extra in ts[1:]:
                        extra.text = ""
                else:
                    t = OxmlElement("w:t")
                    t.set(qn("xml:space"), "preserve")
                    t.text = text
                    run._element.append(t)

            teacher = (self.teacher_name or "").strip()
            _set_t(runs[0], f"任课老师：{teacher}" if teacher else "任课老师：")
            date = self.date_range or _format_date_range(plan.date_range)
            if len(runs) > 1:
                _set_t(runs[1], f"时间：{date}" if date else "时间：")
            else:
                _set_t(runs[0], f"任课老师：{teacher}　　时间：{date}")
            return

    # ---------- 表1：主表 ----------

    def _fill_main(self, tbl, plan: LessonPlan) -> None:
        rows = tbl.rows

        # ---- 前置归一化：清掉父本遗留的孤立「S」标记 ----
        # 父本个别化表的合并痕迹会在某些行的第 0 格留下一个孤立的 `S`，
        # 它会让 `_row_label()` 判不出栏目名（教学过程行因此被跳过）。
        # 这里先统一归一成「教学过程」，后续定位才可靠。
        for row in tbl.rows:
            c0 = _cell_at(row, 0)
            if c0 is not None and _norm(c0.text) in ("S", "s"):
                _set_cell_text(c0, PROC_LABEL)

        # ---- 行0/行1：活动班级信息 ----
        self._fill_class_header(rows, plan)

        # ---- 有益经验（行2）----
        self._set_row_by_label(tbl, "有益经验", plan.class_objectives_text)

        # ---- 材料准备（行3）----
        self._set_row_by_label(tbl, "材料准备", "\n".join(
            f"{i}.{m}" for i, m in enumerate(plan.materials, start=1)))

        # ---- 教学过程：从 PROC_FIRST_ROW 到「活动延伸」前的最后一行 ----
        proc_text = self._build_procedure_text(plan)
        self._fill_procedure(tbl, proc_text)

        # ---- 个别化差异目标表头 + 匿名占位行 ----
        self._fill_individualized(tbl, plan, proc_text)

        # ---- 尾部：活动延伸 / 活动反思 ----
        self._set_row_by_label(tbl, "活动延伸", plan.extension)
        self._set_row_by_label(tbl, "活动反思",
                               plan.reflection_slots or "（留白待填）")

    def _fill_class_header(self, rows, plan: LessonPlan) -> None:
        """行1 是班级信息行：名称 / 年龄范围 / 人数 / 障碍类别。

        「障碍类别」一格按**该班日期最新那份范例**的写法填（`disability_label`）。
        用户本轮明确要求：「障碍类别范例里面不是有每个班的吗，也要填」——
        范例 14 份这一格全都有值，所以照填才与范例一致。
        该栏目名本身是模板固有结构（在 `check_docx_compliance` 的 struct_ok
        白名单里），描述的是**班级构成**、不指向任何具体幼儿；无值时才留空。
        行0 的「轻度/中度/重度/多重障碍」表头是模板固定栏目，原样保留。
        """
        if len(rows) < 2:
            return
        row = rows[1]
        # 逻辑列 1/2/3 = 名称 / 年龄范围 / 人数
        for col, val in ((1, self.class_label or plan.class_label),
                         (2, self.age_range or plan.age_range),
                         (3, str(self.class_size))):
            c = _cell_by_logical_col(row, col)
            if c is not None:
                _set_cell_text(c, val)
        # 逻辑列 4 / 5 / 6 / 8 = 轻度 / 中度 / 重度 / 多重障碍
        #   ⚠ 必须按**逻辑列**写，不能按 tc 下标：r0 里「重度」gridSpan=2
        #     （占列 6~7）、「多重障碍」gridSpan=3（占列 8~10）、
        #     「障碍类别」gridSpan=5（占列 11~15），列与格不是一一对应。
        #   ⚠ 填的是**范例的写法**（实测 14 份范例这四格全是 `★`），
        #     含义是「本班包含这些程度构成」，属模板固有栏目、非个体诊断
        #     （四个栏目名都在 `check_docx_compliance` 的 struct_ok 白名单里）。
        for col in (4, 5, 6, 8):
            c = _cell_by_logical_col(row, col)
            if c is not None:
                _set_cell_text(c, self.severity_mark)
        # 逻辑列 11..15 = 「障碍类别」—— 按范例填（用户本轮要求）。
        #   ⚠ 这**一个** tc 的 gridSpan=5 覆盖列 11~15，所以只写列 11 一次；
        #     若逐列写会把同一个格重复写入 5 次。
        if self.disability_label:
            c = _cell_by_logical_col(row, 11)
            if c is not None:
                _set_cell_text(c, self.disability_label)
        else:
            for col in range(11, 17):
                c = _cell_by_logical_col(row, col)
                if c is not None:
                    _set_cell_text(c, "")
        # 行0 第 0 格带有一个孤立的「S」标记（父本遗留的合并痕迹），一并清掉
        c00 = _cell_by_logical_col(rows[0], 0)
        if c00 is not None and _norm(c00.text) in ("S", "s"):
            _set_cell_text(c00, "活动班级")

    def _set_row_by_label(self, tbl, label: str, text: str,
                          col: int = 1) -> bool:
        """按行首栏目名找到行，把该行**逻辑列 col** 设成 text。"""
        target = _norm(label)
        for row in tbl.rows:
            if _row_label(row) == target:
                c = _cell_by_logical_col(row, col)
                if c is not None:
                    _set_cell_text(c, text)
                return True
        return False

    # 教学过程的四个固定小标题（**必须原样保留，只换标题下的正文**）
    #
    # ⚠ 第 1 段**不带序号**，这是范例的写法，不是笔误。
    #   实测语料 14/14 份教案的第一段标题都是裸的「师生互动」，紧跟着的
    #   第 2 段却是「二、热身活动」（少数写「热身活动」/「二.热身活动」）。
    #   也就是说范例原文就是「首段不编号、后续从 二 起编号」。
    #   早期版本写死成 `一、师生互动`，比范例**多出一个「一、」**，
    #   用户对照范例后指出该差异 —— 这里按范例口径去掉。
    #   （若要恢复带序号写法，只改这一行即可，其余逻辑与序号无关。）
    PROC_SECTIONS = ("师生互动", "二、热身活动",
                     "三、主题课活动", "四、活动结束")

    def _build_procedure_text(self, plan: LessonPlan) -> str:
        """按范例口吻拼「教学过程」正文。

        ⚠ 范例的教学过程是**四个固定小标题**结构，标题本身不能动，
        只替换标题下面的正文：

            一、师生互动      ← 师生问好 + 点名互动
            二、热身活动      ← 手指律动 + 音乐律动
            三、主题课活动    ← (一)导入 (二)… (三)… (四)…
            四、活动结束      ← 教师总结

        因此这里不再按 `plan.steps` 平铺，而是把 steps 归到四段里。
        归属靠 `step.name` 与段落关键字的匹配（见 `_section_of`）。
        """
        buckets = {s: [] for s in self.PROC_SECTIONS}
        for st in plan.steps:
            buckets[self._section_of(st.name)].append(st)

        blocks: List[str] = []
        for sec in self.PROC_SECTIONS:
            blocks.append(sec)
            for st in buckets[sec]:
                blocks.append(st.teacher_script)
                if st.child_action:
                    blocks.append(f"幼：{st.child_action}")
                if st.visual_cue:
                    # ⚠ 避免「提示：提示：…」重复前缀 —— 语料里 visual_cue
                    # 本身常以「提示：」开头，此时直接用原文。
                    vc = st.visual_cue
                    blocks.append(vc if vc.startswith("提示：") else f"提示：{vc}")
                for sc in st.scaffolds:
                    blocks.append(f"支持：{sc}")
            # 没有任何 step 落到这一段时，也要保留标题本身
            if not buckets[sec]:
                blocks.append("（本环节内容由教师依本班实际情况组织）")
        return "\n".join(blocks)

    @classmethod
    def _section_of(cls, step_name: str) -> str:
        """把单个教学步骤归到四个固定段落之一。"""
        n = _norm(step_name)
        # 三、主题课活动：导入 / 认识 / 讨论 / 操作 / 游戏 等主体环节
        if any(k in n for k in ("导入", "认识", "了解", "讨论", "分享", "操作",
                                "游戏", "制作", "练习", "巩固", "主题",
                                "提问", "观察", "欣赏")):
            return cls.PROC_SECTIONS[2]
        # 四、活动结束：总结 / 结束 / 再见
        if any(k in n for k in ("总结", "结束", "再见", "评价", "回顾", "收尾")):
            return cls.PROC_SECTIONS[3]
        # 二、热身活动：律动 / 热身 / 手指游戏 / 儿歌
        if any(k in n for k in ("律动", "热身", "手指", "儿歌", "音乐", "唱",
                                "操", "模仿动作")):
            return cls.PROC_SECTIONS[1]
        # 一、师生互动：问好 / 点名 / 集合 / 入场
        if any(k in n for k in ("问好", "点名", "互动", "集合", "入场",
                                "坐好", "常规")):
            return cls.PROC_SECTIONS[0]
        # 兜底：主体环节最容易承载任意活动内容
        return cls.PROC_SECTIONS[2]

    def _fill_procedure(self, tbl, proc_text: str) -> None:
        """把「教学过程」正文填进该栏目行第 1 逻辑列。

        ⚠ 两个坑，都在父本实测踩到过：
          1. **行号不能写死**（早期写 `PROC_FIRST_ROW = 4`，把个别化表头
             行也当成教学过程行覆盖掉）。改为按行首栏目名定位。
          2. **不能用 `row.cells[1]` 取格**。父本该栏目第 0 格 `gridSpan=6`，
             `row.cells` 展开后 `cells[1]` 仍指向第 0 格那个合并单元格，
             正文应写到逻辑列 1（即第 1 个 `<w:tc>`）。已改用
             `_cell_by_logical_col()`。

        父本里「教学过程」栏目跨多行，第 0 格在每行都重复，
        但正文格只有第一行是真内容 —— 所以只写第一行，其余清空。
        """
        targets = [r for r in tbl.rows if _row_label(r) == PROC_LABEL]
        if not targets:
            return
        for i, row in enumerate(targets):
            c = _cell_by_logical_col(row, 1)
            if c is not None:
                _set_cell_text(c, proc_text if i == 0 else "")

    # ---------- 个别化差异目标 ----------

    def _fill_individualized(self, tbl, plan: LessonPlan,
                             proc_text: str) -> None:
        """填表头（原样保留）+ 匿名占位行。

        匿名行的「康复目标」列写分层通用目标，**不引用真实编号**；
        五次评量列留空，交给老师手填。

        行数控制：先把「数据行区间」算出来，然后在该区间内**就地增删**。
        增行时始终以区间内最后一行 tr 为克隆模板，插在它后面，
        避免索引漂移（早期版本边插边按 index 取 rows，导致列表错位）。
        """
        rows = list(tbl.rows)
        # 定位表头行：含「儿童姓名」的行。父本里表头**跨两行**（r4 是
        # 「个别化差异目标/儿童姓名/康复目标/康复基线/评量效果」，
        # r5 是「…/第一次/第二次/第三次/第四次/第五次」），
        # 两行的「儿童姓名」格都命中，所以取**最后一个**作为表头末行，
        # 数据行从它之后开始。
        head_idxs = [i for i, row in enumerate(rows)
                     if any("儿童姓名" in c.text for c in row.cells)]
        if not head_idxs:
            return
        head_last = head_idxs[-1]        # 表头末行（含「第一次…第五次」那行）
        head_first = head_idxs[0]        # 表头首行

        # 数据行区间：head_last+1 … 遇到「活动延伸/反思」为止
        tail_i = len(rows)
        for i in range(head_last + 1, len(rows)):
            if _row_label(rows[i]) in ("活动延伸", "活动反思"):
                tail_i = i
                break
        data_rows = rows[head_last + 1:tail_i]
        if not data_rows:
            return

        entries = self._anon_entries(plan)
        anchor_tr = data_rows[0]._tr                 # 克隆模板
        need = len(entries)
        have = len(data_rows)

        # 增行：插到区间末尾（tail 行之前）
        if need > have:
            tail_el = rows[tail_i]._tr if tail_i < len(rows) else None
            for _ in range(need - have):
                new_tr = copy.deepcopy(anchor_tr)
                if tail_el is not None:
                    tail_el.addprevious(new_tr)
                else:
                    tbl._tbl.append(new_tr)
            rows = list(tbl.rows)
            data_rows = rows[head_last + 1:head_last + 1 + need]

        # 减行：把多余的删掉
        elif need < have:
            for r in data_rows[need:]:
                r._tr.getparent().remove(r._tr)
            rows = list(tbl.rows)
            data_rows = rows[head_last + 1:head_last + 1 + need]

        # 填内容（用逻辑列取格，规避 gridSpan 导致的 cells 展开错位）
        #
        # ⚠ 「个别化差异目标」栏的写法（实测范例结构）：
        #   范例里这一栏是**本课的目标条文总表**（1. 2. 3. … 逐条列出），
        #   并且**只在第一个儿童行写一次**，其余行是纵向合并的空白 ——
        #   含义是「全班共用同一份目标池」。
        #   早期版本误把「该层选中的条文拼接」写进去，与范例不符。
        for idx, row in enumerate(data_rows):
            if idx >= len(entries):
                for col in range(COL_IND_GOAL, COL_EVAL1 + 5):
                    c = _cell_by_logical_col(row, col)
                    if c is not None:
                        _set_cell_text(c, "")
                continue
            e = entries[idx]
            for col, val in ((COL_IND_GOAL, e["goal"]),
                             (COL_CHILD, e["child"]),
                             (COL_TARGET, e["target"]),
                             (COL_BASELINE, e["baseline"])):
                c = _cell_by_logical_col(row, col)
                if c is not None:
                    _set_cell_text(c, val)
            for col in range(COL_EVAL1, COL_EVAL1 + 5):
                c = _cell_by_logical_col(row, col)
                if c is not None:
                    _set_cell_text(c, "")
            # 个别化差异目标列在父本里偶尔残留孤立的「S」标记，一并清掉
            cg = _cell_by_logical_col(row, COL_IND_GOAL)
            if cg is not None and _norm(cg.text) in ("S", "s"):
                _set_cell_text(cg, "")
            # 逻辑列 0（「教学过程」竖排栏）也可能残留孤立「S」，
            # 它会破坏 `_row_label()` 的栏目名判定，必须归一回去
            c0 = _cell_at(row, 0)
            if c0 is not None and _norm(c0.text) in ("S", "s"):
                _set_cell_text(c0, PROC_LABEL)

        # ---------- 区域规整：独立等高格 ----------
        # 用户要求：儿童姓名往右 8 格（儿童姓名/康复目标/康复基线/第一次~第五次）
        # × 向下到「活动延伸」之前的所有行（表头 r4 起 … 最后一个数据行），
        # 全部呈现为**一个个等高的独立格子**；空的就是空的，不合并、不改格高。
        #
        # 背景：父本数据区里存在 h=99 / h=90 / h=184 的行，Word 按 auto
        # 规则会把它们压得近零高 —— 实测导致 S05/S08/S12 三个幼儿在阅读
        # 器里"看不见"，用户因此以为只列了 11 人。统一行高即修复该现象。
        #
        # 行高取「本区域现有各行行高的中位数」，既不突兀、也保证能容纳文字。
        all_rows = list(tbl.rows)
        # tail_i 是按增删前的行列表算的，这里按最新行表重算一次，
        # 避免"活动延伸"行位置漂移后越界
        tail_now = len(all_rows)
        for i in range(head_last + 1, len(all_rows)):
            if _row_label(all_rows[i]) in ("活动延伸", "活动反思"):
                tail_now = i
                break
        heights = []
        for ri in range(head_first, min(tail_now, len(all_rows))):
            trPr = all_rows[ri]._tr.find(qn("w:trPr"))
            if trPr is None:
                continue
            h = trPr.find(qn("w:trHeight"))
            if h is not None and h.get(qn("w:val")):
                heights.append(int(h.get(qn("w:val"))))
        # 统一行高用固定值（见 DATA_ROW_TWIPS 的说明）；heights 只用于日志
        row_h = DATA_ROW_TWIPS
        normalize_region(tbl, head_first, tail_now - 1,
                         COL_CHILD, COL_EVAL1 + 4, row_h)

        # ---------- 表头两行的纵向合并（对齐范例） ----------
        # 用户本轮要求：「将每个『儿童姓名』与其下方单独一行的空格合并为
        # 同一单元格，康复目标和康复基线两列做同样的合并处理」。
        # `normalize_region` 刚把表头两行也拆成了独立格（上格有字、下格空白），
        # 这里按范例重新合回去：上格 restart + 下格续行。
        merge_header_cols(tbl, head_first, head_last,
                          (COL_CHILD, COL_TARGET, COL_BASELINE))

        # ---------- 「个别化差异目标」列：合并成整块 ----------
        # 范例里这一格是**全班共用的目标池**，只在第一个幼儿行写一次，
        # 向下一直合并到最后一个幼儿行（各份范例 r6 皆为 restart、其后全为
        # 续行）。本项目 `normalize_region` 只动逻辑列 8~15，不碰第 7 列，
        # 所以第 7 列一直沿用父本的合并痕迹 —— 父本只有 15 个幼儿，
        # 扩到 37 行后「补出来的行」带着 restart，中途会断开。
        # 这里统一重打：首行 restart、其余续行。
        unify_column_vmerge(all_rows[head_last + 1:tail_now],
                            COL_IND_GOAL, keep_text_row=0)

    def _anon_entries(self, plan: LessonPlan) -> List[Dict[str, str]]:
        """按**分层结果**生成匿名行。

        分层依据（对应用户要求「基于范例中的个别化差异目标判定康复目标的组合与分层」）：
        - 阶梯来自范例教案的「个别化差异目标」条文（`ladder_spec`）；
        - 每个匿名儿童的层级由能力画像（支持层级 + 达标率）判定；
        - 该儿童的「康复目标」列 = 其所在层级在阶梯上选出的**条文序号组合**
          （如 `1、3、5`），这正是范例里该列的写法。

        匿名行的其他列：
        - 儿童姓名列 = 匿名编号 S01/S02…（不含姓名、不含诊断）
        - 个别化差异目标列 = 该层的群体目标描述
        - 康复基线列 = **上一教案同一位幼儿**的评量结果（跨教案传递，见
          `_baseline_for`）。⚠ 早期版本按「基线属个体评量结果、不得搬入教案」
          留空，实测 189 条范例记录**空值 0 条**，该判断是错的。
        - 五次评量列 = 留空（待老师手填）
        """
        from .layering import (build_ladder, assign_layers, pick_goal_indices,
                               layer_from_mastery, layer_from_support,
                               layer_levels, layer_combos)

        # ---- 1) 构造本课阶梯 ----
        if self.ladder_spec:
            ladder = build_ladder(self.ladder_spec)
        else:
            # 退化：把 plan.objectives 当作阶梯条文
            spec = []
            for i, o in enumerate(plan.objectives, start=1):
                spec.append((str(i), o.text))
            ladder = build_ladder(spec)

        # ---- 2) 判定每个儿童的层级 ----
        if self.children:
            layers = assign_layers(self.children, ladder, plan.main_domain)
            # 与人数对齐：不足则按默认层补齐，多余则截断
            _levels = layer_levels()
            _mid = _levels[min(1, len(_levels) - 1)]
            while len(layers) < self.class_size:
                layers.append(ChildLayer(
                    anon_id=f"{self.child_prefix}{len(layers)+1:02d}",
                    layer=_mid, goal_indices=pick_goal_indices(ladder, _mid),
                    basis=f"无独立画像数据，取 L{_mid} 为默认"))
            layers = layers[:self.class_size]
        else:
            # 无画像：按人数均分到各层（层数不写死，取 `layer_levels()`）
            levels = layer_levels()
            for i in range(self.class_size):
                lyr = levels[i % len(levels)]
                layers.append(ChildLayer(
                    anon_id=f"{self.child_prefix}{i+1:02d}",
                    layer=lyr,
                    goal_indices=pick_goal_indices(ladder, lyr),
                    basis=f"无画像数据，按人数均分到 {len(levels)} 层"))

        # ---- 2b) 个别化延续：按幼儿**本人最近一次记录**定层与目标 ----
        #
        # 用户要求（本轮）：分层**不固定 3 层** —— 按语料里「康复目标的组合
        # 规律」分层，有几条规律就分几层（现为 4 层，见
        # `data/structured/layer_tiers.json`）。
        #
        #   层级 ← 该幼儿最近一次记录里的组合落在哪条规律上（`combo_tier`）
        #   目标 ← 本课阶梯上**该层的规律实例**（跨课复用规律、换课换条文）
        #
        # `run_render` 已按调层规则算好 layer / goal / baseline
        # （`lpa.layering.next_state`：评量 → 层级 → 组合 → 基线），这里直接采纳；
        # 没有历史记录的幼儿才回落到分层算法（画像判定）。
        combos = layer_combos(ladder)
        overrides: List[Dict[str, str]] = []
        n_from_record = 0
        for cl in layers:
            rec = (self.child_records or {}).get(cl.anon_id) or {}
            lvl = rec.get("layer")
            if lvl is not None and int(lvl) in combos:
                # 层级由 `lesson_flow.targets_for` 按「组合规律」算出：
                #   最近一次可解释的记录 → 组合落在哪条规律 → L
                #   → 上一份五次评量调层 → 本课该层的组合实例
                cl.layer = int(lvl)
                cl.goal_indices = list(combos[int(lvl)])
                n_from_record += 1
            # ⚠ 判定依据必须与层级**同源**：曾出现「依据写 L4、层级却是 L1」
            #   （依据来自画像、层级来自组合规律，两套口径打架）。
            #   凡 planner 给了说明（组合规律口径）就用它；
            #   lvl 为 None 的幼儿（历史组合无法解释 / 缺勤且无阶梯）保留
            #   `assign_layers` 的**画像判定**依据，两处同样自洽。
            if rec.get("basis"):
                cl.basis = rec["basis"]
            overrides.append(rec)
        self._n_from_record = n_from_record

        # 统一编号，避免输入里带真实序号。
        # ⚠ 按班拆分（`render_multi`）时**不能重编**：若每班各自从 S01 起编，
        #   两个班会撞号（火箭班的 S01 与闪电班的 S01 指两个不同幼儿），
        #   附录里的「儿童代号」也就失去唯一性。此时保持画像里的**全局编号**
        #   （S01…S37），班级由分组决定。
        if self.renumber:
            for i, cl in enumerate(layers, start=1):
                cl.anon_id = f"{self.child_prefix}{i:02d}"
        self._layers = layers
        self._overrides = overrides

        # ---- 3) 生成表行 ----
        # 「个别化差异目标」栏只在**第一个儿童行**写一次完整目标清单，
        # 其余行留空（范例里这几格是纵向合并的）。
        goal_text = self._layer_goal_text(ladder, layers[0]) if layers else ""
        out: List[Dict[str, str]] = []
        for pos, cl in enumerate(layers):
            out.append({
                # 只在第一行写目标清单，其余置空
                "goal": goal_text if pos == 0 else "",
                "child": cl.anon_id,
                # ⚠ 范例里这一列是**连写**的数字串（如 `1346`），
                # 不是带顿号的 `1、3、4、6`。务必保持一致。
                "target": "".join(str(n) for n in cl.goal_indices),
                # 康复基线 = 该幼儿最近一次记录的评量结果（跨教案传递，
                # 整轮未参与则写「未参与」；见 `_baseline_for`）
                "baseline": self._baseline_for(pos),
            })
        return out

    def _baseline_for(self, pos: int) -> str:
        """取该幼儿的「康复基线」—— **上一教案的同一位幼儿**的评量结果。

        ## 为什么不是空

        实测 14 份范例、**189 条**儿童记录，`康复基线` 列**空值 0 条**。
        它记的是"这个孩子接手本课时处在什么水平"，正好和右边的
        「评量效果」（"上完本课后到了什么水平"）构成一对前后对照。

        而且它是**跨教案传递**的：同一幼儿在连续几份教案里是同一个串。
        27 名出现 ≥2 次的幼儿中，12 名的基线串**一字不差**地保持一致：

            卢炜彬  1245 → `P  P PP`   （快乐六一 / 童心向党 / 浓浓端午节 / 清凉夏日）
            杨弘毅  123  → `P  EE`     （同上 5 份）
            朱品合  134  → `P   PP`    （劳动最快乐 / 动物的家 / 浓浓端午节 / 能干的我）

        所以生成新教案时不能留空，要从**前一教案**里把这一列取过来。

        ## 取法与兜底

        1. `child_records`（该幼儿**本人最近一次记录**，见
           `scripts/roster_from_corpus.py`）→ 首选；
        2. `prev_baselines`（上一教案按名单对齐的值）→ 次选；
        3. 都取不到 → 按该幼儿所属层级推一个与该层支持档一致的串
           （`P` = 达标、`E` = 未达标；下层 E 多、上层 P 多），
           附录页会说明这是推演值、供老师替换。

        取到值后统一过一遍 `fit_baseline`，把**记号个数**调到本课该幼儿的
        目标条数（实测 178/178 条记录「记号个数 == 目标编号个数」）。

        ## 「未参与」

        整轮未参与的幼儿，这一列写 `未参与`（用户的统一要求）。范例里确实
        这么写 —— 《第三周-浓浓端午节》《第四周-清凉夏日》的蒙智宇五次评量
        全为「未参与」，基线格也就写着 `未参与`。`fit_baseline` 对不含 P/E
        记号的串原样放行，不会被清空。
        """
        layers = getattr(self, "_layers", None)
        n = 3
        layer = 2
        if layers and pos < len(layers):
            cl = layers[pos]
            n = max(3, min(4, len(cl.goal_indices)))
            layer = cl.layer

        # 0) ⭐ 个别化延续值 —— 该幼儿**本人最近一次记录**的基线。
        #    与 `康复目标` 同源（同一份最近记录），所以记号个数天然对齐；
        #    `未参与` 会被 fit_baseline 原样放行（它不含 P/E 记号）。
        ov = getattr(self, "_overrides", None)
        if ov and pos < len(ov):
            v = (ov[pos] or {}).get("baseline", "")
            if v:
                return fit_baseline(v, n, layer)

        # 1) 次选：上一教案的传递值（记号个数按本课目标条数校正）
        if self.prev_baselines and pos < len(self.prev_baselines):
            v = (self.prev_baselines[pos] or "").strip()
            if v:
                return fit_baseline(v, n, layer)

        # 2) 退化：按层级推演（与支持档一致的达标倾向）
        if not layers or pos >= len(layers):
            return ""
        # 层越高、P 越多：L1 约 1/3 达标，L2 约 2/3，L3 基本达标
        p_ratio = {1: 0.34, 2: 0.67, 3: 0.95}.get(layer, 0.5)
        n_p = max(1, min(n, round(n * p_ratio)))
        return "P" * n_p + "E" * (n - n_p)

    def _layer_goal_text(self, ladder, cl: ChildLayer) -> str:
        """「个别化差异目标」栏的内容 —— **本课的目标条文总表**。

        这一栏是逐条编号的目标清单，例如

            1.能够在课堂上安坐不走出位置
            2.能在全辅助下指认国旗。
            3.能在少量辅助下指认国旗。
            4.能自己独立完成指认国旗。
            5.能在老师的辅助下看图表达出国旗的颜色，五角星的颜色。
            6.能自己主动是爱国。

        它是**全班共用的一份目标池**，与具体层级无关（层级靠「康复目标」
        列的编号组合体现）。因此这里返回完整阶梯，而不是该层选中的条文。

        ⚠ 每一条都必须带「N.」编号，**包括第 1 条**。
        早期版本按部分范例的写法把第 1 条省掉编号，导致读者看到
        「能够在课堂上安坐不走出位置2.能在全辅助下…」这种**粘连文本**，
        误以为缺少小标号「1」。范例里两种写法都存在，但带编号更清晰、
        也与「康复目标」列引用序号一致，故统一带编号。
        """
        items = ladder.items
        if not items:
            return ""
        return "\n".join(f"{it.index}.{it.text}" for it in items)

    # ---------- 分层附录页 ----------

    def append_layer_appendix(self, doc, plan: LessonPlan,
                              evals_by_child: Optional[Dict[str, List[str]]] = None
                              ) -> None:
        """在教案后另起一页，输出「儿童代号 → 所属层级」对照。

        对应两条用户要求：
          · 分层结果**不直接出现在生成的教案中**，而是每份教案后另生成一页，
            标明各儿童代号所属层级；
          · 随时间推进，同一幼儿的分层应依据每次评量结果上调或下调一层，
            须体现该变化的调整机制。

        因此本页包含三块：
          1. 分层依据（阶梯条文 + 两轴说明）
          2. 儿童代号 → 层级 对照表（含初始层级与目标组合）
          3. 动态调层轨迹表（若有评量数据）
        """
        from .layering import (adjust_layer, parse_eval, LAYER_META, LAYER_SPECS,
                               layer_count, layer_levels, layer_combos,
                               pick_goal_indices)

        if not self._layers:
            return

        n_layers = layer_count()

        # 1) 分页（先记下分页前的 body 长度，末尾据此只刷附录页的字体）
        _apx_start = len(doc.element.body)
        doc.add_page_break()

        # 2) 标题
        h = doc.add_paragraph()
        r = h.add_run(f"附录：儿童分层对照表（{plan.title}）")
        r.bold = True
        r.font.size = Pt(14)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER

        # 3) 说明
        note = doc.add_paragraph()
        _add_rich_run(note, (
            f"说明：本页为教学管理用，不属于教案正文。共分 {n_layers} 个层级 —— "
            "层数不是预设的，而是**按范例教案里「康复目标」的组合规律归纳**出来的"
            "（有几种组合规律就分几层，见 §二）；层级依据范例教案的"
            "「个别化差异目标」条文序号组合判定；随每次评量结果，"
            "同一幼儿的层级可上/下调一层。"
), 10.5)

        # 4) 分层依据
        p = doc.add_paragraph()
        p.add_run("一、分层依据（目标阶梯）").bold = True
        if self.ladder_spec:
            spec = self.ladder_spec
        else:
            spec = [(str(i), o.text)
                    for i, o in enumerate(plan.objectives, start=1)]
        from .layering import build_ladder
        ladder = build_ladder(spec)
        t1 = doc.add_table(rows=1, cols=3)
        for i, txt in enumerate(("目标序号", "目标条文", "轴位")):
            t1.rows[0].cells[i].text = txt
        for it in ladder.items:
            if it.is_routine:
                axis = "课堂常规"
            elif it in ladder.support_items():
                # support_rank 越大 = 所需支持越少；这里换算成「需支持档」，
                # 数值越小表示越需要支持，读起来更符合直觉。
                sup = ladder.support_items()
                n = len(sup)
                pos = sup.index(it) + 1        # 1 = 需支持最多
                axis = f"支持轴（需支持第 {pos}/{n} 档）"
            else:
                axis = "发展轴（难度进阶）"
            row = t1.add_row().cells
            row[0].text = str(it.index)
            row[1].text = it.text
            row[2].text = axis

        # 5) 层级 → 目标组合
        doc.add_paragraph()
        p = doc.add_paragraph()
        p.add_run(f"二、{n_layers} 个层级与目标组合").bold = True
        t2 = doc.add_table(rows=1, cols=5)
        for i, txt in enumerate(("层级", "定义", "所需支持", "组合规律",
                                 "本课目标组合")):
            t2.rows[0].cells[i].text = txt
        for L in layer_levels():
            idxs = pick_goal_indices(ladder, L)
            spec = next((s for s in LAYER_SPECS if s["level"] == L), {})
            row = t2.add_row().cells
            row[0].text = f"L{L}"
            row[1].text = LAYER_META[L]["name"]
            row[2].text = LAYER_META[L]["support"]
            row[3].text = "+".join(spec.get("pattern") or []) or "—"
            row[4].text = "、".join(str(i) for i in idxs) or "—"
        # ⚠ 上表是**机制**给出的基准组合；个别幼儿的实际组合按其本人最近一次
        #   记录延续（实测同一幼儿的目标组合跨教案高度稳定：陈映燊 1456 连续
        #   三份、卢炜彬 1245 连续四份）。两处不一致时以第三区块为准，
        #   否则读者会以为表里写错了。
        n2 = doc.add_paragraph()
        _add_rich_run(n2, (
            "注：层级不是预设的，而是**按范例教案里「康复目标」的组合规律"
            f"归纳**出来的 —— 13 份范例、170 条儿童记录里的组合只落在 "
            f"{n_layers} 种形态上（「组合规律」列）；各层的基线达标密度依次为"
            + "、".join(f" L{s['level']} {s.get('p_density')}"
                        for s in LAYER_SPECS)
            + "，单调递增，说明分层与实际能力一致。"
            "个别幼儿的实际组合按其最近一次评量记录延续（个别化连续性），"
            "因此可能与本表不同 —— 请以「三、儿童代号与所属层级」里的"
            "实际组合为准。"
), 10.5)

        # 6) 儿童代号 → 层级
        doc.add_paragraph()
        p = doc.add_paragraph()
        p.add_run("三、儿童代号与所属层级").bold = True
        t3 = doc.add_table(rows=1, cols=5)
        for i, txt in enumerate(("儿童代号", "所属层级", "层级名称",
                                 "本课目标组合（沿用本人最近记录）", "判定依据")):
            t3.rows[0].cells[i].text = txt
        # 按层级排序输出，读起来比原始顺序清楚
        for cl in sorted(self._layers, key=lambda c: (c.layer, c.anon_id)):
            row = t3.add_row().cells
            row[0].text = cl.anon_id
            row[1].text = f"L{cl.layer}"
            row[2].text = LAYER_META[cl.layer]["name"]
            row[3].text = "、".join(str(i) for i in cl.goal_indices)
            row[4].text = cl.basis
        # 分层统计
        cnt: Dict[int, int] = {}
        for cl in self._layers:
            cnt[cl.layer] = cnt.get(cl.layer, 0) + 1
        n_rec = getattr(self, "_n_from_record", 0)
        s = doc.add_paragraph()
        s.add_run(
            "层级分布：" + "，".join(
                f"L{L} {LAYER_META[L]['name']} {cnt.get(L, 0)} 名"
                for L in layer_levels())
            + f"；合计 {len(self._layers)} 名。"
        ).font.size = Pt(10.5)
        # ⚠ 定层有**两个来源**，不写明会被当成口径不一：
        #   ① 有可解释历史记录的 → 按「组合规律」定层（判定依据同理）
        #   ② 历史记录的组合无法在其所在课阶梯上解释（该课阶梯未从范例
        #      恢复好）→ 回落能力画像判定
        s2 = doc.add_paragraph()
        s2.add_run(
            f"定层来源：{n_rec} 名按「本人最近一次可解释记录的组合规律」定层，"
            f"{len(self._layers) - n_rec} 名因该记录所在教案的阶梯无法从范例"
            "恢复，改按能力画像判定（判定依据列已逐条注明各自来源）。"
        ).font.size = Pt(10.5)

        # 7) 动态调层轨迹
        doc.add_paragraph()
        p = doc.add_paragraph()
        p.add_run("四、随评量结果的层级调整机制").bold = True
        p2 = doc.add_paragraph()
        _add_rich_run(p2, (
            "规则：**连续两次**评量达标率 ≥ 85% → 上调一层；"
            "某次评量达标率 < 40% → 下调一层；"
            "「未参与」不计入分母、也不触发调整。"
            f"层级上限 L{n_layers}、下限 L1。"
            "上调后**康复目标**换成新层的组合规律；**康复基线**里"
            "保留下来的目标沿用原记号，**辅助下达成的目标记 E（E=辅助）**"
            "—— 因为它们还没评估过。"
), 10.5)
        p3 = doc.add_paragraph()
        p3.add_run(
            "下表用各层级的能力画像推演五次评量的走向，演示该机制如何工作；"
            "实际使用时由老师在「评量效果」五列记录 P（达标）/ E（未达标），"
            "系统据此重算下一轮层级。"
        ).font.size = Pt(10.5)

        doc.add_paragraph()
        t4 = doc.add_table(rows=1, cols=7)
        for i, txt in enumerate(("儿童代号", "初始层级", "第1次", "第2次",
                                 "第3次", "第4次", "第5次")):
            t4.rows[0].cells[i].text = txt
        ev = evals_by_child or {}
        for cl in sorted(self._layers, key=lambda c: (c.layer, c.anon_id)):
            raw = ev.get(cl.anon_id) or []
            # 无真实评量数据时，按该层级的能力画像推演一组演示序列，
            # 这样附录页能真正展示「调层机制」如何随时间起作用。
            if not raw:
                raw = _simulate_evals(cl)
                cl.history = []
                _, cl.history = adjust_layer(cl.layer, raw, n_layers=n_layers)
            hist = {h["round"]: h for h in cl.history}
            row = t4.add_row().cells
            row[0].text = cl.anon_id
            row[1].text = f"L{cl.layer}"
            for k in range(1, 6):
                if k <= len(raw):
                    e = parse_eval(raw[k - 1])
                    hh = hist.get(k)
                    if e is None:
                        row[k + 1].text = "未参与"
                    else:
                        tag = ""
                        if hh and hh.get("action") == "up":
                            tag = " ↑"
                        elif hh and hh.get("action") == "down":
                            tag = " ↓"
                        row[k + 1].text = (f"{raw[k-1]}{tag} "
                                           f"（{e.passed}/{e.total}）"
                                           f" L{hh['layer']}" if hh else raw[k - 1])
                else:
                    row[k + 1].text = "·"

        # 8) 按班级拆分（用户本轮要求）
        t5 = None
        if self._layer_groups:
            doc.add_paragraph()
            p = doc.add_paragraph()
            p.add_run("五、按班级拆分与儿童姓名代号").bold = True
            p5 = doc.add_paragraph()
            _add_rich_run(p5, (
                "每个班各出一份**独立的教案文件**；各班的班级信息项"
                "（年龄范围 / 人数 / 轻度 / 中度 / 重度 / 多重障碍 / 障碍类别）"
                "按**该班最新日期的那份范例**填写，人数取本课名册里该班的实际人数。"
                "「障碍类别」一栏照范例填写该班的障碍名称与类别 —— "
                "该栏目是模板固有结构（描述班级构成，不指向任何具体幼儿）。"
), 10.5)
            t5 = doc.add_table(rows=1, cols=7)
            for i, txt in enumerate(("班级", "儿童姓名代号", "年龄范围", "人数",
                                     "轻度/中度/重度/多重障碍", "障碍类别",
                                     "信息项来源（该班最新范例）")):
                t5.rows[0].cells[i].text = txt
            for grp, ls in self._layer_groups:
                row = t5.add_row().cells
                row[0].text = grp.get("label", "")
                row[1].text = "、".join(sorted(cl.anon_id for cl in ls))
                row[2].text = grp.get("age_range", "")
                row[3].text = str(grp.get("size", ""))
                mark = grp.get("severity_mark") or "★"
                row[4].text = " / ".join([mark] * 4)
                row[5].text = grp.get("disability_label") or "—"
                row[6].text = grp.get("source", "")

        # 表格样式必须**在行填完之后**统一应用，
        # 否则之后 add_row 新增的单元格拿不到字体设置。
        for _t in (t1, t2, t3, t4, t5):
            if _t is not None:
                _style_appendix_table(_t)

        # 附录整页的字体档与范例对齐（表格 + 说明段落一起刷）
        _restyle_appendix_fonts(doc, _apx_start)


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------


def render_plan(plan: LessonPlan, out_path, parent_path=None,
                class_size: int = 12, class_label: str = "") -> Path:
    r = DocxRenderer(parent_path=parent_path, class_size=class_size,
                     class_label=class_label)
    return r.render(plan, Path(out_path))


# ---------------------------------------------------------------------------
# 渲染后合规校验
# ---------------------------------------------------------------------------


def _iter_unique_cells(tbl):
    """遍历表格中每个物理单元格一次（合并单元格在 `row.cells` 里会重复出现）。

    ⚠ **不能用 `id(cell._tc)` 去重**。python-docx 的 `_Cell` 是**临时包装对象**，
    同一行循环结束后对象即被回收，下一行新建的 `_Cell` 可能拿到**相同的内存 id**，
    于是大量真实单元格被误判为"已见过"而跳过 —— 实测导致真实姓名全部漏检。
    正确做法：把 `cell._tc`（lxml 元素）本身放进集合，元素在文档树里存活，
    身份稳定，`is`/哈希都可靠。
    """
    seen = set()
    for row in tbl.rows:
        for c in row.cells:
            tc = c._tc
            if tc in seen:
                continue
            seen.add(tc)
            yield c


def docx_text(doc_path) -> str:
    """把 docx 的全部可见文本抽出来（含表格、去重合并单元格）。"""
    doc = Document(str(doc_path))
    chunks: List[str] = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for c in _iter_unique_cells(t):
            chunks.append(c.text)
    return "\n".join(chunks)


def check_docx_compliance(doc_path, forbidden_names: Optional[Sequence[str]] = None,
                          exempt_fragments: Optional[Sequence[str]] = None) -> List[str]:
    """检查**渲染后的 docx 文本**是否仍含标签词/真实姓名。

    为什么必须有这一步：`rules.validate_plan()` 只看 LessonPlan 对象，
    而模板里继承来的固有栏目（如「障碍类别」列）根本不经过 LessonPlan。
    实测父本该格写着「智力障碍、自闭症 ★」，继承后直接进入成品 ——
    这就是"先生成文本再校验"和"先生成模板再填内容"两条路线的盲区。

    判据：
      1. **标签词**（BANNED_TERMS 黑名单），**先扣除 `exempt_fragments`**
      2. **真实姓名精确比对**（name_mapping.json 的 37 个姓名）——最可靠
      3. **单元格级人名结构**——docx 表格里每个姓名独占一格，
         不像文本里用顿号分隔，所以必须**逐格**用姓氏+1~2 字判据扫，
         否则漏检（早期版本只扫拼接后的全文，实测漏掉几十个真实姓名）。
      4. **`exempt_fragments`**：模板固有栏目的**规定内容**。造册时最典型的是
         「障碍类别」一格 —— 范例 14 份都有值，用户明确要求照范例填。
         这些整串会先从待检文本里剔除；别处再出现同样的词仍会被判违规。
    """
    from .rules import BANNED_TERMS, _SINGLE_NAME

    doc = Document(str(doc_path))
    bad: List[str] = []

    # ---- 1. 标签词（全文，扣除 exempt_fragments） ----
    #   `exempt_fragments` = 模板固有栏目的**规定内容**。造册时最典型的是
    #   「障碍类别」一格：范例 14 份这一格都有值，用户本轮明确要求照范例填。
    #   先整串剔除再扫；**别处再出现同样的词仍然判违规**。
    text = docx_text(doc_path)
    for frag in (exempt_fragments or ()):
        if frag:
            text = text.replace(frag, "")
    for term in BANNED_TERMS:
        if term in text:
            bad.append(f"标签词泄漏：{term}")

    # ---- 2. 真实姓名精确比对（全文） ----
    if forbidden_names:
        for nm in forbidden_names:
            if nm and nm in text:
                bad.append(f"真实姓名泄漏：{nm}")

    # ---- 3. 单元格级人名结构 ----
    # 模板固有栏目名白名单（含"轻/中/重度""多重障碍""活动班级"等结构词）
    struct_ok = {
        "活动班级", "名称", "年龄范围", "人数", "轻度", "中度", "重度",
        "多重障碍", "障碍类别", "有益经验", "材料准备", "教学过程",
        "个别化差异目标", "儿童姓名", "康复目标", "康复基线", "评量效果",
        "第一次", "第二次", "第三次", "第四次", "第五次",
        "活动延伸", "活动反思", "师生互动", "师生问好",
    }
    for t in doc.tables:
        for c in _iter_unique_cells(t):
            frag = _norm(c.text)
            if not frag or frag in struct_ok:
                continue
            if _SINGLE_NAME.match(frag):
                bad.append(f"单元格疑似人名：{frag}")

    return bad

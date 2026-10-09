"""交付前终检：核对 docx 是否满足全部需求。

本轮起交付形态变了（用户澄清：「拆成两个教案，两个文件」）：
    · **每个班一个独立文件** —— `认知课《国庆节到了》-火箭班.docx`
      / `-闪电班.docx`；每个文件自成一体（大标题 → 任课老师/时间 →
      本班主表 → 本班附录），所以**每个文件只有 1 张主表**；
    · 主表的班级信息行按本班口径填（名称 / 年龄范围 / 人数 / ★×4 /
      **障碍类别**）；障碍类别本轮起**按范例填写**（用户要求），
      不再留空；
    · 大标题居中；
    · 个别化表头两行里「儿童姓名 / 康复目标 / 康复基线」三格纵向合并；
    · 附录 5 张表，只列本班的幼儿。

因此本脚本的判据同步更新，不再用「主表张数 == 班级数」这种合并版的判据。
"""
import json
import os
import sys
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import _Cell

ROOT = Path(__file__).resolve().parents[1]
ST = ROOT / "data" / "structured"
sys.path.insert(0, str(ROOT / "src"))

prof = json.loads((ST / "child_profiles.json").read_text(encoding="utf-8"))
if isinstance(prof, list):
    kids = prof
elif isinstance(prof, dict):
    kids = prof.get("children") or prof.get("items") or []
    if not isinstance(kids, list):
        kids = []
else:
    kids = []
names = set()
for ch in kids:
    if not isinstance(ch, dict):
        continue
    nm = (ch.get("name") or "").strip()
    if len(nm) >= 2:
        names.add(nm)
print(f"[审计] 儿童记录 {len(kids)} 条，姓名黑名单 {len(names)} 条")

# 违规词：诊断/障碍标签。
# ⚠ 「轻度/中度/重度/多重障碍/障碍类别」是**父本班级概况表的固有栏目名**，
# 描述的是班级构成，不指向任何儿童 —— 保留列名是与范例版式一致的必要结构。
# ⚠ 本轮起「障碍类别」一格**按范例填了值**（用户要求：「范例里面有每个班的，
#   也要填」）。该值只出现在**表头行**（主表 r1、附录分班表 r1），下面的
#   `b in header_blob and b not in 非表头文本` 判据会放行；
#   真正要拦的是把标签**写到某个孩子身上**（正文/名册行）。
BANNED = ["孤独症", "自闭", "自闭症", "智力障碍", "智障", "唐氏", "脑瘫",
          "听力障碍", "语言障碍", "多动", "ADHD", "残疾", "特殊儿童",
          "特殊需要", "IEP", "个别化教育计划", "诊断", "病症", "病情"]
HEADER_ONLY = {"轻度", "中度", "重度", "多重障碍", "障碍类别", "障碍"}

# 附录表数量：阶梯 / 层级组合 / 儿童代号 / 调层机制 / 按班级拆分 = 5
APX_TABLES = 5


def raw_cells(row):
    return [_Cell(tc, row._parent) for tc in row._tr.findall(qn("w:tc"))]


def vmerge_of(tc_el) -> str:
    """返回 '' / 'R'(restart) / 'c'(continue)。"""
    tcPr = tc_el.find(qn("w:tcPr"))
    if tcPr is None:
        return ""
    vm = tcPr.find(qn("w:vMerge"))
    if vm is None:
        return ""
    return "R" if (vm.get(qn("w:val")) or "") == "restart" else "c"


def audit(path: Path):
    d = Document(str(path))

    para_texts = [p.text for p in d.paragraphs]

    cell_texts = []
    header_texts = []
    for t in d.tables:
        for ri, row in enumerate(t.rows):
            for c in raw_cells(row):
                tx = c.text
                cell_texts.append(tx)
                if ri <= 1:
                    header_texts.append(tx)

    body_blob = "\n".join(para_texts + cell_texts)
    header_blob = "\n".join(header_texts)

    v_name = sorted({n for n in names if n in body_blob})
    v_banned = []
    for b in BANNED:
        if b not in body_blob:
            continue
        if b in header_blob and b not in "\n".join(
                tx for tx in cell_texts if tx not in header_texts):
            continue
        v_banned.append(b)

    # ---- 结构：第一行是大标题表 ----
    first = next((el for el in d.element.body), None)
    first_is_tbl = first is not None and first.tag == qn("w:tbl")

    # ---- 大标题居中（本轮要求）----
    # ⚠ 必须**两层都查**：段落 jc 只管格内文本；标题表只有一列、宽 3336 twips，
    #   若表格本身没居中（tblPr/jc 缺省且 tblInd 靠在左边），整体仍呈左对齐。
    title_tbls = [t for t in d.tables if len(t.rows) == 1 and len(t.columns) == 1]
    title_center = False
    title_tbl_center = False
    title_off = 0
    if title_tbls:
        _t = title_tbls[0]
        ps = _t.rows[0].cells[0].paragraphs
        para_ok = all(str(p.alignment) == "CENTER (1)" for p in ps if p.text.strip())

        _tblpr = _t._tbl.find(qn("w:tblPr"))
        _jc = _tblpr.find(qn("w:jc")) if _tblpr is not None else None
        jc_ok = _jc is not None and _jc.get(qn("w:val")) == "center"

        # 兜底：拿 tblInd 算真实占位，看表格中心是否落在版心中心
        sec = d.sections[0]
        usable_tw = int((sec.page_width - sec.left_margin - sec.right_margin) / 635)
        _tw = _tblpr.find(qn("w:tblW")) if _tblpr is not None else None
        try:
            tbl_w = int(_tw.get(qn("w:w"))) if _tw is not None else 0
        except (TypeError, ValueError):
            tbl_w = 0
        if tbl_w <= 0:
            _grid = _t._tbl.find(qn("w:tblGrid"))
            if _grid is not None:
                tbl_w = sum(int(g.get(qn("w:w")) or 0)
                            for g in _grid.findall(qn("w:gridCol")))
        _ind = _tblpr.find(qn("w:tblInd")) if _tblpr is not None else None
        try:
            ind_tw = int(_ind.get(qn("w:w"))) if _ind is not None else 0
        except (TypeError, ValueError):
            ind_tw = 0
        if tbl_w > 0 and tbl_w < usable_tw:
            title_off = (ind_tw + tbl_w // 2) - usable_tw // 2
            ind_ok = abs(title_off) <= 40          # 容差 40 twips ≈ 0.07cm
        else:
            ind_ok = True                          # 表宽本身铺满，无从偏移
        title_tbl_center = jc_ok or ind_ok
        title_center = para_ok and title_tbl_center

    meta_ps = [p.text for p in d.paragraphs
               if p.text.strip().startswith("任课老师")]
    meta_ok = bool(meta_ps) and "时间" in (meta_ps[0] if meta_ps else "")
    tab_ok = False
    for p in d.paragraphs:
        if p.text.strip().startswith("任课老师"):
            xml = p._element.xml
            tab_ok = "w:tabs" in xml and 'w:val="right"' in xml and \
                ("w:tab/" in xml or "w:tab " in xml)
            break

    # ---- 主表（认表头里的「活动班级」，不再按行数最大值）----
    mains = [t for t in d.tables
             if t.rows and "活动班级" in "".join(c.text for c in t.rows[0].cells)]

    # ---- 本课阶梯条数 ----
    n_items = 0
    from lpa.layering import layer_count
    n_layers = layer_count()
    for t in d.tables:
        hdr = [c.text.strip() for c in raw_cells(t.rows[0])] if t.rows else []
        if "目标序号" in hdr:
            n_items = len(t.rows) - 1
            break

    # ---- 逐行取「儿童代号 / 康复目标 / 康复基线」（跨所有主表）----
    rowset = []
    for main in mains:
        for ri in range(4, len(main.rows)):
            cs = raw_cells(main.rows[ri])
            if len(cs) > 5 and cs[3].text.strip().startswith("S") \
                    and cs[3].text.strip()[1:].isdigit():
                rowset.append((cs[3].text.strip(), cs[4].text.strip(),
                               cs[5].text.strip()))
    combos = {c for _, c, _ in rowset}
    ids = [i for i, _c, _b in rowset]

    bad_combo, bad_base, n_absent = [], [], 0
    for aid, combo, base in rowset:
        ds = [int(c) for c in combo if c.isdigit()]
        if not (3 <= len(ds) <= 4) or len(set(ds)) != len(ds) \
                or (n_items and max(ds) > n_items):
            bad_combo.append(f"{aid}:{combo}")
        if base == "未参与":
            n_absent += 1
        else:
            nm_marks = sum(1 for ch in base if ch in "PE")
            if not nm_marks or nm_marks != len(ds):
                bad_base.append(f"{aid}:{combo}/{base}")

    # ---- 表头三列纵向合并（本轮要求）----
    #   r4 为 restart、r5 为 continue：儿童姓名 / 康复目标 / 康复基线
    lm_cols = []                       # 合并检查明细
    for main in mains:
        mrows = main.rows
        head_i = next((i for i, r in enumerate(mrows)
                       if any("儿童姓名" in c.text for c in r.cells)), None)
        if head_i is None or head_i + 1 >= len(mrows):
            continue
        tc_t = mrows[head_i]._tr.findall(qn("w:tc"))
        tc_b = mrows[head_i + 1]._tr.findall(qn("w:tc"))
        for idx, label in ((3, "儿童姓名"), (4, "康复目标"), (5, "康复基线")):
            if idx >= len(tc_t) or idx >= len(tc_b):
                continue
            lm_cols.append(
                f"{label}:{vmerge_of(tc_t[idx])}{vmerge_of(tc_b[idx])}")
    merged_ok = bool(lm_cols) and all(v.endswith("Rc") for v in lm_cols)

    # ---- 教学过程首段标题**不得带序号** ----
    #   用户对照范例后指出：教学过程的「一、师生互动」比范例多了一个「一、」。
    #   实测语料 14/14 份的第一段标题都是裸的「师生互动」，紧跟着的下一段
    #   才出现「二、热身活动」。这里按换行取首行做严格比对，锁死该写法。
    proc_head = ""
    for main in mains:
        for row in main.rows:
            for tc in row._tr.findall(qn("w:tc")):
                blob = "".join(n.text or "" for n in tc.iter(qn("w:t")))
                if "师生互动" not in blob:
                    continue
                line = ""
                for node in tc.iter():
                    if node.tag == qn("w:t"):
                        line += node.text or ""
                    elif node.tag in (qn("w:br"), qn("w:cr")):
                        break
                proc_head = line.strip()
                break
            if proc_head:
                break
        if proc_head:
            break
    proc_head_ok = proc_head == "师生互动"

    # ---- 按班级拆分 ----
    class_rows = []
    for t in d.tables:
        hdr = [c.text.strip() for c in raw_cells(t.rows[0])] if t.rows else []
        if "儿童姓名代号" in hdr and "班级" in hdr:
            for r in t.rows[1:]:
                cs = [c.text.strip() for c in raw_cells(r)]
                class_rows.append(cs)
            break
    # ---- 单表回退形态（`--single` / `LPA_FORCE_SINGLE=1`）----
    # 标志：**没有**「按班级拆分」附录表。此时全班在一张表里、文件名不带班级、
    # 附录也少一张（4 张而非 5 张），下列 3 项判据都不适用，一律跳过。
    single_whole = not class_rows
    # 主表张数应等于「按班级拆分」表里的班级数（每文件 1 张 / 1 行）
    class_ok = True if single_whole else (bool(class_rows)
                                          and len(mains) == len(class_rows))
    # 本文件只应装本班幼儿：附录那一行的代号列表与本文件名册行应完全一致
    only_own_ok = single_whole
    if class_rows and rowset:
        codes = [c.strip() for c in
                 class_rows[0][1].replace("，", "、").split("、") if c.strip()]
        only_own_ok = set(codes) == set(ids)
    # 每个文件的表头信息行：名称 / 年龄范围 / 人数 / ★×4 / 障碍类别（按范例填）
    header_fill_ok = True
    header_labels = []
    for main in mains:
        cs = [c.text.strip() for c in raw_cells(main.rows[1])]
        if len(cs) < 9:
            header_fill_ok = False
            break
        if not (cs[1] and cs[2] and cs[3].isdigit() and cs[3] != "0"):
            header_fill_ok = False
        if any(cs[k] != "★" for k in (4, 5, 6, 7)):
            header_fill_ok = False
        # 障碍类别：本轮用户明确要求**照范例填**（不再留空）
        if not cs[8]:
            header_fill_ok = False
        header_labels.append(f"{cs[1]} | {cs[2]} | {cs[3]}人 | {cs[8]}")

    # 文件名里的班级必须与表里的班级一致（每个班一个独立文件）。
    # ⚠ 单表回退形态（`--single`）文件名不带班级，跳过本项 —— 早期版本在这里
    #   判 FAIL，导致 `build --single` 永远报错。
    if single_whole:
        name_ok = True
    else:
        name_ok = bool(header_labels) and all(
            lbl.split(" | ")[0] in path.stem for lbl in header_labels)

    # ---- 字体统一：西文字体不得被设成中文字体名 ----
    #  范例父本 docDefaults = Times New Roman + 宋体，正文 run 只写
    #  `hint="eastAsia"` 靠继承。历史 bug 是显式写了 `w:ascii="宋体"`
    #  却不写 `w:eastAsia` —— 等于把这个中文衬线字体当成「西文字体」用。
    #  注意：范例里确实有少量 `ascii=宋体 + eastAsia=宋体` 的格（整格全宋体，
    #  如 ★ 标记），那是父本原样，不算违规；只有「西文设中文、中文未声明」
    #  这种自相矛盾的组合才算。
    _CJK_FONTS = {"宋体", "黑体", "楷体", "仿宋", "微软雅黑", "等线", "华文中宋"}
    font_bad = []
    for _rf in d.element.body.iter(qn("w:rFonts")):
        _a = _rf.get(qn("w:ascii"))
        _e = _rf.get(qn("w:eastAsia"))
        if _a in _CJK_FONTS and not _e:
            font_bad.append(_a)

    # ---- 附录第三表：层级齐全 ----
    layers_present = set()
    for t in d.tables:
        hdr = [c.text.strip() for c in raw_cells(t.rows[0])] if t.rows else []
        if "所属层级" in hdr:
            for r in t.rows[1:]:
                cells = raw_cells(r)
                if len(cells) > 1:
                    layers_present.add(cells[1].text.strip())
            break

    apx_pages = len(d.tables) - 1 - len(mains)      # 除去标题表与各班主表
    apx_h = [p.text for p in d.paragraphs if p.text.startswith("附录：")]

    print(f"\n### {path.stem}")
    print(f"  第一行是大标题表  : {'PASS' if first_is_tbl else 'FAIL'}")
    print(f"  大标题居中        : {'PASS' if title_center else 'FAIL'}"
          f"{'' if title_center else f'（标题表偏离版心中线 {title_off} twips）'}"
          f"\n  标题表整体居中    : {'PASS' if title_tbl_center else 'FAIL'}"
          f"（tblPr/jc=center 或 tblInd 落在居中位；当前偏离 {title_off} twips）")
    print(f"  任课老师/时间同段 : {'PASS' if meta_ok else 'FAIL'}")
    print(f"  用制表位对齐      : {'PASS' if tab_ok else 'FAIL'}")
    print(f"  主表张数          : {len(mains)} 张（每文件应为 1 张）| "
          f"{len(mains[0].rows) if mains else 0} 行")
    print(f"  班级信息行填妥    : {'PASS' if header_fill_ok else 'FAIL'}"
          f"（名称/年龄范围/人数/★×4/障碍类别）")
    for lbl in header_labels:
        print(f"     → {lbl}")
    print(f"  文件名班级一致    : "
          f"{'PASS' if name_ok else 'FAIL'}"
          f"{'（单表形态，不适用）' if single_whole else ''}")
    print(f"  表头三列已合并    : {'PASS' if merged_ok else 'FAIL'} {lm_cols}")
    print(f"  名册行 / 阶梯条数 : {len(rowset)} 人 / {n_items} 条"
          f"（编号唯一 {len(set(ids))}）")
    print(f"  附录分班行 / 本班 : {len(class_rows)} 行"
          f"（{ '、'.join(r[0] for r in class_rows) }）"
          f"{'  ✅' if only_own_ok else '  ❌ 代号与名册不符'}")
    # 单表形态附录少一张（无「按班级拆分」表），不算违规
    want_apx = APX_TABLES - 1 if single_whole else APX_TABLES
    print(f"  附录表数量        : {apx_pages}（应为 {want_apx}"
          f"{'，单表形态' if single_whole else ''}）")
    print(f"  附录标题          : {apx_h[0] if apx_h else '缺失'}")
    print(f"  康复目标组合      : {len(combos)} 种"
          f"{'  ✅ 全部合法' if not bad_combo else '  ❌ ' + str(bad_combo[:5])}")
    print(f"  康复基线          : {len(rowset) - n_absent} 条 P/E"
          f"（记号数==组合条数） + {n_absent} 条「未参与」"
          f"{'  ✅' if not bad_base else '  ❌ ' + str(bad_base[:5])}")
    want_layers = {f"L{i}" for i in range(1, n_layers + 1)}
    print(f"  分层 {n_layers} 层齐全     : {sorted(layers_present)}"
          f"{'  ✅' if layers_present >= want_layers else '  ❌ 缺 ' + str(sorted(want_layers - layers_present))}")
    print(f"  教学过程首段标题  : {proc_head!r}"
          f"{'  ✅ 与范例一致（不带序号）' if proc_head_ok else '  ❌ 与范例不符（范例首段为「师生互动」）'}")
    print(f"  字体统一          : "
          f"{'PASS' if not font_bad else 'FAIL'} "
          f"（西文不得写成中文字体名；违规 {len(font_bad)} 处"
          f"{'：' + str(sorted(set(font_bad))) if font_bad else ''}）")
    print(f"  姓名违规          : {len(v_name)} 项 {v_name if v_name else ''}")
    print(f"  标签违规（正文）  : {len(v_banned)} 项 {v_banned if v_banned else ''}"
          f"  ※「障碍类别」栏目内的值按范例填写，只出现在表头行，不计违规")
    ok = (first_is_tbl and title_center and meta_ok and tab_ok
          and apx_pages == want_apx
          and merged_ok and class_ok and header_fill_ok
          and name_ok and only_own_ok and proc_head_ok
          and not bad_combo and not bad_base and bool(rowset)
          and len(set(ids)) == len(ids)
          and layers_present >= want_layers
          and len(combos) == n_layers
          and not v_name and not v_banned and not font_bad)
    print(f"  >>> 总判定: {'PASS' if ok else 'FAIL'}")
    return ok


allok = True
# 待检目录：默认 `data/docx`；`LPA_AUDIT_DIR` 可覆盖（供 `lesson_docx.py` CLI 指定）。
_audit_dir = Path(os.environ.get("LPA_AUDIT_DIR") or (ROOT / "data" / "docx"))
_files = sorted(f for f in _audit_dir.glob("*.docx") if not f.name.startswith("~$"))
if not _files:
    print(f"(目录里没有 docx：{_audit_dir})")
for f in _files:
    allok &= audit(f)
print(f"\n{'='*50}\n整体: {'全部 PASS' if allok else '存在 FAIL'}")
raise SystemExit(0 if allok else 1)

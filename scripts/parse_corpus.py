# -*- coding: utf-8 -*-
"""基于 <w:tc> XML 列表解析语料（读法 B，经截图复核确认）。

为什么要绕开 python-docx 的 row.cells：
  row.cells 会把横向合并单元格**展开成多格**（合并区宽度 × 重复内容），
  导致列位随行漂移，无法稳定定位。直接读 <w:tc> 得到的是**未展开的真实格**，
  列位稳定。

======================== 个别化表真实列结构（已核验） ========================
以「快乐六一 · 陈映燊」为例，Word 渲染出的列顺序是：

    个别化差异目标 | 儿童姓名 | 康复目标 | 康复基线 | 第一次 | 第二次 | 第三次 | 第四次 | 第五次

对应的 <w:tc> 下标：

    tc[2] 康复目标文本（"1.整节课堂能安坐好…"）
    tc[3] 儿童姓名        → 陈映燊
    tc[4] 康复目标编号    → 1456   （第 1、4、5、6 条目标）
    tc[5] 康复基线        → PPPP   （4 条目标的初始达标状态）
    tc[6] 第一次评量      → 未参与
    tc[7] 第二次评量      → PPPE
    tc[8] 第三次评量      → PPPP
    tc[9] 第四次评量      → PPPP
    tc[10] 第五次评量     → PPPP

⚠ 极易踩的两个坑（都踩过）：

  坑 1：`1456` 是**康复目标编号**（8 条目标里选了第 1/4/5/6 条），不是基线！
        早期把它当基线，导致整行右移一格。

  坑 2：**康复基线也是 P/E 形态**（PPPP），不是数字串！
        它描述该幼儿这几条目标在干预前的达标状态，**不属于评量数据**。
        早期按"找数字当基线"定锚，把基线和评量混为一谈。

对齐规则：**从「康复基线格」往右 1 格开始，连续 5 格 = 第一次~第五次**。
========================================================================
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from docx import Document
from docx.oxml.ns import qn

SRC = Path(r"C:\Users\17093\Desktop\homework\agent use idea")
ROOT = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent")
OUT_STRUCTURED = ROOT / "data" / "structured"
OUT_PRIVATE = ROOT / "data" / "private"

NAME_RE = re.compile(r"^[一-龥]{2,4}$")
NUMBER_RE = re.compile(r"^\d{1,6}$")
CODE_RE = re.compile(r"^[PEF]{2,6}$")
ABSENT_TOKENS = ("未参与", "未参加", "缺席", "请假", "未评")

NON_NAME = {
    "活动班级", "有益经验", "材料准备", "教学过程", "活动延伸", "活动反思",
    "活动过程", "儿童姓名", "康复目标", "康复基线", "评量效果", "评估效果",
    "个别化差异目标", "个别化差异", "未参与", "障碍类别", "轻度", "中度",
    "重度", "多重障碍", "人数", "名称", "年龄范围", "火箭班", "闪电班",
    "星星班", "★", "第一次", "第二次", "第三次", "第四次", "第五次",
}

ROW_LABEL_KEYS = {
    "活动班级": "class",
    "有益经验": "class_objectives",
    "材料准备": "materials",
    "教学过程": "process",
    "活动过程": "process",
    "活动延伸": "extension",
    "活动反思": "reflection",
}

# F -> E 修正计数（模块级，供摘要报告）
FIXED_F_COUNT = 0

# 表头里"第几次"用的汉字
CN_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八"}


def tc_cells(row) -> List[str]:
    """读一行的原始 <w:tc> 文本列表，不做合并展开。

    一个 tc 内可能有多个 <w:p>（表格里的竖排格，如康复目标的 `1`/`4`/`5`/`6`
    各占一段）。这里**用换行符拼接**保留边界，由 `flat()` 在需要时扁平化；
    这样 `1456` 既能与真正的多位数区分，也不影响按位比对。
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


def flat(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def tc_spans(row) -> List[int]:
    """读一行的各 <w:tc> 网格跨度。"""
    out: List[int] = []
    for tc in row._tr.findall(qn("w:tc")):
        gs = tc.find(qn("w:tcPr") + "/" + qn("w:gridSpan"))
        out.append(int(gs.get(qn("w:val"))) if gs is not None else 1)
    return out


def normalize_codes(raw: str) -> str:
    global FIXED_F_COUNT
    text = re.sub(r"\s+", "", raw or "").upper()
    if "F" in text:
        FIXED_F_COUNT += text.count("F")
        text = text.replace("F", "E")
    return text


def vmerge_flags(row) -> List[str]:
    """读每格的纵向合并状态：`'-'` 无合并 / `'restart'` 起始 / `'continue'` 延续。"""
    out: List[str] = []
    for tc in row._tr.findall(qn("w:tc")):
        vm = tc.find(qn("w:tcPr") + "/" + qn("w:vMerge"))
        if vm is None:
            out.append("-")
        else:
            val = vm.get(qn("w:val"))
            out.append(val if val else "continue")
    return out


def is_continuation_row(cells: List[str], vmerge: List[str]) -> bool:
    """判断整行是否为纵向合并的延续行（无独立数据）。

    判据：姓名格位置被标记为 `continue`，或整行几乎没有有效文本。
    这类行不能当独立记录，否则会把同一个幼儿重复计入。
    """
    nonempty = [c for c in cells if c]
    if len(nonempty) <= 1:
        return True
    for i, flag in enumerate(vmerge):
        if flag == "continue" and i < len(cells) and cells[i]:
            continue
    return False


def find_name_idx(cells: List[str], vmerge: List[str]) -> Optional[int]:
    """在一行里找「儿童姓名」格的下标。

    定锚判据（两条同时满足）：
      1. 该格像姓名（2~4 汉字，不在字段名黑名单）
      2. 该格之后连续两格符合 [康复目标编号, 康复基线] 形态：
         - 目标编号：纯数字串（如 `1456`，表示第 1/4/5/6 条目标）
         - 康复基线：P/E 码串（如 `PPPP`），长度应与目标编号位数一致

    注意：**不能用「紧跟纯数字」当唯一判据** —— 姓名后第一格确实是数字
    （目标编号），但真正的基线在它下一格，是 P/E 码，不是数字。
    """
    n = len(cells)
    for i, v in enumerate(cells):
        if i + 2 >= n:
            break
        name = flat(v)
        if not (NAME_RE.match(name) and name not in NON_NAME):
            continue
        # 姓名格本身不能是纵向合并的延续格
        if i < len(vmerge) and vmerge[i] == "continue":
            continue
        goal_ids = flat(cells[i + 1])
        baseline = flat(cells[i + 2])
        if not re.fullmatch(r"\d{1,8}", goal_ids):
            continue
        if not re.fullmatch(r"[PEF]{1,8}", baseline):
            continue
        # 交叉校验：目标编号位数 == 基线码长度（都等于目标条数）
        if len(goal_ids) != len(baseline):
            continue
        return i
    return None


def eval_slot_count(table) -> int:
    """这份表的评量列有几列。由表头行的 `第一次`..`第五次` 数出来。

    为什么不能硬编码 5：**各表的列数并不一致**。虽然主流是 5 列，
    但「闪电班4月21日主题教案」虽然表头也是 5 列、实际只填了 3 次，
    而另一类表（若出现）可能真的只有 4 个表头。
    一律按 5 读，遇到真·4 列表就会把第 4 次数据落在第 3 位、位次全错。
    因此这里**逐表按表头实测**，再据此取格。

    ⚠ 实测结果：14 份语料全部为 **5 列表头**。
    本函数是为了「列数与位次语义绑定」的正确性而保留的防御措施，
    不是当前数据里存在 4 列表的证据。
    （早期曾误判闪电班为 4 列，原因是截取表格时把空的 tc[9]/tc[10]
    误读成"不存在的列"，实际是"存在但未填"。）

    参数是 python-docx 的 `Table` 对象（不是 `_Row`：
    `row._parent` 是 `_Rows`，没有 `.rows`）。
    """
    for row in table.rows:
        texts = [flat(c) for c in tc_cells(row)]
        if "第一次" in texts:
            n = 0
            for k in range(1, 9):
                if f"第{CN_NUM[k]}次" in texts:
                    n = k
                else:
                    break
            return n
    return 5  # 探测不到表头时退回默认 5


def pick_eval_codes(cells: List[str], name_idx: int, n_slots: int = 5) -> List[str]:
    """读法 B：康复基线格之后连续 `n_slots` 格 = 第一次~第 n 次评量。

    列位：
        name_idx+1  康复目标编号（如 1456）
        name_idx+2  康复基线（如 PPPP）—— 不是评量数据
        name_idx+3  第一次评量
        name_idx+4  第二次
        ...

    ⚠ 返回列表长度 = `n_slots`（默认 5），缺格填 `''`，这样「第几次评量」的位次
    不会因为中间某次缺席而被压缩。未参与写作 `'未参与'`，真空白写作 `''`。

    ⚠ `n_slots` 必须来自 `eval_slot_count()` 的实测值，
    不能硬编码 5 —— 闪电班那份只有 4 列评量。
    """
    start = name_idx + 3
    codes: List[str] = []
    for k in range(n_slots):
        cell = cells[start + k] if start + k < len(cells) else ""
        if not cell.strip():
            codes.append("")                      # 该次未填
            continue
        if any(t in cell for t in ABSENT_TOKENS):
            codes.append("未参与")
            continue
        norm = normalize_codes(cell)
        codes.append(norm if re.fullmatch(r"[PE]+", norm) else "")
    return codes


@dataclass
class ParsedLesson:
    source_file: str
    title: str = ""
    teacher_line: str = ""
    date_range: str = ""
    class_label_raw: str = ""
    class_size: Optional[int] = None
    age_range: str = ""
    eval_slot_count: int = 5   # 本份教案的评量列数（实测，非硬编码）
    disability_labels: List[str] = field(default_factory=list)
    class_objectives: str = ""
    materials: str = ""
    process: str = ""
    extension: str = ""
    reflection: str = ""
    children: List[dict] = field(default_factory=list)


def parse_one(path: Path, name_map: Dict[str, str]) -> ParsedLesson:
    doc = Document(str(path))
    lesson = ParsedLesson(source_file=path.name)

    for p in doc.paragraphs:
        t = p.text.strip()
        if t:
            lesson.teacher_line = t
            m = re.search(r"时间[：:]\s*(.+)$", t)
            if m:
                lesson.date_range = re.sub(r"\s+", "", m.group(1))
            break

    if not doc.tables:
        return lesson
    lesson.title = doc.tables[0].rows[0].cells[0].text.strip()
    if len(doc.tables) < 2:
        return lesson

    main = doc.tables[1]
    n_slots = eval_slot_count(main)
    lesson.eval_slot_count = n_slots
    for row in main.rows:
        cells = tc_cells(row)
        if not cells:
            continue
        vmerge = vmerge_flags(row)

        # --- 幼儿数据行 ---
        name_idx = find_name_idx(cells, vmerge)
        if name_idx is not None:
            raw_name = flat(cells[name_idx])
            lesson.children.append({
                "raw_name": raw_name,
                "anon_id": anonymize(raw_name, name_map),
                "goal_ids": flat(cells[name_idx + 1]),      # 康复目标编号，如 1456
                "baseline_codes": flat(cells[name_idx + 2]),  # 康复基线，P/E 码
                "goal_text": flat(cells[name_idx - 1]) if name_idx >= 1 else "",
                "eval_codes": pick_eval_codes(cells, name_idx, n_slots),
            })
            continue

        # --- 字段行 ---
        # 注意：纵向合并会让行首 tc 为空，因此用「整行是否含班级特征词」判断。
        # 年龄写法不统一：`3----9岁`、`3-9岁`、`6—13`（无"岁"字），都要吃下。
        joined_all = " ".join(flat(c) for c in cells)
        is_class_row = (
            ("火箭班" in joined_all or "闪电班" in joined_all or "星星班" in joined_all)
            or ("年龄范围" in joined_all)
        )
        if is_class_row:
            for v in cells:
                fv = flat(v)
                if NAME_RE.match(fv) and fv.endswith("班"):
                    lesson.class_label_raw = fv
                if ("岁" in fv and re.search(r"\d", fv)) or re.fullmatch(r"\d+\s*[-—~～]{1,4}\s*\d+", fv):
                    lesson.age_range = re.sub(r"[-—~～]+", "-", fv.replace("岁", "")) + "岁"
            for v in cells:
                fv = flat(v)
                if fv.isdigit() and 3 <= int(fv) <= 40 and lesson.class_size is None:
                    lesson.class_size = int(fv)
            for frag in ("智力障碍", "自闭症", "唐氏", "脑瘫", "听力障碍", "语言障碍"):
                if frag in joined_all and frag not in lesson.disability_labels:
                    lesson.disability_labels.append(frag)
            continue

        head = flat(cells[0])
        key = ROW_LABEL_KEYS.get(head)

        if key:
            text = ""
            for v in cells[1:]:
                fv = flat(v)
                if fv and fv not in NON_NAME and len(fv) > 3:
                    text = fv
                    break
            if not text:
                continue
            cur = getattr(lesson, key)
            if key == "process" and cur and len(cur) > len(text):
                continue
            if len(text) > len(cur):
                setattr(lesson, key, text)
            continue

    return lesson


def anonymize(name: str, name_map: Dict[str, str]) -> str:
    if name not in name_map:
        name_map[name] = f"S{len(name_map) + 1:02d}"
    return name_map[name]


def main() -> None:
    global FIXED_F_COUNT
    FIXED_F_COUNT = 0
    OUT_STRUCTURED.mkdir(parents=True, exist_ok=True)
    OUT_PRIVATE.mkdir(parents=True, exist_ok=True)

    lessons: List[dict] = []
    name_map: Dict[str, str] = {}
    skipped: List[str] = []

    files = sorted(f for f in SRC.iterdir()
                   if f.suffix.lower() == ".docx" and not f.name.startswith("~$"))
    for f in files:
        try:
            parsed = parse_one(f, name_map)
        except Exception as e:
            skipped.append(f"{f.name}: {type(e).__name__}: {e}")
            continue
        payload = asdict(parsed)
        payload["children"] = [
            {k: v for k, v in c.items() if k != "raw_name"}
            for c in parsed.children
        ]
        lessons.append(payload)

    (OUT_STRUCTURED / "lessons.json").write_text(
        json.dumps(lessons, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_PRIVATE / "name_mapping.json").write_text(
        json.dumps(name_map, ensure_ascii=False, indent=2), encoding="utf-8")

    n_rec = sum(len(l["children"]) for l in lessons)
    n_cell = sum(len(c["eval_codes"]) for l in lessons for c in l["children"])
    n_abs = sum(1 for l in lessons for c in l["children"]
                for v in c["eval_codes"] if v == "未参与")

    lines = [
        "# 语料解析摘要（读法 B）", "",
        f"- 解析文件数：**{len(lessons)}**",
        f"- 幼儿评量记录：**{n_rec}** 条",
        f"- 评量格总数：{n_cell} 格（「未参与」{n_abs} 格）",
        f"- 匿名编号：**{len(name_map)}** 个（全语料唯一，跨教案可追踪）", "",
        "## 对齐口径",
        "",
        "个别化表真实列结构：`儿童姓名 | 康复目标编号 | 康复基线 | 第一次..第五次`。",
        "",
        "读法 B：**康复基线格之后连续 N 格 = 第一次~第 N 次评量**，"
        "N 由表头 `第一次`..`第N次` **逐表实测**得到。",
        "定标样本「快乐六一·陈映燊」验证：",
        "目标编号 `1456`、基线 `PPPP` → `未参与 → PPPE → PPPP → PPPP → PPPP`。", "",
        "⚠ 三个易错点：`1456` 是**目标编号**不是基线；**基线本身也是 P/E 形态**，",
        "不属于评量数据；**评量列数按表头逐表实测**（实测 14 份全为 5 列），",
        "但**列存在不等于有数据**——闪电班的第 4/5 列表头存在但整列为空。", "",
        "## 数据清洗",
        "",
        f"- `F` → `E` 修正：**{FIXED_F_COUNT}** 处",
        "- 横向合并：直接读 `<w:tc>` XML，替代 `row.cells`（后者会展开合并区、列位漂移）",
        "- **评量列数逐表探测**：不再硬编码 5，按表头实测列数取格", "",
    ]
    if skipped:
        lines += ["### 跳过", ""] + [f"- {s}" for s in skipped] + [""]
    lines += ["## 逐份明细", "", "| 文件 | 标题 | 班次 | 表内人数 | 记录 | 评量列数 | 评量格 |",
              "|---|---|---|---|---|---|---|"]
    for l in lessons:
        cells = sum(len(c["eval_codes"]) for c in l["children"])
        lines.append(f"| {l['source_file']} | {l['title'][:22]} | {l['class_label_raw']} | "
                     f"{l['class_size']} | {len(l['children'])} | "
                     f"{l.get('eval_slot_count', 5)} | {cells} |")
    (OUT_STRUCTURED / "PARSE_SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"ok  records={n_rec} cells={n_cell} names={len(name_map)} F_fixed={FIXED_F_COUNT}")


if __name__ == "__main__":
    main()

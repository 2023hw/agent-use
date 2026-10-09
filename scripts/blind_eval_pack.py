# -*- coding: utf-8 -*-
"""M4-5 教师盲评 —— 材料包生成器（人在环验证）。

做什么
------
把两类**来源不同**的教案，渲染成**同一版式、去掉来源指纹**的匿名卡片，
连同「教师须知 / 评测量表 / 回收统计表 / 封存密钥」打包成可直接打印的材料包：

* **系统产出**：`data/generated/<主题>.json`（W2 生成，LLM 五步链）
* **人工范例**：`data/structured/lessons.json`（语料里真实教师手写的教案）

两份来源混装、按固定种子打乱编号（教案 A/B/C…），教师不知道哪份来自系统、
哪份出自人手 —— 这就是「盲」的意义。

为什么要去掉指纹
----------------
卡片上如果留着班级名、人数、日期、任课老师，教师一眼就能对上号；
系统产出还总写「2026年10月」「12 人」，人工范例总写「2025年5月」「15 人」，
这些**元信息本身就是来源指纹**。所以一律剥离，并把单次时长**归一为 30 分钟**
（语料本就是 30 分钟主题课格式，系统也是照此配置）。

用法
----
    PYTHONUTF8=1 "$PY" scripts/blind_eval_pack.py                  # 用默认种子生成
    PYTHONUTF8=1 "$PY" scripts/blind_eval_pack.py --seed 20261008  # 换种子换编号
    PYTHONUTF8=1 "$PY" scripts/blind_eval_pack.py --out <目录>

产出（默认 `data/blind-eval/`）
-------------------------------
    README.md              组织方说明（这份包怎么用、怎么回收统计）
    0_教师须知.md           发给参评教师
    1_评测量表.md           打分卡（每位教师一份，可打印）
    2_教案卡片/教案-A.md…   匿名教案正文
    3_回收统计表.csv        收分用的模板
    4_分组密钥.md           ⚠ 封存件：编号 ↔ 来源，评完再开封
    打印版.html            全部内容合成一页，浏览器直接打印/导出 PDF

⚠ 材料**不含任何幼儿姓名、诊断或障碍标签**；生成后会逐份扫描把关。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lpa.rules import BANNED_PATTERN, load_forbidden_names  # noqa: E402

GEN = ROOT / "data" / "generated"
OUT = ROOT / "data" / "blind-eval"
LESSONS = ROOT / "data" / "structured" / "lessons.json"
LADDER = ROOT / "data" / "structured" / "goal_ladder.json"
NAME_MAP = ROOT / "data" / "private" / "name_mapping.json"


# --------------------------------------------------------------------------
# 抽题：3 份系统产出 + 3 份人工范例（六份混装，主题各异）
# --------------------------------------------------------------------------
# 系统产出（`data/generated/<名>.json`）。挑交付件 + 两个不同领域的留出主题。
AI_THEMES = ["国庆节到了", "我会穿鞋子", "跟着音乐做动作"]

# 人工范例（`lessons.json` 的 source_file）。刻意避开被用作阶梯来源的四份，
# 免得教师把「范例」与「系统」对上号。
HUMAN_DOCS = [
    "第三周-浓浓端午节(1).docx",
    "第四周-清凉夏日(1).docx",
    "第二周-我和我的同伴-1.docx",
]

# 盲评编码池（按种子打乱后依次分配）
CODES = ["A", "B", "C", "D", "E", "F", "G", "H"]

NORM_DURATION = "约 30 分钟"


# --------------------------------------------------------------------------
# 脱敏：命中真实姓名 / 标签词一律替换（双保险，渲染后又扫一遍）
# --------------------------------------------------------------------------
def _sanitize(text: str, names: list) -> str:
    for n in names:
        if n and n in text:
            text = text.replace(n, "小朋友")
    text = BANNED_PATTERN.sub("某", text)
    # 班级规模是**来源指纹**：系统产出永远是「全班 12 名」，人工范例各班不同
    # （10–15）。留着它教师就能按人数对上号，故一律抹平。
    text = re.sub(r"全班\s*\d+\s*名", "全班幼儿", text)
    return text


def _clean_field(value, names: list):
    """递归清洗 str / list / dict。"""
    if isinstance(value, str):
        return _sanitize(value, names)
    if isinstance(value, list):
        return [_clean_field(v, names) for v in value]
    if isinstance(value, dict):
        return {k: _clean_field(v, names) for k, v in value.items()}
    return value


# --------------------------------------------------------------------------
# 渲染一：系统产出（LessonPlan JSON）→ 卡片
# --------------------------------------------------------------------------
def card_from_plan(d: dict, code: str) -> str:
    out: list = []
    out.append(f"# 教案 {code}\n")
    out.append("> 请仅依据教案内容作答，不要猜测作者或来源。\n")
    out.append("| 项目 | 内容 |")
    out.append("|---|---|")
    out.append(f"| 主题 | {d.get('title', '')} |")
    out.append(f"| 适用年龄 | {d.get('age_range', '')} |")
    out.append(f"| 单次时长 | {NORM_DURATION} |")
    out.append(f"| 活动领域 | {d.get('main_domain', '')} |")
    out.append("")

    out.append("## 一、活动总目标（有益经验）\n")
    out.append(_number_lines(d.get("class_objectives_text", "")))
    out.append("")

    out.append("## 二、分层活动目标\n")
    out.append("| 层级 | 支持方式 | 活动目标 | 达成标准 |")
    out.append("|---|---|---|---|")
    # 每个层级通常有 2–3 条子目标；按层级归并，层级格只在首行出现。
    last_lv = None
    for o in sorted(d.get("objectives", []),
                    key=lambda x: {"A": 0, "B": 1, "C": 2}.get(str(x.get("level", "")), 9)):
        lv = str(o.get("level", ""))
        lv_cell = lv if lv != last_lv else ""
        last_lv = lv
        out.append("| {} | {} | {} | {} |".format(
            _esc(lv_cell), _esc(o.get("support_level", "")),
            _esc(o.get("text", "")), _esc(o.get("criteria", ""))))
    out.append("")

    mats = d.get("materials") or []
    if mats:
        out.append("## 三、材料准备\n")
        out.append(_number_lines(mats))
        out.append("")

    env = (d.get("environment") or "").strip()
    safe = (d.get("safety_notes") or "").strip()
    if env or safe:
        out.append("## 四、环境与安全\n")
        if env:
            out.append(f"- **环境**：{env}")
        if safe:
            out.append(f"- **安全**：{safe}")
        out.append("")

    steps = d.get("steps") or []
    if steps:
        out.append("## 五、教学过程\n")
        for grp, title in _group_steps(steps):
            out.append(f"### {title}\n")
            out.append(f"**时长**：{grp['duration']} 分钟\n")
            for s in grp["steps"]:
                out.append(f"**{s.get('name', '')}**")
                if s.get("teacher_script"):
                    out.append(f"- 教师语言：{s['teacher_script']}")
                if s.get("child_action"):
                    out.append(f"- 幼儿表现：{s['child_action']}")
                if s.get("visual_cue"):
                    out.append(f"- 视觉/手势提示：{s['visual_cue']}")
                if s.get("scaffolds"):
                    out.append("- 支持措施：" + "／".join(s["scaffolds"]))
                if s.get("observation_points"):
                    out.append("- 观察要点：" + "／".join(s["observation_points"]))
                out.append("")

    goals = d.get("individualized_goals") or []
    if goals:
        out.append("## 六、个别化差异目标\n")
        out.append("| 层级 | 适用人数 | 目标 | 起点与评量 | 未达标的下一步 |")
        out.append("|---|---|---|---|---|")
        for g in goals:
            out.append("| {} | {} | {} | {}；{} | {} |".format(
                _esc(g.get("level", "")), _esc(g.get("share_of_class", "")),
                _esc(g.get("goal", "")),
                _esc(g.get("baseline_hint", "")), _esc(g.get("evaluation_method", "")),
                _esc(g.get("follow_up", ""))))
        out.append("")

    ext = (d.get("extension") or "").strip()
    home = (d.get("home_extension") or "").strip()
    if ext or home:
        out.append("## 七、活动延伸与家园共育\n")
        if ext:
            out.append(ext)
            out.append("")
        if home and home not in ext:
            out.append(f"- {home}")
            out.append("")

    alts = d.get("alternatives") or []
    if alts:
        out.append("## 八、应变的备选方案\n")
        for a in alts:
            out.append(f"- **当{a.get('situation', '')}时**：{a.get('alternative_participation', '')}"
                       f"（评量：{a.get('alternative_evaluation', '')}）")
        out.append("")

    refs = d.get("reflection_points") or []
    if refs:
        out.append("## 九、活动反思（留白供教师课后填写）\n")
        for r in refs:
            out.append(f"- {r}")
        out.append("")
    return _renumber("\n".join(out).strip() + "\n")


# --------------------------------------------------------------------------
# 渲染二：人工范例（lessons.json 的原始教案）→ 卡片
# --------------------------------------------------------------------------
# 语料的「教学过程」是一整段文本，但内部保留了四个小标题，按标题切段。
_PROC_SPLIT = re.compile(
    r"(师生互动|师生问好|热身活动|手指律动|主题课活动|活动过程|活动结束|活动延伸|活动反思)")


def card_from_lesson(d: dict, code: str, ladder_items=None) -> str:
    out: list = []
    out.append(f"# 教案 {code}\n")
    out.append("> 请仅依据教案内容作答，不要猜测作者或来源。\n")
    out.append("| 项目 | 内容 |")
    out.append("|---|---|")
    out.append(f"| 主题 | {_esc(d.get('title', ''))} |")
    out.append(f"| 适用年龄 | {_esc(d.get('age_range', ''))} |")
    out.append(f"| 单次时长 | {NORM_DURATION} |")
    out.append(f"| 活动领域 | {_domain_of(d.get('title', ''))} |")
    out.append("")

    goals_text = (d.get("class_objectives") or "").strip()
    if goals_text:
        out.append("## 一、活动总目标（有益经验）\n")
        out.append(_number_lines(goals_text))
        out.append("")

    kids = d.get("children") or []
    if kids:
        out.append("## 二、分层活动目标\n")
        legend = _goal_legend(ladder_items)
        if legend:
            out.append("**目标条文（按编号）**\n")
            for num, text in legend:
                out.append(f"- {num}. {text}")
            out.append("")
        out.append("| 幼儿 | 分配到的目标编号 | 康复基线 |")
        out.append("|---|---|---|")
        for c in kids:
            out.append("| {} | {} | {} |".format(
                _esc(c.get("anon_id", "")), _esc(c.get("goal_ids", "")),
                _esc(str(c.get("baseline_codes", "")).replace(" ", " "))))
        out.append("")

    mats = (d.get("materials") or "").strip()
    if mats:
        out.append("## 三、材料准备\n")
        out.append(_number_lines(mats))
        out.append("")

    proc = (d.get("process") or "").strip()
    if proc:
        out.append("## 五、教学过程\n")
        for title, body in _split_process(proc):
            # 序号先统一写成「（一）」，交给 `_renumber` 按出现顺序重排。
            out.append(f"### （一）{title}\n")
            out.append(body)
            out.append("")

    ext = (d.get("extension") or "").strip()
    if ext:
        out.append("## 七、活动延伸与家园共育\n")
        out.append(_number_lines(ext))
        out.append("")

    ref = (d.get("reflection") or "").strip()
    if ref:
        out.append("## 九、活动反思（原教案课后记录）\n")
        out.append(ref)
        out.append("")
    return _renumber("\n".join(out).strip() + "\n")


def _goal_legend(ladder_items):
    """「编号 → 目标条文」。取自语料的阶梯文件（比幼儿行里的全文更权威）。

    ⚠ 阶梯文件的 `children` 里带**真实姓名**，这里只读 `items`，绝不碰 `children`。
    """
    out = []
    for it in ladder_items or []:
        try:
            num, text = it[0], it[1]
        except (TypeError, IndexError):
            continue
        num, text = str(num).strip(), str(text).strip()
        if num and text:
            out.append((num, text))
    return out


def _domain_of(title: str) -> str:
    m = re.match(r"([\u4e00-\u9fa5]{2,4}?)课", title or "")
    return m.group(1) if m else "主题课"


def _split_process(proc: str):
    """把语料的整段教学过程按四个小标题切段；切不出来就整段当一块。"""
    parts = _PROC_SPLIT.split(proc)
    blocks, cur_title, buf = [], "教学过程", []
    for seg in parts:
        if not seg:
            continue
        if _PROC_SPLIT.fullmatch(seg):
            if buf:
                blocks.append((cur_title, "".join(buf).strip()))
                buf = []
            cur_title = seg
        else:
            buf.append(seg)
    if buf:
        blocks.append((cur_title, "".join(buf).strip()))
    blocks = [(t, b) for t, b in blocks if b]
    if not blocks:
        return [("教学过程", proc)]
    # 首个标题若本身就是正文开头，退化为一段
    return blocks


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _esc(text) -> str:
    """表格单元格：竖线转义、换行压平。"""
    return str(text or "").replace("|", "／").replace("\n", " ").strip()


_CN_NUM = "一二三四五六七八九十"


def _renumber(md: str) -> str:
    """把卡片里的章节序号改成**连续**的。

    每种来源能提供的章节不同（系统产出有「环境与安全」，人工范例没有），
    若按固定序号写，卡片就会出现「一、三、五、七、九」这种跳号 ——
    跳号本身就是来源指纹。这里按实际出现的顺序重新编号。
    """
    c1 = c2 = 0
    out = []
    for line in md.splitlines():
        if re.match(r"^## [%s]+、" % _CN_NUM, line):
            c1 += 1
            out.append("## %s、%s" % (_CN_NUM[c1 - 1], line[line.index("、") + 1:]))
        elif re.match(r"^### （[%s]+）" % _CN_NUM, line):
            c2 += 1
            out.append("### （%s）%s" % (_CN_NUM[c2 - 1], line[line.index("）") + 1:]))
        else:
            out.append(line)
    return "\n".join(out)


def _number_lines(text) -> str:
    """把「1.x2.y」这类挤在一行的编号条文拆成逐条列表；已分行的原样保留。"""
    if isinstance(text, (list, tuple)):
        return "\n".join(f"- {str(x).strip()}" for x in text if str(x).strip())
    text = str(text or "").strip()
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1:
        return "\n".join(ln if re.match(r"^[\-*\d（(]", ln) else f"- {ln}" for ln in lines)
    # 单行：按「数字.」或「数字、」拆分
    chunks = [c.strip() for c in re.split(r"(?=(?:\d+)[.、)）])", text) if c.strip()]
    if len(chunks) > 1:
        return "\n".join(f"- {c}" for c in chunks)
    return f"- {text}"


# 交付件与留出集的环节归组（与 render_docx.PROC_SECTIONS 同口径）。
# 按**关键词**归类而非精确匹配 —— 模型给环节起的名字有变体（热身律动 / 复练与小结）。
_SECTION_RULES = [
    (("师生", "问好", "点名", "问候"), "（一）师生互动"),
    (("热身", "律动", "准备活动"), "（二）热身活动"),
    (("小结", "结束", "总结", "延伸", "收尾"), "（四）活动结束"),
    (("导入", "主题", "核心", "操作", "复练", "练习", "新授"), "（三）主题课活动"),
]
_SECTION_FALLBACK = "（三）主题课活动"
# 固定顺序，保证输出稳定
_SECTION_ORDER = ["（一）师生互动", "（二）热身活动", "（三）主题课活动", "（四）活动结束"]


def _section_of(name: str) -> str:
    for keys, title in _SECTION_RULES:
        if any(k in name for k in keys):
            return title
    return _SECTION_FALLBACK


def _group_steps(steps: list):
    """按固定四段归组，保持出现顺序。"""
    bucket = {}
    for s in steps:
        key = _section_of(str(s.get("name", "")).strip())
        if key not in bucket:
            bucket[key] = {"steps": [], "duration": 0}
        bucket[key]["steps"].append(s)
        bucket[key]["duration"] += int(s.get("duration_min", 0) or 0)
    return [(bucket[k], k) for k in _SECTION_ORDER if k in bucket]


# --------------------------------------------------------------------------
# 材料包其它文件
# --------------------------------------------------------------------------
RUBRIC = [
    ("D1", "目标可测性", "活动目标是否具体、可观察、可评价（而不是“感受”“了解”这类无法检验的话）；"
                        "是否与主题、与幼儿年龄相称。"),
    ("D2", "分层与个别化", "是否照顾到不同能力水平的幼儿；支持方式是否由“多辅助”到“少辅助”形成梯度；"
                          "个别化目标是否落得下去。"),
    ("D3", "环节完整性", "教学过程是否完整（引入—展开—结束）、层次清楚；各环节时间分配是否合理。"),
    ("D4", "材料与环境", "材料准备是否具体、拿得到、用得上；环境布置与安全提示是否考虑到位。"),
    ("D5", "教师语言与可操作性", "教师语言能否照着说；指令是否清楚、适合幼儿；拿到手就能上课的程度。"),
]

QUESTIONNAIRE = """
**Q6（必答）** 如果现在就要上这节课，这份教案你能怎么用？请勾选一项：

- [ ] 可以直接使用
- [ ] 小改一下就能用
- [ ] 需要较大修改
- [ ] 基本不能用

**Q7（必答）** 这份教案**最需要改进**的一点是什么？（一句话即可）

__________________________________________________________________

**Q8（选答）** 这份教案里**最有价值**的部分是什么？

__________________________________________________________________
"""


def build_rubric_md(codes: list) -> str:
    out = ["# 教案评测量表（每位教师一份）\n",
           "> 感谢您参加本次评议。请**逐份**阅读教案卡片，就下列 5 个维度各打 1 个分，"
           "并回答 Q6–Q8。\n",
           "## 一、评分说明\n",
           "每个维度按 **1–5 分**评分（可用半分，如 3.5）：\n",
           "| 分值 | 1 | 2 | 3 | 4 | 5 |",
           "|---|---|---|---|---|---|",
           "| 含义 | 明显不足 | 有待改进 | 基本合格 | 良好 | 优秀 |",
           "",
           "## 二、评测维度\n",
           "| 编号 | 维度 | 看什么 |", "|---|---|---|"]
    for k, name, desc in RUBRIC:
        out.append(f"| {k} | {name} | {desc} |")
    out += ["", "## 三、逐份打分\n"]
    for c in codes:
        out.append(f"### 教案 {c}\n")
        out.append("| 维度 | D1 目标可测性 | D2 分层与个别化 | D3 环节完整性 | "
                   "D4 材料与环境 | D5 语言与可操作性 |")
        out.append("|---|---|---|---|---|---|")
        out.append("| 得分（1–5） |  |  |  |  |  |")
        out.append("")
        out.append(QUESTIONNAIRE.strip())
        out.append("")
    out.append("## 四、教师信息（选填，仅用于统计区分）\n")
    out.append("- 教龄：______ 年　专业背景：____________")
    # ⚠ 措辞避开项目禁词表（`融合教育`/`特教` 都在 BANNED_TERMS 里，
    #   整包复扫是子串扫描，写进去就会被判违规）。
    out.append("- 是否带过混合能力班级：□是　□否")
    out.append("")
    return "\n".join(out)


def build_readme_md(codes: list, seed: int) -> str:
    return f"""# M4-5 教师盲评 · 材料包使用说明（组织方）

> 本包用于验证「系统生成的教案，专业教师是否认为好用」。
> 参评教师**不知道**哪份来自系统、哪份出自人手 —— 这就是「盲」。

## 一、包里有什么

| 文件 | 给谁 | 说明 |
|---|---|---|
| `5_教师评分卡/评分卡-*.docx` | 教师 | ⭐ **推荐路径**：可直接在电脑上填的 Word 评分卡 |
| `回收/` | 组织方 | 教师把填好的评分卡丢这里，一条命令读回，**无需手工转录** |
| `0_教师须知.md` | 教师 | 一页说明：评什么、怎么评、隐私与自愿 |
| `1_评测量表.md` | 教师 | 打分卡（纸质版，可打印） |
| `2_教案卡片/教案-{codes[0]}.md …` | 教师 | {len(codes)} 份匿名教案正文 |
| `3_回收统计表.csv` | 组织方 | 收分表（`--ingest` 自动写入；也可手工誊） |
| `4_分组密钥.md` | **组织方封存** | 编号 ↔ 来源的对照，**评完再开封** |
| `打印版.html` | 教师 | 全部内容合成一页，浏览器「打印 → 存为 PDF」 |

## 二、怎么用（建议流程）

### 路线 A：电脑填写（推荐，零转录）

1. **生成**：`PYTHONUTF8=1 "$PY" scripts/blind_eval_form.py`
   —— 在 `5_教师评分卡/` 得到每位教师一份 `.docx`；教师人数不同就改 `--teachers`。
2. **发放**：把各自那份 `评分卡-<教师>.docx` 发给教师。**不要**发 `4_分组密钥.md`。
3. **评测**：教师打开 Word/WPS 填写 —— 打分格是**下拉选择**，点一下就能选；
   每份教案的正文就在它那张评分表的正上方，读到哪儿评到哪儿。
   单份约 3–5 分钟，全套约 20–30 分钟。
4. **回收**：教师填完保存，放进 `回收/` 文件夹。
5. **读回**：`PYTHONUTF8=1 "$PY" scripts/blind_eval_form.py --ingest`
   —— 自动落进 `3_回收统计表.csv`，并直接打印分组统计；缺格的会逐条提示。

### 路线 B：纸质填写（备选，需要人工转录）

1. **打印**：打开 `打印版.html`，导出 PDF 后打印；或直接打印 `0/1/2` 三个文件。
2. **发放**：每位教师一套「须知 + 卡片 + 量表」。
3. **回收**：把每位教师的分数誊进 `3_回收统计表.csv`。
4. **统计**：`PYTHONUTF8=1 "$PY" scripts/blind_eval_pack.py --tally`
   —— 按来源分组算均值（评完、开封密钥之后再跑）。

## 三、本次抽题（随机种子 `{seed}`）

共 **{len(codes)}** 份：3 份系统产出 + 3 份人工范例；主题各异、来源混装、编号已打乱。

> 编号与来源的对应关系写在 `4_分组密钥.md` 里 —— **开封前不要查看**，
> 否则组织方自己先「破盲」，后面的统计就没有意义了。

## 四、隐私与合规

- 材料已**去净幼儿姓名、诊断名称与障碍标签**；班级名、人数、日期、授课教师等
  **来源指纹**同样已剥离，单次时长归一为 30 分钟。
- 教师信息只用于区分评分者，不与个人身份关联。
- 本材料仅用于课程作业的效果验证，不作他用。

重算：`PYTHONUTF8=1 "$PY" scripts/blind_eval_pack.py --seed {seed}`
"""


def build_notice_md(codes: list) -> str:
    return f"""# 教师须知（请先阅读）

**您好！** 感谢您抽出时间参加本次教案评议。

## 一、这是什么

这里共有 **{len(codes)} 份教案**（编号 {codes[0]}–{codes[-1]}），都是幼儿园主题课教案。
请您以**一线教师**的眼光，判断它们「好不好用」。

> 教案来源本次不公开，请**只依据教案本身**作答。
> 若您觉得某份教案眼熟，也请按您的专业判断打分即可。

## 二、怎么评

1. **打开组织方发给您的 Word 评分卡**（文件名形如「评分卡-xx.docx」）。
   若您拿到的是纸质量表，就在纸上填写，方式相同。
2. 每份教案的正文就排在**它自己那张评分表的正上方**，读完随手打分，不用来回翻页。
3. 在 **D1–D5 五个维度**各打 **1–5 分**（可打半分）。
   带**下拉箭头**的格子用鼠标点一下就会出选项；若您的软件点不开，
   直接把格子里的字删掉、手打一个数也行（例如 4 或 4.5）。
4. 回答 **Q6**（能否直接用，选一项）、**Q7**（最需改进，必答）、**Q8**（最有价值，选答）。
5. **交回**：填完保存文件，放进组织方指定的「回收」文件夹即可
   —— 不用再把分数另抄一份。

## 三、几点说明

- **没有标准答案**，您的真实感受最有价值；不必顾虑分数高低。
- {len(codes)} 份材料的呈现形式可能略有差别（有的环节写成表格、有的写成叙述），
  这是材料本身的样子，**请按内容的实质判断**。
- 全程**匿名**、**自愿**，您可以随时停止，不需要理由。
- 单份约 3–5 分钟，全套约 20–30 分钟。

## 四、材料里没有

本材料**不含任何幼儿姓名、诊断名称或障碍标签**，班级、人数、日期、授课教师
等信息也已隐去。若您发现任何可疑的隐私信息，请立即告知组织方。

---

**再次感谢！**
"""


def build_key_md(items: list) -> str:
    out = ["# 分组密钥（⚠ 组织方封存，评完再开封）\n",
           f"> 生成日期：{date.today().isoformat()}　|　"
           f"共 {len(items)} 份：3 份系统产出 + 3 份人工范例\n",
           "| 编号 | 来源 | 出处 | 主题 |", "|---|---|---|---|"]
    for it in items:
        out.append(f"| {it['code']} | {it['origin']} | `{it['source']}` | {it['title']} |")
    out.append("")
    out.append("> **统计用法**：把 `3_回收统计表.csv` 里的分数按本表分组 —— "
               "`系统产出` 三份为一组、`人工范例` 三份为另一组，"
               "分别算各维度均值后对比。详见 `docs/M4-5_BLIND_EVAL.md`。")
    out.append("")
    return "\n".join(out)


def build_csv(codes: list) -> str:
    head = ["教师编号", "教案编号", "D1目标可测性", "D2分层与个别化", "D3环节完整性",
            "D4材料与环境", "D5语言与可操作性", "Q6可用性", "Q7最需改进", "备注"]
    rows = [",".join(head)]
    for c in codes:
        for t in ("教师1", "教师2", "教师3"):
            rows.append(f"{t},{c},,,,,,,,")
    return "\n".join(rows) + "\n"


def build_html(cards: dict, codes: list, seed: int) -> str:
    """可打印版：须知 + 量表 + 全部卡片合成一页。"""
    parts = ["<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
             "<title>M4-5 教案盲评材料</title>", _HTML_CSS, "</head><body>"]
    parts.append("<section class='page'>" + _md_to_html(build_notice_md(codes)) + "</section>")
    parts.append("<section class='page'>" + _md_to_html(build_rubric_md(codes)) + "</section>")
    for c in codes:
        parts.append(f"<section class='page'>{_md_to_html(cards[c])}</section>")
    parts.append("</body></html>")
    return "\n".join(parts)


_HTML_CSS = """
<style>
  body{font-family:"Songti SC","SimSun",serif;color:#1a1a1a;margin:0;line-height:1.7;}
  .page{max-width:820px;margin:0 auto;padding:32px 40px;border-bottom:2px dashed #bbb;}
  h1{font-size:22px;text-align:center;border-bottom:2px solid #333;padding-bottom:8px;}
  h2{font-size:17px;margin-top:22px;color:#7a1f1f;}
  h3{font-size:15px;margin-top:14px;}
  table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13px;}
  th,td{border:1px solid #999;padding:5px 8px;text-align:left;vertical-align:top;}
  th{background:#f2f2f2;}
  blockquote{background:#f7f7f7;border-left:4px solid #bbb;margin:10px 0;padding:6px 12px;color:#555;}
  @media print{.page{border:none;padding:0 12mm;page-break-after:always;}}
</style>
"""


def _md_to_html(md: str) -> str:
    """够用的 Markdown → HTML（标题 / 表格 / 列表 / 引用 / 加粗）。"""
    html, in_table, in_ul = [], False, False

    def close():
        nonlocal in_table, in_ul
        if in_table:
            html.append("</table>")
            in_table = False
        if in_ul:
            html.append("</ul>")
            in_ul = False

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if re.match(r"^\|[\s:\-|]+\|$", line):        # 表格分隔行
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not in_table:
                close()
                html.append("<table>")
                in_table = True
                html.append("<tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr>")
            else:
                html.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
            continue
        close()
        if line.startswith("### "):
            html.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            html.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            html.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("> "):
            html.append(f"<blockquote>{line[2:]}</blockquote>")
        elif re.match(r"^[-*] ", line):
            if not in_ul:
                html.append("<ul>")
                in_ul = True
            html.append(f"<li>{line[2:]}</li>")
        else:
            html.append(f"<p>{line}</p>")
    close()
    text = "\n".join(html)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def collect(names: list) -> list:
    """汇总六份材料：3 份系统产出 + 3 份人工范例（已脱敏）。"""
    items: list = []
    for theme in AI_THEMES:
        p = GEN / f"{theme}.json"
        if not p.exists():
            print(f"[pack] ⚠ 缺系统产出 {p.name}，跳过")
            continue
        d = _clean_field(json.loads(p.read_text(encoding="utf-8")), names)
        items.append({"origin": "系统产出", "source": f"data/generated/{theme}.json",
                      "title": d.get("title", theme),
                      "card": card_from_plan(d, "{CODE}"), "kind": "ai"})

    L = {x["source_file"]: x for x in json.loads(LESSONS.read_text(encoding="utf-8"))}
    try:
        jl = json.loads(LADDER.read_text(encoding="utf-8")).get("docs", {})
    except Exception:                                          # noqa: BLE001
        jl = {}
    for doc in HUMAN_DOCS:
        d = L.get(doc)
        if d is None:
            print(f"[pack] ⚠ 语料里找不到 {doc}，跳过")
            continue
        d = _clean_field(d, names)
        # ⚠ 只取阶梯的 `items`（编号+条文）；`children` 里有真实姓名，绝不读。
        ladder = _clean_field((jl.get(doc) or {}).get("items") or [], names)
        items.append({"origin": "人工范例", "source": doc,
                      "title": d.get("title", doc),
                      "card": card_from_lesson(d, "{CODE}", ladder_items=ladder),
                      "kind": "human"})
    return items


def tally(out_dir: Path) -> int:
    """收分统计：读回收表 + 封存密钥，按来源分组算各维度均值。

    需要先「开封」——即 `4_分组密钥.md` 存在。评完之前不要跑。
    """
    import csv

    key_p = out_dir / "4_分组密钥.md"
    csv_p = out_dir / "3_回收统计表.csv"
    if not key_p.exists() or not csv_p.exists():
        print(f"[tally] ❌ 缺少 {key_p.name} 或 {csv_p.name}")
        return 1

    origin_of = {}
    for line in key_p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*([A-Z])\s*\|\s*(\S+?)\s*\|", line)
        if m:
            origin_of[m.group(1)] = m.group(2)

    dims = ["D1", "D2", "D3", "D4", "D5"]
    groups: dict = {}
    q6: dict = {}
    n_row = 0
    with csv_p.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = (row.get("教案编号") or "").strip().upper()
            if code not in origin_of:
                continue
            # 回收表表头是「D1目标可测性」这种全名，按前缀取回 D1…D5。
            got = {}
            for k, v in row.items():
                if not k or len(k) < 2 or k[0] != "D" or not k[1].isdigit():
                    continue
                s = (v or "").strip()
                if not s:
                    continue
                try:
                    got[k[:2]] = float(s)
                except ValueError:
                    continue
            if not got:
                continue
            n_row += 1
            g = groups.setdefault(origin_of[code],
                                  {d: [] for d in dims} | {"n": 0})
            g["n"] += 1
            for d, v in got.items():
                g[d].append(v)
            q = (row.get("Q6可用性") or "").strip()
            if q:
                q6.setdefault(origin_of[code], {})
                q6[origin_of[code]][q] = q6[origin_of[code]].get(q, 0) + 1

    if not groups:
        print("[tally] 回收表里还没有分数 —— 先把教师的评分誊进来再跑。")
        return 1

    print("\n=== M4-5 盲评结果（按来源分组）===\n")
    print("| 来源 | 份数 | " + " | ".join(dims) + " | 五维均分 |")
    print("|---|---|" + "---|" * (len(dims) + 1))
    for origin, g in groups.items():
        means = []
        for d in dims:
            means.append(sum(g[d]) / len(g[d]) if g[d] else float("nan"))
        overall = [x for x in means if x == x]
        print("| {} | {} | {} | {:.2f} |".format(
            origin, g["n"],
            " | ".join(f"{m:.2f}" if m == m else "—" for m in means),
            sum(overall) / len(overall) if overall else float("nan")))
    print()
    if q6:
        print("**Q6 可用性分布**\n")
        for origin, dist in q6.items():
            print(f"- {origin}：" + "、".join(f"{k} {v} 次" for k, v in dist.items()))
        print()
    print(f"（有效评分行：{n_row}　|　"
          f"{'⚠ 样本量小，结论宜作定性参考' if n_row < 12 else '样本量尚可'}）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="M4-5 教师盲评材料包生成器")
    ap.add_argument("--seed", type=int, default=20261008, help="打乱编号用的随机种子")
    ap.add_argument("--out", default=str(OUT), help="输出目录")
    ap.add_argument("--tally", action="store_true",
                    help="不开生成：读回收表 + 封存密钥，按来源分组统计（评完后用）")
    args = ap.parse_args()

    out_dir = Path(args.out)
    if args.tally:
        return tally(out_dir)
    names = load_forbidden_names(str(NAME_MAP))
    print(f"[pack] 真实姓名清单 {len(names)} 条；输出目录 {out_dir}")

    items = collect(names)
    if not items:
        print("[pack] ❌ 没有可用的材料")
        return 1

    rnd = random.Random(args.seed)
    rnd.shuffle(items)
    codes = CODES[:len(items)]
    for code, it in zip(codes, items):
        it["code"] = code
        it["card"] = it["card"].replace("{CODE}", code)

    # 合规自检：逐份扫描姓名 / 标签 / 来源指纹
    leak = 0
    for it in items:
        blob = it["card"]
        hit_names = [n for n in names if n and n in blob]
        hit_ban = sorted(set(BANNED_PATTERN.findall(blob)))
        hit_meta = [w for w in ("火箭班", "闪电班", "任课老师", "混龄康教班") if w in blob]
        if hit_names or hit_ban or hit_meta:
            leak += 1
            print(f"[pack] ❌ 教案 {it['code']} 命中：姓名={hit_names[:3]} "
                  f"标签={hit_ban} 指纹={hit_meta}")
        else:
            print(f"[pack] ✅ 教案 {it['code']}（{it['origin']}）合规")

    cards_dir = out_dir / "2_教案卡片"
    cards_dir.mkdir(parents=True, exist_ok=True)
    cards = {}
    for it in items:
        cards[it["code"]] = it["card"]
        (cards_dir / f"教案-{it['code']}.md").write_text(it["card"], encoding="utf-8")

    (out_dir / "README.md").write_text(build_readme_md(codes, args.seed), encoding="utf-8")
    (out_dir / "0_教师须知.md").write_text(build_notice_md(codes), encoding="utf-8")
    (out_dir / "1_评测量表.md").write_text(build_rubric_md(codes), encoding="utf-8")
    (out_dir / "3_回收统计表.csv").write_text(build_csv(codes), encoding="utf-8")
    (out_dir / "4_分组密钥.md").write_text(build_key_md(items), encoding="utf-8")
    (out_dir / "打印版.html").write_text(build_html(cards, codes, args.seed), encoding="utf-8")

    print(f"[pack] 材料包已生成：{out_dir}")

    # 整包复扫：不止卡片，连同须知 / 量表 / 密钥一起扫，防止说明文字里
    # 不小心写出真实姓名或标签词（`final_audit` 与合规检查都是子串扫描）。
    bad = scan_dir(out_dir, names)
    if bad or leak:
        print(f"[pack] ❌ 仍有问题：卡片 {leak} 份 / 文件 {len(bad)} 个")
        return 2
    print(f"[pack] ✅ 整包合规：{len(items)} 份卡片 + "
          f"{len(list(out_dir.rglob('*')))} 个文件，零姓名 / 零标签 / 零来源指纹")
    return 0


def _read_for_scan(p: Path) -> str:
    """把待扫描文件读成文本。docx 是 zip，文字在 `word/*.xml` 里（明文 `w:t`）。"""
    if p.suffix.lower() == ".docx":
        import zipfile
        try:
            with zipfile.ZipFile(p) as z:
                return "".join(z.read(n).decode("utf-8", "ignore")
                               for n in z.namelist()
                               if n.startswith("word/") and n.endswith(".xml"))
        except Exception:                                      # noqa: BLE001
            return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        return ""


def scan_dir(path: Path, names: list) -> list:
    """递归扫描一个目录，返回命中姓名 / 标签词 / 来源指纹的文件清单。

    扫的**不止教案卡片**：连须知、量表、密钥、评分卡一起扫
    —— 说明性文字里写出禁词同样会踩线（本项目合规检查是子串扫描）。
    """
    hit = []
    for p in sorted(path.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in (
                ".md", ".csv", ".html", ".json", ".txt", ".docx"):
            continue
        t = _read_for_scan(p)
        if not t:
            continue
        hn = [n for n in names if n and n in t]
        hb = sorted(set(BANNED_PATTERN.findall(t)))
        hm = [w for w in ("火箭班", "闪电班", "任课老师", "混龄康教班") if w in t]
        if hn or hb or hm:
            hit.append(p.name)
            print(f"[pack] ❌ {p.name}：姓名={hn[:3]} 标签={hb} 指纹={hm}")
    return hit


if __name__ == "__main__":
    raise SystemExit(main())

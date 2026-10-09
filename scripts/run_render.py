"""W3 驱动：把 W2 生成的 3 份 LessonPlan 渲染成模板格式 docx（含分层）。

输入：
  data/generated/{theme}.json       W2 产物（LessonPlan）
  data/structured/child_profiles.json   37 名匿名儿童画像（用于分层）
  data/structured/goal_ladder.json      范例学出的目标阶梯
  data/structured/name_mapping.json     姓名黑名单（合规校验用）

输出：
  data/docx/{title}.docx            教案正文 + 分层附录页
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lpa.layering import build_ladder, GoalLadder                # noqa: E402
from lpa.render_docx import DocxRenderer, check_docx_compliance   # noqa: E402
from lpa.rules import load_forbidden_names                        # noqa: E402
from lpa.schema import LessonPlan                                 # noqa: E402
from lesson_flow import targets_for                               # noqa: E402
from roster_from_corpus import build_history, roster_records      # noqa: E402

# 输入目录：`LPA_GEN_DIR` 可覆盖 —— 消融实验（M4-4）把每个臂的中间稿隔离到
# 自己的临时目录，绝不覆盖 `data/generated/` 里的正式产物。缺省不变。
GEN = Path(os.environ["LPA_GEN_DIR"]) if os.environ.get("LPA_GEN_DIR") \
    else ROOT / "data" / "generated"
# 产物目录：`LPA_OUT_DIR` 可覆盖（供 CLI 指定落点，如输出到临时目录做对比）。
OUT = Path(os.environ["LPA_OUT_DIR"]) if os.environ.get("LPA_OUT_DIR") \
    else ROOT / "data" / "docx"
STRUCT = ROOT / "data" / "structured"

# 交付清单：**只出《国庆节到了》这一份**。
#
# 用户明确要求交付物只有这一个 docx，另外三份（穿鞋子 / 秋天的水果 /
# 音乐做动作）不再产出。它们的中间稿（`data/generated/*.json`）保留在库里，
# 需要时设环境变量 `LPA_RENDER_ALL=1` 即可全量重出，不必回改代码。
RENDER_ONLY: list = ["认知课《国庆节到了》"]

# 每份教案用哪份范例的阶梯。选「结构最完整」的几份做参照。
LADDER_BY_TITLE = {
    "认知课《秋天的水果》": "第四周-能干的我(1).docx",
    "生活自理课《穿鞋子》": "第四周-三月三(1).docx",
    "粗大动作课《音乐做动作》": "第二周-动物的家(1).docx",
    # 新教案《国庆节到了》依据最新教案《童心向党》里幼儿的实际表现生成，
    # 阶梯也沿用同一份范例 —— 目标条文本身就是「指认→说出→表达」的支持梯度，
    # 正好对上幼儿当前「表达」环节的短板。
    "认知课《国庆节到了》": "第五周-童心向党(1).docx",
}

# 手工拆分的目标阶梯覆盖表。
#
# ⚠ 为什么需要它：范例《第五周-童心向党》原文把 6 条目标**全挤在一个单元格**里
# 且没有编号，自动解析只能得到 1 条（语料解析缺口，不是分层逻辑缺口）。
# 但这份范例的目标梯度（全辅助指认 → 少量辅助指认+说出 → 看图表达 → 独立表达）
# 恰好对应幼儿实测表现里的短板（「说出/表达」P 率仅 0.36），
# 所以手工把它拆成带编号的 6 条，供分层引擎使用。
#
# 条文 1 恒为课堂常规，人人必选。
MANUAL_LADDER: dict = {
    # 「句式不动、只换名词」—— 句式取自《第四周-三月三》：
    #   1.整节课堂能安坐好不走出位置不闹情绪
    #   2.能在身体辅助下完成指认三月三习俗名称
    #   3.能在少量手势辅助下完成指认三月三习俗名称
    #   4.能自己独立完成指认三月三习俗名称
    #   5.能自己看图表达表达三月三习俗名称如：跳竹竿舞、抛绣球、对山歌等
    #   6.能参与抛绣球的游戏
    # 只把「三月三习俗名称 / 抛绣球」换成本节课会出现的名词（国旗 / 国庆节庆祝）。
    # ⚠ 句式、辅助量词、动词一律不动。
    "认知课《国庆节到了》": [
        ("1", "整节课堂能安坐好不走出位置不闹情绪"),
        ("2", "能在身体辅助下完成指认国旗"),
        ("3", "能在少量手势辅助下完成指认国旗"),
        ("4", "能自己独立完成指认国旗"),
        ("5", "能自己看图表达国旗的颜色和五角星"),
        ("6", "能参与国庆节的庆祝游戏"),
    ],
}

# 范例语料目录、逐份教案的儿童记录读取，统一收在 `roster_from_corpus.py`，
# 避免两处各写一套行定位规则（早期版本这里另有一份，两边的「筛行」条件
# 不一致，导致同一份范例在两处抽出的人数不同）。


def load_anon_to_name() -> dict:
    """`Sxx` → 真实姓名（私有映射的反向表）。

    仅用于**内部取数**（按身份找上一教案里的同一幼儿），
    产物 docx 里永远只出现 `S01…`，不会写出姓名。
    """
    p = ROOT / "data" / "private" / "name_mapping.json"
    if not p.exists():
        return {}
    m = json.loads(p.read_text(encoding="utf-8"))
    return {v: k for k, v in m.items()}


# ---------------------------------------------------------------------------
# 按班级拆分（本轮新增）
# ---------------------------------------------------------------------------
#
# 用户要求：「按班级拆分『认知课《国庆节到了》』的内容，分别列出各班的
# 儿童姓名代号，并依据每个班级中最新日期的范例，为其填写『轻度』等
# 相关信息项。」
#
# 三个事实来源：
#   ① 幼儿属哪个班  ← lessons.json 里该幼儿所出现教案的 `class_label_raw`
#   ② 班级信息项    ← 该班**日期最新**的那份范例的班级信息行
#   ③ 人数          ← 本课名册里该班的实际人数（用户确认的口径）
#
# ⚠ 两份 `.doc`（星星班6月2日 / 第1周春节回顾）是 Word 97 二进制格式，
#   python-docx 打不开，也不在 lessons.json 里（14 份可解析的 .docx 才有
#   儿童记录）。因此班级只可能来自 .docx 那 14 份。
CORPUS_DIR = Path(r"C:\Users\17093\Desktop\homework\agent use idea")

# ---------------------------------------------------------------------------
# 配置外置：`data/structured/build_config.json` 若存在，覆盖上面的内置默认值。
#
# 为什么要外置：`corpus_dir` / 交付清单 / 阶梯映射 / 手工阶梯原先都硬编码在本文件，
# 换一门课就得改代码。现在改配置即可（配置缺失或解析失败时**原样回退**内置默认，
# 向后兼容）。
#
# 环境变量优先级更高（供 `lesson_docx.py` CLI 调用）：
#   LPA_RENDER_ONLY=主题1,主题2   只渲染这些主题（覆盖 deliver）
#   LPA_RENDER_ALL=1              全量渲染（覆盖一切清单）
#   LPA_CORPUS_DIR=路径           覆盖语料目录
#   LPA_FORCE_SINGLE=1            按班拆分时强制合成单表（回退路径；
#                                 优先级高于配置里的 split_by_class）
# ---------------------------------------------------------------------------
# 每个主题是否需要按班级拆分（来自配置；缺键 = 未表态，沿用旧逻辑）。
SPLIT_BY_CLASS: dict = {}
_CFG_PATH = ROOT / "data" / "structured" / "build_config.json"
if _CFG_PATH.exists():
    try:
        _CFG = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
        if _CFG.get("corpus_dir"):
            CORPUS_DIR = Path(_CFG["corpus_dir"])
        if isinstance(_CFG.get("deliver"), list):
            RENDER_ONLY = list(_CFG["deliver"])
        _themes = _CFG.get("themes") or {}
        if isinstance(_themes, dict):
            for _title, _spec in _themes.items():
                if not isinstance(_spec, dict):
                    continue
                if _spec.get("ladder_source"):
                    LADDER_BY_TITLE[_title] = _spec["ladder_source"]
                if "split_by_class" in _spec:
                    # 显式表态才记：True=拆成多个文件；False=全班一张表
                    SPLIT_BY_CLASS[_title] = bool(_spec["split_by_class"])
                _ml = _spec.get("manual_ladder")
                if _ml:
                    # 统一成 (编号, 条文) 二元组；编号转 str，与 goal_ladder 解析结果同型
                    MANUAL_LADDER[_title] = [(str(n), str(t)) for n, t in _ml]
    except Exception as _e:          # noqa: BLE001 —— 配置坏了不该拖垮渲染
        print(f"[W3] build_config.json 读取失败，用内置默认：{_e}")

if os.environ.get("LPA_CORPUS_DIR"):
    CORPUS_DIR = Path(os.environ["LPA_CORPUS_DIR"])

_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def _date_key(raw: str):
    """把 `2026年6月29日--7月3日` 之类取成可比较的 (年, 月, 日)。"""
    m = _DATE_RE.search(raw or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def children_class_map() -> dict:
    """`Sxx` → 班级名。取自 lessons.json（首现为准）。"""
    les = json.loads((STRUCT / "lessons.json").read_text(encoding="utf-8"))
    out: dict = {}
    for L in les:
        cl = L.get("class_label_raw", "")
        for c in L.get("children") or []:
            out.setdefault(c["anon_id"], cl)
    return out


def _read_class_info_row(path: Path) -> list:
    """读范例主表**第 2 行**（班级信息行）的原始格文本。

    范例该行是 9 个 `<w:tc>`：
      名称 | 年龄范围 | 人数 | 轻度 | 中度 | 重度 | 多重障碍 | 障碍类别
    （含首格空栏）
    """
    from docx import Document
    from docx.oxml.ns import qn
    d = Document(str(path))
    main = max(d.tables, key=lambda t: len(t.rows))
    trs = main._tbl.findall(qn("w:tr"))
    if len(trs) < 2:
        return []
    out = []
    for tc in trs[1].findall(qn("w:tc")):
        out.append("".join(t.text or "" for t in tc.iter(qn("w:t"))).strip())
    return out


def class_latest_exemplar() -> dict:
    """班级 → 该班**日期最新**范例的班级信息项。"""
    les = json.loads((STRUCT / "lessons.json").read_text(encoding="utf-8"))
    best: dict = {}
    for L in les:
        cl = L.get("class_label_raw", "")
        if not cl:
            continue
        k = _date_key(L.get("date_range", ""))
        if cl not in best or k > best[cl][0]:
            best[cl] = (k, L)
    out: dict = {}
    for cl, (_k, L) in best.items():
        fn = L.get("source_file", "")
        info = _read_class_info_row(CORPUS_DIR / fn) if fn else []
        out[cl] = {
            "file": fn,
            "date": L.get("date_range", ""),
            # 信息行布局：0=空栏 1=名称 2=年龄范围 3=人数 4=轻度 … 8=障碍类别
            "name": info[1] if len(info) > 1 else cl,
            "age_range": info[2] if len(info) > 2 else "",
            "size": info[3] if len(info) > 3 else "",
            # 「障碍类别」一格：范例 14 份都有值（火箭班系「智力障碍、自闭症★」、
            # 闪电班系「智力障碍、自闭症   ★」）。用户本轮明确要求照范例填，
            # 所以原样带过去（渲染层只负责往格子里写，不做加工）。
            "disability_label": info[8] if len(info) > 8 else "",
        }
    return out


def build_class_groups(in_session: list, child_records: dict,
                       fallback_age: str = "") -> list:
    """把在册幼儿按班级切成若干组，供 `DocxRenderer.render_multi()` 使用。

    组的顺序按**幼儿编号**排（S01 所在的班在前），保证产物里班级顺序稳定。
    """
    cls_of = children_class_map()
    ex = class_latest_exemplar()

    order: list = []
    for c in in_session:
        cl = cls_of.get(c["anon_id"], "")
        if cl and cl not in order:
            order.append(cl)

    groups: list = []
    for cl in order:
        sub = [c for c in in_session if cls_of.get(c["anon_id"]) == cl]
        if not sub:
            continue
        info = ex.get(cl) or {}
        groups.append({
            "label": cl,
            # 年龄范围取该班最新范例的写法（如 `3----10岁`）；缺则回落
            "age_range": info.get("age_range") or fallback_age,
            # 人数取本课名册里该班的**实际**人数（用户确认的口径）
            "size": len(sub),
            "children": sub,
            "child_records": {c["anon_id"]: child_records[c["anon_id"]]
                              for c in sub if c["anon_id"] in child_records},
            "severity_mark": "★",
            # 障碍类别照**该班最新范例**填（用户本轮要求）
            "disability_label": info.get("disability_label") or "",
            "source": (f"{info.get('file', '')}（{info.get('date', '')}）"
                       if info else ""),
        })
    return groups


def pick_in_session(children: list, n: int, domain: str = "") -> list:
    """从在课儿童里取 n 名代表本班在课样本。

    ⚠ 本函数**当前未被调用** —— 教案现在列**全班名册**（37 人），不再抽样。
    保留它是为了「批量出多份教案、每份只展示一个代表性子集」这类场景。

    不能简单取前 n 名 —— 画像顺序是按原始语料出现顺序排的，前 12 名
    可能恰好集中在某一档（实测出现过 L2=0 的偏态）。

    做法：以**目标领域**的支持层级为依据做等比分层抽样；
    该领域无数据的儿童回落到总体支持层级。
    """
    from collections import defaultdict

    def lv_of(c: dict) -> str:
        if domain:
            for d in c.get("domains") or []:
                if d.get("domain") == domain and d.get("support_level"):
                    return d["support_level"]
        return c.get("support_level", "")

    buckets: dict = defaultdict(list)
    for c in children:
        buckets[lv_of(c)].append(c)

    total = len(children)
    picked: list = []
    alloc = {}
    for lv, xs in buckets.items():
        alloc[lv] = max(1, round(len(xs) / total * n))
    order = sorted(buckets, key=lambda k: -len(buckets[k]))
    while sum(alloc.values()) > n:
        for lv in order:
            if alloc[lv] > 1 and sum(alloc.values()) > n:
                alloc[lv] -= 1
    while sum(alloc.values()) < n:
        for lv in order:
            if alloc[lv] < len(buckets[lv]) and sum(alloc.values()) < n:
                alloc[lv] += 1

    for lv in order:
        picked.extend(buckets[lv][:alloc[lv]])
    chosen = {c["anon_id"] for c in picked}
    for c in children:
        if len(picked) >= n:
            break
        if c["anon_id"] not in chosen:
            picked.append(c)
            chosen.add(c["anon_id"])
    return picked[:n]


def main() -> None:
    profile = json.loads((STRUCT / "class_profile.json").read_text(encoding="utf-8"))
    class_label = profile.get("class_label", "混龄康教班（已匿名）")
    age_range = profile.get("age_range", "3-9岁")

    # ⚠ 不再剔除「缺席」幼儿 —— 这份教案要列**全班名册**。
    #   范例里缺勤幼儿的数据行照样在表里（五次评量写「未参与」，基线格也写
    #   「未参与」，如《第四周-清凉夏日》的蒙智宇整行 7 格全为「未参与」），
    #   所以新教案同样列出，其「康复基线」按用户要求统一标 `未参与`。
    children = json.loads(
        (STRUCT / "child_profiles.json").read_text(encoding="utf-8"))

    ladders = json.loads(
        (STRUCT / "goal_ladder.json").read_text(encoding="utf-8")).get("docs", {})

    OUT.mkdir(parents=True, exist_ok=True)
    jsons = sorted(GEN.glob("*.json"))
    if not jsons:
        print("no generated json found")
        return

    names = load_forbidden_names(str(ROOT / "data" / "private" / "name_mapping.json"))
    print(f"[W3] 姓名黑名单 {len(names)} 条 | 全班在册 {len(children)} 名"
          f"（含缺勤，教案按全班名册出）")

    # 交付清单过滤：优先级 LPA_RENDER_ALL > LPA_RENDER_ONLY > 配置/内置 deliver。
    #   （`LPA_RENDER_ONLY` / `LPA_CORPUS_DIR` 供 `lesson_docx.py` CLI 调用，
    #    不必回改这个文件就能换主题 / 换语料目录。）
    _env_only = [t.strip() for t in
                 os.environ.get("LPA_RENDER_ONLY", "").split(",") if t.strip()]
    only: list = []
    if os.environ.get("LPA_RENDER_ALL") != "1":
        only = _env_only or list(RENDER_ONLY)
    if only:
        print(f"[W3] 交付清单：{'、'.join(only)}（其余跳过；"
              f"全量重出请设 LPA_RENDER_ALL=1）")

    all_ok = True
    for jp in jsons:
        data = json.loads(jp.read_text(encoding="utf-8"))
        plan = LessonPlan(**data)

        if only and plan.title not in only:
            continue

        # 选阶梯：优先用**手工拆分**的覆盖表，其次配置映射，最后回落
        manual = MANUAL_LADDER.get(plan.title)
        ladder_doc = LADDER_BY_TITLE.get(plan.title)
        spec = None
        if manual:
            spec = list(manual)
            src = f"（手工拆分，源自 {ladder_doc or '范例'}）"
        elif ladder_doc and ladder_doc in ladders:
            spec = [(n, t) for n, t in ladders[ladder_doc]["items"]]
            src = ladder_doc
        else:
            for k, v in ladders.items():
                if len(v["items"]) >= 4:
                    spec = [(n, t) for n, t in v["items"]]
                    src = k
                    break
            else:
                src = "（无）"

        out = OUT / f"{plan.title or jp.stem}.docx"

        # ---------- 班级名册：全班在册幼儿，逐个带本人最近一次记录 ----------
        #
        # 用户要求：「补充其他目前未出现在认知课《国庆节到了》中的幼儿代号，
        # 并依据包含其姓名的最新日期范例，补充康复基线与康复目标。」
        #
        # 所以这一列不再是「抽样 12 人」，而是**全班名册**（37 人，含缺勤者），
        # 每位幼儿的 `康复目标` 与 `康复基线` 都取自**他/她最近一次有记录的
        # 教案**（`roster_from_corpus.roster_records`）：
        #   · 有近期记录的（如《第五周-童心向党》那批）→ 直接用最近值；
        #   · 只有早期记录的（如只在《第二周-动物的家》出现过）→ 用那份的值；
        #   · 整轮未参与的 → 基线统一标 `未参与`。
        #
        # 「最近一次」按 lessons.json 里的 `date_range` 起始日排序，
        # 同名多份时以文件名序稳定取后者。
        # 本课阶梯（供分层与调层共用）
        ladder = build_ladder(list(spec)) if spec else GoalLadder(items=[])

        anon2name = load_anon_to_name()
        hist = build_history()
        records = roster_records(anon2name, hist)

        # 本轮新链路：把**上一份教案的五次评量**回灌，算出本课的
        # 层级 / 康复目标 / 康复基线（见 scripts/lesson_flow.py 的配置逻辑）：
        #   评量 ──连续2次≥85%→调层──▶ 层级 ──▶ 该层的组合规律 ──▶ 目标
        #                                   └─▶ 基线：保留目标沿用原记号、
        #                                       新增目标记 E
        planned = targets_for(anon2name, ladder, hist)
        in_session = [c for c in children if c.get("anon_id") in records]
        in_session.sort(key=lambda c: c.get("anon_id", ""))
        child_records = {}
        for c in in_session:
            aid = c["anon_id"]
            rec = dict(records[aid])
            p = planned.get(aid) or {}
            # 层级 / 依据：只在 planner 真给出层级时覆盖（否则保留画像判定）
            if p.get("layer") is not None:
                rec["layer"] = p["layer"]
                rec["prev_layer"] = p.get("prev_layer")
                rec["prev_goal"] = p.get("prev_goal", "")
            # 判定依据与层级**同源**：planner 给了说明就用它，
            # 否则（画像回落 / 缺勤且无阶梯）保留画像口径的依据，两者都不打架
            if p.get("basis"):
                rec["basis"] = p["basis"]
            if p.get("goal"):
                rec["goal"] = p["goal"]
            # 基线：即使层级回落画像，也要把「未参与」这类明确值带过来
            if p.get("baseline"):
                rec["baseline"] = p["baseline"]
            if p.get("absent"):
                rec["absent"] = True
            child_records[aid] = rec
        roster_note = f"全班名册（{len(in_session)} 人在册）"

        # ---------- 按班级拆分：**每班各出一个独立文件** ----------
        # 用户澄清：「拆成两个教案，两个文件」—— 不是一份文档里放两张表。
        # 每个文件都自成一体：大标题 → 任课老师/时间 → 本班主表 → 本班附录。
        # 幼儿编号**不重编**，保持全局唯一（火箭班 S01–S28、闪电班 S29–S37），
        # 这样两个文件的代号互不撞号，也能与附录、其他文件对上。
        groups = build_class_groups(in_session, child_records,
                                    fallback_age=age_range)

        r = DocxRenderer(
            class_size=len(in_session),
            class_label=class_label,
            date_range=plan.date_range,
            children=in_session,
            ladder_spec=spec,
            child_records=child_records,
            # 单表回退路径（LPA_FORCE_SINGLE=1）也要填障碍类别，
            # 取名册里第一个班的写法；走 render_multi 时会被逐班覆盖。
            disability_label=(groups[0]["disability_label"] if groups else ""),
        )

        force_single = os.environ.get("LPA_FORCE_SINGLE") == "1"
        # 拆分与否：配置 `split_by_class` 优先；未配置才回落到「名册跨班就拆」。
        # （三个留出主题在配置里写了 false，此前被忽略、照拆不误。）
        _cfg_split = SPLIT_BY_CLASS.get(plan.title)
        do_split = (len(groups) > 1) if _cfg_split is None else bool(_cfg_split)
        if force_single:
            do_split = False
        outs: list = []          # [(path, group|None, layers, n_from_record)]
        if do_split and len(groups) > 1:
            for g in groups:
                sub = out.with_name(f"{out.stem}-{g['label']}{out.suffix}")
                r.render_multi(plan, sub, [g])
                outs.append((sub, g, list(r._layers),
                             getattr(r, "_n_from_record", 0)))
        else:
            r.render(plan, out)
            # 单表回退：一份文件装全班，班级信息行标「全班」+ 实际总人数
            gg = None
            if groups:
                gg = dict(groups[0])
                gg["label"] = "全班"
                gg["size"] = len(in_session)
            outs.append((out, gg, list(r._layers),
                         getattr(r, "_n_from_record", 0)))

        from docx import Document
        from lpa.layering import layer_count, layer_combos

        n_lv = layer_count()
        print(f"[W3] {plan.title} | 阶梯源：{src}"
              f"（{len(spec) if spec else 0} 条） | 交付 {len(outs)} 个文件")
        for path, g, layers, n_rec in outs:
            # 「障碍类别」是模板固有栏目里**按范例填**的内容，整串豁免标签词扫描
            exempt = ([g["disability_label"]]
                      if g and g.get("disability_label") else [])
            bad = check_docx_compliance(path, names, exempt_fragments=exempt)
            if bad:
                all_ok = False
            d = Document(str(path))
            # ⚠ 不能用 len(rows) 最大的那张 —— 附录的「儿童代号」「调层」两表
            #   都比主表长。认表头里的「活动班级」准。
            main = next((t for t in d.tables
                         if t.rows and "活动班级" in "".join(
                             c.text for c in t.rows[0].cells)),
                        d.tables[-1])
            cnt: dict = {}
            for cl in layers:
                cnt[cl.layer] = cnt.get(cl.layer, 0) + 1
            size = path.stat().st_size
            print(f"  · {path.name} | {'PASS' if not bad else 'FAIL'}"
                  f" | 违规 {len(bad)} 项 | {size} bytes"
                  f"（{size/1024:.1f} KB）")
            if g:
                print(f"      {g['label']}：{g['size']} 人 | 年龄范围"
                      f"「{g['age_range']}」| 轻/中/重/多 {g['severity_mark']}"
                      f" | 障碍类别「{g['disability_label']}」")
                print(f"      信息项源 {g['source']}")
            print(f"      主表 {len(main.rows)}行×{len(main.columns)}列 | "
                  f"总表数 {len(d.tables)}（含附录表）")
            print(f"      分层 {n_lv} 层：" + " ".join(
                f"L{L}={cnt.get(L, 0)}" for L in range(1, n_lv + 1))
                  + f" | 定层来源：按组合规律 {n_rec} 名 | "
                    f"画像回落 {len(layers) - n_rec} 名")
            for b in bad[:5]:
                print(f"       - {b}")

        combos = layer_combos(ladder)
        print("     各层组合：" + " ".join(
            f"L{L}={'/'.join(map(str, c))}" for L, c in sorted(combos.items())))
        n_abs = sum(1 for v in child_records.values() if v.get("absent"))
        n_up = sum(1 for v in child_records.values()
                   if v.get("layer") and v.get("prev_layer")
                   and v["layer"] > v["prev_layer"])
        n_dn = sum(1 for v in child_records.values()
                   if v.get("layer") and v.get("prev_layer")
                   and v["layer"] < v["prev_layer"])
        n_keep = len(in_session) - n_abs - n_up - n_dn
        print(f"     调层：升级 {n_up} | 维持 {n_keep} | 降级 {n_dn} | "
              f"未参与（不调层） {n_abs}")
        print(f"     康复基线：保留目标沿用原记号 + 新增目标记 E"
              f"（{len(in_session) - n_abs} 条）| 未参与 {n_abs} 条")

    print("ok" if all_ok else "HAS VIOLATIONS")


if __name__ == "__main__":
    main()

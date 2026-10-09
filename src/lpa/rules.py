"""硬校验层：把模型的自由发挥关进笼子。

三条铁律：
1. 标签词零泄漏 —— 命中即整份判废，直接触发重写。
2. 目标必须可观测 —— 禁止"培养动手能力"这类空话。
3. 结构必须自洽 —— 时长、材料、分层覆盖、指南对齐，缺一不可。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

from .schema import (
    Alternative,
    LessonPlan,
    Level,
    Objective,
    ProcedureStep,
)
from .taxonomy import OUTPUT_LEVELS, SUPPORT_LEVELS

# --------------------------------------------------------------------------
# 1. 标签词黑名单
# --------------------------------------------------------------------------

BANNED_TERMS: Sequence[str] = (
    "自闭症", "孤独症", "阿斯伯格", "阿斯伯格综合征",
    "发育迟缓", "智力障碍", "智障", "脑瘫", "唐氏", "唐氏综合征",
    "多动症", "ADHD", "注意缺陷", "感统失调", "感觉统合失调",
    "言语障碍", "语言障碍", "听力障碍", "视障", "听障",
    "特殊儿童", "特殊需要儿童", "特殊幼儿", "特殊教育", "特教",
    "随班就读", "资源教室", "个训", "个别化训练", "融合教育",
    "障碍儿童", "迟缓儿童", "问题行为", "异常行为", "缺陷",
)

BANNED_PATTERN = re.compile("|".join(re.escape(t) for t in BANNED_TERMS))

# --------------------------------------------------------------------------
# 2. 可观测动词
# --------------------------------------------------------------------------

# 允许出现在目标里的可观测行为动词
OBSERVABLE_VERBS: Sequence[str] = (
    "说出", "指认", "指出", "拿取", "取出", "放回", "配对", "分类", "排序",
    "串起", "串成", "穿过", "捏住", "抓握", "撕", "贴", "涂", "画", "剪",
    "模仿", "跟随", "完成", "独立", "表达", "回应", "复述",
    "参与", "尝试", "选择", "示意", "点头", "摇头", "挥手", "轮流", "等待",
    "数出", "点数", "比较", "拼接", "搭建", "哼唱", "拍手",
    # 课堂常规类（语料里每份教案的第 1 条都是「安坐」类目标）
    "安坐", "坐好", "保持", "遵守",
    # 2026-10-08 补漏：真 LLM 生成的目标用了这些合法动词，旧白名单没有，
    # 被误判成"缺少可观测动词"。补进来 —— 注意只补真动作/真行为词，
    # 像"进行""具有"这类虚动词不补，那是真的不合格。
    "找到", "找出", "挑选", "辨认", "区分", "拿出", "做出",
    "举起", "举手", "起立", "蹲下", "踏步", "转身", "弯腰", "跨过",
    "搬运", "擦拭", "踢", "跳",
)

# 抽象动词 / 教育套话，出现在目标里即判不合格
VAGUE_TERMS: Sequence[str] = (
    "培养", "提高", "提升", "增强", "发展能力", "养成良好的", "激发兴趣",
    "树立", "陶冶", "熏陶", "懂得道理", "感受美", "体验快乐", "开拓思维",
)

VAGUE_PATTERN = re.compile("|".join(re.escape(t) for t in VAGUE_TERMS))


# --------------------------------------------------------------------------
# 3. 违规项
# --------------------------------------------------------------------------


@dataclass
class Violation:
    code: str
    message: str
    fatal: bool = False

    def __str__(self) -> str:
        tag = "[致命]" if self.fatal else "[警告]"
        return f"{tag} {self.code}: {self.message}"


@dataclass
class ValidationReport:
    violations: List[Violation] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(v.fatal for v in self.violations)

    @property
    def fatal_count(self) -> int:
        return sum(1 for v in self.violations if v.fatal)

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if not v.fatal)

    def add(self, code: str, message: str, fatal: bool = False) -> None:
        self.violations.append(Violation(code, message, fatal))

    def to_text(self) -> str:
        if not self.violations:
            return "校验通过：无致命错误，无警告。"
        head = [
            f"致命错误 {self.fatal_count} 项，警告 {self.warning_count} 项",
            "结论：" + ("不通过，需要重写" if not self.passed else "通过，建议优化"),
            "",
        ]
        return "\n".join(head + [str(v) for v in self.violations])


# --------------------------------------------------------------------------
# 4. 各项检查
# --------------------------------------------------------------------------


def _iter_plan_text(plan: LessonPlan) -> Iterable[str]:
    """收集教案中所有会出现在纸面上的文本。

    ⚠ 这里是「标签词零泄漏」检查的取样范围，必须**覆盖全部可见文本**，
    漏掉任何一处都会让检查形同虚设。
    """
    yield plan.title
    yield plan.theme
    yield plan.class_label
    yield plan.age_range
    yield plan.environment
    yield plan.safety_notes
    yield plan.extension
    yield plan.home_extension
    yield plan.class_objectives_text
    yield plan.main_domain
    yield plan.reflection_slots
    yield plan.key_points.get("focus", "")
    yield plan.key_points.get("difficulty", "")
    yield from plan.materials
    yield from plan.reflection_points
    yield from plan.assessment.values()
    for obj in plan.objectives:
        yield obj.text
        yield obj.criteria
        yield obj.observable_verb
    for step in plan.steps:
        yield step.name
        yield step.teacher_script
        yield step.child_action
        yield step.visual_cue
        yield from step.scaffolds
        yield from step.observation_points
    for goal in plan.individualized_goals:
        yield goal.goal
        yield goal.baseline_hint
        yield goal.evaluation_method
        yield goal.follow_up
    for alt in plan.alternatives:
        yield alt.situation
        yield alt.alternative_participation
        yield alt.alternative_evaluation


def check_no_labels(plan: LessonPlan, report: ValidationReport) -> None:
    """标签词零泄漏。这是本项目最难也最重要的一条规则。"""
    hits = set()
    for text in _iter_plan_text(plan):
        found = BANNED_PATTERN.findall(text or "")
        hits.update(found)
    if hits:
        report.add(
            "LABEL_LEAK",
            f"出现标签词 {sorted(hits)}，违反去标识化要求",
            fatal=True,
        )


# 「进行」「具有」这类词能当谓语，但不可观测 —— 不能拿它冒充动作词
VIRTUAL_TERMS: Sequence[str] = (
    "进行", "具有", "开展", "做到", "达到", "有所", "具备", "成为",
)
VIRTUAL_PATTERN = re.compile("|".join(re.escape(t) for t in VIRTUAL_TERMS))

# 半句话标记：目标文本以省略号收尾 = 模型没写完
ELLIPSIS_PATTERN = re.compile(r"…+|\.{3,}|等等")


def _has_observable_verb(obj) -> bool:
    """目标里到底有没有可观测的动作。两条路任一命中即算合格：

    1. text 含白名单动作动词（OBSERVABLE_VERBS）
    2. text 原样含模型**自己填的** observable_verb，且该词不是虚动词/抽象词

    为什么加第 2 条：白名单永远补不全。LLM 的用词空间比人能列的表大得多，
    2026-10-08 实测补完「找到/做出」，下一轮它立刻改用「拿起/穿上」。
    与其无限打地鼠，不如用模型自己的承诺去验它自己 —— schema 里本来就有
    observable_verb 字段，它既然声明了这个动词，就必须让它出现在 text 里。
    这样判据从「词必须在某人列的清单上」变成「声明与文本必须自洽」，
    既堵住虚动词，又不会误杀合法新词。
    """
    text = obj.text or ""
    if any(v in text for v in OBSERVABLE_VERBS):
        return True
    v = (getattr(obj, "observable_verb", "") or "").strip()
    if not v or v not in text:
        return False
    if VAGUE_PATTERN.search(v) or VIRTUAL_PATTERN.search(v):
        return False
    return True


def check_objectives_observable(plan: LessonPlan, report: ValidationReport) -> None:
    for obj in plan.objectives:
        hit = VAGUE_PATTERN.search(obj.text)
        if hit:
            report.add(
                "OBJ_VAGUE",
                f"目标含抽象表述「{hit.group()}」：{obj.text}",
                fatal=True,
            )
        if ELLIPSIS_PATTERN.search(obj.text):
            report.add(
                "OBJ_TRUNCATED",
                f"目标没写完（含省略号/等等）：{obj.text}",
                fatal=True,
            )
        if not _has_observable_verb(obj):
            report.add(
                "OBJ_NOT_OBSERVABLE",
                f"目标缺少可观测动词：{obj.text}",
                fatal=True,
            )
        if not re.search(r"\d", obj.criteria):
            report.add(
                "CRITERIA_NOT_MEASURABLE",
                f"达成标准缺少可计数条件：{obj.criteria}",
                fatal=False,
            )


def check_level_coverage(plan: LessonPlan, report: ValidationReport) -> None:
    """A/B/C 三层目标必须齐全，评量标准也必须覆盖三层。"""
    levels = {obj.level for obj in plan.objectives}
    missing = {lv.value for lv in Level} - {lv.value for lv in levels}
    if missing:
        report.add(
            "LEVEL_MISSING",
            f"缺少分层目标：{sorted(missing)}",
            fatal=True,
        )
    assess_keys = set(plan.assessment)
    for lv in Level:
        if lv.value not in assess_keys:
            report.add(
                "ASSESS_MISSING",
                f"评价要点缺少 {lv.value} 层标准",
                fatal=True,
            )
    # 个别化目标也必须三层齐全（这是方案甲的核心要求）
    goal_levels = {g.level.value for g in plan.individualized_goals}
    miss_g = {lv.value for lv in Level} - goal_levels
    if miss_g:
        report.add(
            "INDIV_LEVEL_MISSING",
            f"个别化差异目标缺少层次：{sorted(miss_g)}",
            fatal=True,
        )


def check_duration_consistency(plan: LessonPlan, report: ValidationReport) -> None:
    total = sum(step.duration_min for step in plan.steps)
    if total != plan.total_duration_min:
        report.add(
            "DURATION_MISMATCH",
            f"环节时长合计 {total} 分钟，与活动总时长 {plan.total_duration_min} 分钟不一致",
            fatal=True,
        )


def check_scaffold_density(plan: LessonPlan, report: ValidationReport) -> None:
    for step in plan.steps:
        if not step.scaffolds:
            report.add(
                "STEP_NO_SCAFFOLD",
                f"环节 {step.index}「{step.name}」缺少支持措施",
                fatal=True,
            )
        if not step.visual_cue.strip():
            report.add(
                "STEP_NO_CUE",
                f"环节 {step.index}「{step.name}」缺少视觉提示",
                fatal=True,
            )


def check_alternatives(plan: LessonPlan, report: ValidationReport) -> None:
    if len(plan.alternatives) < 2:
        report.add(
            "ALT_INSUFFICIENT",
            f"替代性参与方式只有 {len(plan.alternatives)} 条，至少需要 2 条",
            fatal=True,
        )


def check_target_count(plan: LessonPlan, report: ValidationReport) -> None:
    """目标条数应落在语料实测区间内。

    语料实测：每份教案 4—6 条康复目标（去重后 12 条目标文本，条数 4~6）。
    少于 3 条说明任务分解不足；多于 8 条说明拆得过碎、一节课铺不下。
    """
    n = len(plan.objectives)
    if n < 3:
        report.add("OBJ_FEW", f"目标只有 {n} 条，任务分解不足", fatal=True)
    elif n > 8:
        report.add("OBJ_MANY", f"目标有 {n} 条，超出单节课可承载的范围", fatal=False)


def check_support_levels_present(plan: LessonPlan, report: ValidationReport) -> None:
    """每条目标都应声明合法的支持层级。

    支持层级是本项目「分层」的物理载体 —— 没有它，A/B/C 就只是标签。
    """
    for obj in plan.objectives:
        if obj.support_level not in SUPPORT_LEVELS:
            report.add(
                "SUPPORT_INVALID",
                f"目标「{obj.text[:20]}」的支持层级非法：{obj.support_level!r}",
                fatal=True,
            )


# --------------------------------------------------------------------------
# 姓名泄漏检测（两档，避免误报）
# --------------------------------------------------------------------------
#
# 教训：早期版本用「常见姓氏 + 1~3 个汉字」做通用匹配，结果把
# 「任务拆成」「高清图卡」「许先旁观」全判成了人名 —— 误报率高到不可用。
# 这类"泛模式"检测在中文里根本行不通：姓和常用字高度重叠。
#
# 正确的做法是**瞄准真正的泄漏渠道**，而不是广谱扫描：
#   渠道 1：拿真实姓名清单来查（数据在手，最可靠）
#   渠道 2：检测"名单形态"的结构特征（连续多个姓氏词、顿号/逗号分隔的人名串）

_NAME_ROSTER_SPLIT = re.compile(r"[、，,和及与]\s*")

# 常见姓氏。注意：只在**人名串结构判定**里用，不做全篇扫描 ——
# 全篇扫描的误报率高到不可用（见上面的教训）。
_SURNAMES = (
    "陈蒙谢郑何杨邹丁莫卢覃姚朱梁黄韦刘甘曾"
    "李王张赵吴周徐孙马胡郭高林罗宋唐许韩冯邓曹彭肖田董袁潘于蒋蔡余杜叶程苏"
    "魏吕任沈姜崔钟谭陆汪范金石贾夏付方白孟熊秦邱江尹薛段雷侯龙史陶黎贺顾毛"
)
_SINGLE_NAME = re.compile(
    rf"^([{_SURNAMES}][\u4e00-\u9fa5]{{1,2}})$"
)


def load_forbidden_names(path: str) -> List[str]:
    """从 name_mapping.json 读真实姓名清单。这是最可靠的泄漏判据。"""
    try:
        import json
        with open(path, encoding="utf-8") as f:
            return list(json.load(f).keys())
    except Exception:
        return []


def check_no_real_names(plan: LessonPlan, report: ValidationReport,
                        forbidden: Optional[Sequence[str]] = None) -> None:
    """姓名泄漏检测。

    两档判据，二者都会报致命错误：

    **档 1（精确）**：拿真实姓名清单逐字比对。
    只要 `forbidden` 非空，这是最可靠的 —— 数据就在手上。

    **档 2（结构）**：检测"人名串"的形态。
    真实泄漏的表现不是孤立出现一个词，而是**成串出现**：
    如「陈映燊、蒙智宇、谢集坤」。
    因此判据是：**同一段文本内出现 ≥ 2 个"姓氏 + 1~2 字"且以顿号/逗号分隔的片段**。
    单个疑似词不报（避免「任务拆成」这类误报），成串才报。
    """
    texts = [t for t in _iter_plan_text(plan) if t]

    # 档 1：精确清单
    if forbidden:
        import json
        hits = sorted({n for n in forbidden for t in texts if n in t})
        if hits:
            report.add(
                "REAL_NAME_LEAK",
                f"命中真实姓名清单：{hits}",
                fatal=True,
            )

    # 档 2：人名串结构
    for t in texts:
        for segment in re.split(r"[。；;\n]", t):
            parts = [p.strip() for p in _NAME_ROSTER_SPLIT.split(segment) if p.strip()]
            if len(parts) < 2:
                continue
            roster = [p for p in parts if _SINGLE_NAME.match(p)]
            if len(roster) >= 2:
                report.add(
                    "NAME_ROSTER_LEAK",
                    f"出现疑似人名串：{roster}",
                    fatal=True,
                )
                break


def check_materials_and_steps(plan: LessonPlan, report: ValidationReport) -> None:
    if len(plan.materials) < 3:
        report.add("MATERIAL_FEW", "材料清单少于 3 项", fatal=False)
    if len(plan.steps) < 4:
        report.add(
            "STEP_FEW",
            f"环节只有 {len(plan.steps)} 步，任务分解不足够细",
            fatal=True,
        )


ALL_CHECKS = (
    check_no_labels,
    check_objectives_observable,
    check_level_coverage,
    check_duration_consistency,
    check_scaffold_density,
    check_alternatives,
    check_target_count,
    check_support_levels_present,
    check_materials_and_steps,
)


def validate_plan(plan: LessonPlan,
                  forbidden_names: Optional[Sequence[str]] = None) -> ValidationReport:
    """跑全部硬校验。

    `forbidden_names` 传真实姓名清单（来自 name_mapping.json）时，
    会额外做一次精确的姓名泄漏比对 —— 这是最可靠的判据。
    """
    report = ValidationReport()
    for fn in ALL_CHECKS:
        fn(plan, report)
    check_no_real_names(plan, report, forbidden=forbidden_names)
    return report


# --------------------------------------------------------------------------
# 5. 附加统计指标（用于评估环节）
# --------------------------------------------------------------------------


def plan_metrics(plan: LessonPlan) -> dict:
    total_steps = max(1, len(plan.steps))
    scaffold_total = sum(len(s.scaffolds) for s in plan.steps)
    steps_with_cue = sum(1 for s in plan.steps if s.visual_cue.strip())
    return {
        "steps": total_steps,
        "scaffold_per_step": round(scaffold_total / total_steps, 2),
        "cue_coverage": round(steps_with_cue / total_steps, 2),
        "objectives": len(plan.objectives),
        "alternatives": len(plan.alternatives),
        "individualized_goals": len(plan.individualized_goals),
        "duration": plan.total_duration_min,
    }

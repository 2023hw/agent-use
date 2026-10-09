"""W2 生成链：目标分层 → 环节编排 → 支架注入 → 自检重写。

四步是**顺序依赖**的，每步只做一件事，前步产物是后步输入：

  1. layering   按班级支持层级分布，把主题拆成 A/B/C 三层目标
  2. sequencing 按语料共有的六段式骨架，把目标铺进环节、分配时长
  3. scaffolding 给每个环节注入视觉提示 + 多感官支架 + 任务分解
  4. reflection 跑 rules.py 硬校验；有致命错误就带反馈重写（最多 N 轮）

**为什么拆四步而不是一次生成**：一次生成会让模型在"目标合理性"和
"话术自然度"之间互相让步，且出错时无法定位是哪一环坏了。
拆开后每一步都能单独评估、单独消融，也能单独用 mock 替换。

**mock 模式**：不调 LLM，用 knowledge.py 的规则库拼装。
既保证离线可跑，也是消融实验里"无 LLM"的对照组。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Dict, List, Optional, Sequence

from .knowledge import (
    ALTERNATIVE_LIBRARY,
    DOMAIN_LIBRARY,
    PROCEDURE_SKELETON,
    SAFETY_NOTES,
    SCAFFOLD_LIBRARY,
    domain_scaffold,
    domain_verbs,
    scaffold_for,
)
from .taxonomy import strip_theme_verb
from .llm import LLMClient, LLMError
from .rules import ValidationReport, plan_metrics, validate_plan
from .schema import (
    Alternative,
    IndividualizedGoal,
    LessonPlan,
    Level,
    Objective,
    ProcedureStep,
)
from .taxonomy import (
    OUTPUT_LEVELS,
    SUPPORT_LEVELS,
    classify_domain,
    detect_support,
    support_from_mastery,
)


# --------------------------------------------------------------------------
# 生成请求
# --------------------------------------------------------------------------


@dataclass
class GenerateRequest:
    """一次生成任务的输入。**不含任何幼儿身份信息。**"""

    theme: str                              # 主题原始输入，如「认识秋天的水果」
    theme_core: str = ""                    # 剥掉引导动词后的核心词（留空自动推）
    theme_raw: str = ""                     # 原始主题留档，供产物头部显示
    title: str = ""                         # 教案标题（可显式指定），留空则按领域自动拼
    class_label: str = "混龄康教班（已匿名）"
    age_range: str = "3-9岁"
    total_duration_min: int = 30
    class_objectives_text: str = ""         # 「有益经验」，可留空由系统补
    # 班级支持层级分布：domain -> {support_level: 人数}
    distribution: Dict[str, Dict[str, int]] = field(default_factory=dict)
    class_size: int = 12
    date_range: str = ""
    main_domain_hint: str = ""              # 可指定主领域，留空则自动判定
    materials_hint: List[str] = field(default_factory=list)
    # 教学内容（环节脚本 / 材料 / 有益经验 / 延伸 / 个别化措辞）是否交给 LLM 扩写。
    # mock 模式下本字段无效（一律走模板）—— 这正是 M4-4 消融实验的对照组。
    llm_content: bool = True
    # ---------------- M4-4 消融开关 ----------------
    # 下面是「关掉某个模块」的开关。四个开关**互相独立**，默认全开 = 完整系统。
    # 消融实验（scripts/ablation_study.py）逐个关闭，量出每个模块的边际贡献。
    # 关掉时必须是**确定性的功能缺失**（不是随机失败），指标才可归因。
    llm_objectives: bool = True     # 步骤 1 目标分层是否用 LLM（False → 规则库兜底）
    use_scaffolding: bool = True    # 步骤 3 是否注入支架与视觉提示（False → 留空）
    self_repair: bool = True        # 步骤 4 是否跑自检重写（False → 不做规则修补）


@dataclass
class StepTrace:
    """每一步的留痕，用于报告与调试。"""

    name: str
    ok: bool
    detail: str = ""
    llm_calls: int = 0
    rewritten: int = 0


@dataclass
class GenerateResult:
    plan: Optional[LessonPlan]
    report: ValidationReport
    traces: List[StepTrace] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    usage: Dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------

SYSTEM_BASE = """你是一位资深的特殊教育集体课教师，正在为 3—9 岁混龄康教班撰写一周的集体课教案。

硬性要求（违反任何一条即判废）：
1. **绝不出现任何幼儿姓名、诊断名称、障碍类别。** 不得出现「自闭症」「智力障碍」
   「发育迟缓」「特殊儿童」「特教」「随班就读」等任何标签词，也不得暗示。
2. 目标必须**可观测**：用「说出/指认/配对/完成/跟随」这类能被看到的行为动词，
   禁止「培养」「提高」「感受」「激发兴趣」这类空话。
3. 目标按 A/B/C 三层组织，写清每层各约几名幼儿适用。
4. 每个环节都要有**视觉提示**和**至少一条支持措施**。
5. 环节时长之和必须等于活动总时长。

你的输出必须是**合法 JSON**，不要任何解释性文字。"""


# 教学内容扩写专用 system prompt。与 SYSTEM_BASE 的硬约束**完全一致**，另加两条：
#   ① 要求内容围绕本主题「具体化」（说得出材料、动作、指令），不准写放之四海皆准的空话；
#   ② 再次强调零标签 —— 扩写正文是最容易把「特殊/障碍」这类词带进来的地方。
SYSTEM_CONTENT = SYSTEM_BASE + """

补充要求（本次任务是**扩写教学内容**，不是写评语）：
6. 内容必须**紧扣本次主题**的具体材料、动作与指令，能说出幼儿实际看到、摸到、
   做什么。禁止写「根据幼儿情况灵活调整」「关注个体差异」这类放到任何主题都成立的空话。
7. 每个环节的支持措施要**落在该环节真的会发生的事**上（如"把图片放到视线水平"），
   不要堆砌与环节无关的通用话术。"""


# 虚构人名的常见形态。真实姓名由 `forbidden_names` 清单覆盖，这里堵的是
# **模型自己编的示例名** —— 实测 glm-4-flash 写出过「我是小明的朋友哦」。
# ⚠ 只收**明确是人名/称呼**的写法，**不做姓氏泛匹配**：那套做法在本项目实测过，
#   误报率高到不可用（见 rules.py 顶部关于「任务拆成 / 高清图卡」的教训注释）。
PLACEHOLDER_NAMES: Sequence[str] = (
    "小明", "小红", "小刚", "小丽", "小华", "小强", "小美", "小杰", "小亮",
    "某某某", "某某", "张三", "李四", "王五", "XXX", "×××",
)
PLACEHOLDER_NAME_PATTERN = re.compile(
    "|".join(re.escape(t) for t in PLACEHOLDER_NAMES))

# 内部分层术语。分层是**教师备课时的内部分析**，不能出现在教师对幼儿说的话里 ——
# 实测模型写出过「请 A 层的小朋友站起来，B 层的小朋友拍拍手」这样的教师原话，
# 既泄露了分层标签，实际教学里也不可能这么对幼儿说话。
# ⚠ 用 `\uff21-\uff23`（全角 ＡＢＣ）而不是字面全角字符：本文件的写入链会把
#   字面全角字符规范化成半角，导致全角形态漏网（实测过）。
LEVEL_TERM_PATTERN = re.compile("[ABC\uff21-\uff23]\\s*层")


# --------------------------------------------------------------------------
# 生成器
# --------------------------------------------------------------------------


@dataclass
class LessonGenerator:
    client: LLMClient = field(default_factory=lambda: LLMClient(provider="mock"))
    max_rewrites: int = 2
    verbose: bool = False
    forbidden_names: Sequence[str] = field(default_factory=list)

    # ---------------- 主入口 ----------------

    def generate(self, req: GenerateRequest) -> GenerateResult:
        traces: List[StepTrace] = []

        # ---- 步骤 0：主题归一（只做一次，后三步统一使用剥好的主题）----
        # 语料主题常写成「动词 + 宾语」（「认识秋天的水果」「我会穿鞋子」
        # 「跟着音乐做动作」）。把引导动词剥掉后，"秋天的水果"才能安全地
        # 当宾语嵌进「能了解…」「今天我们要一起看一看…」这类句子。
        # 原主题留在 req.theme_raw 里，供报告与产物头部显示。
        raw_theme = req.theme
        if not req.theme_core:
            req = replace(req, theme_core=strip_theme_verb(raw_theme))
        req = replace(req, theme_raw=raw_theme,
                      theme=req.theme_core)

        # ---- 步骤 1：目标分层 ----
        layering = self._step_layering(req)
        traces.append(layering["trace"])
        if layering.get("error"):
            return GenerateResult(None, ValidationReport(), traces)

        # ---- 步骤 2：环节编排 ----
        sequencing = self._step_sequencing(req, layering)
        traces.append(sequencing["trace"])
        if sequencing.get("error"):
            return GenerateResult(None, ValidationReport(), traces)

        # ---- 步骤 3：支架注入 ----
        scaffolding = self._step_scaffolding(req, layering, sequencing)
        traces.append(scaffolding["trace"])

        # ---- 步骤 3.5：教学内容扩写（LLM）----
        # 前面三步产出的都是「骨架 + 占位文案」：步骤名、时长、支架档位都由代码
        # 定死，正文是 `f"…{theme}…"` 这类句式。本步把这些正文交给 LLM 按主题
        # 写具体内容（教师原话 / 幼儿动作 / 材料 / 有益经验 / 延伸 / 个别化措辞）。
        # ⚠ 只覆盖**内容字段**，不碰 name / index / duration_min —— 渲染层靠
        #   `step.name` 归入四个固定小标题，改名字会直接打乱版式。
        # mock 模式或 req.llm_content=False 时整步跳过，退回模板拼装，
        # 这正是 M4-4 消融实验「无 LLM 扩写」的对照组形态。
        narrative: Optional[Dict[str, Any]] = None
        if self.client.provider != "mock" and req.llm_content:
            narrative = self._step_narrative(req, layering, scaffolding)
            traces.append(narrative["trace"])

        # ---- 步骤 4：自检重写 ----
        plan = self._assemble(req, layering, sequencing, scaffolding, narrative)
        report = self._validate(plan)
        rewrites = 0
        # M4-4 消融：关掉自检重写 → 只校验、不修补，产物保持第一版原样。
        if req.self_repair:
            while not report.passed and rewrites < self.max_rewrites:
                rewrites += 1
                fixed = self._repair(plan, report, req)
                if fixed is None:
                    break
                plan = fixed
                report = self._validate(plan)

        traces.append(StepTrace(
            name="自检重写",
            ok=report.passed,
            detail=(("[消融] 已关自检重写，" if not req.self_repair else "")
                    + f"致命错误 {report.fatal_count} 项、警告 {report.warning_count} 项"
                    + (f"，重写 {rewrites} 轮后通过" if report.passed and rewrites
                       else f"，重写 {rewrites} 轮仍不通过" if not report.passed else "")),
            rewritten=rewrites,
        ))

        return GenerateResult(
            plan=plan,
            report=report,
            traces=traces,
            metrics=plan_metrics(plan),
            usage=self.client.total_usage,
        )

    def _validate(self, plan: LessonPlan) -> ValidationReport:
        return validate_plan(plan, forbidden_names=self.forbidden_names)

    # ---------------- 步骤 1：目标分层 ----------------

    def _step_layering(self, req: GenerateRequest) -> Dict[str, Any]:
        """目标分层。

        ⚠ **本步不修改 req**。早期版本在这里 `replace(req, theme=strip_…)`，
        结果剥离只落在本步内部：`_script_for` / `_assemble` 仍拿到带动词的
        原主题，于是导入语照样写出「今天我们要一起看一看**认识**秋天的水果」。
        正确做法是**主入口只剥一次**（见 `generate()`），各步统一用剥好的主题。
        """
        domain = req.main_domain_hint or self._infer_domain(req.theme)
        dist = self._layer_counts(req, domain)

        called_llm = False
        if self.client.provider == "mock":
            goals = self._mock_objectives(req, domain, dist)
            detail = f"mock 模式，按规则库生成 {len(goals)} 条目标"
        elif not req.llm_objectives:
            # M4-4 消融：关掉 LLM 目标分层，退回规则库拼装（与 mock 同一条路径）。
            goals = self._mock_objectives(req, domain, dist)
            detail = f"[消融] 已关 LLM 分层，规则库生成 {len(goals)} 条目标"
        else:
            called_llm = True
            try:
                goals = self._llm_objectives(req, domain, dist)
                detail = f"{len(goals)} 条目标，主领域 {domain}"
            except LLMError as e:
                goals = self._mock_objectives(req, domain, dist)
                detail = f"LLM 失败({e})，降级 mock，{len(goals)} 条目标"

        return {
            "domain": domain,
            "layer_counts": dist,
            "objectives": goals,
            "trace": StepTrace("目标分层", bool(goals), detail,
                               llm_calls=1 if called_llm else 0),
        }

    def _infer_domain(self, theme: str) -> str:
        d = classify_domain(theme)
        return d if d != "未分类" else "认知"

    @staticmethod
    def _normalize_counts(dist: Dict[str, int], n: int) -> Dict[str, int]:
        """把各支持层级人数按比例归一到班级实际规模。

        ⚠ **切勿把各层人数直接相加**。class_profile.json 的 distribution 是
        **按领域分别统计**的 —— 同一批幼儿在不同领域上会落到不同层级，
        所以各层级人数之和远超班级人数。实测「粗大动作」各层相加 = 18，
        而在课仅 12 名；直接相加会写出「约 11 名」，等于凭空造人。
        这里按比例（最大余数法）归一到 n：既保住层级间的相对关系，
        又保证 A + B + C == n。
        """
        items = [(k, v) for k, v in (dist or {}).items() if v > 0]
        total = sum(v for _, v in items)
        if total <= 0:
            return {}
        raw = {k: v * n / float(total) for k, v in items}
        out = {k: int(v) for k, v in raw.items()}
        order = sorted(raw, key=lambda k: raw[k] - int(raw[k]), reverse=True)
        i = 0
        while sum(out.values()) < n:
            out[order[i % len(order)]] += 1
            i += 1
        return out

    def _layer_counts(self, req: GenerateRequest, domain: str) -> Dict[str, int]:
        """把班级在**主领域**上的支持层级分布，压缩成 A/B/C 三层人数。

        A 层 = 需要最多支持的人（示范模仿 / 大量身体辅助）
        B 层 = 中等支持（少量身体辅助 / 手势辅助）
        C 层 = 可独立完成或接近独立
        这是本系统「识别不同层级幼儿」的直接体现。
        A + B + C 恒等于班级在课人数。
        """
        n = max(3, req.class_size)
        counts = self._normalize_counts((req.distribution or {}).get(domain, {}), n)
        if not counts:
            # 没有分布数据时按经验比例兜底，并保证三层都非空
            a, b = round(n * 0.33), round(n * 0.45)
            return {"A": max(1, a), "B": max(1, b), "C": max(1, n - a - b)}

        a = counts.get("示范模仿", 0) + counts.get("大量身体辅助", 0)
        b = counts.get("少量身体辅助", 0) + counts.get("手势辅助", 0)
        c = counts.get("独立完成", 0)
        # 保底：某层人数为 0 时给 1，避免"某层不存在"导致教案缺层。
        # 只从当前人数最多的层借 1 人，保证合计仍精确等于 n。
        for lv in ("A", "B", "C"):
            if {"A": a, "B": b, "C": c}[lv] == 0:
                donor = max((("A", a), ("B", b), ("C", c)), key=lambda kv: kv[1])[0]
                if donor == "A":
                    a -= 1
                elif donor == "B":
                    b -= 1
                else:
                    c -= 1
                if lv == "A":
                    a = 1
                elif lv == "B":
                    b = 1
                else:
                    c = 1
        return {"A": max(1, a), "B": max(1, b), "C": max(1, c)}

    @staticmethod
    def _obj_label(domain: str) -> str:
        """目标句的宾语类别：让目标读起来像人话，而不是语法占位符。"""
        return {
            "认知": "内容",
            "语言沟通": "内容",
            "精细动作": "操作",
            "粗大动作": "动作",
            "生活自理": "步骤",
            "社会交往": "做法",
            "情绪行为": "课堂常规",
        }.get(domain, "内容")

    def _mock_objectives(self, req: GenerateRequest, domain: str,
                         dist: Dict[str, int]) -> List[Objective]:
        """规则库拼装：每条目标拆成辅助递减的三档。

        ⚠ 生成后用 `_ensure_observable` 兜底 —— 保证每条目标里都**真的**
        含有 OBSERVABLE_VERBS 中的词，否则 rules.py 会判不合格。
        这一步是必须的：领域库的动词是"候选池"，拼进句子后未必命中。

        ⚠ **不要硬套领域库的 `objects`**。早期版本写成
        「说出{theme}中的{objects}」，结果生成出「说出我会穿鞋子中的餐具」
        这种不通的句子 —— 因为 `objects` 是领域的通用宾语池，
        与具体主题未必匹配。改为：**主题本身充当宾语**。

        ⚠ **不要把领域库的 verbs 直接拼成宾语**。更早的版本写
        `相关的{verbs[1]}`，粗大动作生成出「相关的爬」、生活自理生成出
        「相关的收拾」，认知生成出「相关的说出」—— 因为 verbs 是"动词池"，
        拿动词当宾语必错。改为按领域给出**宾语类别词**（内容/操作/动作/步骤）。
        """
        lib = DOMAIN_LIBRARY.get(domain, DOMAIN_LIBRARY["认知"])
        verbs = lib["verbs"]
        core = strip_theme_verb(req.theme)          # 剥掉引导动词的主题原词
        label = self._obj_label(domain)
        form = lib["forms"][0]

        layers = [
            ("A", "示范模仿",
             f"能跟随老师一起{form}，在辅助下指出{core}的{label}"),
            ("B", "手势辅助",
             f"能在手势提示下说出{core}中的 1—2 个{label}"),
            ("C", "独立完成",
             f"能独立完成{core}的{label}，并说出自己的做法"),
        ]
        out: List[Objective] = []
        for lv, support, text in layers:
            out.append(Objective(
                level=Level(lv),
                domain=domain,
                support_level=support,
                text=self._ensure_observable(text, verbs),
                observable_verb=self._pick_verb(text, verbs),
                criteria=f"{dist.get(lv, 1)} 名幼儿中至少 2 名能在本次活动中达到",
            ))
        # 补一条课堂常规目标（语料里每份教案都有的第 1 条）
        out.insert(0, Objective(
            level=Level("A"),
            domain="情绪行为",
            support_level="手势辅助",
            text="能在提示下整节课堂安坐，不走出位置",
            observable_verb="安坐",
            criteria="整节课累计离开位置不超过 1 次",
        ))
        return out

    @staticmethod
    def _pick_verb(text: str, pool: Sequence[str]) -> str:
        for v in pool:
            if v in text:
                return v
        return pool[0] if pool else "完成"

    @staticmethod
    def _ensure_observable(text: str, pool: Sequence[str]) -> str:
        """保证文本含可观测动词；不含则前置一个最通用的。

        这里只兜底，不改变语义 —— 拼装模板本身已尽量带上动词。
        """
        from .rules import OBSERVABLE_VERBS
        if any(v in text for v in OBSERVABLE_VERBS):
            return text
        verb = pool[0] if pool else "说出"
        return f"能{verb}{text[1:]}" if text.startswith("能") else f"能{verb}{text}"

    def _llm_objectives(self, req: GenerateRequest, domain: str,
                        dist: Dict[str, int], hint: str = "") -> List[Objective]:
        from .rules import OBSERVABLE_VERBS
        verbs = "、".join(OBSERVABLE_VERBS)
        prompt = f"""请为主题「{req.theme}」设计集体课的分层活动目标。

班级情况：
- 班次：{req.class_label}，年龄 {req.age_range}，在课 {req.class_size} 名
- 本次主领域：{domain}
- 分层人数：A 层（需较多支持）约 {dist['A']} 名，
  B 层（需中等支持）约 {dist['B']} 名，C 层（可独立完成）约 {dist['C']} 名

输出 JSON，格式：
{{
  "objectives": [
    {{
      "level": "A",
      "domain": "{domain}",
      "support_level": "示范模仿",
      "text": "能在老师示范后跟随做 2 个拍手动作",
      "observable_verb": "跟随",
      "criteria": "全班 12 名中至少 2 名能达到"
    }}
  ]
}}

要求：
- 至少 4 条、至多 8 条目标
- A/B/C 三层都要有
- support_level 只能取这六个之一：独立完成、手势辅助、少量身体辅助、大量身体辅助、示范模仿、未参与
- 第 1 条固定是课堂常规目标（安坐类）
- text 必须是**完整句子**，结尾不许出现「……」或「等等」，也不要只写半句
- text 里必须出现一个可观测的具体动作词，observable_verb 字段填的就是它，
  且它必须**原样**取自这份词表：{verbs}
- 禁止用「进行」「具有」「提升」「加强」这类虚动词充当动作词。
  反例（不合格）：能根据音乐节奏进行简单的舞蹈动作
  正例（合格）：能跟随音乐节奏拍手 3 次
- criteria 里必须出现数字
- 不得出现任何虚构的人名或称呼（如「小明」「小红」「某某」「XX」），
  需要指代幼儿时一律用「小朋友」「幼儿」
- 只输出 JSON"""
        if hint:
            # 修订通道（P0-2）：Agent 拿到校验失败明细后，把「哪里错、怎么改」
            # 作为 hint 带回来重新生成。hint 只补充约束，不豁免上文任何硬性要求。
            prompt += ("\n\n【本次修订要求（按此调整，且不得违反上述全部硬性要求）】\n"
                       + hint.strip())
        data = self.client.complete_json(SYSTEM_BASE, prompt)
        return self._sanitize_objectives(
            [Objective(**o) for o in data["objectives"]])

    # 各层支持层级的默认值（与 `_mock_objectives` 的口径一致）。
    _LEVEL_SUPPORT: ClassVar[Dict[str, str]] = {
        "A": "示范模仿", "B": "手势辅助", "C": "独立完成",
    }

    def _sanitize_objectives(self, objs: List[Objective]) -> List[Objective]:
        """把 LLM 产出的目标规范到合法取值。

        实测模型会犯两类低级错：
          ① `support_level` 填成动作词（真的出现过 `'安坐'`）—— 支持层级是本项目
             「分层」的物理载体，非法即被 `rules.check_support_levels_present`
             判 fatal，且 `_repair` 不修这类错，会一路 FAIL 到终检；
          ② `level` 写成小写或带空白。
        这里按层兜底纠正，让"格式瑕疵"不上升为"判废"。
        """
        out: List[Objective] = []
        from .rules import OBSERVABLE_VERBS
        for o in objs:
            lv = str(getattr(o.level, "value", o.level) or "").strip().upper()
            if lv not in self._LEVEL_SUPPORT:
                lv = "B"
            sup = str(o.support_level or "").strip()
            if sup not in SUPPORT_LEVELS:
                sup = self._LEVEL_SUPPORT[lv]
            # 虚构人名就地替换而非丢弃目标 —— 目标条数是分层覆盖的硬要求
            # （缺层必 fatal），不能因一个示例名把整条目标删掉。
            text = PLACEHOLDER_NAME_PATTERN.sub("小朋友", str(o.text))
            # 可观测动词**自洽性**兜底：模型填的 observable_verb 未必真的出现在
            # text 里（实测出现过「能安静地坐在座位上」却配 verb='安坐'），
            # rules 的自洽判据会判 fatal。这里按「文本原样命中 → 领域动词池」两级
            # 顺序把动词对齐，让格式瑕疵不上升为判废。
            verb = str(o.observable_verb or "").strip()
            if not (verb and verb in text):
                hit = next((v for v in OBSERVABLE_VERBS if v in text), "")
                if hit:
                    verb = hit
                else:
                    pool = DOMAIN_LIBRARY.get(o.domain or "", DOMAIN_LIBRARY["认知"])["verbs"]
                    text = self._ensure_observable(text, pool)
                    verb = self._pick_verb(text, pool)
            out.append(Objective(level=Level(lv), domain=o.domain, support_level=sup,
                                 text=text, observable_verb=verb,
                                 criteria=PLACEHOLDER_NAME_PATTERN.sub(
                                     "小朋友", str(o.criteria))))

        # 条数上限 8（rules.check_target_count 对 >8 报 warn）。截断时**保层**：
        # 先各留 A/B/C 的第一条，再按原顺序补足，绝不因截断弄出"缺层"。
        if len(out) > 8:
            idx: List[int] = []
            for lv in ("A", "B", "C"):
                idx += [i for i, o in enumerate(out) if o.level.value == lv][:1]
            for i in range(len(out)):
                if len(idx) >= 8:
                    break
                if i not in idx:
                    idx.append(i)
            out = [out[i] for i in sorted(idx)]
        return out

    # ---------------- 步骤 2：环节编排 ----------------

    def _step_sequencing(self, req: GenerateRequest,
                         layering: Dict[str, Any]) -> Dict[str, Any]:
        skeleton = self._fit_skeleton(req.total_duration_min)
        domain = layering["domain"]     # 幼儿动作按主领域区分（动作课 vs 认知课）
        steps = []
        for i, seg in enumerate(skeleton, start=1):
            steps.append(ProcedureStep(
                index=i,
                name=seg["name"],
                duration_min=seg["duration_min"],
                teacher_script=self._script_for(seg, req, layering),
                child_action=self._action_for(seg, domain),
                visual_cue="",       # 步骤 3 填
                scaffolds=[],        # 步骤 3 填
                observation_points=[f"观察幼儿在「{seg['name']}」中是否需要提示"],
            ))
        total = sum(s.duration_min for s in steps)
        if total != req.total_duration_min:      # 兜底补齐，保证时长自洽
            steps[-1].duration_min += req.total_duration_min - total
        return {
            "steps": steps,
            "trace": StepTrace("环节编排", True,
                               f"{len(steps)} 个环节，合计 {req.total_duration_min} 分钟"),
        }

    def _fit_skeleton(self, total_min: int) -> List[Dict[str, Any]]:
        """把比例骨架换算成整数分钟，并保证每段至少 1 分钟、合计精确相等。"""
        segs = []
        for s in PROCEDURE_SKELETON:
            d = max(1, round(total_min * s["ratio"]))
            segs.append({"name": s["name"], "goal": s["goal"], "duration_min": d})
        diff = total_min - sum(x["duration_min"] for x in segs)
        if diff:
            # 差额补到「核心活动」（承载主要学习任务的那一段）
            for x in segs:
                if x["name"] == "核心活动":
                    x["duration_min"] += diff
                    break
            else:
                segs[-1]["duration_min"] += diff
        return segs

    # 各环节的教师引导语模板。`{theme}` 在调用处填入**已剥掉引导动词**的主题。
    # ⚠ 不要把整句写死在 `_script_for` 里 —— 早期版本在方法内部再拼一次
    # `f"…认识「{theme}」"`，绕过了归一化，生成出「要看一看认识秋天的水果」。
    SCRIPT_TEMPLATES: ClassVar[Dict[str, str]] = {
        "师生问好": "师：摇铃鼓，小朋友们请坐好，小手放好。小朋友们早上好！"
                    "（等待每一位幼儿用语言、点头或挥手回应）",
        "点名互动": "师：老师要看看今天哪几位小朋友来了。点到名字的小朋友，"
                    "请举手，或者说「到」。",
        "热身律动": "师：请小朋友跟着老师一起动动小手。伸出你的小手，上拍拍；"
                    "伸出你的小手，下拍拍；伸出你的小手，转一转；"
                    "伸出你的小手，坐下来。",
        "活动导入": "师：小朋友看一看，今天老师带来了什么？（出示实物）"
                    "今天我们要一起看一看{theme}。",
        "核心活动": "师：请看图片。图上有什么呀？（停顿 3 秒，等幼儿反应）"
                    "我们一起说说看。来，跟着老师说一遍——{theme}。",
        "复练与小结": "师：我们再来一次。这次老师不说了，请小朋友自己试一试。"
                      "（减少提示，重复核心步骤）",
        "结束与延伸": "师：今天小朋友们都很棒！老师表扬你们顶呱呱！"
                      "回家以后，请和爸爸妈妈一起说一说今天学的内容。再见！",
    }

    def _script_for(self, seg: Dict[str, Any], req: GenerateRequest,
                    layering: Dict[str, Any]) -> str:
        name = seg["name"]
        tpl = self.SCRIPT_TEMPLATES.get(name, f"师：我们来完成「{name}」这一步。")
        return tpl.format(theme=req.theme)

    # 幼儿动作模板。`{theme}` 同 `SCRIPT_TEMPLATES`。
    # ⚠ 认知类主题和动作类主题的幼儿行为不一样：让幼儿"说出看到的东西"对
    # 「跟着音乐做动作」不成立 —— 那份教案的幼儿动作改成"听音乐、跟随老师做动作"。
    ACTION_TEMPLATES: ClassVar[Dict[str, str]] = {
        "师生问好": "幼儿用语言、点头或挥手回应老师",
        "点名互动": "听到自己的名字后举手或用手指自己",
        "热身律动": "跟随老师做手指动作",
        "活动导入": "观察老师出示的材料，尝试说出看到的东西",
        "核心活动": "看图、听指令，完成指认或配对的练习",
        "复练与小结": "在减少提示的情况下重复完成关键步骤",
        "结束与延伸": "听老师小结，和老师挥手道别",
    }

    # 按主领域覆盖「活动导入 / 核心活动」的幼儿动作
    ACTION_BY_DOMAIN: ClassVar[Dict[str, Dict[str, str]]] = {
        "粗大动作": {
            "活动导入": "看老师做动作，尝试跟着动一动",
            "核心活动": "听音乐、看示范，跟随老师做出相应动作",
        },
        "精细动作": {
            "活动导入": "观察老师手里的材料，尝试伸手摸一摸",
            "核心活动": "按步骤动手操作，完成捏、贴、串等动作",
        },
        "生活自理": {
            "活动导入": "观察老师出示的物品，尝试说出名称",
            "核心活动": "按步骤自己动手完成操作，必要时接受提示",
        },
        "社会交往": {
            "活动导入": "看老师和同伴互动，尝试参与进来",
            "核心活动": "与同伴轮流、配合，完成一次互动",
        },
        "语言沟通": {
            "活动导入": "听老师说话，尝试用声音或动作回应",
            "核心活动": "看图、听提问，尝试用词语或短句回应",
        },
    }

    def _action_for(self, seg: Dict[str, Any], domain: str = "") -> str:
        name = seg["name"]
        return self.ACTION_BY_DOMAIN.get(domain, {}).get(
            name, self.ACTION_TEMPLATES.get(name, "按老师指令完成本环节任务"))

    # ---------------- 步骤 3：支架注入 ----------------

    def _step_scaffolding(self, req: GenerateRequest, layering: Dict[str, Any],
                          sequencing: Dict[str, Any]) -> Dict[str, Any]:
        steps: List[ProcedureStep] = sequencing["steps"]
        counts = layering["layer_counts"]
        # 支架强度按 A 层人数占比决定：A 层越多，支架越重
        n = max(1, req.class_size)
        a_ratio = counts["A"] / n
        base = self._base_support(a_ratio)

        # ⚠ 支架**必须按环节分档，不能全套同一份**。
        # 早期版本所有 7 个环节都用同一套 cues，产物一眼就是模板复读。
        # 这里给每个环节指定它真正需要的支持层级：
        #   常规环节（问好/点名）→ 一到两条固定常规支持，不必堆支架
        #   导入/核心（新授）    → 给 base 及以上，且不同层级取不同侧重的 cues
        #   复练（撤提示）       → 必须**比 base 低一档**，否则"减少提示"是假的
        #   结束（迁移）         → 给独立完成的 cues，指向家庭延伸
        plan_by_step = {
            "师生问好": ["用手势和表情示意，等幼儿回应", "允许用点头、挥手代替说话"],
            "点名互动": ["把点名卡放到幼儿视线水平", "点到名字后多等 3 秒，不替他回答"],
            "热身律动": ["老师站在幼儿正前方，保证看得清",
                         "动作放慢到幼儿跟得上", "请坐得好的同伴做小老师"],
            "活动导入": ["先让幼儿摸一摸、闻一闻再提问", "出示实物和图片两种形式"],
            "核心活动": ["把任务拆成「看—指—说」三步，一步一提示",
                         "允许用指认代替命名，用选择代替回答",
                         "关键信息用红色标记，其余刺激撤走"],
            "复练与小结": ["撤去一半图片，只留关键的两张",
                           "老师不说话，只用手指点提示位置",
                           "答对立刻具体表扬（说出他做到了什么）"],
            "结束与延伸": ["请幼儿把学到的内容说给同伴听",
                           "发一张回家练习卡，请家长配合"],
        }
        # 领域专属支架需要知道本课主领域；与 203 行的口径保持一致。
        domain = req.main_domain_hint or self._infer_domain(req.theme)
        if not req.use_scaffolding:
            # M4-4 消融：关掉支架注入。视觉提示与支持措施整块留空（骨架里本就为空），
            # 交给下游规则校验去判「缺支架 / 缺视觉提示」——那正是本模块的贡献度量。
            # ⚠ 不在此处补，也不让 `_repair` 补（见 `_repair` 第 4 条），
            #   否则消融臂会被自动修复回原样，量不出差距。
            return {
                "steps": steps,
                "base_support": base,
                "trace": StepTrace("支架注入", False,
                                   "[消融] 已关支架注入，视觉提示与支持措施留空"),
            }
        for s in steps:
            s.visual_cue = self._cue_for(s.name)
            s.scaffolds = self._scaffolds_for(s.name, base, plan_by_step, domain)
            s.observation_points = self._observe_for(s.name)

        detail = (f"{len(steps)} 个环节按环节分档注入视觉提示与支架，"
                  f"基准支持层级 {base}；A 层占比 {a_ratio:.0%}")
        return {
            "steps": steps,
            "base_support": base,
            "trace": StepTrace("支架注入", True, detail),
        }

    @staticmethod
    def _base_support(a_ratio: float) -> str:
        if a_ratio >= 0.40:
            return "大量身体辅助"
        if a_ratio >= 0.20:
            return "少量身体辅助"
        return "手势辅助"

    @staticmethod
    def _scaffolds_for(step_name: str, base: str,
                       plan_by_step: Dict[str, List[str]],
                       domain: str = "") -> List[str]:
        """按环节语义给支架：新授重、复练轻、常规只保底。

        ⚠ **领域通用 cues 必须筛过再用**。受训教师的经验里，"手势辅助"层级的
        标准话术是「用手指点出目标」「用表情和点头回应」，但把它们塞进
        **热身律动**（"伸出你的小手，上拍拍"）就变成语义冲突 ——
        律动环节根本没有"目标点"可指。所以先按环节白名单过滤，
        只保留在**这个环节里说得通**的通用 cues。
        """
        own = list(plan_by_step.get(step_name, []))
        if step_name in ("师生问好", "点名互动"):
            # 常规建立环节：只用环节专属支持，不叠通用 cues
            return own

        if step_name == "复练与小结":
            # 撤提示环节：取比 base 更轻一档，且只取与撤提示语义相符的
            lighter = {"大量身体辅助": "少量身体辅助",
                       "少量身体辅助": "手势辅助",
                       "手势辅助": "独立完成"}.get(base, "独立完成")
            pool = SCAFFOLD_LIBRARY[lighter]["cues"]
            keep = [c for c in pool if any(k in c for k in ("点", "口头", "表扬", "说"))]
            return own + keep[:1]

        if step_name == "结束与延伸":
            pool = SCAFFOLD_LIBRARY["独立完成"]["cues"]
            keep = [c for c in pool if "口头" in c or "表扬" in c]
            return own + keep[:1]

        # 导入 / 核心 / 热身：从「大量身体辅助 + 少量身体辅助」两档合并池里取，
        # 这两档的 cues 都是「把材料放到手边」「拆成更小步骤」这类
        # **操作层面的支持**，在任何新授环节都成立。
        pool: List[str] = []
        for lv in ("大量身体辅助", "少量身体辅助"):
            pool += list(SCAFFOLD_LIBRARY[lv]["cues"])
        # 去重后按环节取前 2 条，避免各环节完全雷同
        seen, picked = set(own), []
        # ⚠ 原来「活动导入」与「核心活动」的 offset 都是 0，两个环节从同一个
        #   池子里取到的是同一组 cues（实测撞车）。现在按环节错开起点；
        #   池子扩充到 10 条（大量身体辅助 5 + 少量身体辅助 5）后才有错开的空间。
        offset = {"热身律动": 0, "活动导入": 2, "核心活动": 4}.get(step_name, 0)
        for c in pool[offset:]:
            if c in seen:
                continue
            seen.add(c)
            picked.append(c)
            if len(picked) >= 2:
                break
        # 领域专属策略：只在与主题强相关的环节注入（导入 / 核心），
        # 常规与律动环节不加 —— 那些环节与发展领域无关，加了会语义冲突
        # （道理同本函数开头注释：「热身律动没有目标点可指」）。
        # 为不撑长支架条目，用 1 条领域专属顶掉 1 条通用，总数仍是 2 条。
        if domain and step_name in ("活动导入", "核心活动"):
            ds = domain_scaffold(domain, 2)
            idx = 0 if step_name == "活动导入" else 1
            if len(ds) > idx:
                picked = picked[:1] + [ds[idx]]
        return own + picked

    def _cue_for(self, step_name: str) -> str:
        table = {
            "师生问好": "手势：老师挥手，等幼儿回应后点头微笑",
            "点名互动": "视觉：点名卡 + 幼儿照片贴板",
            "热身律动": "视觉：动作示范图卡（拍拍手 / 转一转 / 坐下来）",
            "活动导入": "视觉：实物 + 高清图片，放在幼儿视线水平",
            "核心活动": "视觉：图片按顺序贴出，配手指指示；关键物用红色标记",
            "复练与小结": "视觉：只保留 2 张关键图片，撤去多余刺激",
            "结束与延伸": "视觉：挥手动作 + 再见图卡",
        }
        return table.get(step_name, "视觉：配合语言出示相应图卡")

    def _observe_for(self, step_name: str) -> List[str]:
        base = [f"「{step_name}」中幼儿是否需要提示才能完成"]
        if step_name == "核心活动":
            base += [
                "幼儿用的是语言回应还是指认回应",
                "幼儿在第几次尝试后能独立完成",
                "幼儿是否出现回避或情绪反应",
            ]
        elif step_name == "复练与小结":
            base += ["撤去提示后幼儿能否独立完成", "幼儿的正确率相比首次是否提高"]
        return base

    # ---------------- 步骤 3.5：教学内容扩写 ----------------

    @staticmethod
    def _step_goals() -> Dict[str, str]:
        """环节职责表（name → goal），供 LLM 定位本环节该写什么。"""
        return {s["name"]: s.get("goal", "") for s in PROCEDURE_SKELETON}

    def _step_narrative(self, req: GenerateRequest, layering: Dict[str, Any],
                        scaffolding: Dict[str, Any]) -> Dict[str, Any]:
        """把「骨架 + 模板文案」扩写成「围绕主题的具体教学内容」。

        **为什么分两次 LLM 调用而不是一次**：
          ① 一次同时要 7 段教学过程 + 材料 + 有益经验 + 延伸 + 个别化措辞，
             JSON 过长，模型极易漏字段（实测单次返回 7 段的完整度明显低于分块）；
          ② 两块的可校验口径不同 —— 教学过程要锁「步骤名与顺序一字不差」，
             课前课后不需要锁名字，混在一起会让 prompt 的约束互相稀释。
        任一块失败都**独立降级**（保留模板），不影响另一块。

        ⚠ 本步只写**内容字段**，绝不改 `name` / `index` / `duration_min`。
        """
        steps: List[ProcedureStep] = scaffolding["steps"]
        domain = layering["domain"]
        n_steps, has_support, footnote = 0, False, ""
        support_data: Dict[str, Any] = {}

        # --- 调用 A：教学过程（7 个环节的正文）---
        try:
            proc = self._llm_procedure(req, domain, layering, steps)
            n_steps = self._apply_procedure(steps, proc,
                                            allow_scaffold=req.use_scaffolding)
        except (LLMError, KeyError, TypeError, ValueError, IndexError) as e:
            footnote += f"教学过程扩写失败({type(e).__name__})，该块保留模板；"

        # --- 调用 B：课前课后（有益经验 / 材料 / 环境 / 延伸 / 个别化措辞）---
        try:
            support_data = self._llm_support(req, domain, layering)
            has_support = bool(support_data)
        except (LLMError, KeyError, TypeError, ValueError, IndexError) as e:
            support_data = {}
            footnote += f"课前课后扩写失败({type(e).__name__})，该块保留模板；"

        detail = (f"教学过程扩写 {n_steps}/{len(steps)} 段、"
                  f"课前课后 {'成功' if has_support else '失败'}"
                  + (f"；{footnote}" if footnote else ""))
        return {
            "steps_ok": n_steps,
            "support": support_data,
            "trace": StepTrace("内容扩写", bool(n_steps or has_support), detail,
                               llm_calls=2),
        }

    def _llm_procedure(self, req: GenerateRequest, domain: str,
                       layering: Dict[str, Any],
                       steps: List[ProcedureStep]) -> Dict[str, Any]:
        """调用 A：产出 7 个环节的教师原话 / 幼儿动作 / 视觉提示 / 支持措施。"""
        goals = self._step_goals()
        counts = layering["layer_counts"]
        skeleton_lines = []
        for s in steps:
            skeleton_lines.append(
                f'{s.index}. {s.name}（{s.duration_min} 分钟）——本环节要做的：'
                f'{goals.get(s.name, "按环节常规组织")}')
        skeleton = "\n".join(skeleton_lines)
        obj_lines = "\n".join(
            f'- [{o.level.value}] {o.text}' for o in layering["objectives"])
        prompt = f"""请为集体课《{req.theme}》扩写**教学过程**的正文。

主题：{req.theme}（主领域：{domain}）
班级：{req.class_label}，年龄 {req.age_range}，在课 {req.class_size} 名
分层：A 层（需较多支持）约 {counts['A']} 名、B 层约 {counts['B']} 名、C 层约 {counts['C']} 名

本节课要达成的分层目标（正文要服务于它们）：
{obj_lines}

环节骨架（**名字、顺序、时长都不可改动，只填每个环节的正文**）：
{skeleton}

输出 JSON：
{{
  "steps": [
    {{
      "name": "师生问好",
      "teacher_script": "师：摇铃鼓，小朋友们请坐好……（教师能直接照读的原话，必须以「师：」开头）",
      "child_action": "幼儿……（幼儿在这一环节实际会做的动作）",
      "visual_cue": "（配合语言出示的视觉或手势提示，一句话）",
      "scaffolds": ["（本环节具体怎么支持需要帮助的幼儿，1—2 条）"],
      "observation_points": ["（本环节教师该观察什么，1—2 条）"]
    }}
  ]
}}

硬性要求：
1. **必须原样返回 {len(steps)} 个环节**，name 一字不差、顺序一致、一条不少。
2. 内容必须紧扣「{req.theme}」——写出具体的材料、动作、指令。
   反例（空洞，不合格）：请小朋友跟着老师一起做动作。
   正例（具体，合格）：师：看，老师手里有一面小旗，旗面是红色的，上面还有五颗星星。
3. 不出现任何幼儿姓名、诊断名称、障碍类别，也不得暗示。
4. 不要在正文里写分钟数（时长由系统控制）。
5. 支持措施要按环节分档：新授环节（导入 / 核心）提示要足，复练环节要**主动撤提示**，
   结束环节指向独立完成与家庭延伸。
6. 每条文本都是完整句子，不许出现「……」「等等」这类省略。
7. **每个环节提到的物品、动作都必须是本主题真实会出现的**。
   反例（串味，不合格）：音乐课上写「老师手里有一面红色的小旗，上面有五颗星星」。
   正例（合格）：师：听，音乐响起来了，我们跟着节奏拍拍手。
8. 教师原话、幼儿动作、视觉提示、支持措施里**都不得出现「A 层 / B 层 / C 层」**
   这类说法 —— 分层是老师备课时的内部分析，不是教案正文该有的写法。需要区分时，
   用「需要较多支持的幼儿」「能独立完成的幼儿」这类描述。
   反例（不合格）：教师站在 A 层幼儿旁边，帮助他们跟上节奏。
   正例（合格）：教师站在需要较多支持的幼儿旁边，手把手带着做。
9. 只输出 JSON。"""
        return self.client.complete_json(SYSTEM_CONTENT, prompt)

    def _llm_support(self, req: GenerateRequest, domain: str,
                     layering: Dict[str, Any]) -> Dict[str, Any]:
        """调用 B：产出有益经验 / 材料 / 环境 / 延伸 / 重难点 / 个别化措辞。"""
        counts = layering["layer_counts"]
        prompt = f"""请为集体课《{req.theme}》扩写**课前准备与课后延伸**的内容。

主题：{req.theme}（主领域：{domain}）
班级：{req.class_label}，年龄 {req.age_range}，在课 {req.class_size} 名
分层：A 层约 {counts['A']} 名、B 层约 {counts['B']} 名、C 层约 {counts['C']} 名

输出 JSON：
{{
  "class_objectives_text": "1.……\\n2.……\\n3.……（三条班级总目标「有益经验」，紧扣主题）",
  "materials": ["（本课要准备的具体材料，写清名称与规格，4—6 条）"],
  "environment": "（活动室环境准备，一句话）",
  "extension": "1.生活延伸：……\\n2.家园共育：……（两条）",
  "home_extension": "（给家长的一句话配合建议）",
  "key_points": {{"focus": "（重点）", "difficulty": "（难点）"}},
  "individualized_goals": [
    {{"level": "A", "goal": "（A 层幼儿在本次活动中具体要做到什么）",
      "baseline_hint": "（起点怎么判断）",
      "evaluation_method": "（课堂上怎么记录）",
      "follow_up": "（没达到时下一步怎么办）"}},
    {{"level": "B", "goal": "……", "baseline_hint": "……",
      "evaluation_method": "……", "follow_up": "……"}},
    {{"level": "C", "goal": "……", "baseline_hint": "……",
      "evaluation_method": "……", "follow_up": "……"}}
  ]
}}

硬性要求：
1. 所有内容紧扣「{req.theme}」，写具体的东西（材料要写得出名称）。
2. `class_objectives_text` 是**面向全班**的三条总目标，**不要按 A/B/C 分层分写**
   （分层信息属于 `individualized_goals`），也不要出现「A 层」这类说法。
   反例（不合格）：1. A 层幼儿能够说出三种水果的名称。
   正例（合格）：1. 能说出两种以上水果的名称。
3. **不要**在 goal 里写人数（人数由系统统一填），只写要做到什么。
4. 不出现任何幼儿姓名、诊断名称、障碍类别，也不得暗示。
5. 不许出现「……」「等等」这类省略号或半句话。
6. 材料必须与主题实际相关，禁止「{req.theme}的实物或高仿真模型」这种同义反复的占位写法。
7. 只输出 JSON。"""
        data = self.client.complete_json(SYSTEM_CONTENT, prompt)
        # 清理：只保留非空的已知键，其余丢弃（模型有时会多加字段）。
        # ⚠ 逐块做**标签词防线**：命中即整块丢弃、回退模板，不做修词替换 ——
        #   模板文案已知零标签，而改词容易改出语病或漏网（rules 是子串扫描）。
        from .rules import BANNED_PATTERN
        keep = ("class_objectives_text", "materials", "environment", "extension",
                "home_extension", "key_points", "individualized_goals")
        out: Dict[str, Any] = {}
        for k in keep:
            v = data.get(k)
            if not v:
                continue
            blob = json.dumps(v, ensure_ascii=False)
            if BANNED_PATTERN.search(blob) or PLACEHOLDER_NAME_PATTERN.search(blob):
                continue
            # 「有益经验」是**面向全班**的总目标，分层信息属于 `individualized_goals`；
            # 实测模型会在这里写「A 层幼儿…」，属位置错配 —— 命中即该字段回退模板。
            if k == "class_objectives_text" and LEVEL_TERM_PATTERN.search(blob):
                continue
            out[k] = v
        return out

    @staticmethod
    def _apply_procedure(steps: List[ProcedureStep],
                         proc: Dict[str, Any],
                         allow_scaffold: bool = True) -> int:
        """按 `name` 把 LLM 产出的教学正文并入 steps。

        ⚠ 与内容无关的字段（`index` / `duration_min`）**一律不动** ——
          时长守恒由 `_step_sequencing` 保证，扩写环节无权改数。
        ⚠ `teacher_script` 过短（< 8 字）视为模型没认真写，整段跳过，保留模板。
        ⚠ 命中禁用标签词（BANNED_PATTERN）的整段丢弃 —— 扩写正文是零标签
          最容易失守的地方，模板文案已知零标签，退回模板比修词更安全。
        ⚠ `allow_scaffold=False`（M4-4 消融臂 `use_scaffolding=False`）时**不写
          `visual_cue` / `scaffolds`**。否则会出现"消融污染"：支架注入模块明明
          被关掉了，LLM 扩写却又把支架填回来，臂与基准看起来没差别 —— 实测踩过。
        """
        from .rules import BANNED_PATTERN
        by_name = {str(x.get("name", "")).strip(): x
                   for x in (proc.get("steps") or []) if isinstance(x, dict)}
        n_ok = 0
        for s in steps:
            item = by_name.get(s.name)
            if not isinstance(item, dict):
                continue
            ts = str(item.get("teacher_script") or "").strip()
            ca = str(item.get("child_action") or "").strip()
            vc = str(item.get("visual_cue") or "").strip()
            scs = [str(x).strip() for x in (item.get("scaffolds") or []) if str(x).strip()]
            ops = [str(x).strip() for x in (item.get("observation_points") or [])
                   if str(x).strip()]
            if len(ts) < 8:
                continue
            blob = ts + ca + vc + "".join(scs) + "".join(ops)
            # 零标签 + 零虚构人名：命中即整段回退模板（模板都是干净的）。
            if BANNED_PATTERN.search(blob) or PLACEHOLDER_NAME_PATTERN.search(blob):
                continue
            # 分层术语（A 层 / B 层 / C 层）：
            #   · 出现在教师原话 / 幼儿动作 / 视觉提示里 → 整段回退模板；
            #   · 出现在某一条支持措施里 → **只剔除那一条**，不牵连整段的其余内容。
            if LEVEL_TERM_PATTERN.search(ts + ca + vc):
                continue
            scs = [x for x in scs if not LEVEL_TERM_PATTERN.search(x)]
            ops = [x for x in ops if not LEVEL_TERM_PATTERN.search(x)]
            s.teacher_script = ts
            if ca:
                s.child_action = ca
            if vc and allow_scaffold:
                s.visual_cue = vc
            if scs and allow_scaffold:
                s.scaffolds = scs[:4]
            if ops:
                s.observation_points = ops[:4]
            n_ok += 1
        return n_ok

    # ---------------- 组装 ----------------

    def _assemble(self, req: GenerateRequest, layering: Dict[str, Any],
                  sequencing: Dict[str, Any], scaffolding: Dict[str, Any],
                  narrative: Optional[Dict[str, Any]] = None) -> LessonPlan:
        domain = layering["domain"]
        counts = layering["layer_counts"]
        steps = scaffolding["steps"]
        # 步骤 3.5 的扩写产物。为空（mock / 关闭 / 失败）时下面全部回退模板。
        sup: Dict[str, Any] = (narrative or {}).get("support") or {}

        core = strip_theme_verb(req.theme)
        label = self._obj_label(domain)

        # 个别化差异目标。
        # ⚠ `share_of_class`（人数）**永远由代码按本班层级人数填**，LLM 只写
        #   措辞 —— 人数是硬数据（A+B+C 恒等于在课人数），交给模型必然算错。
        _ind_tpl = {
            "A": ("能跟随老师完成{core}中的一步{label}，并用指认表示理解",
                  "起点依首次评量，不参与也属正常；按后续三次收敛速度判断",
                  "每节课观察记录「是否需要身体辅助」",
                  "若连续两次仍需全辅助，把任务再拆小一步，只要求完成其中一个动作"),
            "B": ("能在手势提示下说出或指出{core}中的 1—2 个{label}",
                  "已有部分基础，需要提示才能稳定呈现",
                  "每节课记录提示次数（0 / 1 / 2 次）",
                  "若提示次数未下降，改为增加复练次数而非降低目标"),
            "C": ("能独立完成{core}的{label}并说出名称，尝试用完整短句表达",
                  "已基本掌握，可进入拓展",
                  "每节课记录独立完成且表达完整的情况",
                  "若已稳定达标，增加表达长度要求或请他做小老师"),
        }
        _ind_llm = {str(g.get("level", "")).strip(): g
                    for g in (sup.get("individualized_goals") or [])
                    if isinstance(g, dict)}

        def _pick(item: Dict[str, Any], key: str, default: str) -> str:
            v = str(item.get(key) or "").strip()
            return v if len(v) >= 4 else default

        ind_goals = []
        for lv in ("A", "B", "C"):
            d_goal, d_base, d_eval, d_next = _ind_tpl[lv]
            item = _ind_llm.get(lv) or {}
            ind_goals.append(IndividualizedGoal(
                level=Level(lv),
                share_of_class=f"约 {counts[lv]} 名",
                goal=_pick(item, "goal", d_goal.format(core=core, label=label)),
                baseline_hint=_pick(item, "baseline_hint", d_base),
                evaluation_method=_pick(item, "evaluation_method", d_eval),
                follow_up=_pick(item, "follow_up", d_next),
            ))

        assessment = {
            "A": "能在身体辅助或示范下参与活动并有回应，即视为达成",
            "B": "能在手势提示下完成核心操作，提示不超过 2 次，即视为达成",
            "C": "能独立完成核心操作并说出名称，即视为达成",
        }

        # 材料优先级：调用方显式指定 > LLM 扩写 > 模板兜底。
        # ⚠ 模型有时把整个材料清单写成**一个长字符串**（用「；」串联），而不是
        #   数组元素 —— 那样材料数只有 1 条，会触发「材料少于 3 项」的警告。
        #   这里按分号 / 换行二次拆分。
        raw_mats = [str(x).strip() for x in (sup.get("materials") or []) if str(x).strip()]
        mats_llm: List[str] = []
        for m in raw_mats:
            parts = [p.strip(" 。") for p in re.split(r"[；;\n]", m) if p.strip(" 。")]
            mats_llm += parts if len(parts) > 1 else [m]
        mats_llm = list(dict.fromkeys(mats_llm))          # 去重保序

        if req.materials_hint:
            materials = list(req.materials_hint)
        elif mats_llm:
            materials = list(mats_llm)
            # rules 要求材料 ≥3 项；LLM 偶尔只给 1—2 项，用通用件补足。
            for extra in (f"{core}高清图卡（大尺寸、无干扰背景）",
                          "红色标记贴、指示棒", "轻快背景音乐播放器"):
                if len(materials) >= 3:
                    break
                if extra not in materials:
                    materials.append(extra)
        else:
            # 模板兜底是主题无关的通用件（「{core}的实物或高仿真模型」这类同义反复），
            # 只在 LLM 完全未产出时使用 —— 它正是 M4-2 里被判定为"模板痕迹"的写法。
            materials = [
                f"{core}的实物或高仿真模型",
                f"{core}高清图卡（大尺寸、无干扰背景）",
                "红色标记贴、指示棒",
                "轻快背景音乐播放器",
                "幼儿操作练习纸（大格、粗线）",
            ]

        # 重难点：LLM 必须给全 focus + difficulty 才采用，否则退回模板。
        kp_llm = sup.get("key_points")
        if not (isinstance(kp_llm, dict) and all(
                len(str(kp_llm.get(k) or "").strip()) >= 4
                for k in ("focus", "difficulty"))):
            kp_llm = {
                "focus": f"了解并表达{core}的主要内容",
                "difficulty": "在减少提示的条件下稳定完成核心操作",
            }

        return LessonPlan(
            title=req.title or f"{domain}课《{core}》",
            class_label=req.class_label,
            # 本班在课人数 = 三层人数之和。模板里「人数」格填的是该班实际人数，
            # 不是全园规模（class_profile.class_size 是全园 35 人）。
            class_size=counts["A"] + counts["B"] + counts["C"],
            age_range=req.age_range,
            theme=req.theme,
            date_range=req.date_range,
            total_duration_min=req.total_duration_min,
            class_objectives_text=(req.class_objectives_text
                                   or str(sup.get("class_objectives_text") or "").strip()
                                   or (
                f"1.能了解{core}，说出或指出其主要特征。"
                f"2.愿意参与集体活动，能和老师、同伴一起完成操作。"
                f"3.在活动中练习听指令与等待，逐步减少对提示的依赖。"
            )),
            main_domain=domain,
            objectives=layering["objectives"],
            materials=materials,
            environment=(str(sup.get("environment") or "").strip()
                         or "活动区清空无关材料，桌椅按半圆摆放，保证每位幼儿都能看到老师与图片。"),
            safety_notes=SAFETY_NOTES,
            steps=steps,
            individualized_goals=ind_goals,
            # ⚠ 原先硬编码 `[:3]`，而 ALTERNATIVE_LIBRARY 已扩到 8 条，
            #   只取前 3 条等于白写 5 条。这里取 4 条（兼顾覆盖度与篇幅）。
            alternatives=[Alternative(**a) for a in ALTERNATIVE_LIBRARY[:4]],
            assessment=assessment,
            extension=(str(sup.get("extension") or "").strip()
                       or ("1.生活延伸：在一日生活的相关环节中，请幼儿说一说、指一指。"
                           "2.家园共育：请家长和孩子一起找一找家里的相关物品，"
                           "孩子说到就及时表扬。")),
            home_extension=(str(sup.get("home_extension") or "").strip()
                            or "请家长配合，在家庭生活中重复本周的学习内容，每次不超过 5 分钟。"),
            key_points=kp_llm,
            reflection_slots="（此处留白，供带班老师课后填写）",
            reflection_points=[
                "本次活动中，哪些幼儿需要比预设更多的支持？",
                "哪个环节的提示可以再减少一些？",
                "复练次数是否足够？下次是否需要增加？",
            ],
        )

    # ---------------- 步骤 4：自检重写 ----------------

    def _repair(self, plan: LessonPlan, report: ValidationReport,
                req: GenerateRequest) -> Optional[LessonPlan]:
        """按校验反馈修补。

        采用**规则修补**而非再调一次 LLM：更快、更可预测、不消耗额度，
        且失败模式明确（哪些致命错误是规则可以自动修的）。
        """
        changed = False
        fatal_msgs = [v.message for v in report.violations if v.fatal]

        # 1. 标签泄漏 / 姓名泄漏 → 无法自动修（涉及改写文本语义），交由人工或重生成
        if any("标签词" in m or "姓名" in m for m in fatal_msgs):
            if self.verbose:
                print(f"[gen] 存在不可自动修补的致命错误：{fatal_msgs}")
            return None

        # 2. 缺层 → 补一条（主题同样要剥掉引导动词）
        core = strip_theme_verb(plan.theme)
        levels = {o.level for o in plan.objectives}
        for lv in Level:
            if lv not in levels:
                plan.objectives.append(Objective(
                    level=lv,
                    domain=plan.main_domain,
                    support_level={"A": "示范模仿", "B": "手势辅助",
                                   "C": "独立完成"}[lv.value],
                    text=f"能在活动{'参与' if lv.value == 'A' else '完成' if lv.value == 'B' else '独立完成'}"
                         f"{core}的{self._obj_label(plan.main_domain)}",
                    observable_verb="完成",
                    criteria="至少 2 名幼儿能达到",
                ))
                changed = True

        # 3. 缺评量标准 → 补
        for lv in Level:
            if lv.value not in plan.assessment:
                plan.assessment[lv.value] = (
                    "能在辅助下参与并有回应即视为达成" if lv.value == "A"
                    else "能在提示下完成核心操作即视为达成" if lv.value == "B"
                    else "能独立完成并说出即视为达成")
                changed = True

        # 4. 缺支架 / 缺视觉提示 → 补
        # ⚠ 消融臂 `use_scaffolding=False` 时**不补** —— 否则会把刚消融掉的支架
        #   自动修回来，量不出本模块的贡献（M4-4 的一个真实陷阱）。
        if req.use_scaffolding:
            for s in plan.steps:
                if not s.visual_cue.strip():
                    s.visual_cue = self._cue_for(s.name)
                    changed = True
                if not s.scaffolds:
                    s.scaffolds = list(scaffold_for("手势辅助")["cues"])[:2]
                    changed = True

        # 5. 时长不齐 → 调整核心环节
        total = sum(s.duration_min for s in plan.steps)
        if total != plan.total_duration_min:
            diff = plan.total_duration_min - total
            for s in plan.steps:
                if s.name == "核心活动":
                    s.duration_min = max(1, s.duration_min + diff)
                    changed = True
                    break
            else:
                plan.steps[-1].duration_min = max(1, plan.steps[-1].duration_min + diff)
                changed = True

        # 6. 替代方式不足 → 补
        while len(plan.alternatives) < 2:
            # ⚠ 取模兜底：ALTERNATIVE_LIBRARY 扩充到 8 条后，若将来把上面的
            #   阈值调大，直接按 len() 取索引会 IndexError。
            pool_alt = ALTERNATIVE_LIBRARY
            plan.alternatives.append(
                Alternative(**pool_alt[len(plan.alternatives) % len(pool_alt)]))
            changed = True

        # 7. 材料太少 → 补
        while len(plan.materials) < 3:
            plan.materials.append(f"{strip_theme_verb(plan.theme)}图卡")
            changed = True

        # 8. 目标含空话 → 替换词
        vague_map = {
            "培养": "练习", "提高": "练习", "提升": "练习", "增强": "练习",
            "激发兴趣": "愿意参与", "感受": "说出", "体验": "完成",
        }
        for o in plan.objectives:
            for bad, good in vague_map.items():
                if bad in o.text:
                    o.text = o.text.replace(bad, good)
                    changed = True

        return plan if changed else None


# --------------------------------------------------------------------------
# 便捷入口
# --------------------------------------------------------------------------


def generate_lesson(req: GenerateRequest,
                    client: Optional[LLMClient] = None,
                    verbose: bool = False) -> GenerateResult:
    gen = LessonGenerator(client=client or LLMClient(provider="mock"), verbose=verbose)
    return gen.generate(req)


def load_class_distribution(path: str) -> Dict[str, Dict[str, int]]:
    """从 class_profile.json 读分布。"""
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("distribution", {})

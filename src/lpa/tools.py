"""Agent 工具集（P0-2）—— 把流水线的每一步能力挂给模型自主调用。

## 为什么有这一层

P0-1 建好了「模型可以调工具」的通道（`LLMClient.run_with_tools`）；本模块
把现有能力包装成工具，让它有东西可调。**只包装，不复制** —— 每个工具都是
现有模块函数的入口，业务逻辑一行都不在本层重写：

| 工具 | 包装的既有能力 |
|---|---|
| `search_corpus`         | 语料库检索（`data/structured/lessons.json`，14 份范例） |
| `get_class_roster`      | 名册与班级概况（child_profiles / class_profile） |
| `get_child_profile`     | 单个幼儿档案 |
| `get_previous_baseline` | 跨课康复基线（`roster_from_corpus` + `lesson_flow.targets_for`） |
| `plan_layering`         | 目标分层规划（步骤 1 的规划部分） |
| `draft_objectives`      | 分层目标生成（步骤 1 的 LLM 部分） |
| `expand_content`        | 环节编排 + 支架注入 + 内容扩写（步骤 2 / 3 / 3.5） |
| `validate_lesson_plan`  | 硬校验（`rules.validate_plan`，**会失败**） |
| `render_docx`           | 渲染（`DocxRenderer`，与交付同一条渲染器） |
| `audit_docx`            | 交付终检（`final_audit.py` 子进程） |

## 三条硬约束

1. **观察值脱敏**：工具回灌给模型的 JSON **永不含真实姓名** —— 内部
   逻辑（基线规划器）按项目既有私密模式用真名取数，出口一律按 `Sxx` 编号。
2. **产物隔离**：本次运行的 docx 全部落在 `run_dir`（`data/_agent_runs/<id>/`），
   与 `data/docx/` 的正式交付件物理隔离。
3. **顺序有契约**：`draft_objectives` 需要分层、`expand_content` 需要目标、
   `validate` 需要内容 —— 顺序错了就**抛带指引的错误**（不静默补默认值），
   由 Agent 自己决定回头补哪一步。

## 故障注入（P0-3）

`faults=["bad_objective"]` 时，首次 `draft_objectives` 会把一条目标替换成
「虚动词」版本（模拟真实 LLM 的输出缺陷，必然触发 `OBJ_NOT_OBSERVABLE`）——
用于演示与回归 Agent 的**自主修复闭环**：发现校验失败 → 读明细 → 重新生成
目标 → 复检通过。**默认空**，不影响任何既有调用与交付流水线。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .generate import GenerateRequest, LessonGenerator
from .layering import GoalLadder, build_ladder, layer_count
from .llm import LLMClient, LLMError, ToolCall, tool_schema
from .render_docx import DocxRenderer, check_docx_compliance
from .rules import load_forbidden_names, plan_metrics, validate_plan
from .taxonomy import classify_domain, strip_theme_verb

# --------------------------------------------------------------------------
# 脚本层构件桥
# --------------------------------------------------------------------------
#
# 名册基线（roster_from_corpus）、跨课规划器（lesson_flow.targets_for）、
# 班级分组（run_render.build_class_groups）目前住在 `scripts/`。它们原本就是
# 交付流水线（run_render.py）的构件 —— 工具层**复用同一批构件**，不复制逻辑。
# 脚本层彼此本来就是 `sys.path` 直连的写法（lesson_flow / run_render 同款），
# 这里沿用，并把导入集中在一处，方便将来把构件上移到 `src/`。

_BRIDGE: Dict[str, Any] = {}


def _bridge(root: Path) -> Dict[str, Any]:
    """懒加载脚本层构件。只导入，不执行任何流水线。"""
    if _BRIDGE:
        return _BRIDGE
    for p in (str(root / "scripts"), str(root / "src")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from lesson_flow import targets_for                                # noqa: E402
    from roster_from_corpus import build_history, roster_records       # noqa: E402
    from run_render import (SPLIT_BY_CLASS, build_class_groups,        # noqa: E402
                            load_anon_to_name)
    _BRIDGE.update({
        "targets_for": targets_for,
        "build_history": build_history,
        "roster_records": roster_records,
        "load_anon_to_name": load_anon_to_name,
        "build_class_groups": build_class_groups,
        "SPLIT_BY_CLASS": SPLIT_BY_CLASS,
    })
    return _BRIDGE


def _trim(text: str, limit: int = 300) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _flatten(value: Any) -> str:
    """把任意层级的 JSON 值摊成一段可检索文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return str(value)


# --------------------------------------------------------------------------
# 工具集
# --------------------------------------------------------------------------


class ToolBox:
    """一次 Agent 运行的工具体。构造时绑定项目根、模型客户端与主题。

    `run_dir` 是本次运行的产物目录（渲染的 docx 落这里）。工具之间通过
    实例状态（`self.state`）传递中间产物 —— 这与 Agent 循环的语义一致：
    每一步的 Observation 喂给下一步，中途产物不丢。
    """

    def __init__(self, *, root: Path, client: LLMClient, run_dir: Path,
                 theme: str, title: str = "",
                 ladder_spec: Optional[Sequence] = None,
                 date_range: str = "2026年10月12日—10月16日",
                 total_duration_min: int = 30,
                 verbose: bool = False,
                 faults: Optional[Sequence[str]] = None):
        self.root = Path(root)
        self.client = client
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.verbose = verbose
        # P0-3 故障注入（默认空 = 完全不影响既有行为）。见 `_maybe_inject_fault`。
        self.faults: List[str] = [str(f) for f in (faults or [])]
        self._faults_fired: set = set()

        # ---- 主题归一（与 `generate()` 主入口同款：只剥一次） ----
        self.theme_raw = (theme or "").strip()
        self.theme_core = strip_theme_verb(self.theme_raw) or self.theme_raw

        # ---- 结构化数据（全部为脱敏数据，无真实姓名） ----
        st = self.root / "data" / "structured"
        self.children: List[Dict[str, Any]] = json.loads(
            (st / "child_profiles.json").read_text(encoding="utf-8"))
        self.class_profile: Dict[str, Any] = json.loads(
            (st / "class_profile.json").read_text(encoding="utf-8"))
        self.lessons: List[Dict[str, Any]] = json.loads(
            (st / "lessons.json").read_text(encoding="utf-8"))
        try:
            self.config: Dict[str, Any] = json.loads(
                (st / "build_config.json").read_text(encoding="utf-8"))
        except Exception:                                    # noqa: BLE001
            self.config = {}
        self.goal_ladder_docs: Dict[str, Any] = json.loads(
            (st / "goal_ladder.json").read_text(encoding="utf-8")).get("docs", {})
        self.by_id = {c["anon_id"]: c for c in self.children}

        # ---- 标题与目标阶梯（与 run_render 的选取顺序一致） ----
        self.title = title or f"{self._infer_domain(self.theme_core)}课《{self.theme_core}》"
        self.ladder_spec: List[tuple] = ([(str(n), str(t)) for n, t in ladder_spec]
                                         if ladder_spec
                                         else self._ladder_from_config(self.title))
        self.ladder = (build_ladder(list(self.ladder_spec))
                       if self.ladder_spec else GoalLadder(items=[]))

        # ---- 生成请求（不含任何幼儿身份信息） ----
        self.req = GenerateRequest(
            theme=self.theme_core,
            theme_core=self.theme_core,
            theme_raw=self.theme_raw,
            title=self.title,
            class_label=self.class_profile.get("class_label") or "混龄康教班（已匿名）",
            age_range=self.class_profile.get("age_range") or "3-9岁",
            class_size=len(self.children),
            total_duration_min=total_duration_min,
            distribution=self.class_profile.get("distribution") or {},
            date_range=date_range,
        )
        self.lg = LessonGenerator(client=client, verbose=verbose)
        self.forbidden_names = self._load_forbidden()

        # ---- 中间状态（工具间传递） ----
        self.state: Dict[str, Any] = {
            "layering": None,       # {domain, layer_counts, objectives, trace}
            "sequencing": None,     # 步骤 2
            "scaffolding": None,    # 步骤 3（narrative 会就地扩写其 steps）
            "narrative": None,      # 步骤 3.5（mock / 关闭时为 None）
            "plan": None,           # 组装出的 LessonPlan
            "report": None,         # 最近一次硬校验
            "rendered": [],         # [(Path, compliance_issues)]
            "audit": None,          # 最近一次终检结果
        }
        self._roster: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Schema：挂给模型的工具定义
    # ------------------------------------------------------------------

    def schemas(self) -> List[Dict[str, Any]]:
        return [
            tool_schema(
                "search_corpus",
                "在 14 份真实康复教案范例里检索与关键词相关的内容，返回标题与原文片段，"
                "用于对齐教案风格、取材与措辞。写分层目标或教学内容之前建议先检索 1–2 次；"
                "只依据返回的片段作答，不要凭记忆编造语料内容。",
                {"query": {"type": "string",
                           "description": "检索关键词，如「国庆节」「水果」「穿鞋」"},
                 "limit": {"type": "integer",
                           "description": "最多返回几份范例（默认 3）"}},
                required=["query"],
            ),
            tool_schema(
                "get_class_roster",
                "获取本班在册幼儿的匿名编号清单（形如 S01…S37）与班级概况"
                "（总人数、年龄段、各领域支持层级分布）。"
                "涉及「有多少名幼儿」「都有哪些编号」的问题必须先调用本工具，"
                "严禁凭猜测作答。",
            ),
            tool_schema(
                "get_child_profile",
                "查询单个幼儿的能力档案：总体掌握度、支持层级、出勤、观察次数。"
                "编号必须来自 get_class_roster 的清单；编号不存在时会报错，"
                "请阅读错误信息后改用正确编号。",
                {"anon_id": {"type": "string",
                             "description": "幼儿匿名编号，形如 S01、S37"}},
                required=["anon_id"],
            ),
            tool_schema(
                "get_previous_baseline",
                "查询一名或多名幼儿**最近一次**教案里的康复目标编号与康复基线记号"
                "（跨课连续性的唯一依据）。写分层结果或个别化目标前必须查询 —— "
                "基线必须承接历史，任何编造的记号都会与历史数据对不上。",
                {"anon_ids": {
                    "type": "array", "items": {"type": "string"},
                    "description": "编号列表，如 [\"S01\",\"S02\"]；"
                                   "传 [\"all\"] 一次取回全部在册幼儿。"}},
            ),
            tool_schema(
                "plan_layering",
                "根据主题与班级数据完成**目标分层规划**：判定本课主领域、三层人数"
                "（A 需较多支持 / B 中等 / C 可独立），并基于每名幼儿的最近记录"
                "统计层级定位与调层情况。这是后续所有生成步骤的前提。",
            ),
            tool_schema(
                "draft_objectives",
                "生成集体课的分层活动目标（需先完成 plan_layering）。"
                "若上一次校验未通过，把校验给出的修改要求写进 revision_hint 重新生成，"
                "例如「目标 3 缺可观测动词，请换成拍手/指认/说出这类动词」。",
                {"revision_hint": {"type": "string",
                                   "description": "修订要求（首次生成为空）"}},
            ),
            tool_schema(
                "expand_content",
                "把环节骨架扩写成完整教学内容：教师原话、幼儿动作、材料清单、"
                "有益经验、个别化措辞（需先完成 draft_objectives）。"
                "如需修订内容：若问题出在目标与内容不匹配，先修订目标"
                "（draft_objectives 带 revision_hint）再重跑本工具；"
                "否则直接重跑即重新生成一版（不保证更好）。",
            ),
            tool_schema(
                "validate_lesson_plan",
                "对当前草稿跑全部硬校验（零姓名 / 零标签 / 目标可观测性 / "
                "支架密度 / 时长自洽 / 分层覆盖等）。**本工具可能失败** —— "
                "失败时逐条阅读 fatal 明细，针对性修复后重新调用本工具，直到通过。",
            ),
            tool_schema(
                "render_docx",
                "把当前草稿渲染成 docx 交付件（建议先 validate 通过）。"
                "渲染走的是正式交付同一条渲染器；产物写入本次运行目录，"
                "附带回读的合规检查结果。",
            ),
            tool_schema(
                "audit_docx",
                "对本次渲染产物跑**交付终检**（版式 + 合规全项，与正式交付同一把尺子）。"
                "返回是否全部通过；未通过时给出行级明细。",
            ),
        ]

    @staticmethod
    def tool_names() -> List[str]:
        return ["search_corpus", "get_class_roster", "get_child_profile",
                "get_previous_baseline", "plan_layering", "draft_objectives",
                "expand_content", "validate_lesson_plan", "render_docx",
                "audit_docx"]

    # ------------------------------------------------------------------
    # 入口：执行一次工具调用
    # ------------------------------------------------------------------

    def run(self, call: ToolCall) -> Any:
        """`run_with_tools` 的 runner。异常直接抛 —— 由 Agent 循环转成观察值。"""
        name = call.name
        if self.verbose:
            args = json.dumps(call.arguments or {}, ensure_ascii=False)
            print(f"  [tool] → {name}({_trim(args, 160)})")
        t0 = time.time()
        if name not in _TOOL_IMPL:
            raise KeyError(
                f"未知工具 {name!r}；本次会话可用：{'、'.join(self.tool_names())}")
        result = getattr(self, _TOOL_IMPL[name])(call.arguments or {})
        if self.verbose:
            print(f"  [tool] ← {name} 完成（{time.time() - t0:.2f}s）")
        return result

    # ------------------------------------------------------------------
    # 查询类工具（只读，随时可调）
    # ------------------------------------------------------------------

    def _t_search_corpus(self, args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query 不能为空，请给出检索关键词")
        limit = int(args.get("limit") or 3)
        hits = []
        for L in self.lessons:
            chunks = {
                "标题": _flatten(L.get("title")),
                "活动目标": _flatten(L.get("class_objectives")),
                "材料": _flatten(L.get("materials")),
                "过程": _flatten(L.get("process")),
                "延伸": _flatten(L.get("extension")),
            }
            snippets = []
            for label, text in chunks.items():
                pos = text.find(query)
                if pos < 0:
                    continue
                lo = max(0, pos - 30)
                hi = min(len(text), pos + len(query) + 60)
                snip = ("…" if lo else "") + text[lo:hi] + ("…" if hi < len(text) else "")
                snippets.append({"field": label, "excerpt": snip})
            if snippets:
                hits.append({
                    "title": _flatten(L.get("title")),
                    "date_range": L.get("date_range", ""),
                    "matches": snippets[:4],
                })
            if len(hits) >= limit:
                break
        return {
            "query": query,
            "n_hits": len(hits),
            "hits": hits,
            "note": ("未找到含该关键词的范例，请换更通用的词再试"
                     if not hits else "以上为语料原文片段，可作风格参照"),
        }

    def _t_get_class_roster(self, args: Dict[str, Any]) -> Dict[str, Any]:
        cp = self.class_profile
        dist = cp.get("distribution") or {}
        summary = {}
        for domain, levels in dist.items():
            n = sum(int(v or 0) for v in levels.values())
            if n:
                summary[domain] = {"total": n, **{k: int(v or 0)
                                                  for k, v in levels.items() if v}}
        return {
            "class_label": cp.get("class_label", ""),
            "age_range": cp.get("age_range", ""),
            "count": len(self.children),
            "ids": [c["anon_id"] for c in self.children],
            "support_distribution": summary,
            "note": "教案名册按以上全部在册幼儿出（含缺勤）；编号全局唯一，不按班重编",
        }

    def _t_get_child_profile(self, args: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(args.get("anon_id") or "").strip().upper()
        c = self.by_id.get(cid)
        if c is None:
            raise KeyError(
                f"编号 {cid!r} 不存在。本班编号形如 S01–S{len(self.children):02d}，"
                f"可先调用 get_class_roster 取回完整清单")
        return {
            "anon_id": c["anon_id"],
            "mastery": c.get("mastery"),
            "support_level": c.get("support_level"),
            "attendance": c.get("attendance"),
            "lessons_observed": c.get("lessons_observed"),
        }

    def _t_get_previous_baseline(self, args: Dict[str, Any]) -> Dict[str, Any]:
        want = args.get("anon_ids") or ["all"]
        if isinstance(want, str):
            want = [want]
        want = [str(w).strip().upper() for w in want]
        ids = ([c["anon_id"] for c in self.children]
               if ("ALL" in want or "全部" in want or not want) else want)
        unknown = [i for i in ids if i not in self.by_id]
        if unknown:
            raise KeyError(f"编号不存在：{'、'.join(unknown)}；"
                           f"可先调用 get_class_roster 核对清单")

        roi = self._ensure_roster()
        records, planned = roi["records"], roi["planned"]
        out, n_absent, n_up, n_dn = [], 0, 0, 0
        for aid in ids:
            rec = dict(records.get(aid) or {})
            p = planned.get(aid) or {}
            goal = p.get("goal") or rec.get("goal") or ""
            baseline = p.get("baseline") or rec.get("baseline") or ""
            absent = bool(p.get("absent") or rec.get("absent"))
            layer, prev_layer = p.get("layer"), p.get("prev_layer")
            if absent:
                n_absent += 1
            if layer and prev_layer:
                n_up += int(layer > prev_layer)
                n_dn += int(layer < prev_layer)
            out.append({
                "anon_id": aid,
                "goal_combo": goal,                      # 康复目标编号串，如 1456
                "baseline_marks": baseline,              # 康复基线，如 P  P PP / 未参与
                "absent_last_round": absent,
                "source_period": _trim(p.get("source") or rec.get("date_range") or "", 60),
                "layer": layer,
                "support_level": (self.by_id.get(aid) or {}).get("support_level", ""),
            })
        return {
            "n_returned": len(out),
            "adjust_summary": {"upgraded": n_up, "downgraded": n_dn,
                               "absent": n_absent},
            "records": out,
            "note": "goal_combo 与 baseline_marks 必须原样承接（缺勤者基线记「未参与」），"
                    "不得编造",
        }

    # ------------------------------------------------------------------
    # 规划 / 生成类工具（有顺序契约）
    # ------------------------------------------------------------------

    def _t_plan_layering(self, args: Dict[str, Any]) -> Dict[str, Any]:
        domain = self.req.main_domain_hint or self._infer_domain(self.req.theme)
        dist = self.lg._layer_counts(self.req, domain)      # noqa: SLF001
        roi = self._ensure_roster()
        planned = roi["planned"]

        per_layer: Dict[str, int] = {}
        n_fallback = n_absent = 0
        for aid in [c["anon_id"] for c in self.children]:
            p = planned.get(aid) or {}
            layer = p.get("layer")
            if layer:
                per_layer[f"L{int(layer)}"] = per_layer.get(f"L{int(layer)}", 0) + 1
            else:
                n_fallback += 1
            if p.get("absent"):
                n_absent += 1

        self.state["layering"] = {
            "domain": domain,
            "layer_counts": dist,
            "objectives": None,          # 由 draft_objectives 填入
            "trace": {"name": "目标分层", "ok": True,
                      "detail": f"主领域 {domain}；A/B/C = "
                                f"{dist['A']}/{dist['B']}/{dist['C']} 名"},
        }
        return {
            "theme_core": self.req.theme,
            "domain": domain,
            "support_layers": dist,
            "n_layers": layer_count(),
            "layer_assignment": per_layer,
            "profile_fallback": n_fallback,
            "absent": n_absent,
            "note": "层级定位来自各幼儿最近一次可解释记录；profile_fallback "
                    "为记录不可解释、回落到画像判定的幼儿数",
        }

    def _t_draft_objectives(self, args: Dict[str, Any]) -> Dict[str, Any]:
        lay = self.state["layering"]
        if not lay:
            raise RuntimeError("尚未完成分层规划 —— 请先调用 plan_layering")
        hint = str(args.get("revision_hint") or "").strip()
        domain, dist = lay["domain"], lay["layer_counts"]
        note = ""
        if self.client.provider == "mock":
            objs = self.lg._mock_objectives(self.req, domain, dist)   # noqa: SLF001
            note = "mock 模式：按规则库生成（不调用模型）"
        else:
            try:
                objs = self.lg._llm_objectives(                        # noqa: SLF001
                    self.req, domain, dist, hint=hint)
            except LLMError as e:
                objs = self.lg._mock_objectives(self.req, domain, dist)   # noqa: SLF001
                note = f"LLM 失败（{e}），已降级规则库"
        lay["objectives"] = objs
        self._maybe_inject_fault(lay)               # P0-3 故障注入（默认关闭）
        objs = lay["objectives"]                    # 观察值以 state 最终版为准
        return {
            "count": len(objs),
            "revision_hint_applied": hint or None,
            "objectives": [{
                "level": getattr(o.level, "value", o.level),
                "support_level": o.support_level,
                "text": o.text,
                "observable_verb": getattr(o, "observable_verb", ""),
            } for o in objs],
            "note": note,
        }

    def _maybe_inject_fault(self, lay: Dict[str, Any]) -> Optional[str]:
        """P0-3 故障注入：**模拟一次真实的 LLM 输出缺陷**（默认关闭）。

        `faults=["bad_objective"]` 时，在首次 `draft_objectives` 之后把一条目标
        换成「虚动词」版本 —— 它必然触发 `OBJ_NOT_OBSERVABLE` 致命项：
        白名单动词不命中，且模型自报的动词（进行）是 `rules.VIRTUAL_TERMS`
        点名的虚动词。

        为什么用注入而不是等真实模型出错：真实缺陷不可复现，没法做演示、
        没法做回归。注入的缺陷**只出现在 state 里**（观察值里也看不出任何
        「这是注入」的提示）—— Agent 必须自己从校验失败明细里发现它。
        修法唯一：重新生成目标（`draft_objectives`，建议带 revision_hint）
        覆盖整个列表；不重新生成，坏目标会一直让校验失败。

        返回被替换后的文本（仅供调用方留痕/调试），不影响工具对外的观察值。
        """
        if "bad_objective" not in self.faults or "bad_objective" in self._faults_fired:
            return None
        objs = lay.get("objectives") or []
        if not objs:
            return None
        self._faults_fired.add("bad_objective")
        idx = 1 if len(objs) > 1 else 0
        bad_text = "能进行集体律动活动，感受节日的欢乐气氛"
        objs[idx] = objs[idx].model_copy(
            update={"text": bad_text, "observable_verb": "进行"})
        return bad_text

    def _t_expand_content(self, args: Dict[str, Any]) -> Dict[str, Any]:
        lay = self.state["layering"]
        if not lay or not lay.get("objectives"):
            raise RuntimeError("尚未生成分层目标 —— 请先完成 "
                               "plan_layering → draft_objectives")
        seq = self.lg._step_sequencing(self.req, lay)                  # noqa: SLF001
        scaf = self.lg._step_scaffolding(self.req, lay, seq)           # noqa: SLF001
        narr = None
        mode = "template"
        if self.client.provider != "mock" and self.req.llm_content:
            narr = self.lg._step_narrative(self.req, lay, scaf)        # noqa: SLF001
            mode = "llm"
        self.state["sequencing"] = seq
        self.state["scaffolding"] = scaf
        self.state["narrative"] = narr
        steps = scaf["steps"]
        return {
            "mode": mode,
            "n_steps": len(steps),
            "total_duration_min": sum(s.duration_min for s in steps),
            "steps": [{
                "index": s.index,
                "name": s.name,
                "duration_min": s.duration_min,
                "script_chars": len(s.teacher_script or ""),
                "n_scaffolds": len(s.scaffolds or []),
                "has_visual_cue": bool((s.visual_cue or "").strip()),
            } for s in steps],
            "first_script_excerpt": _trim(steps[0].teacher_script if steps else "", 90),
            "note": ("教学内容为 LLM 按主题扩写"
                     if mode == "llm" else
                     "模板拼装模式（mock / 开关关闭），内容非主题化"),
        }

    # ------------------------------------------------------------------
    # 校验 / 落盘类工具
    # ------------------------------------------------------------------

    def _assemble_draft(self):
        """把中间产物组装成 LessonPlan（纯代码、确定性，可多次调用）。"""
        lay, seq, scaf = (self.state["layering"], self.state["sequencing"],
                          self.state["scaffolding"])
        if not lay or not lay.get("objectives"):
            raise RuntimeError("尚未生成分层目标 —— 请先完成 plan_layering → draft_objectives")
        if not seq or not scaf:
            raise RuntimeError("尚未扩写教学内容 —— 请先调用 expand_content")
        plan = self.lg._assemble(self.req, lay, seq, scaf,                 # noqa: SLF001
                                 self.state["narrative"])
        self.state["plan"] = plan
        return plan

    def _t_validate_lesson_plan(self, args: Dict[str, Any]) -> Dict[str, Any]:
        plan = self._assemble_draft()
        report = validate_plan(plan, forbidden_names=self.forbidden_names)
        self.state["report"] = report
        fatal = [str(v) for v in report.violations if v.fatal]
        warn = [str(v) for v in report.violations if not v.fatal]
        return {
            "passed": report.passed,
            "fatal_count": report.fatal_count,
            "warning_count": report.warning_count,
            "fatal": [_trim(t, 300) for t in fatal[:20]],
            "warnings": [_trim(t, 300) for t in warn[:20]],
            "metrics": plan_metrics(plan),
            "note": ("已通过全部硬校验，可以进入渲染"
                     if report.passed else
                     "存在致命项，请逐条修复后重新 validate（不要直接渲染）"),
        }

    def _t_render_docx(self, args: Dict[str, Any]) -> Dict[str, Any]:
        plan = self._assemble_draft()
        br = _bridge(self.root)
        roi = self._ensure_roster()
        records, planned = roi["records"], roi["planned"]

        in_session = [c for c in self.children
                      if c.get("anon_id") in records]
        in_session.sort(key=lambda c: c.get("anon_id", ""))
        child_records: Dict[str, Dict[str, Any]] = {}
        for c in in_session:
            aid = c["anon_id"]
            rec = dict(records[aid])
            p = planned.get(aid) or {}
            if p.get("layer") is not None:
                rec["layer"] = p["layer"]
                rec["prev_layer"] = p.get("prev_layer")
                rec["prev_goal"] = p.get("prev_goal", "")
            if p.get("basis"):
                rec["basis"] = p["basis"]
            if p.get("goal"):
                rec["goal"] = p["goal"]
            if p.get("baseline"):
                rec["baseline"] = p["baseline"]
            if p.get("absent"):
                rec["absent"] = True
            child_records[aid] = rec

        groups = br["build_class_groups"](in_session, child_records,
                                          fallback_age=self.req.age_range)
        r = DocxRenderer(
            class_size=len(in_session),
            class_label=self.req.class_label,
            date_range=plan.date_range,
            children=in_session,
            ladder_spec=self.ladder_spec,
            child_records=child_records,
            disability_label=(groups[0].get("disability_label") if groups else ""),
        )
        _cfg_split = br["SPLIT_BY_CLASS"].get(plan.title)
        do_split = (len(groups) > 1) if _cfg_split is None else bool(_cfg_split)

        files: List[Dict[str, Any]] = []
        if do_split and len(groups) > 1:
            for g in groups:
                sub = self.run_dir / f"{plan.title}-{g['label']}.docx"
                r.render_multi(plan, sub, [g])
                bad = check_docx_compliance(
                    sub, self.forbidden_names,
                    exempt_fragments=[g.get("disability_label")] if g.get("disability_label") else [])
                files.append({"path": str(sub), "size": sub.stat().st_size,
                              "class": g["label"], "n_children": g["size"],
                              "compliance_issues": bad})
        else:
            out = self.run_dir / f"{plan.title}.docx"
            r.render(plan, out)
            exempt = ([groups[0]["disability_label"]]
                      if groups and groups[0].get("disability_label") else [])
            bad = check_docx_compliance(out, self.forbidden_names,
                                        exempt_fragments=exempt)
            files.append({"path": str(out), "size": out.stat().st_size,
                          "class": "全班", "n_children": len(in_session),
                          "compliance_issues": bad})

        self.state["rendered"] = files
        n_bad = sum(len(f["compliance_issues"]) for f in files)
        return {
            "n_files": len(files),
            "split_by_class": bool(do_split and len(groups) > 1),
            "files": files,
            "note": ("渲染完成，可跑 audit_docx 终检"
                     if not n_bad else
                     f"渲染完成但回读发现 {n_bad} 项合规问题，请先修复再终检"),
        }

    def _t_audit_docx(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if not self.state["rendered"]:
            raise RuntimeError("尚无可检查的产物 —— 请先调用 render_docx")
        env = dict(os.environ)
        env["LPA_AUDIT_DIR"] = str(self.run_dir)
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(
            [sys.executable, str(self.root / "scripts" / "final_audit.py")],
            cwd=str(self.root), env=env,
            capture_output=True, timeout=300)
        out = (proc.stdout or b"").decode("utf-8", "replace") + \
              (proc.stderr or b"").decode("utf-8", "replace")
        fail_lines = [ln.strip() for ln in out.splitlines()
                      if "FAIL" in ln or "❌" in ln]
        result = {
            "passed": proc.returncode == 0,
            "exit_code": proc.returncode,
            "fail_items": fail_lines[:20],
            "report_tail": out[-1800:],
        }
        self.state["audit"] = result
        if result["passed"]:
            result["note"] = "交付终检全部通过 —— 本次生成闭环完成"
        else:
            result["note"] = ("终检未通过，请阅读 fail_items 与报告尾部，"
                              "修复后重新 render_docx → audit_docx")
        return result

    # ------------------------------------------------------------------
    # 内部：数据与配置
    # ------------------------------------------------------------------

    def _ensure_roster(self) -> Dict[str, Any]:
        """名册 / 历史 / 最近记录 / 跨课规划 —— 与交付流水线同一批构件。

        ⚠ 本层内部按项目既有私密模式**用真名取数**（name_mapping 仅在此使用），
        出口一律按 `Sxx` 编号聚合 —— 真实姓名不会进入任何工具返回值。
        """
        if self._roster is not None:
            return self._roster
        br = _bridge(self.root)
        anon2name = br["load_anon_to_name"]()
        hist = br["build_history"]()
        records = br["roster_records"](anon2name, hist)
        planned = br["targets_for"](anon2name, self.ladder, hist)
        self._roster = {"anon2name": anon2name, "hist": hist,
                        "records": records, "planned": planned}
        return self._roster

    def _infer_domain(self, theme: str) -> str:
        d = classify_domain(theme)
        return d if d != "未分类" else "认知"

    def _ladder_from_config(self, title: str) -> List[tuple]:
        themes = (self.config or {}).get("themes") or {}
        cfg = themes.get(title) or {}
        if cfg.get("manual_ladder"):
            return [(str(n), str(t)) for n, t in cfg["manual_ladder"]]
        src = cfg.get("ladder_source")
        docs = self.goal_ladder_docs

        def _items(key: str) -> List[tuple]:
            return [(str(n), str(t))
                    for n, t in (docs.get(key) or {}).get("items") or []]
        if src and src in docs and _items(src):
            return _items(src)
        for k, v in docs.items():
            if len(v.get("items") or []) >= 4:
                return _items(k)
        return []

    def _load_forbidden(self) -> List[str]:
        p = self.root / "data" / "private" / "name_mapping.json"
        try:
            return load_forbidden_names(str(p))
        except Exception:                                    # noqa: BLE001
            return []


# 工具名 → 实现方法。`run()` 按此分发。
_TOOL_IMPL: Dict[str, str] = {
    "search_corpus": "_t_search_corpus",
    "get_class_roster": "_t_get_class_roster",
    "get_child_profile": "_t_get_child_profile",
    "get_previous_baseline": "_t_get_previous_baseline",
    "plan_layering": "_t_plan_layering",
    "draft_objectives": "_t_draft_objectives",
    "expand_content": "_t_expand_content",
    "validate_lesson_plan": "_t_validate_lesson_plan",
    "render_docx": "_t_render_docx",
    "audit_docx": "_t_audit_docx",
}

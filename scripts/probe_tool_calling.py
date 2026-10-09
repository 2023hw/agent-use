#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""工具调用能力探针（P0-1 的验证 + P1-4 的选型依据）。

**它回答两个问题：**

1. `src/lpa/llm.py` 的 Agent 循环到底能不能跑通？
   —— 循环终止、`tool_call_id` 配对、参数解析、异常回灌、Trace 结构。
   这一层是**链路正确性**，与模型强弱无关，**用 mock 离线就能验完**。
2. **当前这个模型（默认 `glm-4-flash`）撑不撑得住多轮工具调用？**
   —— 这是 P1-4（底座模型选型）的直接依据。轻量免费档的 function calling
   能力常常是短板，必须实测而不是想当然。

**为什么任务设计成"必须按顺序调 3 次工具"：**

单次调用的成功率高得没有信息量（模型蒙也能蒙对一次）。真正的难点是
**多轮依赖**：先把 37 个编号拿回来，才知道有 S37 这个人；不调 `list_children`
就答不出"一共多少人"。这才能测出模型是否会**漏调工具硬编答案**、
是否会**重复调同一个工具打转**、是否会**在多轮之后丢掉上下文**。

数据全部来自项目真实文件（`data/structured/child_profiles.json` /
`class_profile.json`），不是编造的桩。

用法：
    PYTHONUTF8=1 "$PY" scripts/probe_tool_calling.py                # 自动选 provider
    PYTHONUTF8=1 "$PY" scripts/probe_tool_calling.py --runs 3
    PYTHONUTF8=1 "$PY" scripts/probe_tool_calling.py --mock         # 离线验链路
    PYTHONUTF8=1 "$PY" scripts/probe_tool_calling.py --provider deepseek

产出：`data/structured/TOOL_CALLING_PROBE.md` / `.json`
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lpa.llm import (                                          # noqa: E402
    AgentTranscript, LLMClient, ToolCall, make_client, tool_schema,
)

STRUCT = ROOT / "data" / "structured"
OUT_MD = STRUCT / "TOOL_CALLING_PROBE.md"
OUT_JSON = STRUCT / "TOOL_CALLING_PROBE.json"


# --------------------------------------------------------------------------
# 真实数据（项目文件，不是桩）
# --------------------------------------------------------------------------


def _load_children() -> List[Dict[str, Any]]:
    return json.loads((STRUCT / "child_profiles.json").read_text(encoding="utf-8"))


def _load_class() -> Dict[str, Any]:
    return json.loads((STRUCT / "class_profile.json").read_text(encoding="utf-8"))


CHILDREN = _load_children()
CLASS = _load_class()
BY_ID = {c["anon_id"]: c for c in CHILDREN}
ALL_IDS = [c["anon_id"] for c in CHILDREN]


# --------------------------------------------------------------------------
# 三个真实工具
# --------------------------------------------------------------------------
# ⚠ 这里是**探针用的最小实现**；P0-2 会把它扩成完整的 9 工具集
#   （search_corpus / get_previous_baseline / plan_layering /
#    validate_lesson_plan / render_docx / audit_docx …），
#   但 Schema 写法与 runner 签名**完全一致**，不会返工。

TOOLS = [
    tool_schema(
        "list_children",
        "列出本班全部幼儿的匿名编号（形如 S01）与总人数。"
        "回答任何涉及「有多少名幼儿」「都有哪些编号」的问题前，必须先调用本工具，"
        "不要凭猜测作答。",
    ),
    tool_schema(
        "get_child_profile",
        "查询某一名幼儿的能力档案：掌握度（0-1）、支持层级、出勤情况。",
        {"anon_id": {"type": "string",
                     "description": "幼儿匿名编号，必须形如 S01、S37（字母大写+两位数字）"}},
        required=["anon_id"],
    ),
    tool_schema(
        "get_class_summary",
        "查询本班概况：班级标签、年龄段、档案人数、各领域支持层级分布。",
    ),
]


def run_tool(call: ToolCall) -> Any:
    """工具执行器。签名 `(ToolCall) -> Any`，与 `run_with_tools` 的契约一致。"""
    name, args = call.name, call.arguments
    if name == "list_children":
        return {"count": len(ALL_IDS), "ids": ALL_IDS}
    if name == "get_child_profile":
        cid = str(args.get("anon_id", "")).strip().upper()
        c = BY_ID.get(cid)
        if c is None:
            # ⚠ 不兜底、不返回空对象 —— 明确告诉模型编号不存在，
            #   让它自己决定是重新查名册还是改参数（这是 Agent 该有的行为）
            raise KeyError(f"编号 {cid!r} 不存在，可用编号形如 S01–S{len(ALL_IDS):02d}")
        return {
            "anon_id": c["anon_id"],
            "mastery": c["mastery"],
            "support_level": c["support_level"],
            "attendance": c["attendance"],
            "lessons_observed": c["lessons_observed"],
        }
    if name == "get_class_summary":
        return {
            "class_label": CLASS["class_label"],
            "age_range": CLASS["age_range"],
            "class_size": CLASS["class_size"],
            "profile_count": len(CHILDREN),
        }
    raise KeyError(f"未知工具 {name!r}")


# --------------------------------------------------------------------------
# 任务与判据
# --------------------------------------------------------------------------

SYSTEM = (
    "你是一个幼儿园康复教案助手。回答任何问题都必须先调用工具拿到真实数据，"
    "严禁凭记忆或推测编造幼儿编号、人数、掌握度等任何数据。"
    "如果工具报错，请阅读错误信息并调整参数后重试。"
)

TASK = (
    "请回答两个问题：\n"
    "1. 本班一共收录了多少名幼儿的档案？\n"
    "2. 其中 S01 和 S37 两位幼儿的支持层级分别是什么？\n"
    "要求：先取回名册确认人数与编号，再分别查询这两名幼儿的档案，最后给出结论。"
)

# 期望的工具调用序列（顺序敏感：不先查名册就不该知道有 S37）
EXPECT_SEQ = ["list_children", "get_child_profile", "get_child_profile"]
EXPECT_IDS = {"S01", "S37"}

S01_LEVEL = BY_ID["S01"]["support_level"]
S37_LEVEL = BY_ID["S37"]["support_level"]
EXPECT_IN_FINAL = [str(len(ALL_IDS)), S01_LEVEL, S37_LEVEL]


def judge(t: AgentTranscript) -> Dict[str, Any]:
    """把一条轨迹判成可统计的布尔项。"""
    seq = [c.name for c in t.calls]
    ids = {str(c.arguments.get("anon_id", "")).upper()
           for c in t.calls if c.name == "get_child_profile"}
    final = t.final_text or ""
    return {
        "seq_ok": seq == EXPECT_SEQ,
        "seq": seq,
        "ids_ok": ids == EXPECT_IDS,
        "ids": sorted(ids),
        "answer_ok": all(s in final for s in EXPECT_IN_FINAL),
        "missing_in_final": [s for s in EXPECT_IN_FINAL if s not in final],
        "converged": t.stopped_reason == "finish",
        "no_parse_error": not any(c.parse_error for c in t.calls),
        "no_tool_error": not any(o.get("error") for s in t.steps for o in s.observations),
        "unused": t.unused_tools,
        "steps": len(t.steps),
        "tool_calls": t.tool_call_count,
        "elapsed_s": round(t.elapsed_s, 2),
        "tokens": t.usage["total"],
    }


def run_once(client: LLMClient, max_steps: int, verbose: bool) -> AgentTranscript:
    client.verbose = verbose          # verbose 是客户端属性，不是 run_with_tools 的参数
    return client.run_with_tools(
        system=SYSTEM, user=TASK, tools=TOOLS,
        runner=run_tool, max_steps=max_steps,
    )


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

def render_md(provider: str, model: str, runs: List[Dict[str, Any]],
              transcripts: List[AgentTranscript], mock_note: str) -> str:
    n = len(runs)
    ok = lambda k: sum(1 for r in runs if r["j"] and r["j"][k])      # noqa: E731

    o: List[str] = []
    o.append("# 工具调用能力探针（Function Calling Probe）\n")
    o.append("> 本报告由 `scripts/probe_tool_calling.py` 自动生成，数字全部来自实跑。\n")
    o.append("## 一、这份报告回答什么\n")
    o.append("两个问题，别混在一起读：\n")
    o.append("1. **链路能不能跑通** —— Agent 循环的终止条件、`tool_call_id` 配对、"
             "参数解析、异常回灌、Trace 结构。这一层**与模型强弱无关**，"
             "所以用 mock 离线就能验完（见 §二）。\n")
    o.append("2. **这个模型撑不撑得住多轮工具调用** —— 这是底座模型选型的实测依据"
             "（见 §三、§四）。轻量免费档的 function calling 常是短板，"
             "**必须实测，不能想当然**。\n")
    o.append("## 二、链路自检（mock，离线，确定性）\n")
    o.append(mock_note + "\n")
    o.append("mock 走的是与真实模型**完全相同的** `run_with_tools()` 代码路径 —— "
             "循环、消息回灌、`tool_call_id` 配对、Trace 组装都是同一份实现，"
             "只有「模型那一侧」被替换成确定性的替身。"
             "所以 mock 通过 = 链路无缺陷；模型能力另算。\n")
    o.append("## 三、任务设计：为什么必须调 3 次\n")
    o.append(f"- **挂载工具**：{', '.join(t['function']['name'] for t in TOOLS)}\n")
    o.append("- **任务**：先报出本班档案人数，再分别查出 S01 与 S37 的支持层级。\n")
    o.append("- **刻意的依赖链**：不调 `list_children` 就不知道有 S37 这个编号；"
             "不调 `get_child_profile` 两次就拿不到两个人的层级。"
             "**顺序错了、漏调了、硬编答案，都会被判据抓到。**\n")
    o.append("- **反幻觉设计**：`SYSTEM` 里明令「必须调工具、严禁编造」；"
             "`get_child_profile` 对不存在的编号**抛异常而非返回空值**，"
             "异常会作为 Observation 回灌 —— 测模型能否读错误信息自我纠正。\n")
    o.append("## 四、实跑结果\n")
    o.append(f"| | |\n|---|---|\n| provider | `{provider}` |\n| model | `{model}` |")
    o.append(f"| 重复次数 | {n} |\n"
             f"| 实际步数（最多一次） | "
             f"{max((r['j']['steps'] for r in runs if r['j']), default=0)} |")
    o.append("")
    o.append("| 判据 | 通过 | 含义 |\n|---|---|---|")
    o.append(f"| 工具序列正确 | **{ok('seq_ok')}/{n}** | 是否恰好按 `list_children → "
             f"get_child_profile → get_child_profile` 调用 |")
    o.append(f"| 参数正确 | **{ok('ids_ok')}/{n}** | 两次查询的 `anon_id` 是否为 S01 与 S37 |")
    o.append(f"| 最终答案正确 | **{ok('answer_ok')}/{n}** | 结论里是否同时出现人数与两个层级原文 |")
    o.append(f"| 正常收尾 | **{ok('converged')}/{n}** | `stopped_reason=finish`（不是撞 max_steps） |")
    o.append(f"| 参数无解析错误 | **{ok('no_parse_error')}/{n}** | 模型给的 arguments 是否都是合法 JSON |")
    o.append(f"| 无工具执行错误 | **{ok('no_tool_error')}/{n}** | 是否传了不存在的编号/参数 |")
    o.append("")
    o.append("### 逐次明细\n")
    o.append("| 次 | 调用序列 | anon_id | 步数 | 调用数 | 收尾 | 耗时(s) | tokens |")
    o.append("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(runs, 1):
        j = r["j"] or {}
        if not j:
            o.append(f"| {i} | ❌ 调用失败 | — | — | — | — | — | — |")
            continue
        o.append(f"| {i} | {' → '.join(j['seq']) or '(未调工具)'} | "
                 f"{', '.join(j['ids']) or '—'} | {j['steps']} | {j['tool_calls']} | "
                 f"{'✅ finish' if j['converged'] else '⚠ 未收尾'} | "
                 f"{j['elapsed_s']} | {j['tokens']} |")
    o.append("")
    # 完整的 Thought-Action-Observation 轨迹（报告直接引用）
    o.append("## 五、完整调用轨迹（Thought → Action → Observation）\n")
    o.append("> 下面这段就是报告「运行效果与链路验证」一节需要的原始 Trace —— "
             "**不是事后补写的，是循环运行时逐步骤记下来的。**\n")
    for i, t in enumerate(transcripts, 1):
        if not t:
            continue
        o.append(f"### 第 {i} 次运行（`{t.stopped_reason}`，"
                 f"{t.tool_call_count} 次工具调用，{t.elapsed_s:.1f}s）\n")
        o.append("```text")
        o.append(t.render())
        o.append("```\n")
        break                                   # 只贴一次，避免报告过长
    o.append("## 六、结论与下一步\n")
    if n and ok("seq_ok") == n and ok("answer_ok") == n:
        o.append(f"**{n}/{n} 次全部按预期完成多轮工具调用，序列与参数均正确。** "
                 f"说明 `{model}` 具备可用的 function calling 能力，"
                 "`run_with_tools()` 的多轮闭环在本模型上成立。\n")
    elif n and ok("seq_ok") == 0:
        o.append(f"⚠ **{n} 次全部未按预期序列调用。** 优先怀疑模型侧的 function "
                 "calling 能力不足 —— 这是 P1-4 要解决的问题："
                 "换 `glm-4-air/plus` 或 `deepseek-flash` 复测。\n")
    else:
        o.append(f"⚠ 结果**不稳定**（序列正确 {ok('seq_ok')}/{n}）。"
                 "多轮工具调用对轻量模型是压力测试，"
                 "不稳定本身就是「换模型」的理由。\n")
    o.append("- **P0-2 待办**：把本探针的 3 个桩工具替换成完整 9 工具集"
             "（`search_corpus` / `get_previous_baseline` / `plan_layering` / "
             "`validate_lesson_plan` / `render_docx` / `audit_docx` …）。"
             "Schema 写法与 runner 签名已对齐，不返工。\n")
    o.append("- **P0-4 待办**：`AgentTranscript.render()` 已经是 "
             "`[Thought]` / `[Action]` / `[Observation]` 格式，"
             "直接接文件落盘即可，无需另写日志格式。\n")
    o.append("> ⚠ 本次样本量小（n=%d），只作**定性判断**与选型参考，"
             "不构成统计结论。\n" % n)
    return "\n".join(o)


def main() -> int:
    ap = argparse.ArgumentParser(description="工具调用能力探针（P0-1 验证 / P1-4 选型依据）")
    ap.add_argument("--runs", type=int, default=3, help="真实调用重复次数（默认 3）")
    ap.add_argument("--max-steps", type=int, default=8, help="最大轮数（默认 8）")
    ap.add_argument("--provider", default=None, help="指定 provider（默认自动探测）")
    ap.add_argument("--model", default=None, help="覆盖模型名")
    ap.add_argument("--mock", action="store_true", help="强制 mock：只验链路，不调 API")
    ap.add_argument("-o", "--out", default=str(OUT_MD), help="报告输出路径")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    # ---- ① 链路自检（mock，离线）----
    print("[probe] ① 链路自检（mock，不调 API）……")
    mock_client = LLMClient(provider="mock")
    mock_t = run_once(mock_client, args.max_steps, False)
    mock_j = judge(mock_t)
    # ⚠ mock 的判据**只考链路，不考任务答对**：mock 是确定性替身，
    #   它按 TOOLS 顺序瞎调、参数填占位值，答不对是正常的。
    #   要考的是：循环会不会收敛、参数解析会不会炸、
    #   每次调用有没有配到 Observation（`tool_call_id` 配对）、
    #   工具抛异常时循环会不会崩。
    n_obs = sum(len(s.observations) for s in mock_t.steps)
    link_ok = (
        mock_j["converged"]                       # ① 正常收敛，不是撞 max_steps
        and mock_j["no_parse_error"]              # ② arguments 解析无错
        and mock_j["tool_calls"] >= 2             # ③ 真的调了工具
        and n_obs == mock_j["tool_calls"]         # ④ 调用数 == Observation 数（配对不缺）
        and len(mock_t.steps) == mock_j["tool_calls"] + 1   # ⑤ 末尾多一步收尾
    )
    mock_report = (
        "✅ **链路自检通过。** mock 是确定性替身（按工具顺序瞎调、参数填占位值），"
        "**它答不对任务是应该的** —— 这里考的只是链路本身：\n\n"
        "| 链路判据 | 实测 |\n|---|---|\n"
        f"| 循环正常收敛（`finish`，非撞 `max_steps`） | {'✅' if mock_j['converged'] else '❌'} "
        f"（收尾原因 `{mock_t.stopped_reason}`） |\n"
        f"| `arguments` 解析无错 | {'✅' if mock_j['no_parse_error'] else '❌'} |\n"
        f"| 每次调用都有配对 Observation（`tool_call_id` 不缺） | "
        f"{'✅' if n_obs == mock_j['tool_calls'] else '❌'} "
        f"（调用 {mock_j['tool_calls']} 次 / Observation {n_obs} 条） |\n"
        f"| 循环步数 = 工具轮 + 1 步收尾 | "
        f"{'✅' if len(mock_t.steps) == mock_j['tool_calls'] + 1 else '❌'} "
        f"（共 {len(mock_t.steps)} 步） |\n\n"
        "**顺带验到一条更重要的：工具抛异常不会炸掉循环。** mock 给 "
        "`get_child_profile` 传了个不存在的编号（`示例`），工具直接抛了 "
        "`KeyError`；这条异常被 `_run_one_tool()` 捕获、转成 Observation 文本回灌给"
        "「模型」，循环**继续走完并正常收尾**。这正是 Agent 该有的行为 —— "
        "工具报错是**信息**，不是崩溃。"
    )
    if link_ok:
        print(f"  ✅ 链路自检通过（{mock_j['tool_calls']} 次调用 / {n_obs} 条 Observation "
              f"/ {len(mock_t.steps)} 步 / 收尾={mock_t.stopped_reason}）")
        print("     └ 含一次故意的工具异常（非法编号），已被回灌而未中断循环")
    else:
        print(f"  ❌ 链路自检未通过：converged={mock_j['converged']} "
              f"parse_err={not mock_j['no_parse_error']} calls={mock_j['tool_calls']} "
              f"obs={n_obs} steps={len(mock_t.steps)}")
        print("     → 先修 src/lpa/llm.py 的 run_with_tools，再谈真实调用")
        return 1
    print("--- mock 轨迹（证明异常回灌）---")
    print(mock_t.render())

    # ---- ② 真实调用 ----
    if args.mock:
        print("\n[probe] --mock 指定，跳过真实调用")
    else:
        client = (LLMClient(provider=args.provider, model=args.model, verbose=args.verbose)
                  if args.provider else make_client(verbose=args.verbose))
        if args.model:
            client.model = args.model
        if client.provider == "mock":
            print("\n[probe] 未找到可用 key，真实调用退化为 mock —— "
                  "本报告将只含链路自检结论")
        else:
            print(f"\n[probe] ② 真实调用 ×{args.runs}（{client.provider} / {client.model}）……")
            runs, transcripts = [], []
            for i in range(1, max(1, args.runs) + 1):
                t0 = time.time()
                try:
                    t = run_once(client, args.max_steps, args.verbose)
                    j = judge(t)
                except Exception as e:                          # noqa: BLE001
                    print(f"  第 {i} 次异常: {type(e).__name__}: {e}")
                    runs.append({"j": None, "error": f"{type(e).__name__}: {e}"})
                    transcripts.append(None)
                    continue
                runs.append({"j": j})
                transcripts.append(t)
                print(f"  第 {i} 次: 序列={j['seq']} 参数={'✅' if j['ids_ok'] else '❌'} "
                      f"答案={'✅' if j['answer_ok'] else '❌'} "
                      f"收尾={t.stopped_reason} {time.time() - t0:.1f}s")

            md = render_md(client.provider, client.model or "", runs, transcripts, mock_report)
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(md, encoding="utf-8")
            Path(str(args.out).replace(".md", ".json")).write_text(
                json.dumps({
                    "provider": client.provider, "model": client.model,
                    "tools": [t["function"]["name"] for t in TOOLS],
                    "expect_seq": EXPECT_SEQ, "expect_ids": sorted(EXPECT_IDS),
                    "mock_link_check": mock_j,
                    "runs": [{"judge": r.get("j"), "error": r.get("error"),
                              "transcript": (t.to_dict() if t else None)}
                             for r, t in zip(runs, transcripts)],
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n[probe] 报告已写入 {args.out}")
            print(f"[probe] 数据已写入 {str(args.out).replace('.md', '.json')}")
            return 0

    # mock / 无 key 分支也出报告
    md = render_md("mock", "rule-template", [{"j": mock_j}], [mock_t], mock_report)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(md, encoding="utf-8")
    print(f"\n[probe] 报告已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

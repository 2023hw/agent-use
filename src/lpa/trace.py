"""Trace 落盘（P0-4）—— 把 Agent 的思考链变成可交付、可回放的证据。

## 为什么有这一层

`AgentTranscript`（`llm.py`）是**内存态**的 Trace 载体：`.render()` 出
`[Thought]/[Action]/[Observation]` 文本、`.to_dict()` 出 JSON —— 但进程一关就没了。
本模块只做两件事，把「内存里的痕迹」变成「磁盘上的证据」：

1. **每步一行 JSON** → `data/trace/<run_id>.jsonl`
   （`StepLogger` 在运行中**实时增量**写：崩溃了也有前半段证据；
   `write_trace_jsonl()` 在跑完后整卷重写一遍兜底，两者格式完全一致）；
2. **人可读文本** → `data/trace/<run_id>.md`
   （`[Thought] / [Action] / [Observation]` 直接贴进报告或答辩材料）。

## 字段规范（与课程要求的 Trace 字段一一对应）

    step / thought / action(tool, args) / observation(摘要) / elapsed_ms / tokens

一步（`ToolRun`）= 一行；一步内若有多个工具调用，`actions` / `observations`
是数组。**observation 只留摘要** —— 工具返回的完整内容另有各工具的产物文件，
trace 里截断是刻意的（保证一行可读、可 diff）。

## 对应的课程红线

「满分档要求附带完整的思考链与工具调用 Trace/日志」——
`data/trace/*.jsonl` + `*.md` 就是这条要求的直接应答；
也直接对冲「黑盒截图扣 10 分」。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .llm import AgentTranscript, ToolRun

# observation 摘要上限（字符）。工具的完整返回值体积远大于此，
# trace 是「证据索引」不是「数据仓库」——需要全文去运行目录找产物。
_OBS_SUMMARY_LIMIT = 600


def _summarize(text: str, limit: int = _OBS_SUMMARY_LIMIT) -> str:
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[: limit - 1] + "…"


def step_to_event(run: ToolRun) -> Dict[str, Any]:
    """一步（一轮模型响应 + 其工具调用/观察值）→ 一行 JSON 事件。"""
    actions: List[Dict[str, Any]] = []
    for c in run.calls:
        item: Dict[str, Any] = {"tool": c.name, "args": c.arguments}
        if c.parse_error:
            item["parse_error"] = c.parse_error
        actions.append(item)
    observations: List[Dict[str, Any]] = []
    for o in run.observations:
        item = {
            "tool": o.get("name", ""),
            "ok": not o.get("error"),
            "summary": _summarize(o.get("content") or ""),
        }
        if o.get("error"):
            item["error"] = o["error"]
        observations.append(item)
    return {
        "step": run.index,
        "thought": run.thought,
        "actions": actions,
        "observations": observations,
        "elapsed_ms": int(round(run.elapsed_s * 1000)),
        "tokens": {"prompt": run.prompt_tokens, "completion": run.completion_tokens},
        "result": run.result or "continue",
    }


class StepLogger:
    """`run_with_tools(on_step=...)` 的实时落盘器。

    每一步一到达就 append + flush —— 运行中断/崩溃时，已发生的步骤不会丢。
    跑完后用 `write_trace_jsonl()` 整卷重写（幂等、同格式）兜底；
    即使重写失败，这里的前半段证据仍在。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w", encoding="utf-8")  # 一次运行一个文件，重跑覆盖
        self.count = 0

    def __call__(self, run: ToolRun) -> None:
        self._fh.write(json.dumps(step_to_event(run), ensure_ascii=False) + "\n")
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:                                    # noqa: BLE001
            pass


def write_trace_jsonl(transcript: AgentTranscript, path: Path) -> Path:
    """整卷重写 jsonl（幂等；与 `StepLogger` 实时写入同格式）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for run in transcript.steps:
            fh.write(json.dumps(step_to_event(run), ensure_ascii=False) + "\n")
    return path


def render_trace_md(transcript: AgentTranscript,
                    meta: Optional[Dict[str, Any]] = None) -> str:
    """人可读 Trace：元信息头 + `[Thought]/[Action]/[Observation]` 全文。"""
    meta = meta or {}
    out: List[str] = []
    run_id = meta.get("run_id", "未命名运行")
    out.append(f"# Agent Trace · {run_id}")
    out.append("")
    out.append(f"- 生成时间：{meta.get('generated_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if meta.get("scene"):
        out.append(f"- 场景：{meta['scene']}")
    if meta.get("theme"):
        out.append(f"- 主题：{meta['theme']}")
    out.append(f"- 模型：{transcript.provider} / {transcript.model or '（未知）'}"
               + ("（mock）" if transcript.mocked else ""))
    out.append(f"- 运行结果：{transcript.stopped_reason or '（未收尾）'}"
               f"；{len(transcript.steps)} 步；{transcript.tool_call_count} 次工具调用"
               f"；耗时 {transcript.elapsed_s:.1f}s")
    out.append(f"- Token：prompt {transcript.prompt_tokens} / "
               f"completion {transcript.completion_tokens}")
    used = transcript.used_tools
    out.append(f"- 工具使用：{len(used)}/{len(transcript.tools)}"
               + (f" —— {'、'.join(used)}" if used else ""))
    if transcript.unused_tools:
        out.append(f"- 挂载未调用：{'、'.join(transcript.unused_tools)}")
    out.append("")
    out.append("## Thought → Action → Observation")
    out.append("")
    out.append(transcript.render())
    out.append("")
    out.append("---")
    out.append("由 `lpa.trace` 自动生成（P0-4）。同目录同名 `.jsonl` 为逐行 JSON 版本，"
               "字段：step / thought / actions / observations / elapsed_ms / tokens。")
    return "\n".join(out)


def save_trace(transcript: AgentTranscript, trace_dir: Path, run_id: str,
               meta: Optional[Dict[str, Any]] = None) -> Dict[str, Path]:
    """跑完后落盘整套 Trace：`<run_id>.jsonl` + `<run_id>.md`。"""
    trace_dir = Path(trace_dir)
    meta = dict(meta or {})
    meta.setdefault("run_id", run_id)
    jsonl = write_trace_jsonl(transcript, trace_dir / f"{run_id}.jsonl")
    md_path = trace_dir / f"{run_id}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_trace_md(transcript, meta), encoding="utf-8")
    return {"jsonl": jsonl, "md": md_path}

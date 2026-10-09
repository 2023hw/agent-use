"""可切换模型层。

五个 provider，全部走 OpenAI 兼容接口，一套代码切到底：
  - DeepSeek     https://api.deepseek.com/v1        → deepseek-flash (V4.1-Flash)
  - SiliconFlow  https://api.siliconflow.cn/v1      → Qwen2.5-7B
  - 智谱 GLM     https://open.bigmodel.cn/api/paas/v4 → glm-4-flash（免费）
  - ModelScope   https://api-inference.modelscope.cn/v1 → DeepSeek-V4.1-Flash（免费额度）
  - Ollama       http://localhost:11434/v1          （本地，无 key）

**模型名别写错（2026-10-08 核对官方文档）。**
DeepSeek-V4.1-Flash 在 API 里的 model id 是 **`deepseek-flash`**，
不是 `deepseek-V4.1-Flash`，也不是老的 `deepseek-chat`（那是 V3.2）。
旧名 `deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` 虽还能调，
但底层已下线，会被路由到 V4.1-Flash 并按 Flash 价计费 —— 属于待清理的技术债。

**思考模式默认关闭。**
V4.1-Flash 默认开思考模式（effort=high），思维链会吃掉大量 token 且
`temperature` 失效。本项目的生成任务是"按策略库扩写教案"，不需要深度推理，
故统一发 `{"thinking": {"type": "disabled"}}`。想打开就设 `LPA_THINKING=1`。

**key 从哪来（按优先级）。**
  1. 进程环境变量（命令行里临时设）
  2. 项目根 `.env` 文件（推荐，不进 git）
  3. 都没有 → 降级 mock

**关键设计：mock 模式。**
没有 API key 时自动降级到 mock，用规则模板拼出合法输出。
这样：
  - 全链路可以离线跑通（答辩现场断网也不怕）
  - 消融实验里"无 LLM"这一档就是 mock
  - 单元测试不消耗额度

**关键设计：指数退避。**
免费档普遍限速（每分钟 20 次、并发 2），批量跑必撞 429。
退避序列 1→2→4→8→16 秒 + 抖动，最多 6 次。

**三个入口，别混用（2026-10-09 加的工具调用能力）。**

| 入口 | 形态 | 谁在用 |
|---|---|---|
| `complete(system, user)` | 单轮，纯文本出 | **固定流水线**（W2 五个步骤）—— 保持原样不动 |
| `complete_raw(messages, tools=…)` | 任意多轮，**保留 `tool_calls`** | Agent 循环的底座，也是排查「模型到底调了什么」的地方 |
| `run_with_tools(system, user, tools, runner)` | **Agent 主循环**：模型自己决定调哪个工具、调几次 | P0-2 起的工具化生成；返回可直接当 Trace 用的 `AgentTranscript` |

`complete()` 与 `complete_raw()` **共用同一份重试逻辑**（`_chat()`）——
免费档限速在 Agent 循环下撞得更频繁，两套各自实现迟早会漏掉一边。

**工具调用的四条硬规矩（都踩过或防过）：**

1. **回灌历史时必须原样带回 `tool_calls`** —— 否则下一轮 `role="tool"` 消息
   找不到对应的 `tool_call_id`，接口直接报 400。
2. **工具抛异常不中断循环** —— 异常文本转成 Observation 回灌给模型，让它
   自己看见错误并修参数。工具报错是**信息**，不是崩溃。
3. **参数解析失败不兜底** —— `arguments` 不是合法 JSON 时，把错误原文回给模型
   重填，**绝不填默认值**（硬凑参数会静默产出一份错教案，比报错危险得多）。
4. **必须有 `max_steps` 硬闸** —— 没有它模型会在两个工具之间无限来回。
   到顶即停并记 `stopped_reason="max_steps"`，这是失败信号不是正常收尾。
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------
# .env 加载（在 provider 表之前，保证 make_client 能读到）
# --------------------------------------------------------------------------


def _load_dotenv() -> None:
    """把项目根 / 当前目录的 .env 灌进 os.environ（不覆盖已有的环境变量）。

    手写而不是引 python-dotenv：少一个依赖，且行为完全可控。
    """
    here = Path(__file__).resolve()
    root = here.parents[2] if len(here.parents) >= 3 else here.parent
    candidates = [root / ".env", Path.cwd() / ".env"]
    seen: set = set()
    for p in candidates:
        try:
            rp = p.resolve()
        except Exception:                                    # noqa: BLE001
            continue
        if rp in seen or not p.is_file():
            continue
        seen.add(rp)
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:                                    # noqa: BLE001
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


_load_dotenv()


# --------------------------------------------------------------------------
# Provider 配置
# --------------------------------------------------------------------------


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    model: str
    key_env: str          # 从哪个环境变量读 key
    needs_key: bool = True
    note: str = ""
    extra_body: Dict[str, Any] = field(default_factory=dict)
    thinking_off: bool = False   # True → 默认发 thinking=disabled


PROVIDERS: Dict[str, ProviderConfig] = {
    "deepseek": ProviderConfig(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-flash",
        key_env="DEEPSEEK_API_KEY",
        note="DeepSeek-V4.1-Flash（官方现役 Flash，1M 上下文，需充值）",
        thinking_off=True,
    ),
    "siliconflow": ProviderConfig(
        name="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        model="Qwen/Qwen2.5-7B-Instruct",
        key_env="SILICONFLOW_API_KEY",
        note="注册送额度，9B 以下模型长期免费",
    ),
    "zhipu": ProviderConfig(
        name="zhipu",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-4-flash",
        key_env="ZHIPU_API_KEY",
        note="Flash 系列长期免费，无需充值",
    ),
    "zhipu45": ProviderConfig(
        name="zhipu45",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-4.5-flash",
        key_env="ZHIPU_API_KEY",
        note="GLM-4.5-Flash（免费）：思考模式下工具调用前输出 reasoning_content，"
             "Trace 可见完整思考链（LPA_PROVIDER=zhipu45 启用）",
        extra_body={"thinking": {"type": "enabled"}},
    ),
    "modelscope": ProviderConfig(
        name="modelscope",
        base_url="https://api-inference.modelscope.cn/v1",
        model="deepseek-ai/DeepSeek-V4.1-Flash",
        key_env="MODELSCOPE_API_KEY",
        note="魔搭社区免费额度，同款 V4.1-Flash（8K 上下文）",
    ),
    "ollama": ProviderConfig(
        name="ollama",
        base_url="http://localhost:11434/v1",
        model="qwen2.5:7b",
        key_env="",
        needs_key=False,
        note="本地部署，无需联网与 key",
    ),
}


def mask_key(key: str) -> str:
    """把 key 打码成 `sk-abc…7890`，方便打印诊断信息而不泄露。"""
    k = (key or "").strip()
    if not k:
        return "(未设置)"
    if len(k) <= 10:
        return k[:2] + "…" + "*" * max(len(k) - 2, 0)
    return f"{k[:6]}…{k[-4:]}"


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 调用结果
# --------------------------------------------------------------------------


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    mocked: bool = False
    attempts: int = 1
    elapsed_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def usage(self) -> Dict[str, int]:
        return {
            "prompt": self.prompt_tokens,
            "completion": self.completion_tokens,
            "total": self.prompt_tokens + self.completion_tokens,
        }


# --------------------------------------------------------------------------
# 工具调用（Function Calling）
# --------------------------------------------------------------------------
#
# 三个数据结构 + 两个工具函数，服务同一件事：**让模型自己决定调哪个工具、调几次**。
# 它们同时是报告的 Trace 载体 —— `AgentTranscript` 就是可直接引用的
# `[Thought] / [Action] / [Observation]` 记录，不需要另写一套日志格式。


@dataclass
class ToolCall:
    """模型请求的一次工具调用。"""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str = ""        # 非空 = 模型给的 arguments 不是合法 JSON

    def to_message_part(self) -> Dict[str, Any]:
        """还原成 OpenAI `assistant.tool_calls[]` 里的一项。

        ⚠ 回灌历史时必须把这一步的 `tool_calls` **原样带回** —— 否则下一轮的
        `role="tool"` 消息找不到对应的 `tool_call_id`，接口直接报 400。
        """
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.raw_arguments
                or json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass
class ToolRun:
    """Agent 循环里的一步 = 一次 Thought → Action → Observation。"""

    index: int
    thought: str = ""                                    # 模型这一步的正文
    calls: List[ToolCall] = field(default_factory=list)
    observations: List[Dict[str, Any]] = field(default_factory=list)
    result: str = ""                                     # "finish" = 这一步即最终答复
    elapsed_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class AgentTranscript:
    """一次 `run_with_tools()` 的完整轨迹 —— **它本身就是可核验的 Trace 记录**。"""

    system: str = ""
    user: str = ""
    tools: List[str] = field(default_factory=list)        # 本轮挂上去的工具名
    steps: List[ToolRun] = field(default_factory=list)
    final_text: str = ""
    stopped_reason: str = ""                             # finish / max_steps / guarded_input / guarded_output
    provider: str = ""
    model: str = ""
    mocked: bool = False
    elapsed_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    guard_events: List[Dict[str, Any]] = field(default_factory=list)   # P0-5 护栏裁决记录

    @property
    def calls(self) -> List[ToolCall]:
        return [c for s in self.steps for c in s.calls]

    @property
    def tool_call_count(self) -> int:
        return len(self.calls)

    @property
    def used_tools(self) -> List[str]:
        """实际被调用过的工具名（去重、保持首次出现顺序）。

        「挂上去了但一次没调」是很重要的诊断信号 —— 说明该工具的描述
        或参数对模型没有吸引力，需要重写 description。
        """
        seen: List[str] = []
        for c in self.calls:
            if c.name and c.name not in seen:
                seen.append(c.name)
        return seen

    @property
    def unused_tools(self) -> List[str]:
        return [t for t in self.tools if t not in self.used_tools]

    @property
    def usage(self) -> Dict[str, int]:
        return {
            "prompt": self.prompt_tokens,
            "completion": self.completion_tokens,
            "total": self.prompt_tokens + self.completion_tokens,
            "steps": len(self.steps),
            "tool_calls": self.tool_call_count,
        }

    def render(self) -> str:
        """渲染成报告可直接引用的 `[Thought] / [Action] / [Observation]` 文本。"""
        out: List[str] = []
        for s in self.steps:
            if s.thought:
                out.append(f"[Thought] {s.thought}")
            for c in s.calls:
                out.append(f"[Action] {c.name}({json.dumps(c.arguments, ensure_ascii=False)})")
            for o in s.observations:
                body = " ".join((o.get("content") or "").split())
                if len(body) > 300:
                    body = body[:300] + "…"
                out.append(f"[Observation] {body}")
        if self.final_text:
            out.append(f"[Final] {self.final_text}")
        return "\n".join(out)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "mocked": self.mocked,
            "tools": self.tools,
            "used_tools": self.used_tools,
            "unused_tools": self.unused_tools,
            "stopped_reason": self.stopped_reason,
            "guard_events": self.guard_events,
            "elapsed_s": round(self.elapsed_s, 2),
            "usage": self.usage,
            "steps": [
                {
                    "index": s.index,
                    "thought": s.thought,
                    "result": s.result,
                    "elapsed_s": round(s.elapsed_s, 2),
                    "calls": [
                        {"id": c.id, "name": c.name, "arguments": c.arguments,
                         "parse_error": c.parse_error}
                        for c in s.calls
                    ],
                    "observations": s.observations,
                }
                for s in self.steps
            ],
            "final_text": self.final_text,
        }


def tool_schema(name: str, description: str,
                properties: Optional[Dict[str, Any]] = None,
                required: Optional[List[str]] = None) -> Dict[str, Any]:
    """造一个 OpenAI 格式的工具定义。

    `properties` 的值可以是类型名（`"string"`）或完整的 JSON Schema 片段 ——
    长描述用后者，能显著提高模型的选工具准确率。
    """
    props: Dict[str, Any] = {}
    for k, v in (properties or {}).items():
        props[k] = {"type": "string"} if isinstance(v, str) else dict(v)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": list(required or []),
            },
        },
    }


def _tool_name(spec: Dict[str, Any]) -> str:
    return ((spec or {}).get("function") or {}).get("name") or ""


def _extract_tool_calls(msg: Dict[str, Any]) -> List[ToolCall]:
    """把 assistant message 的 `tool_calls` 解析成 `ToolCall`，**永不抛异常**。

    两种模型行为都要认：`function.arguments` 是 JSON 字符串（OpenAI 标准），
    也可能已被上游解析成 dict（部分国产模型直接给对象）。
    参数坏掉时不在这里报错 —— 留着 `parse_error`，由循环回灌给模型让它自己修。
    """
    out: List[ToolCall] = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        raw = fn.get("arguments")
        args: Dict[str, Any] = {}
        perr = ""
        if isinstance(raw, dict):
            args = raw
            raw_str = json.dumps(raw, ensure_ascii=False)
        else:
            raw_str = raw or ""
            if raw_str.strip():
                try:
                    parsed = json.loads(raw_str)
                    if isinstance(parsed, dict):
                        args = parsed
                    else:
                        perr = f"arguments 不是 JSON 对象（得到 {type(parsed).__name__}）"
                except json.JSONDecodeError as e:
                    perr = f"arguments 不是合法 JSON: {e}"
        out.append(ToolCall(
            id=tc.get("id") or f"call_{i}",
            name=fn.get("name") or "",
            arguments=args,
            raw_arguments=raw_str,
            parse_error=perr,
        ))
    return out


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


@dataclass
class LLMClient:
    """OpenAI 兼容客户端。

    provider 取 PROVIDERS 的键；传 'mock' 强制走规则模板。
    环境变量覆盖：
      LPA_PROVIDER  强制指定 provider（跳过自动探测）
      LPA_MODEL     覆盖模型名
      LPA_THINKING  设为 1 时打开思考模式（默认关）
    """

    provider: str = "mock"
    model: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout: int = 120
    max_retries: int = 6
    verbose: bool = False
    calls: List[LLMResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cfg: Optional[ProviderConfig] = None
        self.api_key: str = ""
        if self.provider == "mock":
            return
        if self.provider not in PROVIDERS:
            raise LLMError(f"未知 provider: {self.provider}，可选 {list(PROVIDERS)} 或 'mock'")
        self.cfg = PROVIDERS[self.provider]
        if self.model is None:
            # LPA_MODEL 优先于 provider 默认模型
            self.model = os.environ.get("LPA_MODEL", "").strip() or self.cfg.model
        if self.cfg.needs_key:
            self.api_key = os.environ.get(self.cfg.key_env, "").strip()
            if not self.api_key:
                # 没有 key 就降级，而不是报错 —— 保证全链路可跑
                if self.verbose:
                    print(f"[llm] {self.cfg.key_env} 未设置，降级为 mock")
                self.provider = "mock"
                self.cfg = None

    # ---------------- 公开入口 ----------------

    def complete(self, system: str, user: str,
                 json_mode: bool = False) -> LLMResult:
        """单轮文本生成：system + user 进，纯文本出。

        这是**固定流水线**用的接口（W2 的五个步骤都走它），签名与行为保持不变。
        需要模型自己决定调工具时走 `run_with_tools()`。
        """
        if self.provider == "mock":
            r = _mock_complete(system, user, json_mode)
            self.calls.append(r)
            return r

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        self._apply_provider_params(payload)

        data, attempts, elapsed = self._chat(payload)
        msg_obj = data["choices"][0]["message"] or {}
        msg = msg_obj.get("content") or ""
        if not msg:
            # 思考模式下 content 可能为空（思维链吃满 max_tokens）
            msg = msg_obj.get("reasoning_content") or ""
        usage = data.get("usage", {}) or {}
        r = LLMResult(
            text=msg,
            provider=self.provider,
            model=data.get("model") or self.model or "",
            attempts=attempts,
            elapsed_s=elapsed,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
        self.calls.append(r)
        return r

    def complete_json(self, system: str, user: str) -> Dict[str, Any]:
        """调一次并解析 JSON。对常见的模型输出瑕疵做容错。"""
        r = self.complete(system, user, json_mode=True)
        return parse_json_loose(r.text)

    # ---------------- 工具调用（Function Calling）----------------

    def complete_raw(self, messages: List[Dict[str, Any]],
                     tools: Optional[List[Dict[str, Any]]] = None,
                     tool_choice: Optional[str] = None,
                     json_mode: bool = False) -> Dict[str, Any]:
        """原始对话补全：收完整 messages、可挂 tools，回**原始 assistant message**。

        与 `complete()` 的分工：
          - `complete()`  = system + user 进、纯文本出 —— 给固定流水线用；
          - `complete_raw()` 保留 `tool_calls` 字段、接受任意多轮 messages
            —— 它是 Agent 循环的底座，也是排查「模型到底调了什么」的地方。

        返回 `{"message": {...}, "usage": {...}, "model": str,
               "elapsed_s": float, "attempts": int}`（故意不封装成对象，
              方便直接把 `message` 原样塞回 messages 列表）。
        """
        if self.provider == "mock":
            return _mock_raw_completion(messages, tools)

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        self._apply_provider_params(payload)

        data, attempts, elapsed = self._chat(payload)
        usage = data.get("usage", {}) or {}
        return {
            "message": data["choices"][0]["message"] or {},
            "usage": usage,
            "model": data.get("model") or self.model or "",
            "elapsed_s": elapsed,
            "attempts": attempts,
        }

    def run_with_tools(self, system: str, user: str,
                       tools: List[Dict[str, Any]],
                       runner: Any,
                       max_steps: int = 8,
                       on_step: Any = None) -> AgentTranscript:
        """**Agent 主循环**：让模型自己决定调哪个工具、调几次、什么时候收尾。

        每一轮：
          1. 把「历史 messages + 工具清单」发给模型；
          2. 若它回了 `tool_calls` → 本地执行 `runner(call)`，
             把结果以 `role="tool"` 回灌，进入下一轮；
          3. 若它只回正文、不调工具 → 视为给出最终答复，循环结束。

        参数
        ----
        tools     OpenAI 工具 Schema 列表（用 `tool_schema()` 造）。
        runner    可调用对象，签名 `runner(ToolCall) -> str | dict | list`。
                  **抛异常不中断循环** —— 异常文本会作为 Observation 回灌，
                  让模型自己看见错误并修正参数（这才是 Agent 该有的行为，
                  而不是让整个流程崩掉）。
        max_steps 最大轮数；到顶即停并记 `stopped_reason="max_steps"`。
                  ⚠ 这是防死循环的硬闸 —— 没有它，模型可以在两个工具之间
                  无限来回（本项目实测过模型反复重试同一工具的情况）。
        on_step   每步回调 `on_step(ToolRun)`，供实时落盘 Trace 用。

        返回 `AgentTranscript` —— **它本身就是可核验的 Trace**，
        `.render()` 出 `[Thought]/[Action]/[Observation]` 文本，
        `.to_dict()` 出可存档的 JSON。
        """
        t0 = time.time()
        transcript = AgentTranscript(
            system=system,
            user=user,
            tools=[_tool_name(t) for t in (tools or [])],
            provider=self.provider,
            model=self.model or "",
            mocked=self.provider == "mock",
        )
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        for idx in range(1, max(1, max_steps) + 1):
            step_t0 = time.time()
            resp = self.complete_raw(messages, tools=tools)
            msg = resp.get("message") or {}
            # 思考模型（如 glm-4.5-flash 开 thinking）把推理放在
            # reasoning_content、把面向用户的句子放在 content —— 两段都进
            # Trace 的 [Thought]；非思考模型只有 content，行为不变。
            reasoning = (msg.get("reasoning_content") or "").strip()
            content = (msg.get("content") or "").strip()
            thought = (reasoning + ("\n" + content if content else "")).strip() \
                if reasoning else content
            calls = _extract_tool_calls(msg)
            usage = resp.get("usage") or {}

            run = ToolRun(
                index=idx,
                thought=thought,
                calls=calls,
                elapsed_s=time.time() - step_t0,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            )
            transcript.prompt_tokens += run.prompt_tokens
            transcript.completion_tokens += run.completion_tokens

            # 回灌本步的 assistant 消息。带 tool_calls 时必须原样带回，
            # 否则下一轮的 tool 消息找不到对应 tool_call_id → 接口 400。
            assistant_msg: Dict[str, Any] = {
                "role": "assistant", "content": msg.get("content") or "",
            }
            if calls:
                assistant_msg["tool_calls"] = [c.to_message_part() for c in calls]
            messages.append(assistant_msg)

            if not calls:
                run.result = "finish"
                # 收尾答案以 content 为准；思考模型 content 为空时退用 thought
                transcript.final_text = content or thought
                transcript.stopped_reason = "finish"
                transcript.steps.append(run)
                if on_step:
                    on_step(run)
                break

            for call in calls:
                content, error = self._run_one_tool(call, runner)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": content,
                })
                run.observations.append({
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "content": content,
                    "error": error,
                })
            transcript.steps.append(run)
            if on_step:
                on_step(run)
            if self.verbose:
                names = ", ".join(c.name for c in calls)
                print(f"[llm] 第 {idx} 轮：调用 {names}")
        else:
            transcript.stopped_reason = "max_steps"
            if self.verbose:
                print(f"[llm] 达到 max_steps={max_steps}，强制收尾")

        transcript.elapsed_s = time.time() - t0
        return transcript

    def _run_one_tool(self, call: ToolCall, runner: Any) -> tuple:
        """执行一次工具调用。**任何异常都转成 Observation 文本，不向上抛。**"""
        if call.parse_error:
            # 参数不是合法 JSON —— 不要瞎猜、不要兜底成默认值，
            # 把错误原样回给模型让它自己重填（硬凑默认参数会静默产出错教案）。
            return (f"[参数解析失败] {call.parse_error}\n"
                    f"收到的原始 arguments：{call.raw_arguments[:400]}"), call.parse_error
        try:
            out = runner(call)
            text = out if isinstance(out, str) else json.dumps(
                out, ensure_ascii=False, default=str)
            return text, ""
        except Exception as e:                                  # noqa: BLE001
            if self.verbose:
                print(f"[llm] 工具 {call.name} 执行失败: {type(e).__name__}: {e}")
            return f"[工具执行失败] {type(e).__name__}: {e}", f"{type(e).__name__}: {e}"

    # ---------------- 内部：对话补全底座 ----------------

    def _apply_provider_params(self, payload: Dict[str, Any]) -> None:
        """叠加 provider 专属参数（如 DeepSeek 的思考模式开关）。"""
        cfg = self.cfg
        if cfg is None:
            return
        payload.update(dict(cfg.extra_body or {}))
        if cfg.thinking_off and os.environ.get("LPA_THINKING", "").strip() not in ("1", "true", "on"):
            payload["thinking"] = {"type": "disabled"}

    def _chat(self, payload: Dict[str, Any]) -> tuple:
        """发一次请求 + 指数退避重试。

        返回 `(原始响应 dict, 实际尝试次数, 耗时秒)`。
        单轮 `complete()` 与多轮 `complete_raw()` **共用这一份重试逻辑** ——
        免费档限速（20 次/分钟）在 Agent 循环下会撞得更频繁，
        两套各自实现迟早会漏掉一边。
        """
        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            t0 = time.time()
            try:
                data = self._post(payload)
                return data, attempt, time.time() - t0
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "ignore")[:300]
                except Exception:                            # noqa: BLE001
                    pass
                last_err = LLMError(f"HTTP {e.code}: {body}")
                # 429 / 5xx 退避重试；4xx（除 429）直接失败
                if e.code == 429 or e.code >= 500:
                    self._sleep(attempt)
                    continue
                raise last_err
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = LLMError(f"网络错误: {type(e).__name__}: {e}")
                self._sleep(attempt)
        raise LLMError(f"重试 {self.max_retries} 次仍失败: {last_err}")

    # ---------------- 内部 ----------------

    def _sleep(self, attempt: int) -> None:
        """指数退避 + 抖动。1→2→4→8→16→32 秒。"""
        base = 2 ** (attempt - 1)
        delay = min(base, 32) + random.uniform(0, 0.5)
        if self.verbose:
            print(f"[llm] 第 {attempt} 次失败，退避 {delay:.1f}s 后重试")
        time.sleep(delay)

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        assert self.cfg is not None
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ---------------- 统计 ----------------

    @property
    def total_usage(self) -> Dict[str, int]:
        p = sum(c.prompt_tokens for c in self.calls)
        c = sum(c.completion_tokens for c in self.calls)
        return {"prompt": p, "completion": c, "total": p + c,
                "calls": len(self.calls)}


# --------------------------------------------------------------------------
# JSON 容错解析
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_loose(text: str) -> Dict[str, Any]:
    """尽最大努力从模型输出里抠出 JSON 对象。

    模型常见的三种瑕疵：
      1. 包在 ```json ... ``` 里
      2. 前后有解释性文字
      3. 尾随逗号
    """
    s = (text or "").strip()
    m = _FENCE.search(s)
    if m:
        s = m.group(1).strip()
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            s = s[i:j + 1]
    s = re.sub(r",(\s*[}\]])", r"\1", s)   # 去尾随逗号
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON 解析失败: {e}\n原文前 500 字:\n{s[:500]}") from e
    if not isinstance(obj, dict):
        raise LLMError(f"期望 JSON 对象，得到 {type(obj).__name__}")
    return obj


# --------------------------------------------------------------------------
# Mock：规则模板
# --------------------------------------------------------------------------


def _mock_complete(system: str, user: str, json_mode: bool) -> LLMResult:
    """无 key 时的兜底：返回一个结构合法的占位输出。

    ⚠ 这里**不生成教学内容**，只保证 schema 通过，让流水线能跑通。
    真正的 mock 内容由 generate.py 的 template 分支负责（那才是"无 LLM 消融"）。
    """
    body = {"_mock": True, "_system_head": system[:120], "_user_head": user[:120]}
    text = json.dumps(body, ensure_ascii=False)
    return LLMResult(text=text, provider="mock", model="rule-template", mocked=True)


def _mock_raw_completion(messages: List[Dict[str, Any]],
                         tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """mock 下的工具循环替身：**确定性地**调前 2 个工具后收尾。

    目的不是"假装有智能"，而是让 `run_with_tools()` 在没有 key / 断网时
    也能被端到端验证 —— 循环终止条件、消息回灌顺序、`tool_call_id` 配对、
    Trace 结构，这些是**链路正确性**，与模型强弱无关，必须能离线测。

    真正"无 LLM"的教案内容仍由 `generate.py` 的模板分支负责，这里只管链路。
    """
    done = sum(1 for m in messages if m.get("role") == "tool")
    limit = min(len(tools or []), 2)
    if tools and done < limit:
        fn = (tools[done] or {}).get("function") or {}
        props = (fn.get("parameters") or {}).get("properties") or {}
        args = {k: _mock_arg(v) for k, v in props.items()}
        msg: Dict[str, Any] = {
            "role": "assistant",
            "content": f"（mock）先调用 {fn.get('name', '')} 获取信息。",
            "tool_calls": [{
                "id": f"mock-call-{done + 1}",
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }],
        }
    else:
        msg = {
            "role": "assistant",
            "content": f"（mock）已完成 {done} 次工具调用，给出最终答复。",
        }
    return {
        "message": msg,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "model": "rule-template",
        "elapsed_s": 0.0,
        "attempts": 1,
    }


def _mock_arg(schema: Any) -> Any:
    """按 JSON Schema 造一个占位参数值（让 mock 的工具调用参数能过校验）。"""
    t = (schema or {}).get("type") if isinstance(schema, dict) else None
    return {
        "string": "示例", "integer": 1, "number": 1.0,
        "boolean": True, "array": [], "object": {},
    }.get(t, "示例")


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------


def detect_providers() -> List[tuple]:
    """列出所有 provider 的可用性：(名字, 模型, key掩码, 可用)。"""
    out = []
    for name, cfg in PROVIDERS.items():
        if not cfg.needs_key:
            out.append((name, cfg.model, "(本地，无需 key)", False))
            continue
        key = os.environ.get(cfg.key_env, "").strip()
        out.append((name, cfg.model, mask_key(key), bool(key)))
    return out


def make_client(prefer: Optional[List[str]] = None, verbose: bool = False) -> LLMClient:
    """按优先级挑一个**有 key 的** provider；全都没有就返回 mock。

    这样调用方不用关心环境变量到底配了哪个。
    设 LPA_PROVIDER=xxx 可强制指定（跳过探测顺序）。
    """
    order = prefer or ["deepseek", "siliconflow", "zhipu", "modelscope", "ollama"]
    forced = os.environ.get("LPA_PROVIDER", "").strip()
    if forced:
        # ⚠ `mock` 是**特殊值**（不是 PROVIDERS 的键），必须在这里单独短路返回。
        #   早期写法只把它放进 order，循环里 `PROVIDERS.get("mock")` 得到 None 后
        #   被 `continue` 跳过，于是照旧落到有 key 的 provider —— 实测
        #   `LPA_PROVIDER=mock` 完全无效（想离线跑却真调了 API）。
        if forced == "mock":
            if verbose:
                print("[llm] LPA_PROVIDER=mock：强制走规则模板（不调 API）")
            return LLMClient(provider="mock", verbose=verbose)
        if forced not in PROVIDERS:
            print(f"[llm] LPA_PROVIDER={forced} 不在 {list(PROVIDERS)}，忽略")
        else:
            order = [forced] + [n for n in order if n != forced]
    for name in order:
        cfg = PROVIDERS.get(name)
        if cfg is None:
            continue
        if not cfg.needs_key:
            # ollama 只要进程在就算可用，但探测成本高，放最后且不主动探
            continue
        if os.environ.get(cfg.key_env, "").strip():
            if verbose:
                print(f"[llm] 使用 {name}（{cfg.model}）")
            return LLMClient(provider=name, verbose=verbose)
    if verbose:
        print("[llm] 未找到任何 API key，使用 mock")
    return LLMClient(provider="mock", verbose=verbose)

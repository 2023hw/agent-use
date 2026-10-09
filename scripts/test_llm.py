# -*- coding: utf-8 -*-
"""模型层自测：不联网，只验退避/JSON 容错/provider 选择/统计。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lpa.llm import (  # noqa: E402
    PROVIDERS,
    LLMClient,
    LLMError,
    make_client,
    parse_json_loose,
)

fails = []


def ck(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  <- {detail}"))
    if not cond:
        fails.append(name)


# 1. provider 全部登记
# ⚠ 原来写死 `len(PROVIDERS) == 4` —— 2026-10-08 加了 ollama 后这条就恒 FAIL。
#   改成**断言名单**而不是断言数量：以后再加 provider 只需补名字，
#   不会又变成一条"假红灯"（假失败会让人渐渐不信任整套自测）。
_EXPECT_PROVIDERS = {"deepseek", "siliconflow", "zhipu", "zhipu45", "modelscope", "ollama"}
_missing = _EXPECT_PROVIDERS - set(PROVIDERS)
ck("provider 名单齐全", not _missing, f"缺少 {sorted(_missing)}；实际 {sorted(PROVIDERS)}")
ck("ollama 不需要 key", PROVIDERS["ollama"].needs_key is False)

# 2. JSON 容错：五种脏输入
cases = [
    ('{"a": 1}', {"a": 1}, "干净 JSON"),
    ('```json\n{"a": 1}\n```', {"a": 1}, "markdown 围栏"),
    ('好的，结果如下：\n{"a": 1}\n希望对你有帮助', {"a": 1}, "前后有文字"),
    ('{"a": 1,}', {"a": 1}, "尾随逗号"),
    ('```\n{"a": [1,2],}\n```', {"a": [1, 2]}, "围栏+尾随逗号"),
]
for raw, expect, label in cases:
    try:
        got = parse_json_loose(raw)
        ck(f"JSON 容错: {label}", got == expect, f"得到 {got}")
    except Exception as e:
        ck(f"JSON 容错: {label}", False, str(e))

# 3. 非法输入应抛错
try:
    parse_json_loose("完全不是 JSON")
    ck("非 JSON 抛错", False, "没抛")
except LLMError:
    ck("非 JSON 抛错", True)

# 4. mock 客户端能跑
c = LLMClient(provider="mock")
r = c.complete("sys", "usr", json_mode=True)
ck("mock 返回", r.mocked and r.provider == "mock")
ck("mock 输出可解析为 JSON", isinstance(parse_json_loose(r.text), dict))
ck("mock 计入 calls", len(c.calls) == 1)

# 5. 未知 provider 抛错
try:
    LLMClient(provider="不存在的")
    ck("未知 provider 抛错", False, "没抛")
except LLMError:
    ck("未知 provider 抛错", True)

# 6. 缺 key 自动降级
for name in ("deepseek", "siliconflow", "zhipu"):
    env = PROVIDERS[name].key_env
    os.environ.pop(env, None)
c2 = LLMClient(provider="deepseek")
ck("缺 key 降级为 mock", c2.provider == "mock", c2.provider)

# 7. make_client 无 key 时返回 mock
c3 = make_client()
ck("make_client 无 key → mock", c3.provider == "mock", c3.provider)

# 8. 有 key 时正确选中
os.environ["SILICONFLOW_API_KEY"] = "fake-key-for-test"
c4 = make_client(prefer=["deepseek", "siliconflow"])
ck("有 key 时选中 siliconflow", c4.provider == "siliconflow", c4.provider)
ck("model 自动填充", c4.model == PROVIDERS["siliconflow"].model, str(c4.model))
os.environ.pop("SILICONFLOW_API_KEY", None)

# 9. 统计累加
c5 = LLMClient(provider="mock")
for _ in range(3):
    c5.complete("a", "b")
ck("usage 累计 calls=3", c5.total_usage["calls"] == 3, str(c5.total_usage))


# ==========================================================================
# 10. 工具调用（Function Calling）—— 全部离线，不联网
#     这几条是 llm.py 改动的安全网：Agent 循环的每一条硬规矩都得钉住。
# ==========================================================================

from lpa.llm import (  # noqa: E402
    AgentTranscript, ToolCall, _extract_tool_calls, tool_schema,
)

# 10.1 tool_schema 形状
_s = tool_schema("f", "描述", {"a": "string", "b": {"type": "integer"}}, required=["a"])
ck("tool_schema 形状", _s["function"]["name"] == "f"
   and _s["function"]["parameters"]["properties"]["a"] == {"type": "string"}
   and _s["function"]["parameters"]["properties"]["b"]["type"] == "integer"
   and _s["function"]["parameters"]["required"] == ["a"], json.dumps(_s))

# 10.2 tool_calls 解析：OpenAI 标准（arguments 是 JSON 字符串）
_calls = _extract_tool_calls({"tool_calls": [
    {"id": "c1", "function": {"name": "g", "arguments": '{"x": 1}'}}]})
ck("解析 arguments 为 JSON 字符串", len(_calls) == 1 and _calls[0].arguments == {"x": 1}
   and not _calls[0].parse_error, str(_calls))

# 10.3 有的国产模型直接把 arguments 给成 dict —— 也得认
_calls = _extract_tool_calls({"tool_calls": [
    {"id": "c2", "function": {"name": "g", "arguments": {"x": 2}}}]})
ck("解析 arguments 为 dict", _calls[0].arguments == {"x": 2} and not _calls[0].parse_error)

# 10.4 坏参数**不抛异常**，只打标记（抛出去整个循环就死了）
_calls = _extract_tool_calls({"tool_calls": [
    {"id": "c3", "function": {"name": "g", "arguments": "{坏掉的"}}]})
ck("坏 JSON 只标记不抛", _calls[0].parse_error != "" and _calls[0].arguments == {})

# 10.5 无 tool_calls 时返回空表
ck("无 tool_calls → 空表", _extract_tool_calls({"content": "hi"}) == [])

# 10.6 回灌格式：tool_calls 必须能原样还原（少 tool_call_id 下一轮就 400）
_part = ToolCall(id="c9", name="g", arguments={"x": 3}).to_message_part()
ck("to_message_part 配对字段齐全",
   _part["id"] == "c9" and _part["function"]["name"] == "g"
   and json.loads(_part["function"]["arguments"]) == {"x": 3}, str(_part))

# 10.7 Agent 主循环（mock）：收敛 / 配对 / 步数
_TOOLS = [tool_schema("no_arg", "无参工具"), tool_schema("one_arg", "一参工具", {"k": "string"})]
_ran = []
cm = LLMClient(provider="mock")
_t = cm.run_with_tools("sys", "usr", _TOOLS, lambda call: _ran.append(call.name) or "ok",
                       max_steps=8)
ck("循环收敛到 finish", _t.stopped_reason == "finish", _t.stopped_reason)
ck("确实调了工具", _t.tool_call_count == 2, str(_t.tool_call_count))
ck("调用数 == Observation 数（tool_call_id 配对不缺）",
   sum(len(s.observations) for s in _t.steps) == _t.tool_call_count)
ck("runner 真被执行", _ran == ["no_arg", "one_arg"], str(_ran))
ck("收敛步数 = 工具轮 + 1 步收尾", len(_t.steps) == 3, str(len(_t.steps)))
ck("used/unused_tools 正确", _t.used_tools == ["no_arg", "one_arg"] and not _t.unused_tools,
   f"{_t.used_tools} / {_t.unused_tools}")

# 10.8 工具抛异常 → 转 Observation，不中断循环
def _boom(call):
    raise KeyError("编号不存在")
_t2 = LLMClient(provider="mock").run_with_tools("sys", "usr", _TOOLS, _boom, max_steps=8)
_obs = [o for s in _t2.steps for o in s.observations]
ck("工具异常被捕获并回灌", any(o.get("error") for o in _obs) and _t2.stopped_reason == "finish",
   str(_t2.stopped_reason))
ck("异常文本进了 Observation", any("KeyError" in (o.get("content") or "") for o in _obs))

# 10.9 max_steps 是硬闸（防死循环）
_SPIN = [tool_schema("spin", "永远被调用的工具")]
class _Spinner(LLMClient):
    """永远只回 tool_calls、从不收尾 —— 用来验证 max_steps 真的会拦。"""
    def complete_raw(self, messages, tools=None, tool_choice=None, json_mode=False):
        return {"message": {"role": "assistant", "content": "",
                            "tool_calls": [{"id": "s1", "type": "function",
                                            "function": {"name": "spin", "arguments": "{}"}}]},
                "usage": {}, "model": "spin", "elapsed_s": 0.0, "attempts": 1}
_t3 = _Spinner(provider="mock").run_with_tools("s", "u", _SPIN, lambda c: "ok", max_steps=4)
ck("max_steps 拦住死循环", _t3.stopped_reason == "max_steps" and len(_t3.steps) == 4,
   f"{_t3.stopped_reason} / {len(_t3.steps)}")

# 10.10 Trace 渲染格式（报告要直接引用）
_txt = _t.render()
ck("Trace 含 Action/Observation", "[Action]" in _txt and "[Observation]" in _txt
   and "[Final]" in _txt, _txt[:120])
ck("to_dict 可序列化", isinstance(json.dumps(_t.to_dict(), ensure_ascii=False), str))

# 10.11 还原 make_client 的探测环境（别影响后续手工跑）
for _n in ("deepseek", "siliconflow", "zhipu", "modelscope"):
    os.environ.pop(PROVIDERS[_n].key_env, None)

print()
print(f"结果：{'全部通过' if not fails else '失败 ' + str(len(fails)) + ' 项: ' + str(fails)}")
sys.exit(1 if fails else 0)

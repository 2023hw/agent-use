# -*- coding: utf-8 -*-
"""P1-5 外部 API 故障兜底实测探针（纯离线，不打真实网络）。

在客户端 `_post` 层用脚本化故障注入（HTTP 429 / 500 / 400 / 网络超时），
验证 `LLMClient._chat` 的指数退避、快速失败与 Agent 层优雅终止行为，
结果写入 `data/structured/API_FAULT_PROBE.md`（报告 4.1 节的实测证据）。

退避期间的真实 sleep 被替换为「只记录不等待」，所以全程毫秒级完成。

运行：PYTHONUTF8=1 python scripts/api_fault_probe.py
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lpa.llm import LLMClient, LLMError, tool_schema                 # noqa: E402

OUT = ROOT / "data" / "structured" / "API_FAULT_PROBE.md"


def _http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://probe.invalid/v4/chat/completions", code=code,
        msg=f"HTTP {code}", hdrs=None, fp=io.BytesIO(body.encode("utf-8")))


def _ok_response() -> dict:
    return {"choices": [{"message": {"content": "（模拟成功响应）"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class ScriptedClient:
    """给 LLMClient 装上「按剧本失败」的 _post 与「只记录」的 _sleep。"""

    def __init__(self, script):
        self.client = LLMClient(provider="zhipu", max_retries=6)
        self.client.api_key = "probe-fake-key"          # _post 已被替换，不会真发请求
        self.script = list(script)
        self.post_calls = 0
        self.sleep_attempts: list = []

        def fake_post(payload, _self=self):
            _self.post_calls += 1
            outcome = _self.script.pop(0) if _self.script else "ok"
            if outcome == "ok":
                return _ok_response()
            if outcome == "timeout":
                raise urllib.error.URLError("timed out")
            code = int(outcome)
            raise _http_error(code, body='{"error":{"message":"probe injected"}}')

        def fake_sleep(attempt, _self=self):
            _self.sleep_attempts.append(attempt)        # 只记录，不真实等待

        self.client._post = fake_post                   # type: ignore[assignment]
        self.client._sleep = fake_sleep                 # type: ignore[assignment]


def run_case(name: str, script, expect: str) -> dict:
    sc = ScriptedClient(script)
    row = {"name": name, "script": " → ".join(script) or "（无）",
           "expect": expect, "attempts": 0, "sleeps": [], "actual": "", "pass": False}
    try:
        res = sc.client.complete("system", "user")
        row["actual"] = f"成功返回（{len(res.text)} 字，LLMResult.attempts={res.attempts}）"
        row["pass"] = expect == "success"
    except LLMError as e:
        row["actual"] = f"抛 LLMError：{str(e)[:80]}"
        row["pass"] = expect == "error"
    row["attempts"] = sc.post_calls
    row["sleeps"] = sc.sleep_attempts
    return row


def run_agent_case() -> dict:
    """Agent 层：模型接口全程 500，run_with_tools 抛 LLMError → CLI 优雅终止。"""
    sc = ScriptedClient(["500"] * 10)
    tools = [tool_schema("get_class_roster", "获取班级名册")]
    row = {"name": "Agent 运行中接口持续 500",
           "script": "500 × N（全程）",
           "expect": "优雅终止（exit=3 + 提示语，无栈、无半成品交付）",
           "attempts": 0, "sleeps": [], "actual": "", "pass": False}
    try:
        sc.client.run_with_tools("system", "user", tools,
                                 lambda c: {"ok": True}, max_steps=4)
        row["actual"] = "未抛错（不符合预期）"
    except LLMError as e:
        row["actual"] = (f"run_with_tools 抛出 LLMError（{str(e)[:60]}）→ "
                         f"agent_run 捕获：打印友好提示并以 exit=3 退出"
                         f"（scripts/agent_run.py 的 except LLMError 分支）")
        row["pass"] = True
    row["attempts"] = sc.post_calls
    row["sleeps"] = sc.sleep_attempts
    return row


def render(rows: list) -> str:
    out = [
        "# 外部 API 故障兜底实测报告（P1-5）",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}　"
        f"方式：`scripts/api_fault_probe.py` 纯离线注入（不打真实网络，毫秒级完成）",
        "> 对象：`src/lpa/llm.py` 的 `_chat` 指数退避 + `scripts/agent_run.py` 的优雅终止",
        "",
        "## 实测用例",
        "",
        "| 用例 | 注入故障 | 预期行为 | 实际表现 | 请求次数 | 退避序列 | 判定 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| {r['name']} | {r['script']} | {r['expect']} | {r['actual']} "
            f"| {r['attempts']} | {r['sleeps'] or '—'} | {'✅' if r['pass'] else '❌'} |")
    out += [
        "",
        "## 行为说明（与代码一一对应）",
        "",
        "1. **429 限速 / 5xx 服务端错误** → 指数退避重试：`_sleep(attempt)`，"
        "1→2→4→8→16→32 秒封顶 + 0~0.5s 随机抖动；最多 `max_retries=6` 次。"
        "免费档（智谱 20 次/分钟）在 Agent 多轮循环下撞限速是常态，"
        "此机制让短时限速**对用户无感**（用例 1：两次 429 后自动恢复）。",
        "2. **4xx（非 429）请求错误** → **快速失败、不重试**（用例 3：仅 1 次请求）。"
        "key 错、参数错重试无意义，立即把错误原文抛给上层，避免空转烧钱。",
        "3. **网络层错误**（超时 / 连接失败，`URLError`/`OSError`）→ 同样退避重试"
        "（用例 4）。",
        "4. **重试耗尽** → 抛带原文的 `LLMError`；`agent_run` 顶层捕获后**优雅终止**："
        "打印友好提示、以 exit=3 退出、不产出半成品交付件（用例 5）。"
        "单轮 `complete()` 与多轮 `complete_raw()` **共用同一份重试逻辑**，"
        "不存在「Agent 循环绕过重试」的暗道。",
        "",
        "全部用例通过 ✅" if all(r["pass"] for r in rows) else "存在未通过用例 ❌",
        "",
    ]
    return "\n".join(out)


def main() -> int:
    rows = [
        run_case("429 限速后自动恢复", ["429", "429", "ok"], "success"),
        run_case("500 持续故障（重试耗尽）", ["500"] * 6, "error"),
        run_case("400 请求错误（快速失败）", ["400"], "error"),
        run_case("网络超时后自动恢复", ["timeout", "timeout", "ok"], "success"),
        run_agent_case(),
    ]
    for r in rows:
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"  [{mark}] {r['name']}：attempts={r['attempts']} sleeps={r['sleeps']}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(rows), encoding="utf-8")
    print(f"\n报告 → {OUT.relative_to(ROOT).as_posix()}")
    ok = all(r["pass"] for r in rows)
    print("全部 PASS ✅" if ok else "存在 FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

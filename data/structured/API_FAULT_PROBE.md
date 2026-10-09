# 外部 API 故障兜底实测报告（P1-5）

> 生成时间：2026-10-09 11:44　方式：`scripts/api_fault_probe.py` 纯离线注入（不打真实网络，毫秒级完成）
> 对象：`src/lpa/llm.py` 的 `_chat` 指数退避 + `scripts/agent_run.py` 的优雅终止

## 实测用例

| 用例 | 注入故障 | 预期行为 | 实际表现 | 请求次数 | 退避序列 | 判定 |
|---|---|---|---|---|---|---|
| 429 限速后自动恢复 | 429 → 429 → ok | success | 成功返回（8 字，LLMResult.attempts=3） | 3 | [1, 2] | ✅ |
| 500 持续故障（重试耗尽） | 500 → 500 → 500 → 500 → 500 → 500 | error | 抛 LLMError：重试 6 次仍失败: HTTP 500: {"error":{"message":"probe injected"}} | 6 | [1, 2, 3, 4, 5, 6] | ✅ |
| 400 请求错误（快速失败） | 400 | error | 抛 LLMError：HTTP 400: {"error":{"message":"probe injected"}} | 1 | — | ✅ |
| 网络超时后自动恢复 | timeout → timeout → ok | success | 成功返回（8 字，LLMResult.attempts=3） | 3 | [1, 2] | ✅ |
| Agent 运行中接口持续 500 | 500 × N（全程） | 优雅终止（exit=3 + 提示语，无栈、无半成品交付） | run_with_tools 抛出 LLMError（重试 6 次仍失败: HTTP 500: {"error":{"message":"probe injected"}}）→ agent_run 捕获：打印友好提示并以 exit=3 退出（scripts/agent_run.py 的 except LLMError 分支） | 6 | [1, 2, 3, 4, 5, 6] | ✅ |

## 行为说明（与代码一一对应）

1. **429 限速 / 5xx 服务端错误** → 指数退避重试：`_sleep(attempt)`，1→2→4→8→16→32 秒封顶 + 0~0.5s 随机抖动；最多 `max_retries=6` 次。免费档（智谱 20 次/分钟）在 Agent 多轮循环下撞限速是常态，此机制让短时限速**对用户无感**（用例 1：两次 429 后自动恢复）。
2. **4xx（非 429）请求错误** → **快速失败、不重试**（用例 3：仅 1 次请求）。key 错、参数错重试无意义，立即把错误原文抛给上层，避免空转烧钱。
3. **网络层错误**（超时 / 连接失败，`URLError`/`OSError`）→ 同样退避重试（用例 4）。
4. **重试耗尽** → 抛带原文的 `LLMError`；`agent_run` 顶层捕获后**优雅终止**：打印友好提示、以 exit=3 退出、不产出半成品交付件（用例 5）。单轮 `complete()` 与多轮 `complete_raw()` **共用同一份重试逻辑**，不存在「Agent 循环绕过重试」的暗道。

全部用例通过 ✅

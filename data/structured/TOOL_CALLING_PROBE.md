# 工具调用能力探针（Function Calling Probe）

> 本报告由 `scripts/probe_tool_calling.py` 自动生成，数字全部来自实跑。

## 一、这份报告回答什么

两个问题，别混在一起读：

1. **链路能不能跑通** —— Agent 循环的终止条件、`tool_call_id` 配对、参数解析、异常回灌、Trace 结构。这一层**与模型强弱无关**，所以用 mock 离线就能验完（见 §二）。

2. **这个模型撑不撑得住多轮工具调用** —— 这是底座模型选型的实测依据（见 §三、§四）。轻量免费档的 function calling 常是短板，**必须实测，不能想当然**。

## 二、链路自检（mock，离线，确定性）

✅ **链路自检通过。** mock 是确定性替身（按工具顺序瞎调、参数填占位值），**它答不对任务是应该的** —— 这里考的只是链路本身：

| 链路判据 | 实测 |
|---|---|
| 循环正常收敛（`finish`，非撞 `max_steps`） | ✅ （收尾原因 `finish`） |
| `arguments` 解析无错 | ✅ |
| 每次调用都有配对 Observation（`tool_call_id` 不缺） | ✅ （调用 2 次 / Observation 2 条） |
| 循环步数 = 工具轮 + 1 步收尾 | ✅ （共 3 步） |

**顺带验到一条更重要的：工具抛异常不会炸掉循环。** mock 给 `get_child_profile` 传了个不存在的编号（`示例`），工具直接抛了 `KeyError`；这条异常被 `_run_one_tool()` 捕获、转成 Observation 文本回灌给「模型」，循环**继续走完并正常收尾**。这正是 Agent 该有的行为 —— 工具报错是**信息**，不是崩溃。

mock 走的是与真实模型**完全相同的** `run_with_tools()` 代码路径 —— 循环、消息回灌、`tool_call_id` 配对、Trace 组装都是同一份实现，只有「模型那一侧」被替换成确定性的替身。所以 mock 通过 = 链路无缺陷；模型能力另算。

## 三、任务设计：为什么必须调 3 次

- **挂载工具**：list_children, get_child_profile, get_class_summary

- **任务**：先报出本班档案人数，再分别查出 S01 与 S37 的支持层级。

- **刻意的依赖链**：不调 `list_children` 就不知道有 S37 这个编号；不调 `get_child_profile` 两次就拿不到两个人的层级。**顺序错了、漏调了、硬编答案，都会被判据抓到。**

- **反幻觉设计**：`SYSTEM` 里明令「必须调工具、严禁编造」；`get_child_profile` 对不存在的编号**抛异常而非返回空值**，异常会作为 Observation 回灌 —— 测模型能否读错误信息自我纠正。

## 四、实跑结果

| | |
|---|---|
| provider | `zhipu` |
| model | `glm-4-flash` |
| 重复次数 | 3 |
| 实际步数（最多一次） | 4 |

| 判据 | 通过 | 含义 |
|---|---|---|
| 工具序列正确 | **3/3** | 是否恰好按 `list_children → get_child_profile → get_child_profile` 调用 |
| 参数正确 | **3/3** | 两次查询的 `anon_id` 是否为 S01 与 S37 |
| 最终答案正确 | **3/3** | 结论里是否同时出现人数与两个层级原文 |
| 正常收尾 | **3/3** | `stopped_reason=finish`（不是撞 max_steps） |
| 参数无解析错误 | **3/3** | 模型给的 arguments 是否都是合法 JSON |
| 无工具执行错误 | **3/3** | 是否传了不存在的编号/参数 |

### 逐次明细

| 次 | 调用序列 | anon_id | 步数 | 调用数 | 收尾 | 耗时(s) | tokens |
|---|---|---|---|---|---|---|---|
| 1 | list_children → get_child_profile → get_child_profile | S01, S37 | 4 | 3 | ✅ finish | 5.55 | 2483 |
| 2 | list_children → get_child_profile → get_child_profile | S01, S37 | 4 | 3 | ✅ finish | 10.03 | 2477 |
| 3 | list_children → get_child_profile → get_child_profile | S01, S37 | 4 | 3 | ✅ finish | 4.74 | 2479 |

## 五、完整调用轨迹（Thought → Action → Observation）

> 下面这段就是报告「运行效果与链路验证」一节需要的原始 Trace —— **不是事后补写的，是循环运行时逐步骤记下来的。**

### 第 1 次运行（`finish`，3 次工具调用，5.6s）

```text
[Action] list_children({})
[Observation] {"count": 37, "ids": ["S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08", "S09", "S10", "S11", "S12", "S13", "S14", "S15", "S16", "S17", "S18", "S19", "S20", "S21", "S22", "S23", "S24", "S25", "S26", "S27", "S28", "S29", "S30", "S31", "S32", "S33", "S34", "S35", "S36", "S37"]}
[Action] get_child_profile({"anon_id": "S01"})
[Observation] {"anon_id": "S01", "mastery": 0.352, "support_level": "大量身体辅助", "attendance": "在课", "lessons_observed": 4}
[Action] get_child_profile({"anon_id": "S37"})
[Observation] {"anon_id": "S37", "mastery": 0.0, "support_level": "未参与", "attendance": "缺席", "lessons_observed": 1}
[Thought] 1. 本班一共收录了37名幼儿的档案。
2. 幼儿 S01 的支持层级是大量身体辅助，幼儿 S37 的支持层级是未参与。
[Final] 1. 本班一共收录了37名幼儿的档案。
2. 幼儿 S01 的支持层级是大量身体辅助，幼儿 S37 的支持层级是未参与。
```

## 六、结论与下一步

**3/3 次全部按预期完成多轮工具调用，序列与参数均正确。** 说明 `glm-4-flash` 具备可用的 function calling 能力，`run_with_tools()` 的多轮闭环在本模型上成立。

- **P0-2 待办**：把本探针的 3 个桩工具替换成完整 9 工具集（`search_corpus` / `get_previous_baseline` / `plan_layering` / `validate_lesson_plan` / `render_docx` / `audit_docx` …）。Schema 写法与 runner 签名已对齐，不返工。

- **P0-4 待办**：`AgentTranscript.render()` 已经是 `[Thought]` / `[Action]` / `[Observation]` 格式，直接接文件落盘即可，无需另写日志格式。

> ⚠ 本次样本量小（n=3），只作**定性判断**与选型参考，不构成统计结论。

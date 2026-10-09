# 融教通 · 融合教育幼儿园康复教案生成 Agent

一句自然语言需求（「生成认知课《国庆节到了》的教案」）→ Agent 自主查证班级数据与历史基线 → 分层规划 → 生成目标与内容 → 自检修复 → 渲染成与园所模板一致的 docx 并通过交付终检，全程留可审计的 Thought-Action-Observation Trace。

- **真 Agent**：LLM 自主决定 10 个工具的调用顺序（Function Calling 多轮循环），非写死的流水线；校验失败时读失败明细自主修复复检。
- **业务硬约束**：零真实姓名 / 零诊断 / 零障碍标签（数据层源头脱敏 + 三层护栏）；跨课康复基线（P/E 记号）连续传递。
- **可核验**：每次运行落盘 Trace（jsonl + 人可读 md）；自动合规指标、留出对照、消融、LLM-as-judge 一整套评测证据链。

## 快速开始

```bash
git clone https://github.com/2023hw/agent-use.git
cd agent-use
pip install -r requirements.txt          # Python 3.12+
cp .env.example .env                      # 填入 ZHIPU_API_KEY（智谱免费档）
python scripts/lesson_docx.py doctor      # 环境自检
```

跑一个完整 Agent 任务：

```bash
python scripts/lesson_docx.py auto                        # 标准成功用例（约 2 分钟）
python scripts/lesson_docx.py auto --fault bad_objective  # 校验失败 → 自主修复闭环
python scripts/lesson_docx.py auto --scenario no_theme    # 缺参数 → 主动追问
python scripts/lesson_docx.py auto --scenario inject      # 注入攻击 → 护栏拦截
LPA_PROVIDER=zhipu45 python scripts/lesson_docx.py auto   # 思考链模型（Trace 含推理文本）
```

产物：`data/_agent_runs/<run_id>/`（docx）+ `data/trace/<run_id>.{jsonl,md}`（Trace）+ `data/structured/AUTONOMOUS_RUN.md`（汇总报告）。

## 仓库结构

```
src/lpa/            主工程
  llm.py            模型层：多 provider 客户端 + Function Calling 主循环（run_with_tools）
  agent.py          自主编排：prompt / 运行 / 闭环判定（System Prompt 源文件在 prompts/）
  tools.py          10 个工具（只包装不复制，与生产流水线同一批能力）
  guard.py          三层护栏：输入正则短路 / 输出泄漏后验
  trace.py          Trace 实时落盘（jsonl）+ 人可读渲染
  rules.py          校验引擎（零姓名/零标签/可观测动词/支架密度/时长自洽…）
  generate.py       生成链（目标分层 LLM / 内容扩写 / 自检重写）
  render_docx.py    docx 渲染（与正式交付同一渲染器）
prompts/            System Prompt 权威源文件
scripts/            CLI 入口与探针/自测（lesson_docx.py 是总入口）
docs/               架构图、差距分析、项目计划、报告草稿
data/structured/    全部运行与测评报告（自动重排，勿手改）
data/trace/         真跑 Trace（jsonl + md）
```

## 常用命令

| 命令 | 作用 |
|---|---|
| `lesson_docx.py doctor` | 环境自检（依赖 / key / 语料 / 模板） |
| `lesson_docx.py build -t "认知课《国庆节到了》"` | 生产流水线：渲染 + 终检正式交付件 |
| `lesson_docx.py auto [--fault/--scenario]` | **Agent 自主编排运行** |
| `lesson_docx.py tools [--mock]` | 模型工具调用能力探针 |
| `lesson_docx.py eval / holdout / ablation / judge` | 自动化评测链（M4-1/2/4/3） |
| `python scripts/test_llm.py` | 模型层离线自测（34 项，改 llm.py 后必跑） |
| `python scripts/agent_orch_selftest.py` | 编排/护栏离线自测（51 项） |
| `python scripts/api_fault_probe.py` | API 故障兜底实测（429/500/400/超时） |
| `python scripts/cost_summary.py` | 时延与 token 成本汇总 |

## 文档导航

- 架构拓扑 / Prompt 拆解 / 记忆机制 → `docs/ARCHITECTURE.md`
- 课程要求对照与整改进度 → `docs/SUBMISSION_GAP_ANALYSIS.md`
- 项目报告（按作业模板） → `docs/REPORT_DRAFT.md`
- 真跑汇总（7 次运行） → `data/structured/AUTONOMOUS_RUN.md`

## 隐私与合规

- 系统内只有匿名编号 S01–S37；真实姓名映射表（`data/private/`）不入库、不参与生成。
- `.env` 已被 `.gitignore` 排除；推送公开仓库前请确认无真实数据残留。

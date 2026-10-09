#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""儿童康复教案 docx 流水线 —— 统一 CLI 入口。

把原先散落在多个脚本、靠「手改常量 + 设环境变量」驱动的流程，收成一条命令。

用法
----
    python scripts/lesson_docx.py doctor
        环境与数据自检：依赖、语料目录、关键数据文件、可用主题、LLM key 状态。

    python scripts/lesson_docx.py key --provider deepseek --key sk-xxxxx
        把 API key 写进项目根 .env（不进 git）。同一 provider 重复写会覆盖。

    python scripts/lesson_docx.py ping
        发一条最小请求验证 key 是否真的能通，打印实际模型名与 token 消耗。
        ⚠ 这一步才会真正花钱（几分钱以内）。

    python scripts/lesson_docx.py list
        列出 data/generated/ 里可渲染的主题。

    python scripts/lesson_docx.py build  -t "认知课《国庆节到了》"
        渲染 + 终检一条龙（推荐入口）。产物落在 data/docx/。

    python scripts/lesson_docx.py render -t "..." [--single] [--all]
        只渲染，不终检。

    python scripts/lesson_docx.py audit  [--dir data/docx]
        只对已有产物跑终检（W4：判版式与合规，判不了好坏）。

    python scripts/lesson_docx.py eval
        M4-1 质量侧自动指标：去标识化前后对比、名册覆盖、分层结构、
        W2 生成层硬校验。产出 data/structured/EVAL_METRICS.md / .json。
        ⚠ 与 audit 不是一回事：audit = 能不能交付，eval = 质量证据。

    python scripts/lesson_docx.py holdout
        M4-2 留出对照：拿 3 个「没见过的主题」当实验组、已交付主题当对照组，
        比 去标识化 / 结构 / 分层链路 / 内容区分度 四个维度，回答
        「系统的能力是不是只对某个具体主题有效」。
        产出 data/structured/HOLDOUT_COMPARISON.md / .json。
        ⚠ eval 算的是**绝对**证据，holdout 算的是**相对**证据（泛化能力）。

    python scripts/lesson_docx.py ablation [--only 臂] [--reuse]
        M4-4 消融实验：从完整系统出发，**每次只关一个模块**，重跑同一批主题，
        量出每个模块的边际贡献（关掉谁掉多少分）。
        臂：full / no_obj / no_content / no_scaffold / no_repair。
        产出 data/structured/ABLATION_STUDY.md / .json。
        ⚠ 这一步**真调 LLM**、逐臂完整重跑，耗时较长（数分钟到十几分钟）；
          每臂产物隔离在 data/_ablation/<臂>/，不动正式产物。

    python scripts/lesson_docx.py blindeval [--seed 整数] [--tally]
        M4-5 教师盲评：把「系统产出」与语料里的**人工范例**混装成同一版式、
        去掉来源指纹（班级 / 人数 / 日期 / 姓名 / 标签）的匿名卡片，教师按统一量表打分。
        ⚠ 这是**人在环**的独立环节，脚本只造材料，不替教师下结论。
        产出 data/blind-eval/（README / 教师须知 / 量表 / 教案卡片 / 回收表 / 封存密钥）。
        --tally  评完开封后，读回收表 + 密钥，按来源分组统计。

    python scripts/lesson_docx.py blindform [--teachers 名字] [--ingest]
        M4-5 的**收发端**（备料之后、统计之前）：
        默认把匿名卡片包成**可直接在电脑上填**的 Word 评分卡 —— 打分格是下拉选择，
        每份教案正文就排在它那张评分表上方；产出到 data/blind-eval/5_教师评分卡/
        并建好 回收/ 文件夹。教师填完丢进 回收/，
        --ingest  自动读回 → 写 3_回收统计表.csv → 直接出分组统计（零人工转录）。

    python scripts/lesson_docx.py judge [--runs 次数] [--no-pairs]
                                       [--judge-provider 名] [--judge-model 名]
        M4-3 LLM-as-judge：让**模型**当评委，给教案打分。
        刻意与 M4-5 对齐 —— 同一批匿名卡片、**同一份 D1–D5 量表**（代码里直接 import
        M4-5 的 RUBRIC）、同一组判据，只是把评委从教师换成模型，两边结果可直接对照。
        量法：点式评分 ×N（量评委稳定性）+ 位置对调配对（系统 × 人工，抵消位置偏好）。
        ⚠ **必须真实调 API**（mock 模板不构成证据）；无 key 直接退出。
        ⚠ **同族自偏好偏差**：教案本就是这套模型生成的，让它自评会偏高 ——
          分数视为上限；换一家模型复评（--judge-provider）结论才更硬。
        产出 data/structured/JUDGE_STUDY.md / .json。

    python scripts/lesson_docx.py tools [--runs 次数] [--mock] [--provider 名]
        **P0-1 工具调用能力探针** —— Agent 化的地基验收，**不是教案流水线**。
        分两层测，别混在一起读：
          ① 离线（--mock）验**链路**：循环收敛、tool_call_id 配对、异常回灌，
             与模型强弱无关，确定性替身就能验完；
          ② 真跑验**模型**：任务设计成必须按顺序调 3 次工具才答得出，
             判断当前模型（默认 glm-4-flash）撑不撑得住多轮 function calling。
        链路坏了换模型没用，模型弱了修链路也没用 —— 所以分开测。
        产出 data/structured/TOOL_CALLING_PROBE.md / .json。
        （Agent 主循环实现在 src/lpa/llm.py 的 run_with_tools()）

参数
----
    -t / --theme   主题名，可重复或逗号分隔（与生成的 LessonPlan.title 一致）
    --all          渲染全部主题（覆盖配置里的 deliver 清单）
    --single       按班拆分时强制合成单表（体积更小，班级信息行标「全班」）
    --no-audit     build 时跳过终检
    -q / --quiet   只输出结论行

退出码
------
    0 = 全部通过    1 = 有校验失败    2 = 用法 / 环境错误
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
STRUCT = ROOT / "data" / "structured"
GEN = ROOT / "data" / "generated"
DOCX = ROOT / "data" / "docx"
ENV_FILE = ROOT / ".env"

# 渲染 / 终检两个既有脚本由本 CLI 驱动，不改它们的对外行为。
RENDER_SCRIPT = SCRIPTS / "run_render.py"
AUDIT_SCRIPT = SCRIPTS / "final_audit.py"
EVAL_SCRIPT = SCRIPTS / "eval_metrics.py"      # M4-1 质量侧自动指标
HOLDOUT_SCRIPT = SCRIPTS / "holdout_compare.py"  # M4-2 留出对照
ABLATION_SCRIPT = SCRIPTS / "ablation_study.py"  # M4-4 消融实验
BLIND_EVAL_SCRIPT = SCRIPTS / "blind_eval_pack.py"  # M4-5 教师盲评材料包
JUDGE_SCRIPT = SCRIPTS / "judge_study.py"          # M4-3 LLM-as-judge
sys.path.insert(0, str(ROOT / "src"))          # 让 doctor / ping 能 import lpa

# doctor 要逐个确认存在的关键输入（缺任何一个都会让渲染结果不可信）
REQUIRED_DATA = {
    "data/structured/lessons.json": "语料解析结果（班级归属、幼儿记录来源）",
    "data/structured/child_profiles.json": "37 名匿名儿童能力画像",
    "data/structured/goal_ladder.json": "范例学出的目标阶梯",
    "data/structured/layer_tiers.json": "分层规律（层数由它决定）",
    "data/structured/class_profile.json": "班级概况（名称 / 年龄范围默认值）",
    "data/private/name_mapping.json": "姓名黑名单（合规校验用）",
    "data/structured/build_config.json": "流水线配置（可改，非必需）",
}


def _die(msg: str, code: int = 2) -> None:
    print(f"[错误] {msg}", file=sys.stderr)
    raise SystemExit(code)


def _child_env() -> dict:
    """给子进程的环境：保证 UTF-8 输出，避免 Windows 控制台代码页把中文打花。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _run(script: Path, extra_env: dict | None, quiet: bool,
         extra_args: list | None = None) -> int:
    """跑一个子脚本，原样透传输出，返回退出码。

    `extra_args` 透传命令行参数（如消融实验的 `--only` / `--reuse`）。
    """
    if not script.exists():
        _die(f"找不到脚本 {script}")
    env = _child_env()
    env.update(extra_env or {})
    proc = subprocess.run(
        [sys.executable, str(script)] + [str(x) for x in (extra_args or [])],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    out = proc.stdout or ""
    if quiet:
        for line in out.splitlines():
            if any(k in line for k in ("PASS", "FAIL", "总判定", "整体", "违规",
                                       "HAS VIOLATIONS", "ok", "[错误]")):
                print(line)
    else:
        print(out, end="" if out.endswith("\n") else "\n")
    return proc.returncode


def load_config() -> dict:
    cfg_path = STRUCT / "build_config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:                       # noqa: BLE001
        print(f"[警告] build_config.json 解析失败，按内置默认走：{exc}")
        return {}


def available_themes() -> list:
    """data/generated/*.json 里的 LessonPlan.title（这才是可渲染的真实清单）。"""
    out = []
    for jp in sorted(GEN.glob("*.json")):
        try:
            t = json.loads(jp.read_text(encoding="utf-8")).get("title")
        except Exception:                          # noqa: BLE001
            continue
        if t:
            out.append(t)
    return out


def split_theme_arg(raw: list | None) -> list:
    """`-t A -t B` 与 `-t "A,B"` 都支持。"""
    out: list = []
    for item in raw or []:
        out.extend([t.strip() for t in str(item).split(",") if t.strip()])
    return out


# --------------------------------------------------------------------------
# LLM 相关
# --------------------------------------------------------------------------
def _provider_names() -> list:
    from lpa.llm import PROVIDERS                  # noqa: PLC0415
    return list(PROVIDERS)


def cmd_key(args) -> int:
    """把 key 写进项目根 .env。不改代码，不打印完整 key。"""
    try:
        from lpa.llm import PROVIDERS              # noqa: PLC0415
    except Exception as exc:                       # noqa: BLE001
        _die(f"无法导入 lpa.llm：{exc}")
    provider = (args.provider or "").strip()
    if provider not in PROVIDERS:
        _die(f"未知 provider「{provider}」，可选：{'、'.join(PROVIDERS)}")
    key = (args.key or "").strip()
    if not key:
        _die("key 是空的")
    env_var = PROVIDERS[provider].key_env

    lines: list = []
    if ENV_FILE.is_file():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    found = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith(f"{env_var}="):
            lines[i] = f"{env_var}={key}"
            found = True
            break
    if not found:
        lines.append(f"{env_var}={key}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 立刻生效（同进程内后续命令也能看见）
    os.environ[env_var] = key
    print(f"[key] 已写入 {ENV_FILE}  →  {env_var}={key[:6]}…{key[-4:]}")
    print(f"[key] provider = {provider}，模型 = {PROVIDERS[provider].model}")
    print("[key] 下一步：python scripts/lesson_docx.py ping")
    return 0


def cmd_ping(args) -> int:
    """发一条最小请求，验证 key 真能用。"""
    try:
        from lpa.llm import LLMError, make_client  # noqa: PLC0415
    except Exception as exc:                       # noqa: BLE001
        _die(f"无法导入 lpa.llm：{exc}")

    forced = getattr(args, "provider", None)
    if forced:
        os.environ["LPA_PROVIDER"] = forced
    client = make_client(verbose=True)
    if client.provider == "mock":
        print("[ping] 没找到任何可用的 API key —— 当前会走 mock（不出教学内容）。")
        print("       配置方式：python scripts/lesson_docx.py key --provider <名字> --key <你的key>")
        print("       或直接编辑项目根 .env")
        return 1

    print(f"[ping] 正在呼叫 {client.provider} / {client.model} ……")
    try:
        r = client.complete(
            system="你是一个幼儿园教案助手。",
            user="只回复两个字：可用",
            json_mode=False,
        )
    except LLMError as exc:
        print(f"[ping] 失败：{exc}")
        return 1
    text = (r.text or "").strip().replace("\n", " ")
    print(f"[ping] 成功 ✅  实际模型 = {r.model}")
    print(f"[ping] 耗时 {r.elapsed_s:.2f}s，token = {r.usage['total']}"
          f"（提示 {r.usage['prompt']} / 生成 {r.usage['completion']}）")
    print(f"[ping] 回话：{text[:80]}")
    if getattr(args, "json", False):
        try:
            obj = client.complete_json(
                system="只输出 JSON。",
                user='输出 {"ok": true, "msg": "可用"}',
            )
            print(f"[ping] JSON 模式 ✅  {obj}")
        except LLMError as exc:
            print(f"[ping] JSON 模式失败 ❌ {exc}")
            return 1
    return 0


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def cmd_list(args) -> int:
    themes = available_themes()
    cfg = load_config()
    deliver = cfg.get("deliver") or []
    if not themes:
        print("(data/generated/ 里没有可渲染的 json —— 先跑 W2 生成)")
        return 0
    print(f"可渲染主题（共 {len(themes)} 门）：")
    for t in themes:
        mark = "  ← 默认交付" if t in deliver else ""
        print(f"  · {t}{mark}")
    return 0


def cmd_doctor(args) -> int:
    print("=" * 68)
    print("环境与数据自检")
    print("=" * 68)
    ok = True

    # 1. 依赖
    try:
        import docx                                          # noqa: F401
        print("  [OK]   python-docx 可导入")
    except Exception as exc:                                 # noqa: BLE001
        ok = False
        print(f"  [FAIL] python-docx 不可用：{exc}")

    # 2. 关键数据文件
    for rel, desc in REQUIRED_DATA.items():
        p = ROOT / rel
        if p.exists():
            print(f"  [OK]   {rel}  ({desc})")
        else:
            hard = "build_config" not in rel                 # 配置可缺省
            if hard:
                ok = False
            print(f"  [{'FAIL' if hard else 'WARN'}] {rel}  ({desc})")

    # 3. 语料目录
    cfg = load_config()
    corpus = Path(cfg.get("corpus_dir") or "")
    if corpus and corpus.exists():
        n = len([f for f in corpus.glob("*.docx") if not f.name.startswith("~$")])
        print(f"  [OK]   语料目录 {corpus}（{n} 份 .docx）")
    elif corpus:
        ok = False
        print(f"  [FAIL] 语料目录不存在：{corpus}")
        print("         → 改 data/structured/build_config.json 的 corpus_dir，"
              "或设环境变量 LPA_CORPUS_DIR")

    # 4. 主题
    themes = available_themes()
    print(f"  [OK]   可渲染主题 {len(themes)} 门：{'、'.join(themes) if themes else '（无）'}")
    if not themes:
        print("         → 先跑 scripts/run_generate.py 生成 LessonPlan")

    # 5. 脚本在位
    for s in (RENDER_SCRIPT, AUDIT_SCRIPT):
        if s.exists():
            print(f"  [OK]   {s.relative_to(ROOT)}")
        else:
            ok = False
            print(f"  [FAIL] 缺少脚本 {s.relative_to(ROOT)}")

    # 6. LLM / key 状态（W2 生成要用；W3 渲染不需要，故不算 FAIL）
    try:
        from lpa.llm import detect_providers, make_client     # noqa: PLC0415
        print(f"  [INFO] .env 文件：{ENV_FILE}"
              f"{'（存在）' if ENV_FILE.is_file() else '（不存在，key 可放这里）'}")
        rows = detect_providers()
        usable = [n for n, _m, _k, okk in rows if okk]
        for name, model, masked, okk in rows:
            flag = "可用" if okk else "  —"
            print(f"         [{flag}] {name:<12} {model:<32} key={masked}")
        cur = make_client()
        if cur.provider == "mock":
            print("         → 当前无 key，W2 生成会降级为 mock（出不了教学内容）")
            print("         → 配 key：python scripts/lesson_docx.py key --provider <名字> --key <key>")
        else:
            print(f"         → 当前将使用：{cur.provider} / {cur.model}"
                  f"（检测顺序 {usable}）")
    except Exception as exc:                                  # noqa: BLE001
        print(f"  [WARN] LLM 模块自检失败：{exc}")

    print("-" * 68)
    print("自检结果：" + ("全部通过，可以 build" if ok else "存在问题，先修上面的 FAIL"))
    return 0 if ok else 1


def _render(env_extra: dict, quiet: bool) -> int:
    return _run(RENDER_SCRIPT, env_extra, quiet)


def cmd_render(args) -> int:
    themes = split_theme_arg(args.theme)
    env: dict = {}
    out = getattr(args, "out", None)
    if out:
        env["LPA_OUT_DIR"] = str(Path(out).resolve())
    if args.all:
        env["LPA_RENDER_ALL"] = "1"
    elif themes:
        known = set(available_themes())
        unknown = [t for t in themes if t not in known]
        if unknown:
            _die(f"这些主题在 data/generated/ 里找不到：{'、'.join(unknown)}\n"
                 f"       可用：{'、'.join(sorted(known)) or '（无）'}")
        env["LPA_RENDER_ONLY"] = ",".join(themes)
    if args.single:
        env["LPA_FORCE_SINGLE"] = "1"
    return _render(env, args.quiet)


def cmd_audit(args) -> int:
    # `build` 复用本函数，它的参数表里没有 `--dir`，故用 getattr 兜底；
    # 未指定时回落到 `--out`（build -o 时应检刚产出的那个目录）。
    audit_dir = getattr(args, "dir", None) or getattr(args, "out", None)
    env = {"LPA_AUDIT_DIR": str(Path(audit_dir).resolve())} if audit_dir else {}
    return _run(AUDIT_SCRIPT, env, args.quiet)


def cmd_build(args) -> int:
    themes = split_theme_arg(args.theme)
    if not themes and not args.all:
        cfg = load_config()
        themes = cfg.get("deliver") or []
        if themes:
            print(f"[build] 未指定 --theme，按配置 deliver 清单：{'、'.join(themes)}")
    rc = cmd_render(args)
    if rc != 0:
        print("\n[build] 渲染阶段已失败，跳过终检。")
        return rc
    if args.no_audit:
        return 0
    print("\n[build] 渲染完成，进入交付前终检 ……")
    rc2 = cmd_audit(args)
    print("\n" + "=" * 68)
    out_dir = getattr(args, "out", None) or "data/docx"
    print("[build] 结论：" + (f"全部通过 ✅ 产物在 {out_dir}" if rc2 == 0
                             else "终检未通过 ❌ 请检查上面的 FAIL 项"))
    return rc2


def cmd_eval(args) -> int:
    """M4-1 质量侧自动指标。

    ⚠ 与 `audit`（W4 交付终检）不是一回事：
        audit → 判**版式与合规**（能不能交付）
        eval  → 判**质量证据**（去标识化效果、名册覆盖、分层结构，
                              产出 EVAL_METRICS.md / .json）
    教师盲评（M4-5）是独立环节，不在本命令内。
    """
    print("[eval] M4-1 自动指标（质量侧，非版式终检）……")
    rc = _run(EVAL_SCRIPT, None, args.quiet)
    print("\n" + "=" * 68)
    print("[eval] 结论：" + ("指标已产出 ✅ data/structured/EVAL_METRICS.md"
                            if rc == 0 else "指标计算失败 ❌"))
    return rc


def cmd_holdout(args) -> int:
    """M4-2 留出对照：留出主题 vs 已交付主题，看能力是否只对某个主题有效。

    ⚠ 与 `eval`（M4-1 自动指标）不是一回事：
        eval    → 算**绝对**质量证据（去标识化、名册覆盖、分层结构）
        holdout → 算**相对**证据（实验组 vs 对照组，即泛化能力）
    产出 data/structured/HOLDOUT_COMPARISON.md / .json。
    """
    print("[holdout] M4-2 留出对照（实验组 vs 对照组）……")
    rc = _run(HOLDOUT_SCRIPT, None, args.quiet)
    print("\n" + "=" * 68)
    print("[holdout] 结论：" + ("对照报告已产出 ✅ data/structured/HOLDOUT_COMPARISON.md"
                                if rc == 0 else "对照计算失败 ❌"))
    return rc


def cmd_ablation(args) -> int:
    """M4-4 消融实验：逐个关掉模块，量出每个模块的贡献。

    与 `holdout`（换主题）不同，消融是**换配置**：同一批主题、每次只关一个模块。
    会**真调 LLM** 逐臂重跑，耗时较长；每臂的中间稿与 docx 隔离在
    data/_ablation/<臂>/，不动正式产物。
    产出 data/structured/ABLATION_STUDY.md / .json。
    """
    print("[ablation] M4-4 消融实验（逐模块关闭，需调 LLM，耗时较长）……")
    extra = []
    only = getattr(args, "only", "") or ""
    if only:
        extra += ["--only", only]
    if getattr(args, "reuse", False):
        extra += ["--reuse"]
    rc = _run(ABLATION_SCRIPT, None, args.quiet, extra_args=extra)
    print("\n" + "=" * 68)
    print("[ablation] 结论：" + ("消融报告已产出 ✅ data/structured/ABLATION_STUDY.md"
                                 if rc == 0 else "消融实验失败 ❌"))
    return rc


def cmd_blindforms(args) -> int:
    """M4-5 教师盲评的**收发端**：发下可填的 Word 评分卡 / 收回分数。

    `blindeval` 管备料（抽题脱敏、出匿名卡片与封存密钥），本命令管：
    教师拿到一份 `.docx`（打分格是下拉选择，教案正文就在评分表上方），
    填完丢进 `回收/`，`--ingest` 自动读回并落进 `3_回收统计表.csv` —— 全程零转录。
    """
    script = SCRIPTS / "blind_eval_form.py"
    extra = []
    if getattr(args, "ingest", False):
        extra += ["--ingest"]
        print("[blindform] M4-5 读回回收的评分卡 → 回收表 + 分组统计……")
    else:
        if getattr(args, "teachers", None):
            extra += ["--teachers", args.teachers]
        print("[blindform] M4-5 生成可填写的 Word 评分卡 + 回收夹……")
    rc = _run(script, None, args.quiet, extra_args=extra)
    print("\n" + "=" * 68)
    if args.ingest:
        print("[blindform] 回收解析 " + ("完成 ✅" if rc == 0 else "失败 ❌"))
    else:
        print("[blindform] 评分卡：" + ("已产出 ✅ data/blind-eval/5_教师评分卡/"
                                        if rc == 0 else "生成失败 ❌"))
    return rc


def cmd_blindeval(args) -> int:
    """M4-5 教师盲评：生成匿名材料包（或收分后统计）。

    与前四项机器评估不同，这一环的**尺子握在人手里** ——
    脚本只负责把「系统产出」与「人工范例」混装成同一版式、去掉来源指纹的匿名卡片，
    教师不知道来源，按统一量表打分，评完开封 `4_分组密钥.md` 再分组统计。

    `--tally` 不开生成，直接读回收表 + 密钥出分组均值。
    """
    if getattr(args, "ingest", False):
        return cmd_blindforms(args)

    extra = []
    if getattr(args, "seed", None) is not None:
        extra += ["--seed", str(args.seed)]
    if getattr(args, "tally", False):
        extra += ["--tally"]
        print("[blindeval] M4-5 收分统计（读回收表 + 封存密钥）……")
    else:
        print("[blindeval] M4-5 生成教师盲评材料包（匿名混装卡片）……")
    rc = _run(BLIND_EVAL_SCRIPT, None, args.quiet, extra_args=extra)
    print("\n" + "=" * 68)
    if getattr(args, "tally", False):
        print("[blindeval] 统计完成 " + ("✅" if rc == 0 else "❌"))
    else:
        print("[blindeval] 材料包：" + ("已产出 ✅ data/blind-eval/"
                                       "（详见其中 README.md）" if rc == 0 else "生成失败 ❌"))
        if rc == 0:
            print("[blindeval] 提示：接着跑 `blindform` 出可填写的 Word 评分卡")
    return rc


def cmd_judge(args) -> int:
    """M4-3 LLM-as-judge：让模型当评委，给教案打分。

    与 M4-5 **刻意对齐**：同一批匿名卡片、同一份 D1–D5 量表、同一组判据，
    只是把评委从教师换成模型 —— 这样两边的结果能直接对照。
    量法为「点式评分 ×N + 位置对调配对」。
    **必须真实调 API**（mock 模板不构成证据）；无 key 直接报错退出。
    产出 data/structured/JUDGE_STUDY.md / .json。
    """
    extra = ["--runs", str(getattr(args, "runs", 2) or 2)]
    if getattr(args, "no_pairs", False):
        extra += ["--no-pairs"]
    if getattr(args, "from_json", False):
        extra += ["--from-json"]
    if getattr(args, "judge_provider", None):
        extra += ["--judge-provider", args.judge_provider]
    if getattr(args, "judge_model", None):
        extra += ["--judge-model", args.judge_model]
    print("[judge] M4-3 LLM-as-judge（模型当评委，需真实 API key）……")
    rc = _run(JUDGE_SCRIPT, None, args.quiet, extra_args=extra)
    print("\n" + "=" * 68)
    print("[judge] 结论：" + ("评测报告已产出 ✅ data/structured/JUDGE_STUDY.md"
                              if rc == 0 else "评测失败 ❌"))
    return rc


PROBE_TOOLS_SCRIPT = SCRIPTS / "probe_tool_calling.py"   # P0-1 工具调用能力探针
AGENT_RUN_SCRIPT = SCRIPTS / "agent_run.py"              # P0-3 自主编排运行器


def cmd_tools(args) -> int:
    """P0-1 工具调用能力探针：验 Agent 循环 + 实测底座模型的 function calling。

    这一环**不是**教案流水线，而是 **Agent 化的地基验收**：
      ① 离线（mock）验**链路** —— 循环收敛、`tool_call_id` 配对、异常回灌，
         与模型强弱无关，用确定性替身就能验完；
      ② 真跑验**模型** —— 任务设计成**必须按顺序调 3 次工具**才答得出
         （不调 `list_children` 就不知道有 S37），用来判断当前模型
         （默认 `glm-4-flash`）撑不撑得住多轮工具调用。

    这两件事必须分开看：链路坏了换模型没用，模型弱了修链路也没用。

    产出 `data/structured/TOOL_CALLING_PROBE.md` / `.json`。
    """
    extra = ["--runs", str(getattr(args, "runs", 3) or 3)]
    if getattr(args, "mock", False):
        extra += ["--mock"]
    if getattr(args, "provider", None):
        extra += ["--provider", args.provider]
    if getattr(args, "model", None):
        extra += ["--model", args.model]
    if getattr(args, "max_steps", None):
        extra += ["--max-steps", str(args.max_steps)]
    print("[tools] P0-1 工具调用能力探针（离线链路自检 + 真实模型实测）……")
    rc = _run(PROBE_TOOLS_SCRIPT, None, args.quiet, extra_args=extra)
    print("\n" + "=" * 68)
    print("[tools] 结论：" + ("报告已产出 ✅ data/structured/TOOL_CALLING_PROBE.md"
                              if rc == 0 else "探针失败 ❌（链路问题先修 src/lpa/llm.py）"))
    return rc


def cmd_auto(args) -> int:
    """P0-3 自主编排：模型自己决定工具调用顺序，并自主修复校验失败。

    与 `build`（固定流水线）相对：本命令**不写死任何步骤顺序**，只给模型
    目标与纪律（数据必须查证 / 只准匿名 / 失败必须修复不许绕过），由它
    自己调用工具完成「生成 → 校验 → 修复 → 交付」全链路。

    `--fault bad_objective` 注入一个首稿缺陷（坏目标），演示
    「拿到失败明细 → 自主修复 → 复检通过」闭环；证据在
    `data/trace/<run_id>.jsonl` / `.md`（P0-4），汇总报告在
    `data/structured/AUTONOMOUS_RUN.md`。
    """
    extra: list = []
    if getattr(args, "theme", None):
        extra += ["--theme", args.theme]
    if getattr(args, "fault", None):
        extra += ["--fault", args.fault]
    if getattr(args, "scenario", None):
        extra += ["--scenario", args.scenario]
    if getattr(args, "max_steps", None):
        extra += ["--max-steps", str(args.max_steps)]
    if getattr(args, "run_id", None):
        extra += ["--run-id", args.run_id]
    if getattr(args, "mock", False):
        extra += ["--mock"]
    if getattr(args, "report_only", False):
        extra += ["--report-only"]
    if getattr(args, "quiet", False):
        extra += ["-q"]
    if getattr(args, "report_only", False):
        print("[auto] 只重排 AUTONOMOUS_RUN.md（不运行）……")
        rc = _run(AGENT_RUN_SCRIPT, None, args.quiet, extra_args=extra)
        print("[auto] 重排完成。")
        return rc
    print("[auto] P0-3 自主编排运行（模型自主决定工具调用顺序）……")
    rc = _run(AGENT_RUN_SCRIPT, None, args.quiet, extra_args=extra)
    print("\n" + "=" * 68)
    if getattr(args, "mock", False):
        print("[auto] mock 冒烟：" + ("管道通过 ✅（未做校验/终检，非真实验收）"
                                       if rc == 0 else "管道失败 ❌"))
        return rc
    _line = {0: "完整闭环 ✅ 报告：data/structured/AUTONOMOUS_RUN.md",
             2: "未闭环 ⚠（如实记录，详情见报告与 Trace）"}
    print("[auto] 结论：" + _line.get(rc, f"运行失败（exit={rc}）"))
    return rc


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="lesson_docx.py",
        description="儿童康复教案 docx 流水线（渲染 + 合规 + 终检）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("退出码")[0],
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("-t", "--theme", action="append", metavar="主题",
                       help="主题名，可重复或逗号分隔")
        p.add_argument("--all", action="store_true", help="渲染全部主题")
        p.add_argument("--single", action="store_true",
                       help="按班拆分时强制合成单表")
        p.add_argument("-o", "--out", metavar="目录",
                       help="产物目录（默认 data/docx）")
        p.add_argument("-q", "--quiet", action="store_true", help="只输出结论行")

    p_build = sub.add_parser("build", help="渲染 + 终检（推荐）")
    add_common(p_build)
    p_build.add_argument("--no-audit", action="store_true", help="跳过终检")
    p_build.set_defaults(func=cmd_build)

    p_render = sub.add_parser("render", help="只渲染")
    add_common(p_render)
    p_render.set_defaults(func=cmd_render)

    p_audit = sub.add_parser("audit", help="只终检")
    p_audit.add_argument("--dir", metavar="目录", help="待检 docx 目录（默认 data/docx）")
    p_audit.add_argument("-q", "--quiet", action="store_true")
    p_audit.set_defaults(func=cmd_audit)

    p_eval = sub.add_parser(
        "eval", help="M4-1 质量侧自动指标（去标识化/名册/分层，出 EVAL_METRICS）")
    p_eval.add_argument("-q", "--quiet", action="store_true")
    p_eval.set_defaults(func=cmd_eval)

    p_ho = sub.add_parser(
        "holdout", help="M4-2 留出对照（留出主题 vs 已交付主题，出 HOLDOUT_COMPARISON）")
    p_ho.add_argument("-q", "--quiet", action="store_true")
    p_ho.set_defaults(func=cmd_holdout)

    p_abl = sub.add_parser(
        "ablation", help="M4-4 消融实验（逐模块关闭，量各模块贡献，出 ABLATION_STUDY）")
    p_abl.add_argument("--only", metavar="臂", default="",
                       help="只跑指定臂（逗号分隔，如 full,no_content）")
    p_abl.add_argument("--reuse", action="store_true",
                       help="复用已生成的中间稿，只重新评分")
    p_abl.add_argument("-q", "--quiet", action="store_true")
    p_abl.set_defaults(func=cmd_ablation)

    p_be = sub.add_parser(
        "blindeval", help="M4-5 教师盲评（生成匿名材料包 / 收分后统计）")
    p_be.add_argument("--seed", type=int, default=None, metavar="整数",
                      help="打乱编号用的随机种子（实施中途别改）")
    p_be.add_argument("--tally", action="store_true",
                      help="不开生成：读回收表 + 封存密钥，按来源分组统计")
    p_be.add_argument("--ingest", action="store_true",
                      help="等价于 `blindform --ingest`（读回填好的 Word 评分卡）")
    p_be.add_argument("-q", "--quiet", action="store_true")
    p_be.set_defaults(func=cmd_blindeval)

    p_bf = sub.add_parser(
        "blindform", help="M4-5 教师盲评收发端（出可填 Word 评分卡 / 读回分数）")
    p_bf.add_argument("--teachers", default=None, metavar="名字",
                      help="生成哪几位教师的评分卡，逗号分隔（默认 教师1,教师2,教师3）")
    p_bf.add_argument("--ingest", action="store_true",
                      help="不开生成：读 回收/ 里填好的评分卡 → 回收表 + 分组统计")
    p_bf.add_argument("-q", "--quiet", action="store_true")
    p_bf.set_defaults(func=cmd_blindforms)

    p_jd = sub.add_parser(
        "judge", help="M4-3 LLM-as-judge（模型当评委给教案打分，需真实 key）")
    p_jd.add_argument("--runs", type=int, default=2, metavar="次数",
                      help="点式评分重复次数，用于量评委稳定性（默认 2）")
    p_jd.add_argument("--no-pairs", action="store_true",
                      help="跳过位置对调配对（更快，但少了稳健性检查）")
    p_jd.add_argument("--judge-provider", default=None, metavar="名",
                      help="指定评委 provider（换族可减轻自偏好偏差）")
    p_jd.add_argument("--judge-model", default=None, metavar="名",
                      help="覆盖评委模型名")
    p_jd.add_argument("--from-json", action="store_true",
                      help="不调模型：读存档 JUDGE_STUDY.json 重排报告（只改文字时用）")
    p_jd.add_argument("-q", "--quiet", action="store_true")
    p_jd.set_defaults(func=cmd_judge)

    p_tl = sub.add_parser(
        "tools", help="P0-1 工具调用能力探针（Agent 循环 + 模型 function calling 实测）")
    p_tl.add_argument("--runs", type=int, default=3, metavar="次数",
                      help="真实调用重复次数（默认 3）")
    p_tl.add_argument("--max-steps", type=int, default=None, metavar="步数",
                      help="Agent 循环最大轮数（默认 8）")
    p_tl.add_argument("--provider", default=None, metavar="名",
                      help="指定 provider（默认自动探测）")
    p_tl.add_argument("--model", default=None, metavar="名",
                      help="覆盖模型名")
    p_tl.add_argument("--mock", action="store_true",
                      help="只跑离线链路自检，不调 API")
    p_tl.add_argument("-q", "--quiet", action="store_true")
    p_tl.set_defaults(func=cmd_tools)

    p_auto = sub.add_parser(
        "auto", help="P0-3 自主编排：模型自己决定工具顺序 + 自主修复（Agent 模式）")
    p_auto.add_argument("--theme", default="国庆节到了", metavar="主题",
                        help="主题（默认「国庆节到了」）")
    p_auto.add_argument("--fault", default="", choices=["", "bad_objective"],
                        help="故障注入：bad_objective = 首稿含坏目标，演示自主修复闭环")
    p_auto.add_argument("--scenario", default="",
                        choices=["", "inject", "inject_social",
                                 "no_theme", "out_of_scope"],
                        help="P0-6 用例：inject 明显注入 / inject_social 社工刺探 /"
                             " no_theme 缺参数追问 / out_of_scope 超范围请求")
    p_auto.add_argument("--max-steps", type=int, default=24, metavar="N",
                        help="Agent 循环轮数上限（默认 24）")
    p_auto.add_argument("--run-id", default="", metavar="ID", help="自定义运行 ID")
    p_auto.add_argument("--mock", action="store_true",
                        help="离线冒烟（mock 替身，只验管道不调 API）")
    p_auto.add_argument("--report-only", action="store_true",
                        help="不跑，只重排 AUTONOMOUS_RUN.md")
    p_auto.add_argument("-q", "--quiet", action="store_true")
    p_auto.set_defaults(func=cmd_auto)

    p_list = sub.add_parser("list", help="列出可渲染主题")
    p_list.set_defaults(func=cmd_list)

    p_doc = sub.add_parser("doctor", help="环境与数据自检（含 LLM key 状态）")
    p_doc.set_defaults(func=cmd_doctor)

    p_key = sub.add_parser("key", help="把 API key 写进项目根 .env")
    p_key.add_argument("--provider", required=True, metavar="名字",
                       help="deepseek / zhipu / siliconflow / modelscope")
    p_key.add_argument("--key", required=True, metavar="密钥", help="你的 API key")
    p_key.set_defaults(func=cmd_key)

    p_ping = sub.add_parser("ping", help="发最小请求验证 key 可用（会消耗极少量额度）")
    p_ping.add_argument("--provider", metavar="名字", help="强制指定 provider")
    p_ping.add_argument("--json", action="store_true", help="顺带测 JSON 输出模式")
    p_ping.set_defaults(func=cmd_ping)
    return ap


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""W2 端到端：用班级画像生成一份教案，跑四步链 + 硬校验，输出 Markdown。

用法
----
    python scripts/run_generate.py
        生成内置留出集（3 门，不进知识库，用于盲测）。

    python scripts/run_generate.py "国庆节到了:认知"
        生成**指定主题**（可给多个），格式 `主题` 或 `主题:领域`；
        不写领域则交给生成器按主题自动推断。

    ⚠ 目标文件已存在时会**自动备份**到 `data/_archive/<日期>/pre-regen/` 再覆盖，
      万一新版不如旧版，直接从那里拷回即可。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lpa.generate import GenerateRequest, LessonGenerator, load_class_distribution  # noqa: E402
from lpa.llm import make_client  # noqa: E402
from lpa.rules import load_forbidden_names  # noqa: E402

ROOT = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent")
OUT = ROOT / "data" / "structured"
GEN = ROOT / "data" / "generated"
GEN.mkdir(parents=True, exist_ok=True)

# 留出集主题（不进知识库，用于盲测）
THEMES = [
    ("认识秋天的水果", "认知"),
    ("我会穿鞋子", "生活自理"),
    ("跟着音乐做动作", "粗大动作"),
]

# ⚠⚠ 交付主题《国庆节到了》**天生不在上面的 THEMES 里**，两条原因：
#
#   ① 它是交付件 —— 重生成会直接动 `data/docx/` 里的成品，不属于"日常重跑"；
#   ② 2026-10-01 曾因无 key 时贸然重跑，`data/generated/国庆节到了.json` 被
#      mock 模板覆盖（主题课活动正文 1523 → 1067 字符，具体内容全丢）。
#
# 现在已配好 ZHIPU_API_KEY（glm-4-flash，免费），要重生成交付主题**显式指定**：
#
#     python scripts/run_generate.py "国庆节到了:认知"
#
# 脚本会先把旧产物备份进 `data/_archive/<日期>/pre-regen/` 再覆盖。


def _themes_from_argv(argv: list) -> list:
    """命令行主题：`主题` 或 `主题:领域`（领域留空 → 交给生成器自动推断）。"""
    out: list = []
    for raw in argv:
        text = str(raw).strip()
        if not text or text.startswith("-"):
            continue
        parts = re.split(r"[:：]", text, maxsplit=1)
        theme = parts[0].strip()
        domain = parts[1].strip() if len(parts) > 1 else ""
        if theme:
            out.append((theme, domain))
    return out


def _deliver_titles() -> set:
    """配置里的交付清单（形如 `认知课《国庆节到了》` 的**完整标题**）。"""
    try:
        cfg = json.loads((OUT / "build_config.json").read_text(encoding="utf-8"))
        return set(cfg.get("deliver") or [])
    except Exception:                       # noqa: BLE001
        return set()


def _backup_existing(theme: str) -> None:
    """重生成前把已有产物复制进归档，避免「真产物被覆盖」这类事故。

    归档路径：`data/_archive/<YYYY-MM-DD>/pre-regen/`。
    目标不存在则什么都不做（首次生成无需备份）。
    """
    dest = ROOT / "data" / "_archive" / date.today().isoformat() / "pre-regen"
    copied: list = []
    for suffix in (".json", ".md"):
        src = GEN / f"{theme}{suffix}"
        if not src.exists():
            continue
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / src.name)
        copied.append(src.name)
    if copied:
        print(f"[run] 已备份旧产物 → {dest}（{'、'.join(copied)}）")


def main() -> None:
    cli_themes = _themes_from_argv(sys.argv[1:])
    themes = cli_themes or THEMES
    deliver = _deliver_titles()

    if cli_themes:
        print(f"[run] 命令行指定主题 {len(themes)} 门："
              + "、".join(t for t, _d in cli_themes))
        for theme, _domain in cli_themes:
            if any(theme in d for d in deliver):
                print(f"[run] ⚠ 「{theme}」是**交付主题**，重生成会覆盖 data/docx/ 的成品；"
                      f"旧产物已备份，可随时回退")
            _backup_existing(theme)

    dist = load_class_distribution(str(OUT / "class_profile.json"))
    forbidden = load_forbidden_names(str(ROOT / "data" / "private" / "name_mapping.json"))
    client = make_client(verbose=True)
    print(f"[run] provider = {client.provider}, forbidden_names = {len(forbidden)}")

    gen = LessonGenerator(client=client, max_rewrites=2, verbose=True,
                          forbidden_names=forbidden)
    summary = []
    for theme, domain in themes:
        req = GenerateRequest(
            theme=theme,
            total_duration_min=30,
            distribution=dist,
            class_size=sum(sum(v.values()) for v in dist.values()) and 12,
            main_domain_hint=domain or "",
            date_range="2026年10月12日—10月16日",
        )
        res = gen.generate(req)
        slug = theme
        if res.plan is None:
            summary.append({"theme": theme, "ok": False, "reason": "生成失败"})
            continue
        md = res.plan.to_markdown()
        (GEN / f"{slug}.md").write_text(md, encoding="utf-8")
        (GEN / f"{slug}.json").write_text(
            res.plan.model_dump_json(indent=2), encoding="utf-8")
        summary.append({
            "theme": theme,
            "ok": res.report.passed,
            "fatal": res.report.fatal_count,
            "warn": res.report.warning_count,
            "metrics": res.metrics,
            "traces": [{"step": t.name, "ok": t.ok, "detail": t.detail} for t in res.traces],
        })
        print(f"[run] {theme}: passed={res.report.passed} "
              f"fatal={res.report.fatal_count} warn={res.report.warning_count}")
        for v in res.report.violations:
            print(f"       [{'FATAL' if v.fatal else 'WARN '}] {v.message[:160]}")

    lines = ["# W2 端到端生成报告", "",
             f"- provider：**{client.provider}**",
             f"- 生成份数：**{len(themes)}**",
             f"- 通过硬校验：**{sum(1 for s in summary if s.get('ok'))}**",
             "",
             "## 逐份结果", "",
             "| 主题 | 通过 | 致命 | 警告 | 环节 | 支架/环节 | 提示覆盖 | 目标 |",
             "|---|---|---|---|---|---|---|---|"]
    for s in summary:
        if not s.get("ok") and "metrics" not in s:
            lines.append(f"| {s['theme']} | ❌ | - | - | - | - | - | - |")
            continue
        m = s["metrics"]
        lines.append(
            f"| {s['theme']} | {'✅' if s['ok'] else '❌'} | {s['fatal']} | {s['warn']} | "
            f"{m['steps']} | {m['scaffold_per_step']} | {m['cue_coverage']} | {m['objectives']} |")

    lines += ["", "## 四步链留痕", ""]
    for s in summary:
        if "traces" not in s:
            continue
        lines.append(f"### {s['theme']}")
        lines.append("")
        lines.append("| 步骤 | 通过 | 说明 |")
        lines.append("|---|---|---|")
        for t in s["traces"]:
            lines.append(f"| {t['step']} | {'✅' if t['ok'] else '❌'} | {t['detail']} |")
        lines.append("")

    lines += ["", "## 模型用量", "",
              f"```json\n{json.dumps(client.total_usage, ensure_ascii=False, indent=2)}\n```"]

    (OUT / "W2_GENERATION_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print("ok")


if __name__ == "__main__":
    main()

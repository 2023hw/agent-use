"""P0-2 自测：遍历全部工具，验证契约、隐私与「校验→渲染→终检」闭环。

跑法：
    PYTHONUTF8=1 "$PY" scripts/agent_tools_selftest.py          # mock（离线、秒级）
    PYTHONUTF8=1 "$PY" scripts/agent_tools_selftest.py --real   # 真实模型（约 1 分钟）

覆盖：
1. 10 个工具的 schema 完整性；
2. 每个工具的真实调用（渲染与终检走真链路）；
3. **隐私契约**：所有工具返回值里不得出现任何真实姓名；
4. **顺序契约**：顺序错了必须抛带指引的错误（不静默兜底）；
5. **Agent 循环可插性**：`ToolBox.run` 能直接挂进 `run_with_tools`。

⚠ `--real` 模式下「硬校验」不强制通过 —— 真实模型首稿的校验结果属于内容质量
问题（正是 P0-3 里 Agent 要自己修的东西），此处只如实报告，不算工具层失败。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lpa.llm import LLMClient, ToolCall, make_client          # noqa: E402
from lpa.tools import ToolBox                                 # noqa: E402

OUTPUTS: list = []          # 供隐私检查的产物汇总


def call(box: ToolBox, name: str, args: dict | None = None):
    """调一次工具。返回 (ok, result_or_error, 秒)。"""
    t0 = time.time()
    try:
        r = box.run(ToolCall(id=f"c-{name}", name=name, arguments=args or {}))
        OUTPUTS.append({name: r})
        return True, r, time.time() - t0
    except Exception as e:                                     # noqa: BLE001
        OUTPUTS.append({name: {"_error": f"{type(e).__name__}: {e}"}})
        return False, e, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true",
                    help="用真实模型（默认 mock，离线秒级）")
    args = ap.parse_args()

    run_dir = ROOT / "data" / "_agent_runs" / (
        "selftest_real" if args.real else "selftest_mock")
    client = make_client() if args.real else LLMClient(provider="mock")

    failures: list = []

    def check(cond: bool, label: str, detail: str = "") -> None:
        tag = "PASS" if cond else "FAIL"
        print(f"  [{tag}] {label}" + (f"  {detail}" if detail else ""))
        if not cond:
            failures.append(label)

    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)

    box = ToolBox(root=ROOT, client=client, run_dir=run_dir,
                  theme="国庆节到了", verbose=True)
    print(f"ToolBox 就绪 | 标题 {box.title!r} | 阶梯 {len(box.ladder_spec)} 条 "
          f"| 在册 {len(box.children)} 名 | 客户端 {client.provider}\n")

    # ---- 1. schema 完整性 ----
    print("== 1. schema ==")
    schemas = box.schemas()
    names = [t["function"]["name"] for t in schemas]
    check(len(schemas) == 10 and names == ToolBox.tool_names(),
          "10 个工具 schema 齐全且顺序一致", str(names))
    check(all(t["function"].get("description") for t in schemas),
          "每个工具都有描述")

    # ---- 2. 顺序契约：先调生成类必须报错 ----
    print("== 2. 顺序契约 ==")
    ok, err, _ = call(box, "draft_objectives")
    check(not ok and "plan_layering" in str(err), "draft 先于 layering 时报错且带指引",
          f"{type(err).__name__}")
    ok, err, _ = call(box, "expand_content")
    check(not ok and "plan_layering" in str(err), "expand 先于目标时报错且带指引")

    # ---- 3. 查询类 ----
    print("== 3. 查询类 ==")
    ok, r, dt = call(box, "get_class_roster")
    check(ok and r["count"] == len(box.children) == 37, "名册 37 人",
          f"{dt:.2f}s")
    ok, r, _ = call(box, "get_child_profile", {"anon_id": "s01"})
    check(ok and r["anon_id"] == "S01", "单档案查询（小写编号自动规整）")
    ok, err, _ = call(box, "get_child_profile", {"anon_id": "S99"})
    check(not ok and "不存在" in str(err), "非法编号报错", f"{type(err).__name__}")
    ok, r, dt = call(box, "search_corpus", {"query": "音乐"})
    check(ok and r["n_hits"] >= 1, "语料检索命中", f"{r.get('n_hits')} 份 {dt:.2f}s")
    ok, r, _ = call(box, "search_corpus", {"query": "国旗"})
    check(ok and r["n_hits"] == 0 and "未找到" in r["note"],
          "未命中时明确告知并给出换词指引（不编造语料）")
    ok, r, dt = call(box, "get_previous_baseline", {"anon_ids": ["all"]})
    check(ok and r["n_returned"] == 37, "跨课基线取回全部 37 人", f"{dt:.2f}s")
    n_goal = sum(1 for x in r["records"] if x["goal_combo"])
    check(n_goal > 0, "基线记录含康复目标编号", f"{n_goal}/37 人有编号")

    # ---- 4. 规划 / 生成 ----
    print(f"== 4. 规划与生成（{'真跑' if args.real else 'mock'}） ==")
    ok, r, dt = call(box, "plan_layering")
    check(ok and r["domain"] == "认知"
          and sum(r["support_layers"].values()) == 37,
          "分层规划：领域=认知、三层合计=37", f"{r.get('support_layers')} {dt:.2f}s")
    check(ok and r["n_layers"] >= 3, "层数来自数据（不写死）",
          f"{r.get('n_layers')} 层")
    ok, r, dt = call(box, "draft_objectives")
    check(ok and r["count"] >= 3, "目标生成", f"{r.get('count')} 条 {dt:.1f}s")
    ok, r, dt = call(box, "expand_content")
    check(ok and r["n_steps"] >= 5 and r["total_duration_min"] == 30,
          "内容扩写：环节数与时长自洽",
          f"{r.get('n_steps')} 环节 mode={r.get('mode')} {dt:.1f}s")

    # ---- 5. 校验 → 渲染 → 终检 ----
    print("== 5. 校验 / 渲染 / 终检 ==")
    ok, r, _ = call(box, "validate_lesson_plan")
    if args.real:
        check(ok and isinstance(r["passed"], bool), "硬校验返回结论（真跑）",
              f"passed={r.get('passed')} fatal={r.get('fatal_count')}")
        if ok and not r["passed"]:
            print("    首稿致命项（属内容质量问题，P0-3 由 Agent 自行修复）：")
            for ln in r["fatal"][:3]:
                print("      -", ln)
    else:
        check(ok and r["passed"], "硬校验通过（mock）", f"fatal={r.get('fatal_count')}")
    ok, r, dt = call(box, "render_docx")
    check(ok and r["n_files"] >= 1, "渲染出 docx", f"{r.get('n_files')} 个 {dt:.1f}s")
    n_issues = sum(len(f["compliance_issues"]) for f in (r.get("files") or []))
    check(ok and n_issues == 0, "渲染回读零合规问题", f"{n_issues} 项")
    if ok:
        for f in r["files"]:
            print(f"    · {Path(f['path']).name} | {f['size']} B | {f['class']}")
    ok, r, dt = call(box, "audit_docx")
    check(ok and r["passed"], "交付终检通过", f"exit={r.get('exit_code')} {dt:.1f}s")
    if ok and not r["passed"]:
        print("    终检未过项：", r.get("fail_items"))

    # ---- 6. 隐私契约 ----
    print("== 6. 隐私契约 ==")
    private = json.loads((ROOT / "data" / "private" / "name_mapping.json")
                         .read_text(encoding="utf-8"))
    blob = json.dumps(OUTPUTS, ensure_ascii=False)
    leaked = [nm for nm in private if nm and nm in blob]
    check(not leaked, "全部工具返回值零真实姓名", f"泄漏 {leaked[:3]}" if leaked else "")

    # ---- 7. Agent 循环可插性 ----
    print("== 7. Agent 循环可插性 ==")
    client7 = make_client() if args.real else LLMClient(provider="mock")
    t = client7.run_with_tools(
        system="你是教案助手，回答任何问题前必须先调用工具拿真实数据。",
        user="本班有多少名幼儿？",
        tools=box.schemas(),
        runner=box.run,
        max_steps=4,
    )
    check(t.stopped_reason == "finish" and t.tool_call_count >= 1,
          "ToolBox.run 挂进 run_with_tools 后正常收尾",
          f"调用 {t.tool_call_count} 次，原因 {t.stopped_reason}")

    print("\n" + "=" * 62)
    if failures:
        print(f"存在 FAIL：{failures}")
        return 1
    print("全部 PASS —— 工具集契约定型")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

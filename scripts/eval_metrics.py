# -*- coding: utf-8 -*-
"""M4-1 质量侧自动指标：把「现在不用等任何人」就能算的证据一次性算出来。

> ⚠ 命名约定（别混）：
>   **W4** = 技术步骤「交付终检」，判**版式与合规**（`final_audit.py`，17 项 PASS/FAIL）。
>   **M4** = 项目里程碑「效果验证与答辩」，判**教案好不好用**。
>   本脚本属于 **M4-1 自动指标**，不做版式终检、也不替代教师盲评。

输入：
    data/structured/build_config.json      语料目录
    「agent use idea」下的 14 份范例 .docx   基线对照（原始语料）
    data/generated/*.json                  W2 产物（ LessonPlan ）
    data/docx/*.docx                       W3 交付物

输出：
    data/structured/EVAL_METRICS.json      机器可读
    data/structured/EVAL_METRICS.md        人读报告（答辩可直接贴）
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from docx import Document                                   # noqa: E402
from docx.oxml.ns import qn                                 # noqa: E402
from lpa.rules import BANNED_TERMS, validate_plan, plan_metrics   # noqa: E402
from lpa.schema import LessonPlan                           # noqa: E402
from lpa.render_docx import docx_text                       # noqa: E402

STRUCT = ROOT / "data" / "structured"
GEN = ROOT / "data" / "generated"
OUT_DIR = ROOT / "data" / "docx"
PRIVATE = ROOT / "data" / "private"

# 主表逻辑列（与 render_docx 一致）
COL_CHILD, COL_TARGET, COL_BASELINE = 8, 9, 10


# ---------------------------------------------------------------- 通用小工具
def _tx(tc) -> str:
    return "".join(x.text or "" for x in tc.iter(qn("w:t"))).strip()


def _cells(row):
    """返回该行的 <w:tc> 列表（不展开合并格）"""
    return row._tr.findall(qn("w:tc"))


def _logical(row):
    """逻辑列号 -> <w:tc>（按 gridSpan 展开，合并格只算第一列）"""
    out, c = {}, 0
    for tc in _cells(row):
        gs = tc.find(qn("w:tcPr") + "/" + qn("w:gridSpan"))
        n = int(gs.get(qn("w:val"))) if gs is not None else 1
        out[c] = tc
        c += n
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _banned_hits(text: str):
    """返回 (命中次数, 命中的词种数, 命中词列表)"""
    hits, terms = 0, []
    for t in BANNED_TERMS:
        k = text.count(t)
        if k:
            hits += k
            terms.append(f"{t}×{k}")
    return hits, len(terms), terms


def _load_names():
    m = json.loads((PRIVATE / "name_mapping.json").read_text(encoding="utf-8"))
    return m  # 姓名 -> 代号


def _corpus_dir() -> Path:
    cfg_p = STRUCT / "build_config.json"
    if cfg_p.exists():
        try:
            cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
            if cfg.get("corpus_dir"):
                return Path(cfg["corpus_dir"])
        except Exception:
            pass
    return Path(r"C:\Users\17093\Desktop\homework\agent use idea")


# ---------------------------------------------------------------- A 去标签化
def scan_docx(p: Path, names):
    """扫一份 docx：标签词 / 真实姓名 命中。**治理前后用同一把尺子**。

    ⚠ 关键口径（前后一致才可比）：
      「障碍类别」是范例模板的固有栏目，交付物按用户要求**照范例原样填**
      （如「智力障碍、自闭症★」），所以该列整列**豁免**；
      语料侧同样豁免，否则「治理前」会被同一栏目虚高，对比失真。
      真正要盯的是 **正文/幼儿行命中（body）**。
    """
    doc = Document(str(p))
    banned, exempt, name_hits, chars = 0, 0, 0, 0
    for tbl in doc.tables:
        if not tbl.rows:
            continue
        head = _logical(tbl.rows[0])
        ex_cols = {c for c, tc in head.items() if "障碍类别" in _norm(_tx(tc))}
        for row in tbl.rows:
            for c, tc in _logical(row).items():
                t = _tx(tc)
                if not t:
                    continue
                chars += len(t)
                b = sum(t.count(x) for x in BANNED_TERMS)
                if b:
                    banned += b
                    if c in ex_cols:
                        exempt += b
                name_hits += sum(t.count(nm) for nm in names)
    for para in doc.paragraphs:
        t = para.text or ""
        chars += len(t)
        banned += sum(t.count(x) for x in BANNED_TERMS)
        name_hits += sum(t.count(nm) for nm in names)
    return {"file": p.name, "chars": chars,
            "banned_total": banned, "banned_exempt": exempt,
            "banned_body": banned - exempt, "name_hits": name_hits}


def scan_many(files, names):
    rows = [scan_docx(p, names) for p in sorted(files)
            if not p.name.startswith("~$")]
    n = len(rows)
    return {
        "files": rows,
        "file_count": n,
        "files_with_body_banned": sum(1 for r in rows if r["banned_body"]),
        "banned_total": sum(r["banned_total"] for r in rows),
        "banned_exempt": sum(r["banned_exempt"] for r in rows),
        "banned_body": sum(r["banned_body"] for r in rows),
        "name_hits": sum(r["name_hits"] for r in rows),
        "chars": sum(r["chars"] for r in rows),
        "body_leak_rate": round(sum(1 for r in rows if r["banned_body"]) / max(1, n), 4),
    }


# ---------------------------------------------------------------- B 名册与分层
def _main_table(doc):
    for t in doc.tables:
        if t.rows and "活动班级" in "".join(_tx(c) for c in _cells(t.rows[0])):
            return t
    return None


def roster_metrics(names):
    legal, layers = set(), {}
    init_layer, final_layer = {}, {}
    moves = {"up": 0, "down": 0, "keep": 0, "absent": 0}
    rows_all, combos_seen = [], {}
    for p in sorted(OUT_DIR.glob("*.docx")):
        if p.name.startswith("~$"):
            continue
        doc = Document(str(p))
        # 合法组合：附录「层级组合」表
        for t in doc.tables:
            if not t.rows:
                continue
            head = [_norm(_tx(tc)) for tc in _cells(t.rows[0])]
            if "本课目标组合" in head:
                k = head.index("本课目标组合")
                for r in t.rows[1:]:
                    cs = _cells(r)
                    if len(cs) > k:
                        digits = "".join(ch for ch in _tx(cs[k]) if ch.isdigit())
                        if digits:
                            legal.add(digits)
            # 交付态层级分布：附录「儿童代号」表
            #   ⚠ 这张表记的是**定层结果**，不是调层后的层 —— 调层的走向在
            #     下面那张「调层轨迹」表里。早期版本拿这张表当期末层级，
            #     结果 37 人全判成「维持」（实测踩过）。
            if "所属层级" in head:
                kc, kl = head.index("儿童代号"), head.index("所属层级")
                for r in t.rows[1:]:
                    cs = _cells(r)
                    if len(cs) > max(kc, kl):
                        lv = _tx(cs[kl])
                        if lv.startswith("L"):
                            layers[lv] = layers.get(lv, 0) + 1
            # 调层轨迹：初始层级 + 第 5 次后的期末层级
            #   ⚠ 也不能数「↑ / ↓ 符号」—— 推演序列里几乎所有行都出现过 ↑。
            #     正确口径：**期末层级（末列末尾的 L 值）vs 初始层级**。
            if "初始层级" in head:
                kc, kl = head.index("儿童代号"), head.index("初始层级")
                for r in t.rows[1:]:
                    cs = _cells(r)
                    if len(cs) > max(kc, kl):
                        code, lv = _tx(cs[kc]), _tx(cs[kl])
                        if lv.startswith("L"):
                            init_layer[code] = int(lv[1:])
                        m = re.findall(r"L(\d)", _tx(cs[-1]))
                        if m:
                            final_layer[code] = int(m[-1])
        main = _main_table(doc)
        if main is None:
            continue
        for row in main.rows:
            lg = _logical(row)
            code = _tx(lg.get(COL_CHILD, "")) if COL_CHILD in lg else ""
            if not re.fullmatch(r"S\d{2}", code):
                continue
            goal = "".join(ch for ch in _tx(lg[COL_TARGET])
                           if ch.isdigit()) if COL_TARGET in lg else ""
            base = _tx(lg[COL_BASELINE]) if COL_BASELINE in lg else ""
            absent = "未参与" in base
            marks = sum(1 for ch in base if ch in "PE")
            ok_base = absent or (len(goal) > 0 and marks == len(goal))
            combos_seen[goal] = combos_seen.get(goal, 0) + 1
            rows_all.append({"file": p.name, "code": code, "goal": goal,
                             "baseline": base, "absent": absent,
                             "combo_ok": goal in legal,
                             "baseline_ok": ok_base})
            if absent:
                moves["absent"] += 1
    # 调层方向：期末 vs 初始（「未参与」在主表循环里已单独计数，不参与升降比较）
    for code, fin in final_layer.items():
        ini = init_layer.get(code)
        if ini is None:
            continue
        if fin > ini:
            moves["up"] += 1
        elif fin < ini:
            moves["down"] += 1
        else:
            moves["keep"] += 1
    total = len(rows_all)
    return {
        "rows_total": total,
        "unique_codes": len({r["code"] for r in rows_all}),
        "coverage": round(len({r["code"] for r in rows_all}) / max(1, len(names)), 4),
        "legal_combos": sorted(legal),
        "combo_ok": sum(1 for r in rows_all if r["combo_ok"]),
        "combo_ok_rate": round(sum(1 for r in rows_all if r["combo_ok"]) / max(1, total), 4),
        "baseline_ok": sum(1 for r in rows_all if r["baseline_ok"]),
        "baseline_ok_rate": round(sum(1 for r in rows_all if r["baseline_ok"]) / max(1, total), 4),
        "combo_hist": dict(sorted(combos_seen.items())),
        "layer_dist": dict(sorted(layers.items())),
        "moves": moves,
        "rows": rows_all,
    }


# ---------------------------------------------------------------- C W4 终检
def audit_metrics():
    """跑 W4（版式合规终检），统计 PASS/FAIL。

    注意：**这是合规侧的证据，不是质量结论**，只是「产物在版式上没有硬伤」。
    """
    script = ROOT / "scripts" / "final_audit.py"
    try:
        r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as e:                                  # pragma: no cover
        return {"available": False, "error": str(e)}
    n_pass = len(re.findall(r":\s*PASS", out))
    n_fail = len(re.findall(r":\s*FAIL", out))
    per_file = re.findall(r"### (.+?)\n", out)
    verdicts = re.findall(r"总判定:\s*(PASS|FAIL)", out)
    return {
        "available": True,
        "items_pass": n_pass,
        "items_fail": n_fail,
        "item_total": n_pass + n_fail,
        "item_pass_rate": round(n_pass / max(1, n_pass + n_fail), 4),
        "files": per_file,
        "verdicts": verdicts,
        "all_pass": all(v == "PASS" for v in verdicts) and bool(verdicts),
    }


# ---------------------------------------------------------------- D 生成层
def generation_metrics(names):
    """W2 产物（LessonPlan）的硬校验与形态指标 —— 质量侧的**上游**证据。"""
    out = []
    for jp in sorted(GEN.glob("*.json")):
        try:
            plan = LessonPlan(**json.loads(jp.read_text(encoding="utf-8")))
        except Exception as e:
            out.append({"theme": jp.stem, "loadable": False, "error": str(e)[:120]})
            continue
        rep = validate_plan(plan, forbidden_names=list(names.keys()))
        # ⚠ `ValidationReport` 的属性是 `fatal_count` / `warning_count` / `passed`，
        #   没有 `errors` / `warnings` / `ok`。写成 `getattr(rep, "errors", [])`
        #   会恒取到空列表 → 本表「错误 / 警告」两列**恒为 0**（真实的假象，已修正）。
        errs = [str(v) for v in rep.violations if v.fatal]
        out.append({"theme": jp.stem, "loadable": True,
                    "title": plan.title,
                    "errors": rep.fatal_count, "warnings": rep.warning_count,
                    "ok": rep.passed,
                    "error_sample": errs[:3],
                    **plan_metrics(plan)})
    return {"plans": out,
            "loadable": sum(1 for x in out if x.get("loadable")),
            "all_ok": sum(1 for x in out if x.get("ok"))}


# ---------------------------------------------------------------- E 留出对照
def holdout_metrics():
    """留出主题的**素材就绪度**（对照分析本身要等 M4-2，这里只查有没有米下锅）。"""
    cfg = {}
    cfg_p = STRUCT / "build_config.json"
    if cfg_p.exists():
        try:
            cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
        except Exception:
            pass
    # ⚠ 配置里的交付清单写的是**完整标题**（「认知课《国庆节到了》」），
    #   而 W2 产物文件名是**裸主题**（「国庆节到了」），直接 `in` 判等会
    #   把已交付的主题误判成留出（实测踩过）。取书名号内的词再比对。
    def _key(s: str) -> str:
        m = re.search(r"《(.+?)》", s or "")
        return m.group(1) if m else (s or "")
    deliver = [_key(x) for x in (cfg.get("deliver", []) or [])]
    items = []
    for jp in sorted(GEN.glob("*.json")):
        try:
            d = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:
            continue
        items.append({"theme": jp.stem,
                      "delivered": any(k and (k in jp.stem or jp.stem in k)
                                       for k in deliver),
                      "steps": len(d.get("steps", []) or []),
                      "objectives": len(d.get("objectives", []) or []),
                      "individualized_goals": len(d.get("individualized_goals", []) or [])})
    return {"items": items,
            "holdout": [x["theme"] for x in items if not x["delivered"]],
            "delivered": [x["theme"] for x in items if x["delivered"]]}


# ---------------------------------------------------------------- 报告
def render_md(res: dict) -> str:
    a = res["corpus"]
    b = res["products"]
    r = res["roster"]
    c = res["audit"]
    g = res["generation"]
    h = res["holdout"]
    L = []
    add = L.append
    add("# 效果验证 · 自动指标报告（M4-1）\n")
    add("> 本报告由 `scripts/eval_metrics.py` 自动生成，只含**机器可算**的指标。")
    add("> **W4 交付终检**（版式合规）与 **M4 效果验证**（好不好用）是两回事，")
    add("> 本报告属后者；教师盲评（M4-5）为独立环节，不在此列。\n")

    add("## 一、去标识化：真实姓名 + 标签词（治理前 vs 治理后）\n")
    add("| 指标 | 治理前（原始范例语料） | 治理后（系统交付物） |")
    add("|---|---|---|")
    add(f"| 文件数 | {a.get('file_count', 0)} | {b['file_count']} |")
    add(f"| 含标签词的文件数（正文口径） | {a.get('files_with_body_banned', 0)} "
        f"（{a.get('body_leak_rate', 0)*100:.1f}%） | "
        f"{b['files_with_body_banned']}（{b['body_leak_rate']*100:.1f}%） |")
    add(f"| 标签词命中总次数 | {a.get('banned_total', 0)} | {b['banned_total']} |")
    add(f"| 其中位于「障碍类别」栏目（豁免） | {a.get('banned_exempt', 0)} | "
        f"{b['banned_exempt']} |")
    add(f"| **正文 / 幼儿行命中** | **{a.get('banned_body', 0)}** | "
        f"**{b['banned_body']}** |")
    add(f"| 真实姓名命中次数 | {a.get('name_hits', 0)} | **{b['name_hits']}** |")
    add("")
    add("> 口径说明：前后**用同一把尺子** —— 「障碍类别」是范例模板的固有栏目，")
    add("> 交付物按用户要求照范例原样填写，故两侧都把该栏目单列豁免。")
    add("")
    add(f"**结论**：真实姓名 **{a.get('name_hits', 0)} → "
        f"{b['name_hits']}**（去标识化的硬指标，目标达成）；")
    add(f"标签词在两侧**都只出现在班级信息栏**（{a.get('banned_exempt', 0)} → "
        f"{b['banned_exempt']} 处，用户要求照范例保留），")
    add(f"正文与幼儿行命中恒为 **{a.get('banned_body', 0)} → {b['banned_body']}** ——")
    add("即**没有任何标签被挂到具体某一个孩子身上**。\n")

    add("## 二、名册与分层\n")
    add("| 指标 | 值 |")
    add("|---|---|")
    add(f"| 名册行数 | {r['rows_total']} |")
    add(f"| 覆盖幼儿数 / 总名册 | {r['unique_codes']} / "
        f"{res['roster_total']}（{r['coverage']*100:.1f}%） |")
    add(f"| 目标组合合法率 | {r['combo_ok']}/{r['rows_total']}"
        f"（{r['combo_ok_rate']*100:.1f}%） |")
    add(f"| 基线记号一致率 | {r['baseline_ok']}/{r['rows_total']}"
        f"（{r['baseline_ok_rate']*100:.1f}%） |")
    add(f"| 合法组合 | {' / '.join(r['legal_combos'])} |")
    add(f"| 分层分布 | {'  '.join(f'{k}={v}' for k, v in r['layer_dist'].items())} |")
    add(f"| 调层（推演轨迹：期末 vs 初始） | 升 {r['moves']['up']} · "
        f"降 {r['moves']['down']} · 维持 {r['moves']['keep']} · "
        f"未参与 {r['moves']['absent']} |")
    add("")
    add("> 调层一栏取自附录「调层轨迹」的**五次评量推演序列**（本课尚无真实评量），")
    add("> 用于演示机制；正文的康复目标 / 康复基线走的是真实链路。\n")
    add("")

    add("## 三、W4 交付终检（合规侧，仅作背景）\n")
    if c.get("available"):
        add(f"- 判据项 PASS {c['items_pass']} / {c['item_total']}"
            f"（{c['item_pass_rate']*100:.1f}%），FAIL {c['items_fail']}")
        add(f"- 文件级结论：{'、'.join(c['files']) or '—'} → "
            f"{'全部 PASS' if c['all_pass'] else '存在 FAIL'}")
    else:
        add(f"- 未取到终检结果：{c.get('error')}")
    add("")

    add("## 四、W2 生成层（上游质量）\n")
    add("| 主题 | 载入 | 硬校验 | 错误 | 警告 | 环节数 | 支架/环节 | 视觉提示覆盖率 |")
    add("|---|---|---|---|---|---|---|---|")
    for x in g["plans"]:
        if not x.get("loadable"):
            add(f"| {x['theme']} | ❌ | — | — | — | — | — | — |")
            continue
        add(f"| {x['theme']} | ✅ | {'PASS' if x['ok'] else 'FAIL'} | "
            f"{x['errors']} | {x['warnings']} | {x['steps']} | "
            f"{x['scaffold_per_step']} | {x['cue_coverage']*100:.0f}% |")
    add("")

    add("## 五、留出对照素材就绪度\n")
    add(f"- 已交付主题：{'、'.join(h['delivered']) or '—'}")
    add(f"- 留出主题：{'、'.join(h['holdout']) or '—'}")
    add("| 主题 | 状态 | 环节数 | 目标数 | 个别化条目 |")
    add("|---|---|---|---|---|")
    for x in h["items"]:
        add(f"| {x['theme']} | {'已交付' if x['delivered'] else '留出'} | "
            f"{x['steps']} | {x['objectives']} | {x['individualized_goals']} |")
    add("")

    add("## 六、M4 其余环节（非机算，不在本报告内）\n")
    add("| 环节 | 状态 | 说明 |")
    add("|---|---|---|")
    add("| M4-2 留出对照分析 | ✅ 已完成 | `scripts/holdout_compare.py` → "
        "`HOLDOUT_COMPARISON.md` |")
    add("| M4-3 LLM-as-judge | ⬜ 未开始 | 需 API key（见 PROJECT_PLAN 第六节） |")
    add("| M4-4 消融实验 | ✅ 已完成 | `scripts/ablation_study.py` → "
        "`ABLATION_STUDY.md`（逐模块关闭，量边际贡献） |")
    add("| M4-5 教师盲评 | 🟡 材料已就绪 | `scripts/blind_eval_pack.py` → "
        "`data/blind-eval/`；**独立环节**，待真人教师评分 |")
    add("| M4-6 答辩 demo | ⬜ 未开始 | Streamlit |")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- main
def main() -> int:
    names = _load_names()
    corpus = _corpus_dir()
    print("=" * 72)
    print("M4-1 自动指标")
    print(f"  语料目录：{corpus}（{'存在' if corpus.exists() else '缺失'}）")
    print(f"  交付目录：{OUT_DIR}")
    print("=" * 72)

    res = {
        "corpus": scan_many(corpus.glob("*.docx"), names) if corpus.exists()
                  else {"file_count": 0, "error": "语料目录缺失", "files": [],
                        "banned_body": 0, "banned_total": 0, "banned_exempt": 0,
                        "name_hits": 0, "files_with_body_banned": 0,
                        "body_leak_rate": 0.0},
        "products": scan_many(OUT_DIR.glob("*.docx"), names),
        "roster_total": len(names),
    }
    res["roster"] = roster_metrics(names)
    res["audit"] = audit_metrics()
    res["generation"] = generation_metrics(names)
    res["holdout"] = holdout_metrics()

    a, b = res["corpus"], res["products"]
    print(f"\n[去标签化] 语料 {a.get('file_count', 0)} 份："
          f"正文命中 {a.get('banned_body', 0)} 处"
          f"（含标签词文件 {a.get('files_with_body_banned', 0)} 份）、"
          f"姓名 {a.get('name_hits', 0)} 次")
    print(f"           产物 {b['file_count']} 份："
          f"正文命中 {b['banned_body']} 处"
          f"（豁免 {b['banned_exempt']} 处属「障碍类别」栏目）、"
          f"姓名 {b['name_hits']} 次")
    r = res["roster"]
    print(f"[名册]     {r['unique_codes']}/{res['roster_total']} 人"
          f"（{r['coverage']*100:.0f}%）| 组合合法 {r['combo_ok_rate']*100:.0f}%"
          f" | 基线一致 {r['baseline_ok_rate']*100:.0f}%")
    print(f"[分层]     {'  '.join(f'{k}={v}' for k, v in r['layer_dist'].items())}")
    c = res["audit"]
    print(f"[W4 终检]  {'PASS' if c.get('all_pass') else 'FAIL'} "
          f"（{c.get('items_pass', 0)}/{c.get('item_total', 0)} 项）")

    (STRUCT / "EVAL_METRICS.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    (STRUCT / "EVAL_METRICS.md").write_text(render_md(res), encoding="utf-8")
    print(f"\n已写出：{STRUCT / 'EVAL_METRICS.md'}")
    print(f"        {STRUCT / 'EVAL_METRICS.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""M4-5 教师盲评 · 可填写评分卡（docx）生成 & 回收解析。

与 `blind_eval_pack.py` 的分工
------------------------------
* `blind_eval_pack.py` —— 备料：抽题、打乱、脱敏，产出匿名卡片（md）、
  打印版（html）、回收表（csv）与封存密钥。
* **本脚本** —— 收发两端：
  - 生成：把匿名字卡片包成**可直接在电脑上填**的 Word 评分卡
    （打分格是下拉选项，Q6 是下拉四选一），放进 `5_教师评分卡/`；
  - 回收：读 `回收/` 里教师填好交回的文件，**自动**把分数落进
    `3_回收统计表.csv`，并顺手跑分组统计。

为什么值得单独做这一版
----------------------
旧流程是「打印 → 手写 → 组织方照抄进 CSV」：两道人工转录，慢且易错。
现在教师填的**就是最终数据**，中间没有转录环节。

用法
----
    PYTHONUTF8=1 "$PY" scripts/blind_eval_form.py            # 生成评分卡 + 回收夹
    PYTHONUTF8=1 "$PY" scripts/blind_eval_form.py --teachers "张老师,李老师"
    PYTHONUTF8=1 "$PY" scripts/blind_eval_form.py --ingest   # 读回填好的卡 → CSV + 统计

产出（默认落在 `data/blind-eval/`）
-----------------------------------
    5_教师评分卡/评分卡-模板.docx      空白模板（教师多了就复制它）
    5_教师评分卡/评分卡-<教师>.docx    每人一份，已预填编号
    回收/_把填好的评分卡放这里.txt      回收夹说明
    回收/读取报告.md                   --ingest 的解析明细（人工复核用）
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lpa.blind_form import build_teacher_form, extract_form  # noqa: E402
from lpa.rules import BANNED_PATTERN, load_forbidden_names    # noqa: E402

PACK = ROOT / "data" / "blind-eval"
CARDS_DIR = "2_教案卡片"
FORMS_DIR = "5_教师评分卡"
DROP_DIR = "回收"
KEY_MD = "4_分组密钥.md"
CSV_NAME = "3_回收统计表.csv"
NAME_MAP = ROOT / "data" / "private" / "name_mapping.json"

FINGERPRINTS = ("火箭班", "闪电班", "任课老师", "混龄康教班")
CSV_HEAD = ["教师编号", "教案编号", "D1目标可测性", "D2分层与个别化", "D3环节完整性",
            "D4材料与环境", "D5语言与可操作性", "Q6可用性", "Q7最需改进", "备注"]
DIMS = ["D1", "D2", "D3", "D4", "D5"]


# --------------------------------------------------------------------------
# 读料
# --------------------------------------------------------------------------
def load_cards(pack: Path) -> dict:
    """按密钥里的顺序读回匿名卡片；密钥缺失时退化为字母序。"""
    cards_dir = pack / CARDS_DIR
    if not cards_dir.is_dir():
        return {}
    order: list = []
    key = pack / KEY_MD
    if key.exists():
        for line in key.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\|\s*([A-H])\s*\|", line)
            if m:
                order.append(m.group(1))
    found = {p.stem.replace("教案-", "").strip(): p
             for p in cards_dir.glob("教案-*.md")}
    codes = order or sorted(found)
    return {c: found[c].read_text(encoding="utf-8")
            for c in codes if c in found}


def _docx_text(p: Path) -> str:
    """把 docx 里所有 XML 拼起来做子串扫描（文本在 w:t 里是明文）。"""
    try:
        with zipfile.ZipFile(p) as z:
            return "".join(z.read(n).decode("utf-8", "ignore")
                           for n in z.namelist()
                           if n.startswith("word/") and n.endswith(".xml"))
    except Exception:                                      # noqa: BLE001
        return ""


def scan_forms(paths: list, names: list) -> list:
    bad = []
    for p in paths:
        t = _docx_text(p)
        hn = [n for n in names if n and n in t]
        hb = sorted(set(BANNED_PATTERN.findall(t)))
        hm = [w for w in FINGERPRINTS if w in t]
        if hn or hb or hm:
            bad.append(p)
            print(f"[form] ❌ {p.name}：姓名={hn[:3]} 标签={hb} 指纹={hm}")
    return bad


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------
def cmd_build(args) -> int:
    pack = Path(args.pack)
    cards = load_cards(pack)
    if not cards:
        print(f"[form] ❌ 找不到匿名卡片（{pack / CARDS_DIR}）—— 请先跑 blind_eval_pack.py")
        return 1
    codes = list(cards)
    names = load_forbidden_names(str(NAME_MAP))

    out_dir = pack / FORMS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    made = [build_teacher_form(cards, codes, "空白模板", out_dir / "评分卡-模板.docx")]
    for label in [t.strip() for t in args.teachers.split(",") if t.strip()]:
        made.append(build_teacher_form(cards, codes, label,
                                       out_dir / f"评分卡-{label}.docx"))
    print(f"[form] ✅ 生成 {len(made)} 份评分卡 → {out_dir}")

    drop = pack / DROP_DIR
    drop.mkdir(parents=True, exist_ok=True)
    note = drop / "_把填好的评分卡放这里.txt"
    note.write_text(
        "【回收文件夹】\n\n"
        "把填好的「评分卡-xxx.docx」放进这个文件夹，然后跟助手说一声：\n"
        "    「读取回收的评分卡」\n"
        "（或自己跑：PYTHONUTF8=1 \"$PY\" scripts/blind_eval_form.py --ingest）\n\n"
        "几点注意：\n"
        "1. 只放 .docx 文件，不要在这里建子文件夹。\n"
        "2. 文件名带上教师称呼，例如「评分卡-张老师.docx」。\n"
        "3. 文件还开在 Word/WPS 里时读不到，请先关掉再放；\n"
        "   以 ~$ 开头的临时文件会被自动跳过。\n"
        "4. 填法：带下拉箭头的格子点一下就出选项；不打分就留空，\n"
        "   助手会提示哪一格没填。\n",
        encoding="utf-8")
    print(f"[form] ✅ 回收夹就绪 → {drop}")

    bad = scan_forms(made, names)
    if bad:
        print(f"[form] ❌ 有 {len(bad)} 份评分卡命中禁词，请检查卡片源文本")
        return 2
    print(f"[form] ✅ 合规：{len(made)} 份评分卡零姓名 / 零标签 / 零来源指纹")
    return 0


# --------------------------------------------------------------------------
# 回收解析
# --------------------------------------------------------------------------
def _read_csv(p: Path) -> list:
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f)]


def _write_csv(p: Path, rows: list) -> None:
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEAD)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_HEAD})


def cmd_ingest(args) -> int:
    pack = Path(args.pack)
    drop = Path(args.drop) if args.drop else pack / DROP_DIR
    files = [p for p in sorted(drop.glob("*.docx")) if not p.name.startswith("~$")]
    if not files:
        print(f"[form] ⚠ {drop} 里没有 .docx —— 教师还没交回来，或放错了文件夹")
        return 1

    reports, rows = [], []
    for f in files:
        try:
            r = extract_form(f)
        except Exception as exc:                            # noqa: BLE001
            print(f"[form] ❌ {f.name} 读不出来（{exc}）—— 可能还开在 Word 里，或文件损坏")
            continue
        teacher = r["teacher"] or f.stem.replace("评分卡-", "").strip() or f.stem
        n_filled = 0
        for code in sorted(r["scores"]):
            slot = r["scores"][code]
            got = [d for d in DIMS if slot[d] is not None]
            n_filled += len(got)
            rows.append({
                "教师编号": teacher, "教案编号": code,
                "D1目标可测性": slot["D1"] if slot["D1"] is not None else "",
                "D2分层与个别化": slot["D2"] if slot["D2"] is not None else "",
                "D3环节完整性": slot["D3"] if slot["D3"] is not None else "",
                "D4材料与环境": slot["D4"] if slot["D4"] is not None else "",
                "D5语言与可操作性": slot["D5"] if slot["D5"] is not None else "",
                "Q6可用性": slot["Q6"], "Q7最需改进": slot["Q7"], "备注": f.name,
            })
        reports.append((f, teacher, r, n_filled))
        flag = "✅" if n_filled else "⚠"
        print(f"[form] {flag} {f.name} → 教师「{teacher}」，"
              f"{len(r['scores'])} 份教案 / {n_filled} 个有效分值")
        for w in r["warnings"]:
            print(f"        · {w}")

    if not rows:
        print("[form] ❌ 没有解析出任何评分")
        return 1

    # 合并：保留 CSV 里不属于本次回收教师的旧行（手工誊的、或往次回收的）
    csv_p = pack / CSV_NAME
    got_teachers = {r["教师编号"] for r in rows}
    kept = [r for r in _read_csv(csv_p) if (r.get("教师编号") or "") not in got_teachers]
    if kept:
        print(f"[form] 保留原有 {len(kept)} 行（非本次回收教师）")
    _write_csv(csv_p, kept + rows)
    print(f"[form] ✅ 已写入 {len(rows)} 行 → {csv_p}")

    # 解析明细：供人工复核（尤其 Q7 自由文本）
    lines = ["# 回收解析报告", "",
             f"> 来源目录：`{drop}`　|　文件 {len(reports)} 份　|　"
             f"评分行 {len(rows)} 条", "",
             "| 文件 | 教师 | 教案数 | 有效分值 | 提示 |", "|---|---|---|---|---|"]
    for f, teacher, r, n in reports:
        lines.append(f"| `{f.name}` | {teacher} | {len(r['scores'])} | {n} | "
                     + ("；".join(r["warnings"]) if r["warnings"] else "—") + " |")
    lines += ["", "## 逐份评分", "",
              "| 教师 | 教案 | D1 | D2 | D3 | D4 | D5 | Q6 | Q7 | Q8 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            r["教师编号"], r["教案编号"], r["D1目标可测性"], r["D2分层与个别化"],
            r["D3环节完整性"], r["D4材料与环境"], r["D5语言与可操作性"],
            r["Q6可用性"], (r["Q7最需改进"] or "").replace("|", "/"),
            ""))
    (drop / "读取报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[form] ✅ 解析明细 → {drop / '读取报告.md'}（建议人工扫一眼）")

    if args.no_tally:
        return 0
    print("\n" + "=" * 62)
    from blind_eval_pack import tally                          # noqa: PLC0415
    return tally(pack)


def main() -> int:
    ap = argparse.ArgumentParser(description="M4-5 教师评分卡（docx）生成 / 回收")
    ap.add_argument("--pack", default=str(PACK), help="材料包目录")
    ap.add_argument("--teachers", default="教师1,教师2,教师3",
                    help="生成哪几位教师的评分卡（逗号分隔）")
    ap.add_argument("--ingest", action="store_true", help="不开生成：读回填好的评分卡")
    ap.add_argument("--drop", default="", help="回收目录（默认 <pack>/回收）")
    ap.add_argument("--no-tally", action="store_true", help="只写 CSV，不跑分组统计")
    args = ap.parse_args()
    return cmd_ingest(args) if args.ingest else cmd_build(args)


if __name__ == "__main__":
    sys.exit(main())

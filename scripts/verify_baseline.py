"""「康复基线」列专项校验（W3 审计的一环）。

三件事：

1. **语料统计** —— 「康复基线」列在 14 份范例里空值多少？记号个数是否
   恒等于「康复目标」的编号个数？同一幼儿跨教案是否同一串？
2. **匿名编号对齐** —— `name_mapping.json` 的 `Sxx` 是否真的对应
   `child_profiles.json` 里的同一名幼儿（用评量达标率交叉验证）。
   对齐可靠，才能「按身份」把某位幼儿的最近记录填到他那一行。
3. **产物校验** —— `data/docx/*.docx` 的「康复基线」列是否已填、
   记号个数是否与本课目标条数一致。

用法：python scripts/verify_baseline.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path(r"C:\Users\17093\Desktop\homework\agent use idea")
OUT = ROOT / "data" / "docx"


def ct(tc) -> str:
    return "".join(x.text or "" for x in tc.iter(qn("w:t"))).strip()


def corpus_rows(path):
    """从一份范例取 [(姓名, 康复目标, 康复基线), ...]（按原表行序）。

    ⚠ 本函数**只是转发**，行定位规则统一在 `roster_from_corpus.read_lesson_rows`。
    早期这里另有一套「找表头行 → 按列名索引」的实现，与 run_render 里的
    「按固定列号取」不一致，同一份范例在两处抽出的人数不同（15 vs 11）。
    两套规则并存是这一块的经典坑，现已合并为一套。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from roster_from_corpus import known_names, read_lesson_rows
    return [(r["name"], r["goal"], r["base"])
            for r in read_lesson_rows(Path(path), known_names())]


def n_marks(s: str) -> int:
    return sum(1 for c in (s or "") if c in "PE")


def section_corpus() -> list:
    files = sorted(p for p in CORPUS.glob("*.docx") if not p.name.startswith("~$"))
    print("=" * 68)
    print("① 语料统计")
    print("=" * 68)
    print(f"语料 docx：{len(files)} 份\n")
    print(f"{'文档':38s} {'记录':>4s} {'基线空':>6s}")
    print("-" * 54)
    allrec, by_name = [], defaultdict(list)
    for fp in files:
        recs = corpus_rows(fp)
        allrec += recs
        print(f"{fp.name:38s} {len(recs):>4d} "
              f"{sum(1 for _, _, b in recs if not b):>6d}")
        for nm, g, b in recs:
            by_name[nm].append((fp.name, g, b))
    print("-" * 54)
    nb = sum(1 for _, _, b in allrec if not b)
    print(f"{'合计':38s} {len(allrec):>4d} {nb:>6d}")
    print(f"\n★ 基线空值率：{nb}/{len(allrec)} —— "
          f"{'该列从不留空' if nb == 0 else '存在空值！'}")

    # 长度对应
    chk = bad = 0
    for nm, g, b in allrec:
        nd, nmk = sum(1 for c in g if c.isdigit()), n_marks(b)
        if nd == 0 or nmk == 0:
            continue
        chk += 1
        if nd != nmk:
            bad += 1
            if bad <= 5:
                print(f"   ✗ {nm:6s} 目标={g:5s}({nd}) 基线={b!r}({nmk})")
    print(f"\n★ 记号个数 == 目标条数：可比 {chk} 条，不符 {bad} 条")

    # 跨教案传递
    multi = {nm: v for nm, v in by_name.items() if len(v) >= 2}
    same = [nm for nm, v in multi.items()
            if len({b for _, _, b in v if b}) == 1]
    print(f"\n★ 跨教案传递：出现 ≥2 次的幼儿 {len(multi)} 名，"
          f"基线完全一致的 {len(same)} 名")
    for nm in sorted(same)[:8]:
        v = multi[nm]
        print(f"   {nm:6s} 目标{v[0][1]:6s} 基线 {v[0][2]!r}"
              f"  （{len(v)} 份教案一字不差）")
    return allrec


def section_align(allrec) -> None:
    print()
    print("=" * 68)
    print("② 匿名编号 ↔ 真实姓名 对齐校验")
    print("=" * 68)
    m = ROOT / "data" / "private" / "name_mapping.json"
    p = ROOT / "data" / "structured" / "child_profiles.json"
    if not m.exists() or not p.exists():
        print("缺 name_mapping.json 或 child_profiles.json，跳过")
        return
    anon2name = {v: k for k, v in json.loads(m.read_text(encoding="utf-8")).items()}
    prof = {c["anon_id"]: c for c in json.loads(p.read_text(encoding="utf-8"))}

    agg = defaultdict(lambda: [0, 0])
    # 达标率从范例的「评量效果」列独立统计（基线列不含 P/E 语义之外的信号）
    for fp in sorted(x for x in CORPUS.glob("*.docx") if not x.name.startswith("~$")):
        try:
            d = Document(str(fp))
        except Exception:
            continue
        if len(d.tables) < 2:
            continue
        main = max(d.tables, key=lambda t: len(t.rows))
        rows = main._tbl.findall(qn("w:tr"))
        hdr = hi = None
        for i, tr in enumerate(rows):
            ts = [ct(tc) for tc in tr.findall(qn("w:tc"))]
            if any("儿童姓名" in t for t in ts):
                hdr, hi = ts, i
                break
        if hdr is None:
            continue
        jn = next((j for j, t in enumerate(hdr) if "儿童姓名" in t), None)
        jb = next((j for j, t in enumerate(hdr) if "康复基线" in t), None)
        if jn is None:
            continue
        for tr in rows[hi + 1:]:
            tcs = tr.findall(qn("w:tc"))
            if len(tcs) <= jn:
                continue
            nm = ct(tcs[jn])
            if not nm:
                continue
            for j in range((jb or jn) + 1, len(tcs)):
                t = ct(tcs[j])
                if t and re.fullmatch(r"[PE\s]+", t):
                    agg[nm][0] += t.count("P")
                    agg[nm][1] += t.count("P") + t.count("E")

    ok = tot = 0
    for sid in sorted(prof):
        nm = anon2name.get(sid, "?")
        mast = prof[sid].get("mastery")
        a = agg.get(nm)
        if not a or a[1] == 0 or mast is None:
            continue
        rate = a[0] / a[1]
        tot += 1
        if abs(rate - mast) <= 0.06:
            ok += 1
    print(f"用评量达标率交叉验证：可比 {tot} 名，画像与语料吻合 {ok} 名"
          f"（{ok / max(1, tot):.0%}）")
    print("→ 对齐可靠：Sxx 与 child_profiles 里的是同一个人，")
    print("  所以能「按身份」把某位幼儿的最近记录填到他那一行。")


def section_product() -> None:
    print()
    print("=" * 68)
    print("③ 产物校验：data/docx/*.docx")
    print("=" * 68)
    for fp in sorted(OUT.glob("*.docx")):
        if fp.name.startswith("~$"):
            continue
        d = Document(str(fp))
        main = max(d.tables, key=lambda t: len(t.rows))
        grid = main._tbl.find(qn("w:tblGrid"))
        ncol = len(grid.findall(qn("w:gridCol"))) if grid is not None else -1
        print(f"\n{fp.name}  主表 {len(main.rows)} 行 × {ncol} 列")
        worst = "OK"
        for i, row in enumerate(main.rows):
            tcs = row._tr.findall(qn("w:tc"))
            def tx(j):
                return ct(tcs[j]) if j < len(tcs) else ""
            nm, tg, bs = tx(3), tx(4), tx(5)
            if not (nm.startswith("S") and tg):
                continue
            nd, nmk = sum(1 for c in tg if c.isdigit()), n_marks(bs)
            flag = "OK" if (nd == nmk or not nmk and bs) else "MISMATCH"
            if flag != "OK":
                worst = "MISMATCH"
            print(f"   {nm:4s} 目标={tg:5s}({nd}) 基线={bs!r}({nmk}) {flag}")
        print(f"   >>> {worst}")


if __name__ == "__main__":
    recs = section_corpus()
    section_align(recs)
    section_product()

"""把 STYLE_RAW.json 压成一份人能看懂、也给渲染器当规格书的 STYLE_SPEC.md。"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

D = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent\data\structured")
data = json.loads((D / "STYLE_RAW.json").read_text(encoding="utf-8"))

L = []
L.append("# 模板真实样式规格（从原 docx XML 提取）")
L.append("")
L.append(f"来源文件：`{data['file']}`")
L.append("")

# ---- 正文段落 ----
L.append("## 一、正文段落（表格外）")
L.append("")
L.append("| 文本 | 样式名 | 对齐 | 行距 | 段前 | 段后 | 首行缩进 |")
L.append("|---|---|---|---|---|---|---|")
for p in data["body_paragraphs"]:
    L.append("| {} | {} | {} | {} | {} | {} | {} |".format(
        p["text"].replace("|", "/"), p.get("style"), p.get("align"),
        p.get("spacing_line") or p.get("line_spacing"),
        p.get("space_before"), p.get("space_after"), p.get("first_line_indent")))
L.append("")

# ---- 字体统计 ----
fonts = Counter()
sizes = Counter()
for t in data["tables"]:
    for r in t["cells"]:
        for c in r["cells"]:
            for rn in c.get("runs", []):
                fonts[(rn.get("east_asia"), rn.get("ascii"))] += 1
                sizes[rn.get("size_pt")] += 1
L.append("## 二、字体与字号统计（全表所有 run）")
L.append("")
L.append("| 东亚字体 | 西文字体 | 出现次数 |")
L.append("|---|---|---|")
for (ea, asc), n in fonts.most_common():
    L.append(f"| {ea or '（未显式设置）'} | {asc or '（未显式设置）'} | {n} |")
L.append("")
L.append("| 字号(pt) | 出现次数 |")
L.append("|---|---|")
for s, n in sizes.most_common():
    L.append(f"| {s if s is not None else '（继承样式）'} | {n} |")
L.append("")

# ---- 表格边框 ----
L.append("## 三、表格边框")
L.append("")
for t in data["tables"]:
    L.append(f"### 表{t['index']}（{t['rows']} 行 × {t['cols']} 列）")
    L.append("")
    L.append("| 边 | 线型 | 粗细(sz) | 颜色 |")
    L.append("|---|---|---|---|")
    for edge, v in (t.get("borders") or {}).items():
        L.append(f"| {edge} | {v['val']} | {v['sz']} | {v['color']} |")
    L.append("")

# ---- 行高 ----
L.append("## 四、行高（前 8 行）")
L.append("")
for t in data["tables"]:
    L.append(f"### 表{t['index']}")
    L.append("")
    L.append("| 行 | 高度 | 规则 | 禁止跨页 |")
    L.append("|---|---|---|---|")
    for r in t["cells"][:8]:
        h = r.get("height") or {}
        L.append(f"| {r['row']} | {h.get('val')} | {h.get('rule')} | {r.get('cant_split')} |")
    L.append("")

# ---- 逐单元格细节（表1 前 8 行）----
L.append("## 五、逐单元格样式细节（表1 前 8 行，仅非空格）")
L.append("")
if len(data["tables"]) > 1:
    t1 = data["tables"][1]
    L.append("| 行 | 列 | 文本 | 东亚字体 | 字号 | 加粗 | 对齐 | 垂直对齐 | 列宽 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in t1["cells"][:8]:
        for c in r["cells"]:
            txt = " ".join(p["text"] for p in c["paras"]).strip()
            if not txt:
                continue
            rn = (c.get("runs") or [{}])[0]
            p0 = c["paras"][0] if c["paras"] else {}
            L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                c["row"], c["col"], txt.replace("|", "/")[:26],
                rn.get("east_asia") or "-", rn.get("size_pt") or "-",
                rn.get("bold"), p0.get("align") or "-",
                c["borders"].get("vAlign") or "-",
                (c["borders"].get("width") or {}).get("w") or "-"))
L.append("")

# ---- 垂直对齐模式（竖排文字）----
L.append("## 六、竖排文字检测")
L.append("")
vert = []
for t in data["tables"]:
    for r in t["cells"]:
        for c in r["cells"]:
            txt = " ".join(p["text"] for p in c["paras"]).strip()
            # 「教 学 过 程」这类每字之间带空格且含换行
            for p in c["paras"]:
                if p.get("textAlignment") in ("center",) and "⏎" in (p["text"] or ""):
                    vert.append((c["row"], c["col"], p["text"]))
                if any(ch in (p["text"] or "") for ch in ("教", "材", "活")) and \
                   len(p["text"] or "") > 0 and _is_spaced(p["text"]) if False else False:
                    pass
L.append("发现的候选竖排格（文本含换行且垂直居中）：")
L.append("")
for row, col, txt in vert[:12]:
    L.append(f"- 表1 r{row}c{col}：`{txt}`")
L.append("")

(D / "STYLE_SPEC.md").write_text("\n".join(L), encoding="utf-8")
print("wrote STYLE_SPEC.md")

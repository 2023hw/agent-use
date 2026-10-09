# -*- coding: utf-8 -*-
"""清理一次性调试脚本（保留与数据清洗相关的审计脚本）。

保留：diagnose_first_session.py（第 1 次评量诊断，报告要用）
      audit_f.py（F 笔误审计，需留痕）
删除：其余 debug_*/verify_*/check_*/_* 临时脚本
删除的调试成果已汇总进 docs/PROJECT_PLAN.md 与 memory。
"""

from pathlib import Path

ROOT = Path(r"C:\Users\17093\WorkBuddy\2026-09-28-10-57-57\lessonplan-agent\scripts")

KEEP = {
    "parse_corpus.py", "build_profiles.py", "analyze_sequences.py",
    "diagnose_first_session.py", "audit_f.py", "dump_targets.py",
    "extract_docx.py", "inspect_corpus.py", "cleanup_scripts.py",
}

removed, kept = [], []
for f in sorted(ROOT.glob("*.py")):
    if f.name in KEEP:
        kept.append(f.name)
        continue
    f.unlink()
    removed.append(f.name)

print("removed:", removed)
print("kept:", kept)

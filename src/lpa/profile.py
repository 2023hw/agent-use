"""能力画像反推：从历史教案的评量序列里算出每个幼儿的能力水平，再聚合成班级画像。

编码口径（**经带教老师确认**）：P = 达标，E = 未达标。

数据形态的一个已知不确定性：
模板里「评量效果」有 5 个子列（第一次~第五次），每个单元格内含若干个 P/E 字母。
因此一个单元格 = 一次评量，单元格内的字母 = 该次评量的多个目标。
只有当 5 次评量的字母数一致时，才能按「目标序号」做纵向追踪（首达位置、退化检测）；
否则只做整体达标率统计。这个判断由 evaluate_child 自动降级处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .taxonomy import (
    EvalCode,
    classify_domain,
    detect_support,
    is_classroom_behavior_goal,
    parse_code,
    support_from_mastery,
)


@dataclass
class TargetSpec:
    """一条康复目标。"""

    index: int
    text: str
    domain: str
    declared_support: Optional[str] = None


@dataclass
class SessionResult:
    """一次评量的结果。"""

    session_index: int  # 1-based
    codes: List[EvalCode]

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.codes if c.is_pass)

    @property
    def pass_rate(self) -> float:
        return self.pass_count / len(self.codes) if self.codes else 0.0


@dataclass
class DomainStat:
    domain: str
    evidence: int = 0
    passes: int = 0

    @property
    def mastery(self) -> float:
        return self.passes / self.evidence if self.evidence else 0.0


@dataclass
class ChildEvaluation:
    anon_id: str
    attendance: str
    sessions: List[SessionResult] = field(default_factory=list)
    domain_stats: Dict[str, DomainStat] = field(default_factory=dict)
    total_evidence: int = 0
    total_passes: int = 0
    trackable_per_target: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def overall_mastery(self) -> float:
        return self.total_passes / self.total_evidence if self.total_evidence else 0.0

    @property
    def support_level(self) -> str:
        absent = self.attendance == "缺席" or self.total_evidence == 0
        return support_from_mastery(self.overall_mastery, absent=absent)

    def first_pass_target(self, target_idx: int) -> Optional[int]:
        """某个目标首次达标出现在第几次评量。不可追踪时返回 None。"""
        if not self.trackable_per_target:
            return None
        for s in self.sessions:
            if target_idx < len(s.codes) and s.codes[target_idx].is_pass:
                return s.session_index
        return None

    def regressions(self) -> List[Tuple[int, int]]:
        """返回出现退化的 (目标序号, 退化发生的第几次评量)。"""
        if not self.trackable_per_target:
            return []
        n_targets = len(self.sessions[0].codes)
        out = []
        for t in range(n_targets):
            seen_pass = False
            for s in self.sessions:
                code = s.codes[t] if t < len(s.codes) else None
                if code is None:
                    continue
                if code.is_pass:
                    seen_pass = True
                elif seen_pass:
                    out.append((t + 1, s.session_index))
                    break
        return out


MARKER = re.compile(r"(?<![0-9])(\d{1,2})\s*[.、．]\s*")


def split_goals(text: str) -> List[str]:
    """把 '1.xxx 2.yyy 3.zzz' 形式的目标清单切成单条目标。

    实际语料里编号经常脱号（第 4 条漏写编号）、或写成 '3.… 4.…  6.…'，
    因此这里容忍不超过 2 的跳号，而不是要求严格连续。
    """
    if not text:
        return []

    # 有些目标的第 1 条写成 '1整节课堂…'，补一个分隔符
    text = re.sub(r"^(\d{1,2})(?=[\u4e00-\u9fa5])", r"\1. ", text.strip())

    candidates = [m for m in MARKER.finditer(text) if 1 <= int(m.group(1)) <= 12]
    if len(candidates) < 2:
        return [text.strip()]

    # 语料里大量目标清单漏写了开头的 '1.'，因此不要求必须从 1 开始，
    # 只要第一个编号足够小（<=3）就把它当作清单起点。
    start_idx = None
    for i, m in enumerate(candidates):
        if int(m.group(1)) <= 3:
            start_idx = i
            break
    accepted: List[re.Match] = []
    last = 0
    for m in candidates[start_idx:]:
        num = int(m.group(1))
        if not accepted:
            accepted.append(m)
            last = num
        elif last < num <= last + 2:
            accepted.append(m)
            last = num
    if len(accepted) < 2:
        return [text.strip()]

    pieces: List[str] = []
    for i, m in enumerate(accepted):
        start = m.end()
        end = accepted[i + 1].start() if i + 1 < len(accepted) else len(text)
        piece = text[start:end].strip(" 　.。、")
        if piece:
            pieces.append(piece)
    return pieces


def build_targets(goal_text: str) -> List[TargetSpec]:
    specs: List[TargetSpec] = []
    for i, piece in enumerate(split_goals(goal_text), start=1):
        piece = piece.strip()
        if len(piece) < 2:
            continue
        specs.append(
            TargetSpec(
                index=i,
                text=piece,
                domain=classify_domain(piece),
                declared_support=detect_support(piece),
            )
        )
    return specs


def main_domain_of(targets: Sequence[TargetSpec]) -> str:
    """活动主领域。语料里第 1 条固定是模板化的「课堂安坐」，不计入主领域投票。"""
    votes: Dict[str, int] = {}
    for t in targets:
        if is_classroom_behavior_goal(t.text):
            continue
        if t.domain == "未分类":
            continue
        votes[t.domain] = votes.get(t.domain, 0) + 1
    if not votes:
        substantive = [t.domain for t in targets if t.domain != "未分类"]
        return substantive[0] if substantive else "未分类"
    return max(votes.items(), key=lambda kv: kv[1])[0]


def evaluate_child(
    anon_id: str,
    eval_codes: Sequence[str],
    targets: Sequence[TargetSpec],
) -> ChildEvaluation:
    """把一个幼儿在本份教案里的评量格子转成 SessionResult 序列。

    ⚠ 关键口径（详见 parse_corpus.py 的说明）：
    `eval_codes` 的元素顺序 = 原表评量列从左到右的顺序（第一次到第五次）。
    「未参与」的格子解析为空码，**不计入 sessions**，但 session_index 仍保留
    它在原表中的位次，这样"第三次评量达标"指的就是表格里的第三次，
    不会因为前面某次缺席而被压号。

    出勤由评量序列推出：整份表的评量格全「未参与」记为缺席，否则记为在课。

    ⚠ `eval_codes` 的长度**等于该份教案的评量列数**（实测全部为 5）。
    但"列存在"不等于"有数据"：有的教案只填到第 3 次，后面是空格。
    本函数按实际长度遍历，不硬编码 5。
    """
    valid_absent = [c for c in eval_codes if not parse_code(c)]
    attendance = "缺席" if (eval_codes and len(valid_absent) == len(eval_codes)) else "在课"
    ev = ChildEvaluation(anon_id=anon_id, attendance=attendance)

    sessions: List[SessionResult] = []
    absent_at: List[int] = []
    for i, raw in enumerate(eval_codes, start=1):
        codes = parse_code(raw)
        if codes:
            sessions.append(SessionResult(session_index=i, codes=codes))
        else:
            absent_at.append(i)
    ev.sessions = sessions
    if absent_at and len(absent_at) < len(eval_codes):
        ev.notes.append(
            f"原表第 {','.join(str(k) for k in absent_at)} 次评量为「未参与」，已按缺失处理"
        )

    if not sessions:
        ev.notes.append("本份教案全部评量均未参与")
        return ev

    lengths = {len(s.codes) for s in sessions}
    ev.trackable_per_target = len(lengths) == 1

    ev.total_evidence = sum(len(s.codes) for s in sessions)
    ev.total_passes = sum(s.pass_count for s in sessions)

    # 目标层面的归因：一次活动内的多条目标归属于不同领域时，
    # 无法把某个字母精确对应到某条目标，因此按目标所属领域的占比加权分配。
    if targets:
        domains = [t.domain for t in targets]
        unique_domains = list(dict.fromkeys(domains))
        counts = {d: domains.count(d) for d in unique_domains}
        total = sum(counts.values())
        allocated = 0
        for idx, domain in enumerate(unique_domains):
            if idx == len(unique_domains) - 1:
                share = ev.total_evidence - allocated
            else:
                share = round(ev.total_evidence * counts[domain] / total)
            stat = ev.domain_stats.setdefault(domain, DomainStat(domain=domain))
            stat.evidence += share
            stat.passes += round(share * ev.overall_mastery)
            allocated += share
    else:
        stat = ev.domain_stats.setdefault("未分类", DomainStat(domain="未分类"))
        stat.evidence = ev.total_evidence
        stat.passes = ev.total_passes

    if not ev.trackable_per_target:
        ev.notes.append(
            f"各次评量的目标数不一致（{sorted(lengths)}），无法按目标做纵向追踪，仅按整体达标率计算"
        )
    return ev


def aggregate_across_lessons(records: List[ChildEvaluation]) -> ChildEvaluation:
    """把同一个幼儿在多次活动中的评量合并。"""
    if not records:
        raise ValueError("无记录")
    merged = ChildEvaluation(
        anon_id=records[0].anon_id,
        attendance="在课" if any(r.attendance == "在课" for r in records) else "缺席",
    )
    offset = 0
    for r in records:
        for s in r.sessions:
            merged.sessions.append(
                SessionResult(session_index=offset + s.session_index, codes=s.codes)
            )
        offset += len(r.sessions)
        merged.total_evidence += r.total_evidence
        merged.total_passes += r.total_passes
        for domain, stat in r.domain_stats.items():
            target = merged.domain_stats.setdefault(domain, DomainStat(domain=domain))
            target.evidence += stat.evidence
            target.passes += stat.passes
        merged.notes.extend(r.notes)
    return merged

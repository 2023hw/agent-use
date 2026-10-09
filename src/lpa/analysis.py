"""评量序列分析：pairwise / gain / stability 三种口径。

设计动机（重要，写进报告）：
原始语料里不同幼儿的评量次数不一致（135 条记录中，完整 5 次只有 87 条），
且缺失不是随机的——只有 1 次评量的那份教案覆盖了 14 名幼儿。
因此「总体达标率」与「按第 N 次评量的平均达标率」两个口径都不可靠：
前者的分母包含不同的人，后者连成折线时前后不是同一批儿童。

改用三个不依赖总体平均的口径：

  A. pairwise   只保留序列完整的记录，让分母固定，曲线可比
  B. gain       对每条记录算「末次 − 首次」，只看个体自身变化
  C. stability  按个体内序列形态分类（稳定达标 / 波动 / 未掌握），不做平均

三个口径分别服务于生成层的不同决策，见各自 docstring。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .profile import SessionResult


# --------------------------------------------------------------------------
# 口径 A：配对分析
# --------------------------------------------------------------------------


@dataclass
class PairwiseResult:
    """只统计序列完整的记录，保证前后是同一批儿童。"""

    required_sessions: int
    n_records_total: int
    n_records_complete: int
    by_session: Dict[int, float] = field(default_factory=dict)

    @property
    def retention(self) -> float:
        return self.n_records_complete / self.n_records_total if self.n_records_total else 0.0

    @property
    def is_monotonic(self) -> bool:
        vals = [self.by_session[k] for k in sorted(self.by_session)]
        return all(b >= a - 1e-9 for a, b in zip(vals, vals[1:]))

    def to_text(self) -> str:
        lines = [
            f"仅统计完整 {self.required_sessions} 次评量的记录："
            f"{self.n_records_complete} / {self.n_records_total}（保留率 {self.retention:.0%}）",
            "",
            "| 第几次 | 样本数（固定） | 达标率 |",
            "|---|---|---|",
        ]
        for k in sorted(self.by_session):
            lines.append(f"| {k} | {self.n_records_complete} | {self.by_session[k]:.3f} |")
        lines.append("")
        lines.append("单调递增" if self.is_monotonic else "存在回落，需关注复练环节")
        return "\n".join(lines)


def pairwise(sessions_list: Sequence[List[SessionResult]]) -> PairwiseResult:
    """口径 A。

    用途：为生成层提供「一周内的正常进步节奏」参照，
    据此设定 B 层目标在一周后应达到的水平，避免目标定得过高或过低。

    ⚠ 保留策略（2026-09-29 修订）：
    以**语料里的主流评量次数**（众数，实测为 5）为准，只保留长度**恰好等于**它的记录。
    两种不合格记录都会被排除：
      - 长度 < 众数（中途缺席、转班、只上过几次课，如闪电班只填到第 3 次）
      - 长度 > 众数（**当前数据中不存在**，防御性排除；若某表列出 6 列评量，
        把它与 5 列的混在一起会让位次语义错乱）

    早期版本用 `max(set(lengths), key=lengths.count)` 取众数后复用，
    但**没有显式排除非众数长度的记录**，导致「只评了 3 次」的记录
    可能与「完整 5 次」的记录一起进入同一分母，位次语义不一致。
    现在改为**严格等长**，保证分母固定且位次可比。
    """
    if not sessions_list:
        return PairwiseResult(0, 0, 0)
    lengths = [len(s) for s in sessions_list if s]
    if not lengths:
        return PairwiseResult(0, 0, 0)
    target = max(set(lengths), key=lengths.count)
    # 严格等长：只评了 3 次的记录不进配对样本，保证分母与位次语义一致
    complete = [s for s in sessions_list if len(s) == target and target > 0]

    by_session: Dict[int, List[float]] = {i: [] for i in range(1, target + 1)}
    for s in complete:
        for r in s:
            by_session[r.session_index].append(r.pass_rate)

    return PairwiseResult(
        required_sessions=target,
        n_records_total=len(sessions_list),
        n_records_complete=len(complete),
        by_session={k: sum(v) / len(v) for k, v in by_session.items() if v},
    )


# --------------------------------------------------------------------------
# 口径 B：个体进步量
# --------------------------------------------------------------------------


@dataclass
class GainRecord:
    anon_id: str
    course_key: str
    first_rate: float
    last_rate: float
    n_sessions: int

    @property
    def gain(self) -> float:
        return self.last_rate - self.first_rate


@dataclass
class GainSummary:
    records: List[GainRecord] = field(default_factory=list)

    def _stats(self) -> Tuple[float, float, float, float]:
        gains = [r.gain for r in self.records]
        if not gains:
            return 0.0, 0.0, 0.0, 0.0
        gains_sorted = sorted(gains)
        n = len(gains_sorted)
        mid = n // 2
        median = gains_sorted[mid] if n % 2 else (gains_sorted[mid - 1] + gains_sorted[mid]) / 2
        return sum(gains) / n, median, min(gains), max(gains)

    @property
    def mean_gain(self) -> float:
        return self._stats()[0]

    @property
    def median_gain(self) -> float:
        return self._stats()[1]

    @property
    def improved_ratio(self) -> float:
        if not self.records:
            return 0.0
        return sum(1 for r in self.records if r.gain > 0) / len(self.records)

    @property
    def declined_ratio(self) -> float:
        if not self.records:
            return 0.0
        return sum(1 for r in self.records if r.gain < 0) / len(self.records)

    def to_text(self) -> str:
        mean, median, lo, hi = self._stats()
        lines = [
            f"个体进步量样本：{len(self.records)} 条（每条对应一名幼儿在一个教案周期内的头尾差）",
            f"平均进步：**{mean:+.3f}**　中位数：{median:+.3f}　区间：[{lo:+.3f}, {hi:+.3f}]",
            f"进步的比例：{self.improved_ratio:.0%}　退步的比例：{self.declined_ratio:.0%}",
            "",
            "说明：该口径只看每个幼儿自己的首末两次，不受他缺席几次的影响，",
            "因此不依赖总体平均，是本项目评估进步幅度的**主指标**。",
        ]
        return "\n".join(lines)


def gains(records: Sequence[Tuple[str, str, List[SessionResult]]]) -> GainSummary:
    """口径 B。

    用途：识别「几乎没动」的幼儿群体，据此决定 A 层目标需要多重的辅助与
    多小的步骤；同时给出正常进步幅度的基准，用于校验生成的目标是否离谱。
    """
    out: List[GainRecord] = []
    for anon_id, course_key, sessions in records:
        valid = [s for s in sessions if s.codes]
        if len(valid) < 2:
            continue
        out.append(
            GainRecord(
                anon_id=anon_id,
                course_key=course_key,
                first_rate=valid[0].pass_rate,
                last_rate=valid[-1].pass_rate,
                n_sessions=len(valid),
            )
        )
    return GainSummary(records=out)


# --------------------------------------------------------------------------
# 口径 C：个体内一致性
# --------------------------------------------------------------------------


class Stability:
    STABLE = "稳定达标"
    WAVE_CONVERGED = "波动已收敛"
    WAVE_UNSETTLED = "波动未巩固"
    NOT_MASTERED = "从未达标"
    NO_DATA = "数据不足"


def classify_stability(sessions: Sequence[SessionResult]) -> Tuple[str, float]:
    """返回 (稳定度类别, 平均达标率)。

    分类规则（可随带教老师意见调整）：
      - 每次评量都全达标                -> 稳定达标
      - 出现过达标，但末次未达标         -> 波动·未巩固（需复练）
      - 出现过达标，且末次已达标但中途回落 -> 波动·已收敛（可往下推）
      - 从未达标                        -> 未掌握
      - 实际评到不足 2 次                -> 数据不足
    """
    # 只看「实际评到」的次数，未参与不算一次评量
    valid = [s for s in sessions if s.codes]
    if not valid:
        return Stability.NO_DATA, 0.0
    if len(valid) < 2:
        return Stability.NO_DATA, valid[0].pass_rate

    avg = sum(s.pass_rate for s in valid) / len(valid)
    if all(s.pass_rate >= 1.0 for s in valid):
        return Stability.STABLE, avg

    any_pass = any(s.pass_count > 0 for s in valid)
    last_pass = valid[-1].pass_count > 0
    if not any_pass:
        return Stability.NOT_MASTERED, avg
    if not last_pass:
        return Stability.WAVE_UNSETTLED, avg
    # 末次已达标，检查中途是否存在回落
    seen_pass = False
    regressed = False
    for s in valid:
        if s.pass_rate >= 1.0:
            seen_pass = True
        elif seen_pass:
            regressed = True
            break
    return (Stability.WAVE_CONVERGED if regressed else Stability.WAVE_CONVERGED), avg


@dataclass
class StabilitySummary:
    counts: Dict[str, int] = field(default_factory=dict)
    details: List[Tuple[str, str, str]] = field(default_factory=list)  # (anon_id, 教案, 类别)

    def to_text(self) -> str:
        total = sum(self.counts.values()) or 1
        lines = [
            "个体内一致性的分布（不做平均）：",
            "",
            "| 类型 | 人数 | 占比 | 教学含义 |",
            "|---|---|---|---|",
        ]
        meaning = {
            Stability.STABLE: "该目标已掌握，可进入 C 层",
            Stability.WAVE_CONVERGED: "波动后已收敛，可留 B 层并减少辅助",
            Stability.WAVE_UNSETTLED: "波动且末次未达标，需安排复练",
            Stability.NOT_MASTERED: "从未达标，下沉 A 层并加大辅助",
            Stability.NO_DATA: "实际评到不足 2 次，无法判断，需补评",
        }
        for k in (Stability.STABLE, Stability.WAVE_CONVERGED,
                  Stability.WAVE_UNSETTLED, Stability.NOT_MASTERED,
                  Stability.NO_DATA):
            n = self.counts.get(k, 0)
            lines.append(f"| {k} | {n} | {n / total:.0%} | {meaning[k]} |")
        lines.append("")
        lines.append("该口径直接回答「哪些目标该往下走、哪些该复练」，"
                     "是生成层安排环节与支架的直接依据。")
        return "\n".join(lines)


def stability(
    records: Sequence[Tuple[str, str, List[SessionResult]]]
) -> StabilitySummary:
    """口径 C。

    用途：把「稳定达标」的目标推到 C 层、「波动」的留在 B 层并插入复练环节、
    「未掌握」的下沉到 A 层并增加辅助强度。
    """
    counts: Dict[str, int] = {}
    details: List[Tuple[str, str, str]] = []
    for anon_id, course_key, sessions in records:
        cls, _ = classify_stability(sessions)
        counts[cls] = counts.get(cls, 0) + 1
        details.append((anon_id, course_key, cls))
    return StabilitySummary(counts=counts, details=details)

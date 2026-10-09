"""领域模型：能力档案 + 教案结构化 Schema。

适配对象：3—9 岁混龄康教班的个别化主题教案。
设计要点：
1. 输入层不接受姓名与诊断，只接受匿名编号 + 能力画像。
2. 目标使用双轴描述：发展领域 × 支持层级。
3. 教案以结构化对象建模，禁止模型输出成篇散文。
4. 个别化差异表按 A/B/C 层组织，不出现任何幼儿姓名。
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .taxonomy import DEVELOPMENT_DOMAINS, SUPPORT_LEVELS


class Level(str, Enum):
    A = "A"
    B = "B"
    C = "C"


# --------------------------------------------------------------------------
# 输入层：能力画像
# --------------------------------------------------------------------------


class DomainProfile(BaseModel):
    """单个幼儿在某个发展领域上的画像。"""

    domain: str
    support_level: str = Field(description="六档支持层级之一")
    mastery: float = Field(ge=0.0, le=1.0, description="达标率 0-1")
    first_pass_session: Optional[int] = Field(default=None, description="首次达标的第几次评量")
    regressed: bool = False
    evidence_count: int = 0

    @field_validator("domain")
    @classmethod
    def check_domain(cls, v: str) -> str:
        if v not in DEVELOPMENT_DOMAINS and v != "未分类":
            raise ValueError(f"未知发展领域: {v}")
        return v

    @field_validator("support_level")
    @classmethod
    def check_level(cls, v: str) -> str:
        if v not in SUPPORT_LEVELS:
            raise ValueError(f"未知支持层级: {v}")
        return v


class ChildProfile(BaseModel):
    """匿名幼儿画像。绝不含姓名、诊断、障碍类别。"""

    anon_id: str = Field(description="匿名编号，如 S01")
    attendance: str = "在课"
    domain_profiles: List[DomainProfile] = Field(default_factory=list)

    def profile_of(self, domain: str) -> Optional[DomainProfile]:
        for p in self.domain_profiles:
            if p.domain == domain:
                return p
        return None


class ClassProfile(BaseModel):
    """班级画像：由幼儿画像聚合，不含个体身份信息。"""

    class_label: str = Field(description="班次代号，已去除原班名中的身份信息")
    age_range: str
    class_size: int
    # domain -> support_level -> 人数
    distribution: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    notes: str = ""

    @classmethod
    def from_children(
        cls,
        children: List[ChildProfile],
        class_label: str,
        age_range: str,
        notes: str = "",
    ) -> "ClassProfile":
        dist: Dict[str, Dict[str, int]] = {}
        for child in children:
            if child.attendance == "缺席":
                continue
            for dp in child.domain_profiles:
                bucket = dist.setdefault(dp.domain, {lv: 0 for lv in SUPPORT_LEVELS})
                bucket[dp.support_level] = bucket.get(dp.support_level, 0) + 1
        attending = [c for c in children if c.attendance != "缺席"]
        return cls(
            class_label=class_label,
            age_range=age_range,
            class_size=len(attending),
            distribution=dist,
            notes=notes,
        )

    def needs_support_ratio(self, domain: str) -> float:
        """该领域上需要支持（非独立完成）的占比。"""
        counts = self.distribution.get(domain)
        if not counts:
            return 0.0
        total = sum(counts.values())
        if total == 0:
            return 0.0
        needy = total - counts.get("独立完成", 0)
        return needy / total

    def to_prompt(self) -> str:
        lines = [
            f"班次：{self.class_label}，年龄范围 {self.age_range}，在课幼儿 {self.class_size} 名。",
            "各发展领域的支持层级分布：",
        ]
        for domain, counts in self.distribution.items():
            if not any(counts.values()):
                continue
            parts = [f"{lv} {counts.get(lv, 0)}人" for lv in SUPPORT_LEVELS if counts.get(lv, 0)]
            lines.append(f"- {domain}：" + "，".join(parts))
        if self.notes:
            lines.append(self.notes)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 生成层：教案结构化 Schema
# --------------------------------------------------------------------------


class Objective(BaseModel):
    """一条活动目标。必须可观测、可计数。"""

    level: Level
    domain: str
    support_level: str
    text: str
    observable_verb: str
    criteria: str

    @field_validator("text")
    @classmethod
    def not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("目标文本不能为空")
        return v


class ProcedureStep(BaseModel):
    """活动环节中的一个最小步骤。"""

    index: int
    name: str
    duration_min: int = Field(gt=0)
    teacher_script: str = Field(description="教师可直接照说的原话")
    child_action: str
    visual_cue: str = Field(description="配合语言呈现的视觉或手势提示")
    scaffolds: List[str] = Field(default_factory=list)
    observation_points: List[str] = Field(default_factory=list)


class IndividualizedGoal(BaseModel):
    """个别化差异目标。方案甲：按层组织，不出现幼儿姓名。"""

    level: Level
    share_of_class: str = Field(description="预估适用人数，如 '约 2-3 名'")
    goal: str
    baseline_hint: str
    evaluation_method: str
    follow_up: str = Field(default="", description="未达标的下一步处理")


class Alternative(BaseModel):
    situation: str
    alternative_participation: str
    alternative_evaluation: str


class LessonPlan(BaseModel):
    title: str
    class_label: str
    age_range: str
    theme: str
    class_size: int = Field(
        default=0,
        description="本班在课人数 = A/B/C 三层人数之和。"
                    "⚠ 不是全园人数 —— class_profile.json 的 class_size 是全园规模，"
                    "模板里「人数」格填的是该班实际在课人数。")
    total_duration_min: int = Field(gt=0)
    date_range: str = ""

    class_objectives_text: str = Field(description="原模板的「有益经验」，班级总目标")
    main_domain: str

    objectives: List[Objective]
    materials: List[str]
    environment: str = ""
    safety_notes: str = ""

    steps: List[ProcedureStep]
    individualized_goals: List[IndividualizedGoal]
    alternatives: List[Alternative]
    assessment: Dict[str, str] = Field(description="以 A/B/C 为键的达成标准")
    extension: str = Field(description="活动延伸 / 家园共育")
    reflection_slots: str = Field(default="", description="留给教师填写的活动反思位置")

    # 以下字段供 rules.py 的硬校验使用
    key_points: Dict[str, str] = Field(
        default_factory=dict, description="重难点：focus / difficulty")
    home_extension: str = Field(default="", description="家园共育（可并入 extension）")
    reflection_points: List[str] = Field(
        default_factory=list, description="留给教师填写的反思提示")

    @model_validator(mode="after")
    def check_levels(self) -> "LessonPlan":
        if set(self.assessment) != {lv.value for lv in Level}:
            raise ValueError("assessment 必须覆盖 A/B/C 三层")
        levels = {g.level for g in self.individualized_goals}
        missing = set(Level) - levels
        if missing:
            raise ValueError(f"个别化目标缺少层次: {sorted(m.value for m in missing)}")
        return self

    @model_validator(mode="after")
    def check_main_domain(self) -> "LessonPlan":
        if self.main_domain not in DEVELOPMENT_DOMAINS and self.main_domain != "未分类":
            raise ValueError(f"未知发展领域: {self.main_domain}")
        return self

    def to_markdown(self, show_level: bool = True) -> str:
        lines = [f"# {self.title}", ""]
        lines.append(f"班次：{self.class_label}　年龄范围：{self.age_range}　时长：{self.total_duration_min} 分钟")
        lines.append(f"主题：{self.theme}　主领域：{self.main_domain}")
        lines.append("")
        lines.append("## 有益经验")
        lines.append(self.class_objectives_text)
        lines.append("")
        lines.append("## 分层目标")
        for obj in self.objectives:
            prefix = f"[{obj.level.value}] " if show_level else ""
            lines.append(f"- {prefix}{obj.text}")
        lines.append("")
        lines.append("## 活动准备")
        for item in self.materials:
            lines.append(f"- {item}")
        if self.environment:
            lines.append(f"- 环境：{self.environment}")
        if self.safety_notes:
            lines.append(f"- 安全：{self.safety_notes}")
        lines.append("")
        lines.append("## 活动过程")
        for step in self.steps:
            lines.append(f"{step.index}. {step.name}（{step.duration_min} 分钟）")
            lines.append(f"   教师：{step.teacher_script}")
            lines.append(f"   幼儿：{step.child_action}")
            lines.append(f"   提示：{step.visual_cue}")
            for sc in step.scaffolds:
                lines.append(f"   支持：{sc}")
        lines.append("")
        lines.append("## 个别化差异目标")
        for goal in self.individualized_goals:
            lines.append(
                f"- [{goal.level.value}] 适用 {goal.share_of_class}：{goal.goal}"
                f"（起点：{goal.baseline_hint}；评量：{goal.evaluation_method}"
                + (f"；未达标处理：{goal.follow_up}）" if goal.follow_up else "）")
            )
        lines.append("")
        lines.append("## 支持与替代方式")
        for alt in self.alternatives:
            lines.append(f"- 若{alt.situation}，可{alt.alternative_participation}；评价：{alt.alternative_evaluation}")
        lines.append("")
        lines.append("## 评量标准")
        for key in ("A", "B", "C"):
            if key in self.assessment:
                lines.append(f"- {key}层：{self.assessment[key]}")
        lines.append("")
        lines.append("## 活动延伸")
        lines.append(self.extension)
        if self.reflection_slots:
            lines.append("")
            lines.append("## 活动反思")
            lines.append(self.reflection_slots)
        return "\n".join(lines)

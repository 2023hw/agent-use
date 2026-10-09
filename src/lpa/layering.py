"""分层引擎：基于范例「个别化差异目标」条文序号做分层与动态调层。

## 为什么要这样分层（设计依据）

范例里每份教案的「个别化差异目标」都是一串**带序号的目标条文**，
序号不是难度等级，而是**阶梯位置**。以 `第四周-能干的我` 为例：

    1  整节课堂能安坐好不走出位置不闹情绪        ← 课堂常规（人手一条）
    2  能在身体辅助下完成看图指出事项名称        ← 支持梯度最低档
    3  能在少量手势辅助下完成看图指出事项名称    ← 支持梯度中档
    4  能自己独立完成看图指出事项名称            ← 支持梯度最高档
    5  能看图表达"我会……"                      ← 进阶：指认 → 语言表达
    6  能听令做事情                              ← 进阶：泛化到指令执行

也就是说范例的目标排列遵循**两条轴**：

- **支持轴**（2→3→4）：同一任务，辅助量递减 —— 身体辅助 / 手势辅助 / 独立
- **发展轴**（4→5→6）：任务难度递增 —— 指认 / 表达 / 泛化应用

儿童的「康复目标」列（如 `134`）就是**该儿童本课所选条文的序号集合**。
这套做法正是范例自身的分层逻辑：**用目标条文的组合来实现个别化，而不是另设 A/B/C 档**。

因此本模块不做「重造分层」，而是：
1. 从范例教案里**学出**目标阶梯（支持档位 + 发展档位）；
2. 按儿童能力画像，在其所属阶梯上选一个**组合**（1 + 支持档 + 可选进阶档）；
3. 依据**五次评量结果**动态上调/下调一档，并记录调层轨迹。

## 分层结果不写进教案正文

按用户要求：分层结果**不出现在生成的教案里**，而是每份教案后**另附一页**
「儿童代号 → 所属层级」的对照页。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------
# 目标阶梯：从范例归纳的档位语义
# --------------------------------------------------------------------------

# 支持轴关键词 → 档位（数值越大 = 所需支持越少 = 能力越强）
# ⚠ 必须用「最长关键词优先」匹配，否则 `辅助` 会盖掉 `身体辅助`。
SUPPORT_RANK = {
    "全辅助": 1, "全身体辅助": 1, "大量身体辅助": 1,
    "身体辅助": 2,
    "少量身体辅助": 3,
    "少量手势辅助": 4, "少量辅助": 4,
    "手势辅助": 5,
    "示范模仿": 6, "展示示范": 6, "模仿": 6,
    "口语提示": 7, "视觉提示": 7,
    "独立": 8, "自己": 8,
}

# 发展轴关键词 → 档位（数值越大 = 任务难度越高）
DEV_RANK = {
    "不走出位置": 0, "安坐": 0, "常规": 0,
    "指认": 1, "指出": 1, "找出": 1, "配对": 1, "点数": 1, "认识": 1,
    "表达": 2, "说出": 2, "回答": 2,
    "参与": 2, "游戏": 2,
    "听令": 3, "完成": 3, "泛化": 3, "主动": 3,
}

# --------------------------------------------------------------------------
# 层级 = 语料里的「组合规律」（**不固定 3 层**）
# --------------------------------------------------------------------------
#
# 用户要求：不要固定 3 层，按案例里康复目标的组合规律分层，
# **有几种组合规律就分几层**。
#
# 从 13 份范例、170 条儿童记录里挖出来的规律是：所有组合只落在 **4 种**
# **轴位签名**上（`R`=课堂常规、`Sk`=支持轴第 k 档、`Ak`=发展轴第 k 条）：
#
#   层   组合规律       语料实例              基线 P 密度   所需支持
#   L1   R+S1+S2       123 / 134             0.48          身体辅助
#   L2   R+S1+S3       124 / 1234 / 1346     0.56          少量手势辅助
#   L3   R+S3+A1       145 / 125 / 135       0.80          独立完成
#   L4   R+S3+A1+A2    1456 / 1345 / 156     0.92          独立完成并泛化
#
# 轴位是**可跨教案移植**的：各课序号都从 1 开始但条文内容不同，只有轴位不变
# （`resolve_pattern` 把规律落到本课阶梯上 → 换课换条文、规律不变）。
#
# 基线 P 密度（`P` 占基线的比例）单调递增，是该分层的**独立证据**：
# 层越高，孩子接手本课时已达标的条目越多。
# 复算脚本：`scripts/mine_layer_tiers.py`（产出下面这个 JSON）。
LAYER_TIERS_FILE = ROOT / "data" / "structured" / "layer_tiers.json"

# 兜底表（与挖掘结果一致；`layer_tiers.json` 缺失时用它）
DEFAULT_LAYER_SPECS: List[dict] = [
    {"level": 1, "name": "高支持层", "support": "身体辅助",
     "pattern": ["R", "S1", "S2"], "exemplars": ["123", "134"],
     "p_density": 0.48},
    {"level": 2, "name": "中支持层", "support": "少量手势辅助",
     "pattern": ["R", "S1", "S3"], "exemplars": ["124", "1234", "1346"],
     "p_density": 0.56},
    {"level": 3, "name": "低支持层", "support": "独立完成",
     "pattern": ["R", "S3", "A1"], "exemplars": ["145", "125", "135"],
     "p_density": 0.80},
    {"level": 4, "name": "泛化层", "support": "独立完成并泛化",
     "pattern": ["R", "S3", "A1", "A2"], "exemplars": ["1456", "1345", "156"],
     "p_density": 0.92},
]


def load_layer_specs() -> List[dict]:
    """读挖掘出来的层级表；缺失或损坏时回落 `DEFAULT_LAYER_SPECS`。"""
    try:
        data = json.loads(LAYER_TIERS_FILE.read_text(encoding="utf-8"))
        tiers = data.get("tiers") or []
        if tiers and all("pattern" in t for t in tiers):
            return sorted(tiers, key=lambda t: t["level"])
    except Exception:
        pass
    return list(DEFAULT_LAYER_SPECS)


LAYER_SPECS: List[dict] = load_layer_specs()

# 兼容旧接口：{level: {"code","name","support"}}（渲染附录表用）
LAYER_META: Dict[int, dict] = {
    t["level"]: {"code": f"L{t['level']}", "name": t["name"],
                 "support": t["support"], "pattern": "+".join(t["pattern"])}
    for t in LAYER_SPECS
}


def layer_count() -> int:
    """层数 = 语料里「组合规律」的种数（**不是写死的 3**）。"""
    return len(LAYER_SPECS)


def layer_levels() -> List[int]:
    return [t["level"] for t in LAYER_SPECS]


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class GoalItem:
    """一条目标条文。"""

    index: int                  # 范例里的序号（1~6）
    text: str
    support_rank: int = 0       # 支持轴档位（越大 = 所需支持越少）
    dev_rank: int = 0           # 发展轴档位（越大 = 任务越难）

    @property
    def is_routine(self) -> bool:
        """是否课堂常规条（人人适用）。

        判据是**文本本身在讲课堂常规**，而不是「没识别出支持/发展关键词」——
        后者会把「能在全辅助下通过障碍物…」这类漏判成常规。
        """
        t = self.text or ""
        return any(k in t for k in ("安坐", "不走出位置", "不闹情绪")) and \
            "辅助" not in t and "独立" not in t


@dataclass
class GoalLadder:
    """一份教案的目标阶梯。"""

    items: List[GoalItem]

    def by_index(self) -> Dict[int, GoalItem]:
        return {it.index: it for it in self.items}

    # -- 分轴：不靠单一关键词，而靠「任务主干」的成组关系 --------------
    def _supports_skeleton(self) -> List[str]:
        """支持轴条文共有的任务主干（去辅助词、去常规词后的剩余文本）。

        以 `第二周-动物的家` 为例，3/4/5 三条去掉辅助词后主干完全一致：
            「通过障碍物把动物送回家」
        说明它们是**同一任务的三档辅助**，构成支持轴。
        """
        cands: List[str] = []
        for it in self.items:
            if it.is_routine or it.support_rank <= 0:
                continue
            cands.append(_strip_support_words(it.text))
        # 主干重复出现 ≥2 次 → 是支持梯度
        from collections import Counter
        cnt = Counter(c for c in cands if c)
        return [c for c, n in cnt.items() if n >= 2]

    def support_items(self) -> List[GoalItem]:
        """支持轴条文：同一任务、不同辅助量，按所需支持从多到少。"""
        skel = self._supports_skeleton()
        xs = [it for it in self.items
              if not it.is_routine and it.support_rank > 0
              and (_strip_support_words(it.text) in skel)]
        if not xs:
            # 主干匹配不上时（范例原文常把多条挤在一格导致串行），
            # 退化为「所有带辅助词的条文」，但排除纯进阶条。
            xs = [it for it in self.items
                  if not it.is_routine and it.support_rank > 0]
        return sorted(xs, key=lambda it: (it.support_rank, it.index))

    def advanced_items(self) -> List[GoalItem]:
        """发展轴条文：不在支持轴上的、任务难度更高的进阶条。"""
        sup = {it.index for it in self.support_items()}
        xs = [it for it in self.items
              if not it.is_routine and it.index not in sup and it.dev_rank >= 1]
        return sorted(xs, key=lambda it: (it.dev_rank, it.index))

    def routine_index(self) -> Optional[int]:
        for it in self.items:
            if it.is_routine:
                return it.index
        return None


@dataclass
class ChildLayer:
    """一名儿童的层级判定结果。"""

    anon_id: str
    layer: int                          # 1/2/3，1 支持最多
    goal_indices: List[int]             # 该儿童本课的目标条文序号组合
    basis: str                          # 判定依据
    mastery: float = 0.0
    support_level: str = ""
    history: List[dict] = field(default_factory=list)  # 调层轨迹


# --------------------------------------------------------------------------
# 阶梯构造
# --------------------------------------------------------------------------


def _strip_support_words(text: str) -> str:
    """剥掉辅助量描述，留下任务主干。用于判定「是否同一任务的不同辅助档」。

    `能在全辅助下通过障碍物把动物送回家` → `通过障碍物把动物送回家`
    这样 3/4/5 三条主干一致，可判定为支持轴。

    ⚠ 范例原文常漏字（如「能在少量辅助通过障碍物…」缺「下」），
    剥完辅助词会残留一个孤立的「在」。必须一并剥掉，否则同一条
    支持轴的条文主干不一致，会被误判成发展轴。
    """
    t = re.sub(r"[\s\u3000]+", "", text or "")
    # 去掉引号/标点，避免 `送“回家”` 与 `送回家的` 主干不一致
    t = re.sub(r"[，。、；：（）()（）“”\"'‘’]", "", t)
    # 去掉辅助量短语
    for kw in sorted(SUPPORT_RANK, key=len, reverse=True):
        t = t.replace(kw, "")
    # 去掉「能/在…下」这类连接词；先处理完整形式，再处理残留裸字
    t = re.sub(r"能?在?下", "", t)
    t = re.sub(r"^能", "", t)
    t = t.replace("能", "")          # 句中残留的「能」（如「能自己…」剥完还剩）
    t = t.replace("在", "")          # 漏字导致的孤立「在」
    t = t.replace("下", "")          # 漏字导致的孤立「下」
    # 去掉「完成 / 做 / 进行」这类**动词壳** —— 它们不改变任务实质，
    # 但会让同一条支持轴的条文主干不一致。
    #   例：「能自己独立完成指认国旗」 vs 「能在全辅助下指认国旗」
    #       剥壳前 → `完成指认国旗` / `指认国旗`（不匹配，误判成两条轴）
    #       剥壳后 → `指认国旗` / `指认国旗`（匹配，正确归入支持轴）
    for shell in ("完成", "进行", "做出", "做出"):
        t = t.replace(shell, "")
    return t.strip()


def classify_goal_text(text: str) -> Tuple[int, int]:
    """给一条目标条文判定 (支持轴档位, 发展轴档位)。
    支持轴档位数值越大 = 所需支持越少。必须「最长关键词优先」匹配，
    否则 `辅助` 之类的短词会盖掉 `少量身体辅助` 这样的长词。
    """
    t = text or ""
    sup = 0
    for kw in sorted(SUPPORT_RANK, key=len, reverse=True):
        if kw in t:
            sup = SUPPORT_RANK[kw]
            break
    dev = 0
    for kw in sorted(DEV_RANK, key=len, reverse=True):
        if kw in t:
            dev = max(dev, DEV_RANK[kw])
    return sup, dev


def build_ladder(items: List[Tuple[str, str]]) -> GoalLadder:
    """从 [(序号 or '?', 条文)] 构造阶梯。'?' 按顺序补号。"""
    out: List[GoalItem] = []
    auto = 1
    for raw_idx, text in items:
        if raw_idx == "?":
            idx = auto
        else:
            idx = int(raw_idx)
        auto = max(auto, idx) + 1
        sup, dev = classify_goal_text(text)
        out.append(GoalItem(index=idx, text=text.strip(), support_rank=sup, dev_rank=dev))
    return GoalLadder(items=sorted(out, key=lambda x: x.index))


def load_ladders(path: Optional[Path] = None) -> Dict[str, Dict]:
    """读取范例学出的阶梯 + 儿童原始编号，作为分层的参照库。"""
    p = path or (ROOT / "data" / "structured" / "goal_ladder.json")
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("docs", {})


# --------------------------------------------------------------------------
# 分层判定
# --------------------------------------------------------------------------

# 支持层级语义 → 该幼儿在「支持序列」上的位置（0 = 需支持最多，7 = 独立）
SUPPORT_ORDER = {
    "大量身体辅助": 0, "全辅助": 0, "全身体辅助": 0,
    "身体辅助": 1,
    "少量身体辅助": 2,
    "手势辅助": 3,
    "少量手势辅助": 4, "少量辅助": 4,
    "展示示范": 5, "示范模仿": 5,
    "口语提示": 6, "视觉提示": 6,
    "独立完成": 7,
}


def layer_from_support(support_level: str,
                       n_layers: Optional[int] = None) -> int:
    """支持层级 → N 档层级（1 = 需支持最多）。

    N = 语料里的「组合规律」种数（`layer_count()`），**不是写死的 3**。
    映射按支持序列**等分**：位置 0..7 摊到 1..N。
    """
    n = n_layers or layer_count()
    pos = SUPPORT_ORDER.get((support_level or "").strip(), 3)
    lvl = 1 + round(pos / 7.0 * (n - 1))
    return max(1, min(n, int(lvl)))


def layer_from_mastery(mastery: float,
                       n_layers: Optional[int] = None) -> int:
    """达标率 → N 档层级（等分位；0 分进 L1、满分进 LN）。"""
    n = n_layers or layer_count()
    m = max(0.0, min(1.0, float(mastery or 0.0)))
    return max(1, min(n, 1 + int(m * n)))


def resolve_pattern(ladder: GoalLadder, pattern: List[str]) -> List[int]:
    """把「轴位规律」（如 `["R","S1","S3","A1"]`）落到**本课阶梯**上。

    记号约定：

        R   = 课堂常规条（人人必选）
        Sk  = 支持轴第 k 档（k=1 需支持最多；超出长度则跳过）
        Ak  = 发展轴第 k 条（按难度升序；超出长度则跳过）

    这样「组合规律」就能跨教案复用 —— 各课序号都从 1 开始但条文内容不同，
    只有轴位才是可移植的。
    """
    sup, adv = ladder.support_items(), ladder.advanced_items()
    out: List[int] = []
    for tok in pattern:
        idx: Optional[int] = None
        if tok == "R":
            idx = ladder.routine_index()
        elif tok.startswith("S") and tok[1:].isdigit():
            k = int(tok[1:])
            if 1 <= k <= len(sup):
                idx = sup[k - 1].index
        elif tok.startswith("A") and tok[1:].isdigit():
            k = int(tok[1:])
            if 1 <= k <= len(adv):
                idx = adv[k - 1].index
        if idx is not None and idx not in out:
            out.append(idx)
    return out


def combo_tier(indices: List[int], ladder: GoalLadder,
               n_layers: Optional[int] = None) -> int:
    """按「组合规律」判定一个目标组合属于第几层。

    做法：把本课阶梯上**四条组合规律**的实例都算出来，取与该组合**最接近**
    的那一条（对称差最小；并列时取较低层，保守不冒进）。

    ⚠ 为什么不直接按「有没有独立档 / 几条进阶条」判：不同教案的阶梯结构
    并不一致（有的把支持梯度放在指认上、有的放在表达上，`support_items()`
    分出来的档数在 1~4 之间浮动），硬判会把同一条组合在 A 课判成 L1、
    在 B 课判成 L3。改判"离哪条规律最近"就与分层表自洽了。
    """
    combos = layer_combos(ladder, n_layers)
    if not combos:
        return 1
    s = set(indices)
    lvl, _ = min(combos.items(),
                 key=lambda kv: (len(s ^ set(kv[1])), kv[0]))
    return lvl


def layer_combos(ladder: GoalLadder,
                 n_layers: Optional[int] = None) -> Dict[int, List[int]]:
    """本课的「层级 → 目标组合」表。

    每层一个组合 = 该层「组合规律」在本课阶梯上的实例。
    例（《国庆节到了》6 条阶梯）：L1=`123` L2=`124` L3=`145` L4=`1456`
    —— 这四个组合在语料里的出现次数分别是 26 / 15 / 20 / 24 条记录，
    且基线 P 密度单调递增（0.46 → 0.51 → 0.80 → 1.00），构成真实难度梯度。

    ⚠ 阶梯过短时两条规律可能落到同一个组合（如只有 1 条进阶条时
    `R+S3+A1` 与 `R+S3+A1+A2` 相同）。这里逐层去重：撞车就往上补一条
    **尚未用到、支持档最高**的条文，保证 N 层是 N 个不同组合。
    """
    n = n_layers or layer_count()
    out: Dict[int, List[int]] = {}
    used = set()
    for spec in LAYER_SPECS:
        lvl = int(spec["level"])
        if lvl > n:
            break
        combo = _fit_combo_size(
            ladder, resolve_pattern(ladder, list(spec["pattern"])))
        for _ in range(4):        # 最多补 4 次，够用
            key = frozenset(combo)
            if key not in used:
                break
            combo = _extend_combo(ladder, combo)
        used.add(frozenset(combo))
        out[lvl] = combo
    return out


def _extend_combo(ladder: GoalLadder, combo: List[int]) -> List[int]:
    """给撞车的组合补一条「尚未用到、支持档最高」的条文。"""
    pool = [it for it in ladder.items
            if it.index not in combo and not it.is_routine]
    pool.sort(key=lambda it: (-it.support_rank, -it.dev_rank, it.index))
    if not pool:
        return combo
    return _fit_combo_size(ladder, combo + [pool[0].index])


def pick_goal_indices(ladder: GoalLadder, layer: int,
                      n_layers: Optional[int] = None,
                      include_advanced: bool = False) -> List[int]:
    """按层级取条文组合。返回序号列表。

    ⚠ **必须凑够 3~4 条**。实测范例 13 份、170 条儿童记录，编号集合大小
    只有 3 或 4（`min=3 max=4 avg=3.37`），**从来没有 2 条**。
    """
    combos = layer_combos(ladder, n_layers)
    if not combos:
        return []
    hi = max(combos)
    lvl = max(1, min(hi, int(layer)))
    if lvl not in combos:
        below = [k for k in combos if k <= lvl]
        lvl = max(below) if below else min(combos)
    return list(combos[lvl])


def _fit_combo_size(ladder: GoalLadder, idxs: List[int]) -> List[int]:
    """把组合条数校正到 3~4 条（范例硬约束；见 `pick_goal_indices`）。"""
    idxs = list(dict.fromkeys(idxs))
    if len(idxs) < 3:
        # 补足：优先补**支持轴高档**（更能体现"跨到独立档"的层级差）
        pool = [(it.support_rank, it.index) for it in ladder.items
                if not it.is_routine and it.index not in idxs]
        pool.sort(key=lambda x: (-x[0], x[1]))
        for _, ix in pool:
            if ix not in idxs:
                idxs.append(ix)
            if len(idxs) >= 3:
                break
    if len(idxs) > 4:
        r0 = ladder.routine_index()
        keep = [i for i in idxs if i == r0]
        rest = [i for i in idxs if i != r0]
        sup_set = {it.index for it in ladder.support_items()}
        rest.sort(key=lambda i: (i not in sup_set, i))
        idxs = keep + rest[:max(0, 4 - len(keep))]
    return sorted(set(idxs))


def assign_layers(children: List[dict], ladder: GoalLadder,
                  target_domain: str,
                  force_spread: bool = True) -> List[ChildLayer]:
    """给一组匿名儿童判定层级。

    children 元素形如：
        {"anon_id": "S01", "mastery": 0.35, "support_level": "大量身体辅助",
         "domains": [{"domain": "精细动作", "mastery": 0.4,
                      "support_level": "少量身体辅助"}, ...]}

    判定分两步：

    1. **绝对判定** —— 用目标领域的数据（支持层级 + 达标率）映射三层。
       两条轴都可能给出档位，取**更需支持**的一侧（保守）。
       该领域无数据时回落到总体画像，并在依据里注明。

    2. **缺层补齐**（force_spread=True）—— 混龄康教班的画像里常出现某一档
       扎堆而另一档完全缺失（如语言沟通「手势辅助 12 人、独立完成 2 人」），
       严格按绝对档位会得到 L2=0，教案里那一层就空着。
       此时从相邻档挪**最少数量**的人补齐。

       ⚠ 不能无条件按 1/3 均分：不同领域的能力分布本来就不同
       （认知偏独立完成、语言沟通偏手势辅助），硬凑均分会把领域差异抹平。
       所以只在「某层为空」时才介入。
    """
    # ---- 1) 绝对判定 ----
    rows: List[dict] = []
    for ch in children:
        dp = None
        for d in ch.get("domains") or []:
            if d.get("domain") == target_domain:
                dp = d
                break

        if dp and dp.get("support_level"):
            mastery = float(dp.get("mastery") or 0.0)
            support = dp.get("support_level") or ""
            basis_src = f"领域「{target_domain}」"
        else:
            mastery = float(ch.get("mastery") or 0.0)
            support = ch.get("support_level") or ""
            basis_src = "总体画像（该领域无独立数据）"

        lyr_sup = layer_from_support(support)
        lyr_mas = layer_from_mastery(mastery)
        layer = min(lyr_sup, lyr_mas)
        score = _support_weight(support) * 0.7 + mastery * 0.3

        rows.append({
            "ch": ch, "score": score, "layer": layer,
            "final": layer, "mastery": mastery, "support": support,
            "lyr_sup": lyr_sup, "lyr_mas": lyr_mas,
            "basis_src": basis_src,
        })

    # ---- 2) 缺层补齐 ----
    if force_spread and len(rows) >= 3:
        from collections import Counter as _C
        cnt = _C(r["layer"] for r in rows)
        for L in layer_levels():
            if cnt.get(L, 0) > 0:
                continue
            donor = cnt.most_common(1)[0][0]
            if cnt.get(donor, 0) <= 1:
                continue
            cands = [r for r in rows if r["layer"] == donor]
            cands.sort(key=lambda r: r["score"])
            # 目标层在 donor 上方（更需支持）→ 挪能力最低的；
            # 反之挪能力最高的
            pick = cands[0] if L < donor else cands[-1]
            pick["final"] = L
            pick["adjusted"] = True
            cnt[donor] -= 1
            cnt[L] = 1

    # ---- 3) 按原始顺序组装 ----
    order = {c.get("anon_id"): i for i, c in enumerate(children)}
    rows.sort(key=lambda r: order.get(r["ch"].get("anon_id"), 0))

    out: List[ChildLayer] = []
    for r in rows:
        final = r["final"]
        why = (f"{r['basis_src']} 支持层级「{r['support'] or '无'}」→ 档 "
               f"{r['lyr_sup']}；达标率 {r['mastery']:.2f} → 档 {r['lyr_mas']}；"
               f"取更需支持侧 = L{final}")
        if r.get("adjusted"):
            why += "（该层原本无人，从相邻档按能力最低/最高者补入）"
        out.append(ChildLayer(
            anon_id=r["ch"].get("anon_id", "?"),
            layer=final,
            goal_indices=pick_goal_indices(ladder, final),
            basis=why,
            mastery=r["mastery"],
            support_level=r["support"],
        ))
    return out

def _support_weight(support: str) -> float:
    """把支持层级换成 0~1 的能力分（越大越独立），用于排序校准。"""
    v = SUPPORT_ORDER.get((support or "").strip())
    if v is None:
        return 0.5
    return v / 7.0


# --------------------------------------------------------------------------
# 动态调层：依五次评量结果上调 / 下调
# --------------------------------------------------------------------------


@dataclass
class EvalPoint:
    round_no: int          # 第几次评量
    passed: int            # 判 P 的条目数
    total: int             # 有效条目数（未参与不计）
    raw: str = ""


def parse_eval(raw: str) -> Optional[EvalPoint]:
    """解析一次评量。返回 None 表示未参与（不计入分母）。"""
    s = re.sub(r"[\s\u3000]+", "", raw or "").upper()
    if not s or "未参与" in (raw or ""):
        return None
    tokens = [c for c in s if c in "PE"]
    if not tokens:
        return None
    return EvalPoint(round_no=0, passed=tokens.count("P"), total=len(tokens), raw=raw)


@dataclass
class AdjustRule:
    """调层规则（用户确认的口径）。

    - `up_threshold` + `up_consecutive`：**连续 2 次 P 率 ≥ 85%** → 上调一层；
    - `down_threshold`：单次 P 率 < 40% → 下调一层；
    - 上限 = `layer_count()`（语料里的组合规律种数），**不是写死的 3**。
    """

    up_threshold: float = 0.85     # 连续 `up_consecutive` 次达此 P 率 → 上调
    up_consecutive: int = 2
    down_threshold: float = 0.4    # 单次低于此 P 率 → 下调
    no_data_hold: bool = True      # 未参与不做调整


DEFAULT_RULE = AdjustRule()


def adjust_layer(layer: int, evals: List[str],
                 rule: AdjustRule = DEFAULT_RULE,
                 n_layers: Optional[int] = None) -> Tuple[int, List[dict]]:
    """按评量序列调层。返回 (最终层级, 轨迹)。

    - 连续 `up_consecutive` 次 P 率 ≥ `up_threshold` → 上调一层（**一次只升一层**）
    - 任一有效次 P 率 < `down_threshold` → 下调一层
    - 「未参与」跳过，不计分母、不触发调整

    ⚠ 上限取 `n_layers`（默认 `layer_count()`）。层级 = 语料里的**组合规律**
    序号，升级 = 换到更高一层的组合。
    """
    n = n_layers or layer_count()
    hist: List[dict] = []
    cur = max(1, min(n, int(layer)))
    run_up = 0

    for i, raw in enumerate(evals, start=1):
        ep = parse_eval(raw)
        if ep is None:
            hist.append({"round": i, "raw": raw, "action": "hold",
                         "reason": "未参与，不参与调整", "layer": cur})
            if rule.no_data_hold:
                run_up = 0
            continue

        rate = ep.passed / ep.total if ep.total else 0.0
        act, why = "hold", f"P 率 {rate:.0%}（{ep.passed}/{ep.total}），维持"

        if rate >= rule.up_threshold:
            run_up += 1
            if run_up >= rule.up_consecutive and cur < n:
                cur += 1
                act, why = "up", (f"连续 {run_up} 次 P 率 ≥ {rule.up_threshold:.0%}"
                                  f"（本次 {rate:.0%}），上调至 L{cur}")
                run_up = 0
            elif run_up >= rule.up_consecutive:
                act, why = "hold", (f"连续 {run_up} 次 P 率 ≥ "
                                    f"{rule.up_threshold:.0%}，已是最高层 L{n}")
            else:
                act, why = "hold", (f"P 率 {rate:.0%} 达标，再观察"
                                    f"（连续 {run_up}/{rule.up_consecutive} 次）")
        else:
            run_up = 0
            if rate < rule.down_threshold and cur > 1:
                cur -= 1
                act, why = "down", (f"P 率 {rate:.0%} < {rule.down_threshold:.0%}，"
                                    f"下调至 L{cur}")

        hist.append({"round": i, "raw": raw, "p_rate": round(rate, 3),
                     "action": act, "reason": why, "layer": cur})

    return cur, hist


# --------------------------------------------------------------------------
# 康复目标 / 康复基线：升级后怎么更新
# --------------------------------------------------------------------------


def marks_by_index(indices: List[int], marks: str) -> Dict[int, str]:
    """把「编号串 + 记号串」还原成 {编号: 记号}（逐位对位）。

    范例里两串是**对位书写**的：`134` ↔ `P  PP` 表示
    序号 1→P、3→P、4→P（中间的空格只是排版留白，不是语义）。

    ⚠ 记号数与编号数不一致时**以编号数为准**：多则截断、少则补 `E`。
    """
    ms = "".join(c for c in (marks or "").upper() if c in "PE")
    out: Dict[int, str] = {}
    for k, idx in enumerate(indices):
        out[idx] = ms[k] if k < len(ms) else "E"
    return out


def compose_baseline(prev_marks: Dict[int, str], new_indices: List[int],
                     new_mark: str = "E") -> str:
    """**升级后重建「康复基线」**（用户确认的规则）。

    - 旧组合里**保留**的目标 → 沿用原记号；
    - **新加入**的目标 → `new_mark`（默认 `E` 未达标）。

    依据：基线 = "这个孩子**接手本课时**处在什么位置"。升级时新加进来的
    目标**还没评估过**，只能记未达标；保留下来的目标在上一轮已达标，
    记号继续有效。写回教案时顺序与新组合的编号升序一致。

    ⚠ 与语料的偏差（已在报告里向用户说明）：范例里不少老师会把
    "全 P 的孩子"的新目标也写成 `P`。这里按用户指定口径执行，
    `new_mark` 可配置成 `P` 以复现语料写法。
    """
    return "".join(prev_marks.get(i, new_mark) for i in new_indices)


def next_state(prev_layer: int, prev_indices: List[int], prev_marks: str,
               evals: List[str], ladder: GoalLadder,
               rule: AdjustRule = DEFAULT_RULE,
               n_layers: Optional[int] = None
               ) -> Tuple[int, List[int], str, List[dict]]:
    """算出本课该幼儿的 (层级, 目标组合, 康复基线, 调层轨迹)。

    这就是「评量结果回灌生成链路」的那一步：

        上一份的五次评量 ──adjust_layer──▶ 本课层级
        本课层级         ──layer_combos──▶ 本课目标组合
        本课目标组合 + 旧组合/旧记号 ──compose_baseline──▶ 本课康复基线
    """
    n = n_layers or layer_count()
    cur, hist = adjust_layer(prev_layer, evals, rule, n)
    new_idx = pick_goal_indices(ladder, cur, n)
    by_idx = marks_by_index(prev_indices, prev_marks)
    baseline = compose_baseline(by_idx, new_idx)
    return cur, new_idx, baseline, hist

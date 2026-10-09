# -*- coding: utf-8 -*-
"""生成新主题教案《国庆节到了》。

依据：最新教案《童心向党》（2026-06-29 — 07-03）里 14 名幼儿的实际表现。
实测 P 率：
    第1条 课堂常规        0.87  ← 已普遍掌握
    第2条 全辅助下指认     0.54  ← 约一半稳定
    第3条 少量辅助下指认+说 0.36  ← 明显吃力（最低）
    第4条 独立表达         0.42  ← 薄弱
    整体平均              0.531
结论：难度基本持平、略微前推 —— 继续巩固「指认」，同时把
「说出 / 表达」的支持梯度做足（这正是幼儿当前最大短板）。

产出：data/generated/国庆节到了.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lpa.schema import LessonPlan   # noqa: E402

THEME = "国庆节到了"

PLAN = {
    "title": "认知课《国庆节到了》",
    "class_label": "混龄康教班（已匿名）",
    "age_range": "3-9岁",
    "theme": THEME,
    "class_size": 12,
    "total_duration_min": 30,
    "date_range": "2026年10月12日—10月16日",

    # ---- 有益经验（班级总目标）----
    "class_objectives_text": (
        "1.能了解十月一日是国庆节，知道这是祖国妈妈的生日，"
        "说出或指出国庆节的主要庆祝方式。"
        "2.愿意参与集体活动，能和老师、同伴一起完成操作。"
        "3.在活动中练习听指令与等待，逐步减少对提示的依赖。"
    ),

    "main_domain": "认知",

    # ---- 分层目标（A/B/C）----
    "objectives": [
        {
            "level": "A",
            "domain": "情绪行为",
            "support_level": "手势辅助",
            "text": "能在提示下整节课堂安坐，不走出位置",
            "observable_verb": "安坐",
            "criteria": "整节课累计离开位置不超过 1 次",
        },
        {
            "level": "B",
            "domain": "认知",
            "support_level": "少量手势辅助",
            "text": "能在少量提示下指认国旗，并说出国旗的主要特征",
            "observable_verb": "指认",
            "criteria": "能在 3 张图片中正确指认国旗，并说出 1~2 个特征",
        },
        {
            "level": "C",
            "domain": "认知",
            "support_level": "独立完成",
            "text": "能独立指认国旗，并用完整短句表达国庆节的庆祝方式",
            "observable_verb": "表达",
            "criteria": "能独立指认国旗，并说出 1 种以上庆祝方式",
        },
    ],

    "materials": [
        "国庆节相关的动画视频、图片、PPT课件，如街道挂国旗、"
        "天安门广场升旗、人们庆祝的画面等。",
        "小道具，如小国旗、红色五角星贴纸。",
        "儿歌《国旗国旗真美丽》音频与伴奏。",
        "供幼儿操作的大尺寸国旗拼图（星星可粘贴）。",
        "教师示范用的大幅五星红旗图片一张。",
    ],
    "environment": "活动区清空无关材料，桌椅按半圆摆放，"
                   "保证每位幼儿都能看到老师与图片。",
    "safety_notes": "活动材料均为大尺寸、无尖角，使用前检查有无破损；"
                    "精细动作环节需教师全程在旁，避免幼儿将小件材料放入口中。",

    # ---- 教学过程：steps 会被归入四个固定小标题 ----
    "steps": [
        {
            "index": 1,
            "name": "师生问好",
            "duration_min": 2,
            "teacher_script": "师：摇铃鼓，小朋友们请坐好，小手放好。"
                              "小朋友们早上好！"
                              "（等待每一位幼儿用语言、点头或挥手回应）",
            "child_action": "幼儿用语言、点头或挥手回应老师",
            "visual_cue": "手势：老师挥手，等幼儿回应后点头微笑",
            "scaffolds": ["用手势和表情示意，等幼儿回应",
                          "允许用点头、挥手代替说话"],
            "observation_points": ["幼儿是否需要提示才能完成问好"],
        },
        {
            "index": 2,
            "name": "点名互动",
            "duration_min": 2,
            "teacher_script": "师：下面老师要看看我们班今天都有哪几位小朋友来上课了，"
                              "点到名字的小朋友，要说“到”并举手。",
            "child_action": "被点到的幼儿说“到”并举手",
            "visual_cue": "手势：老师先举起手示范，再逐一邀请幼儿",
            "scaffolds": ["老师先示范一次举手说“到”",
                          "允许用举手或点头代替说“到”"],
            "observation_points": ["幼儿是否能对点名作出回应"],
        },
        {
            "index": 3,
            "name": "手指律动",
            "duration_min": 3,
            "teacher_script": "师：下面请小朋友跟着老师一起来动动小手吧！"
                              "拍拍手，一二三三四、五六七七八、二二三三四、五六七七八。",
            "child_action": "幼儿跟随老师一起拍手、做手指动作",
            "visual_cue": "示范：老师站在幼儿正前方，动作放大放慢",
            "scaffolds": ["老师面对面示范，动作放慢",
                          "必要时手把手带做一次"],
            "observation_points": ["幼儿是否愿意跟着做动作"],
        },
        {
            "index": 4,
            "name": "音乐律动",
            "duration_min": 3,
            "teacher_script": "师：我们一起听着音乐来动一动吧！"
                              "（播放《国旗国旗真美丽》，带领幼儿跟唱并做拍手、挥手的动作）"
                              "国旗国旗真美丽，五颗星星照大地，"
                              "祖国前进我长大，我向国旗敬个礼。",
            "child_action": "幼儿跟唱儿歌，配合做拍手、挥手动作",
            "visual_cue": "提示：老师举小国旗，随节奏左右挥动",
            "scaffolds": ["老师放大动作带头做",
                          "不会唱的幼儿只做拍手动作即可"],
            "observation_points": ["幼儿能否跟上节奏做动作"],
        },
        {
            "index": 5,
            "name": "导入环节",
            "duration_min": 3,
            "teacher_script": "师：小朋友们，看看老师手里拿的是什么呀？"
                              "（举起五星红旗）"
                              "它是红色的，上面还有星星呢。"
                              "你们在什么地方见过它呀？"
                              "（引导幼儿自由回答，引出本节课主题——国庆节）",
            "child_action": "幼儿观察国旗并尝试回答在哪里见过国旗",
            "visual_cue": "实物：老师举起五星红旗，让每位幼儿都能看到",
            "scaffolds": ["老师用手指着国旗的颜色和星星",
                          "允许幼儿只指一指、不回答"],
            "observation_points": ["幼儿是否注意到国旗的颜色与图案"],
        },
        {
            "index": 6,
            "name": "认识国庆节",
            "duration_min": 5,
            "teacher_script": "师：每年的十月一日是国庆节，"
                              "是我们祖国妈妈的生日。"
                              "（播放短视频：街道挂满国旗，人们开心庆祝）"
                              "视频里的人们在做什么呀？"
                              "他们为什么这么开心呢？"
                              "（鼓励幼儿尝试回答，老师及时给出肯定）",
            "child_action": "幼儿观看视频，尝试说出自己看到的画面",
            "visual_cue": "视频 + 图片：街道挂国旗、天安门升旗的画面",
            "scaffolds": ["先请能力好的幼儿回答，再邀请其他幼儿重复",
                          "老师给出两个选项让幼儿选择，例如“是挂灯笼还是挂国旗？”"],
            "observation_points": ["幼儿能否说出国庆节看到的情景"],
        },
        {
            "index": 7,
            "name": "认识五星红旗",
            "duration_min": 6,
            "teacher_script": "师：我们再来仔细看一看国旗。"
                              "它是什么颜色的呀？"
                              "（红色）"
                              "上面有几颗星星呢？"
                              "（五颗）"
                              "是一颗大星星和四颗小星星。"
                              "红色的旗面代表着勇敢，黄色的星星代表着我们团结在一起。"
                              "下面老师指到哪里，小朋友就说出它的名字，好不好？",
            "child_action": "幼儿观察国旗，说出颜色和星星数量",
            "visual_cue": "图片：国旗特写，红色旗面与五颗黄色星星放大呈现",
            "scaffolds": ["老师先完整示范一遍指认与命名",
                          "从“指出颜色”开始，再过渡到“说出星星数量”",
                          "答不出时用二选一提示，例如“是一颗还是五颗？”"],
            "observation_points": ["幼儿能否指认国旗并说出特征",
                                   "是否需要身体辅助才能完成"],
        },
        {
            "index": 8,
            "name": "我指你说游戏",
            "duration_min": 4,
            "teacher_script": "师：我们来玩一个“我指你说”的游戏。"
                              "老师指到国旗，小朋友就大声说出它的名字。"
                              "（依次指向国旗、五角星、红色旗面，"
                              "让每位幼儿都有一次说的机会）",
            "child_action": "幼儿根据老师指的位置，说出对应名称",
            "visual_cue": "手势：老师指向图片上的具体位置",
            "scaffolds": ["先集体齐说一遍，再请个别幼儿单独说",
                          "说不出的幼儿可以只指一指"],
            "observation_points": ["幼儿能否在无身体辅助下说出名称"],
        },
        {
            "index": 9,
            "name": "讨论与分享",
            "duration_min": 3,
            "teacher_script": "师：国庆节的时候，我们可以用什么方式来庆祝呢？"
                              "（引导幼儿说出：挂国旗、看升旗、唱歌、放假出去玩等）"
                              "小朋友们说得真棒！我们要爱我们的祖国妈妈。",
            "child_action": "幼儿尝试说出自己知道的庆祝方式",
            "visual_cue": "图片：挂国旗、升旗仪式、唱国歌的庆祝画面",
            "scaffolds": ["老师先给出一个例子，再请幼儿补充",
                          "用图片提示，让幼儿看图说"],
            "observation_points": ["幼儿能否说出至少一种庆祝方式"],
        },
        {
            "index": 10,
            "name": "教师总结",
            "duration_min": 2,
            "teacher_script": "师：今天小朋友们知道了，"
                              "十月一日是国庆节，是祖国妈妈的生日。"
                              "我们还认识了五星红旗，"
                              "它是红色的，上面有一颗大星星和四颗小星星。"
                              "回家以后，可以和爸爸妈妈一起找一找身边的国旗哦！"
                              "好了，这节课就到这里了，小朋友们再见！",
            "child_action": "幼儿倾听总结，和老师说再见",
            "visual_cue": "手势：老师挥手说再见，等幼儿回应",
            "scaffolds": ["用简洁的短句重复要点",
                          "允许幼儿用挥手代替说再见"],
            "observation_points": ["幼儿能否跟着回顾本节课的关键词"],
        },
    ],

    # ---- 个别化目标（A/B/C 三层）----
    "individualized_goals": [
        {
            "level": "A",
            "share_of_class": "约 5 名",
            "goal": "能跟随老师完成国庆节主题中的一步内容，并用指认表示理解",
            "baseline_hint": "起点依首次评量，不参与也属正常；按后续三次收敛速度判断",
            "evaluation_method": "每节课观察记录「是否需要身体辅助」",
            "follow_up": "若连续两次仍需全辅助，把任务再拆小一步，只要求完成其中一个动作",
        },
        {
            "level": "B",
            "share_of_class": "约 2 名",
            "goal": "能在少量提示下指认国旗，并说出一个主要特征",
            "baseline_hint": "已有指认基础，重点观察提示量能否逐步减少",
            "evaluation_method": "记录每次评量中「说」的部分是否需要手势提示",
            "follow_up": "若连续三次仍需手势提示，把「说」拆成二选一作答",
        },
        {
            "level": "C",
            "share_of_class": "约 5 名",
            "goal": "能独立指认国旗，并用短句表达一种国庆节的庆祝方式",
            "baseline_hint": "指认已稳定，重点观察表达的完整度与主动性",
            "evaluation_method": "记录能否独立说出，以及句子是否完整",
            "follow_up": "若表达仍不完整，先用图片提示补全句子，再逐步撤去",
        },
    ],

    "alternatives": [
        {
            "situation": "幼儿无法完成指认或命名",
            "alternative_participation": "改为让幼儿用手指出、点头示意，"
                                         "或从两个选项中挑选一个",
            "alternative_evaluation": "能在两个选项中正确选出，即视为达成",
        },
        {
            "situation": "幼儿不肯开口表达",
            "alternative_participation": "允许用指认、点头或举小国旗代替说话",
            "alternative_evaluation": "能用非语言方式正确回应，即视为达成",
        },
        {
            "situation": "幼儿情绪波动、无法继续参与",
            "alternative_participation": "先请其在旁边安静休息，"
                                         "待情绪稳定后由老师单独邀请加入",
            "alternative_evaluation": "能在安抚后重新回到集体活动中，即视为达成",
        },
    ],

    "assessment": {
        "A": "能在身体辅助或示范下参与活动并有回应，即视为达成",
        "B": "能在手势提示下完成核心操作，提示不超过 2 次，即视为达成",
        "C": "能独立完成核心操作并说出名称，即视为达成",
    },

    "extension": (
        "1.生活延伸：在一日生活的相关环节中，请幼儿看一看、说一说身边的国旗。"
        "2.家园共育：请家长和孩子一起找一找身边的五星红旗，"
        "孩子说到就及时表扬。"
    ),
    "reflection_slots": "（此处留白，供带班老师课后填写）",

    "key_points": {
        "focus": "认识国庆节与五星红旗，说出其主要特征",
        "difficulty": "在减少提示的条件下，把「指认」推进到「表达」",
    },
    "home_extension": "请家长配合，在家庭生活中和孩子一起找一找国旗，"
                      "每次不超过 5 分钟。",
    "reflection_points": [
        "本次活动中，哪些幼儿需要比预设更多的支持？",
        "哪个环节的提示可以再减少一些？",
        "「说出」环节的复练次数是否足够？下次是否需要增加？",
    ],
}


def main() -> None:
    plan = LessonPlan(**PLAN)
    out = ROOT / "data" / "generated" / f"{THEME}.json"
    out.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8")
    md = ROOT / "data" / "generated" / f"{THEME}.md"
    md.write_text(plan.to_markdown(), encoding="utf-8")
    print(f"[gen] {out}")
    print(f"[gen] {md}")
    print(f"      标题 {plan.title} | 领域 {plan.main_domain} | "
          f"时长 {plan.total_duration_min}min | 步骤 {len(plan.steps)} 个")


if __name__ == "__main__":
    main()

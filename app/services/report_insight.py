"""把能力画像翻译成"人话"：解释、弱项细分、提升建议、反复出错的语句。

## 为什么单独一个模块

`ability_service.py` 只管**打分**（证据 → 数字），这里管**表达**（数字 → 报告正文）。
分开的理由很实际：打分逻辑要能被单元测试钉死（单调性、边界、优先级），
而建议文案会反复改措辞。混在一起的话，改一句话就要动打分模块。

## 全部是规则，不调用大模型

老师在没配 API Key 的机器上也要能导出报告（这是硬要求，见 README 6.7）。
所以这里的每一句话都来自固定规则表：
  · 等级      —— 分数落到哪个档
  · 弱项细分  —— 直接来自 `AbilityService` 收集的 contributors（知识点 + 错误类型）
  · 提升建议  —— 「维度 × 等级」查表，再把"拖后腿的知识点"拼进开头
  · 反复语句  —— 对错题里的代码行做跨记录计数

**建议是总结性话术，不是具体题目。** 给学生一道题属于「举一反三」的职责
（那需要大模型），报告里的建议要说的是"这类问题的根子在哪、下次怎么避免"。
"""

from __future__ import annotations

from collections import Counter

# 等级分档：分数 >= 阈值 就是这一档。顺序必须从高到低。
LEVELS: tuple[tuple[int, str], ...] = (
    (90, "优秀"),
    (75, "良好"),
    (60, "待加强"),
    (0, "薄弱"),
)

# 低于这个分数才在报告里单独展开（满分/接近满分的维度不占用篇幅）
WEAK_SCORE_THRESHOLD = 85

# 「反复出错的语句」的判定门槛
RECURRING_MIN_RECORDS = 2      # 至少在几条不同错题里出现过才算"反复"
RECURRING_LIMIT = 8            # 最多列几行
RECURRING_MIN_LENGTH = 6       # 太短的行（"}"、"return"）没有解释价值
RECURRING_MAX_CHARS = 72       # 报告里一行放得下的长度


def level_of(score: int) -> str:
    """分数 → 等级。"""
    for threshold, label in LEVELS:
        if score >= threshold:
            return label
    return LEVELS[-1][1]


# ---------------------------------------------------------------------------
# 怎么读这张雷达图
# ---------------------------------------------------------------------------
RADAR_GUIDE = (
    "这张图把六项能力分别画在六根轴上，从圆心到最外圈代表 0 到 100 分。"
    "把六个点连起来得到的形状就是当前的能力画像：某个角越靠近圆心，说明这一项越弱；"
    "图上的红色圆点标出的是当前最弱的那一项。"
    "六项分数全部由错题与检测记录折算而来——一条「错误」级记录记 3 分权重、"
    "警告级 1.5 分、提示级 0.75 分，分数 = 100 ÷（1 + 权重和 ÷ 10）。"
    "因此**形状的相对高低比绝对分数更有意义**：它回答的是「先补哪一项收益最大」。"
    "一条记录都没有时六项都是满分，那只表示「暂时还没发现弱点」。"
)

# ---------------------------------------------------------------------------
# 提升建议：维度 × 等级
# ---------------------------------------------------------------------------
# 写作要求（给以后改文案的人）：
#   1. 说清"这类问题的根子在哪"，而不是"多练习"这种空话；
#   2. 给一个**下次能立刻做**的动作；
#   3. 不点具体题目 —— 出题是「举一反三」的职责。
ADVICE: dict[str, dict[str, str]] = {
    "completeness": {
        "薄弱": (
            "代码经常处于「写了一半」的状态：分支没走全、返回值缺失、括号或缩进没有闭合。"
            "这类问题不用运行就能发现，根子在于「想到哪写到哪」。"
            "下次动手前先把要处理的**情况列成清单**（正常值、边界值、空值各是什么），"
            "写完对着清单逐条核对，最后通读一遍整段代码再提交。"
        ),
        "待加强": (
            "整体结构是完整的，但个别分支仍有遗漏。"
            "建议养成「先列情况、再写判断」的顺序，而不是边写边想；"
            "提交前把每个 if / else 都问一遍：剩下的那种情况去哪了。"
        ),
        "良好": "分支覆盖基本完整，继续保持提交前通读一遍的习惯即可。",
        "优秀": "代码结构完整、分支覆盖到位，这是很扎实的基础。",
    },
    "conventions": {
        "薄弱": (
            "反复出现语法与格式类问题，说明写的时候对语言规则的细节还不够熟。"
            "这类错误**编译器或解释器一定会先告诉你**，代价最低也最不该发生。"
            "建议每写完一小段就先运行一次（哪怕只是语法检查），"
            "不要攒到最后一起调试；另外留意缩进与括号成对，写左括号时顺手把右括号补上。"
        ),
        "待加强": (
            "已经能写出跑得通的代码，但格式上还有随意的地方。"
            "建议固定一套排版习惯（缩进、空行、命名风格）并且每次都用同一套，"
            "减少因为格式不一致带来的阅读成本。"
        ),
        "良好": "语法与格式比较规范，继续保持。",
        "优秀": "代码规范意识很好，格式统一、可读性有保障。",
    },
    "readability": {
        "薄弱": (
            "变量名、函数名与注释让代码不容易读懂，这会直接影响你自己过几天再回来看的速度。"
            "根子往往是「先随便起个名，打算以后再改」——而以后通常不会改。"
            "建议命名时直接写清用途（sum_scores 而不是 s），"
            "关键判断与不直观的地方补一行注释说明**为什么**这么写。"
        ),
        "待加强": (
            "代码能读懂，但有几处命名或结构不够清楚。"
            "建议给含义模糊的变量换个名字，并把过长的函数按职责拆成两三个小函数 —— "
            "名字起对了，很多事情不用注释也说得清。"
        ),
        "良好": "命名与注释基本到位，代码读起来不费力。",
        "优秀": "可读性很好，命名达意、结构清晰。",
    },
    "efficiency": {
        "薄弱": (
            "算法与复杂度方面的意识还比较薄弱，容易出现多层嵌套循环或重复计算。"
            "根子在于「先把功能写出来」之后没有回头看一遍代价。"
            "建议写完功能后多问一句：这段循环里有没有重复做的事？能不能先算一次存起来？"
            "另外在动手前养成估算数据规模的习惯（几百条和几百万条，写法是不一样的）。"
        ),
        "待加强": (
            "功能实现没问题，但个别地方存在可以省掉的重复计算。"
            "建议在循环内部避免重复调用同一个函数，把不变的结果提到循环外面。"
        ),
        "良好": "复杂度意识不错，没有明显的多余计算。",
        "优秀": "会主动考虑算法代价与数据规模，这点很难得。",
    },
    "robustness": {
        "薄弱": (
            "多次出现边界与异常相关的问题（越界、空值、除零、未初始化），"
            "说明目前写代码时主要考虑「正常情况」，而对「意外情况」的防御不够。"
            "根子在于没有养成检查输入的习惯。"
            "建议每写一个函数，先问三个问题：参数会不会是空/0/超出范围？"
            "如果会，应该返回什么而不是崩溃？调用方拿到的返回值有没有可能出错？"
        ),
        "待加强": (
            "大部分情况处理到了，个别边界还有疏漏。"
            "建议专门针对「最小、最大、空、重复」这四种输入各想一遍，"
            "尤其是循环的下标范围与结束条件。"
        ),
        "良好": "边界与异常处理比较周到，能把意外情况兜住。",
        "优秀": "防御性写得很到位，这是工程能力的直接体现。",
    },
    "accuracy": {
        "薄弱": (
            "逻辑与结果层面的错误比较集中，说明问题不在「写不出来」，"
            "而在「想清楚之前就动手了」。"
            "根子通常是跳过了用例子推演这一步。"
            "建议写完函数后，用一组具体数字在纸上（或注释里）走一遍，"
            "看每一步的中间结果是不是你预期的；发现对不上，通常就是运算符、"
            "边界条件或返回值用错了。"
        ),
        "待加强": (
            "整体逻辑方向是对的，个别细节会算错。"
            "建议对含有判断和运算的语句单独验算一遍，"
            "特别注意「小于还是小于等于」「加还是减」这类成对出现的符号。"
        ),
        "良好": "逻辑基本正确，结果稳定。",
        "优秀": "逻辑严谨，结果可靠。",
    },
}


def advice_for(dimension: dict) -> str:
    """给一个维度生成提升建议（总结性话术 + 开头点明"是因为什么"）。"""
    key = dimension.get("key", "")
    level = level_of(int(dimension.get("score", 100)))
    text = ADVICE.get(key, {}).get(level, "")
    if not text or level in ("优秀",):
        return text

    contributors = dimension.get("contributors") or []
    if not contributors:
        return text

    top = contributors[0]
    reason = f"「{top['knowledge_point']}」（{top['error_type']}）出现 {top['count']} 次"
    if len(contributors) > 1:
        others = "、".join(f"「{item['knowledge_point']}」" for item in contributors[1:3])
        reason += f"，其次是 {others}"
    return f"主要卡在 {reason}。{text}"


def build_insights(profile: dict) -> list[dict]:
    """把能力画像展开成"逐维度的诊断"：分数、等级、成因、建议。

    只展开**低于阈值**的维度，并且按分数从低到高排 ——
    报告要先讲最该补的那一项，而不是按固定顺序罗列。
    全部达标时返回空列表，由调用方写一句"六项都过关"。
    """
    items: list[dict] = []
    for dimension in profile.get("dimensions", []):
        score = int(dimension.get("score", 100))
        if score >= WEAK_SCORE_THRESHOLD:
            continue
        items.append(
            {
                "key": dimension["key"],
                "label": dimension["label"],
                "hint": dimension["hint"],
                "score": score,
                "level": level_of(score),
                "evidence_count": dimension.get("evidence_count", 0),
                "contributors": list(dimension.get("contributors") or []),
                "advice": advice_for(dimension),
            }
        )
    items.sort(key=lambda item: item["score"])
    return items


def summarize_causes(insights: list[dict]) -> str:
    """把"最该补的几项"压成一句话，放在小节开头。"""
    if not insights:
        return "六项能力都达到了设定标准（85 分以上），本区间没有需要重点补的短板。"
    worst = insights[0]
    line = (
        f"本区间最需要补的是「{worst['label']}」（{worst['score']} 分，{worst['level']}）"
    )
    if len(insights) > 1:
        names = "、".join(f"「{item['label']}」" for item in insights[1:3])
        line += f"，其次是 {names}"
    return line + "。下面逐项说明成因与改进方向。"


# ---------------------------------------------------------------------------
# 反复出错的语句
# ---------------------------------------------------------------------------
def _normalize(line: str) -> str:
    """把一行代码压成用于比对的形式：去掉首尾空白、把连续空白并成一个空格。"""
    return " ".join(line.split())


def _worth_reporting(normalized: str) -> bool:
    """太短或纯符号的行不值得单独列出来。

    `}`、`else:`、`return` 这类行确实会反复出现，但它们出现是因为
    **代码里到处都有**，不构成"这句一直在错"的证据。
    """
    if len(normalized) < RECURRING_MIN_LENGTH:
        return False
    # 只剩符号与空白（括号、分号、大括号…）
    stripped = normalized.strip("{}()[];:, \t")
    return bool(stripped)


# 结构性关键字：函数/类/导入的声明行不是"出错的那句"
_STRUCTURAL_PREFIX = ("def ", "class ", "import ", "from ", "#", "//", "/*", "*", "@")
# 整行就是这些的，同样没有信息量
_STRUCTURAL_EXACT = frozenset(
    {"}", "};", "end", "return", "pass", "break", "continue", "else:", "try:", "fi", "done"}
)
# 控制流关键字：出错频率最高的地方就在这些语句上
_CONTROL_TOKENS = ("for ", "while ", "if ", "elif ", "else if", "range(", "switch", "case ")
# 能参与运算的符号
_OPERATOR_CHARS = "+-*/%<>=!&|"
# 这些语句必须以冒号或花括号收尾；没收尾的本身就是语法错误
_NEEDS_TERMINATOR = (
    "def ", "class ", "if ", "elif ", "else", "for ", "while ", "switch", "case ",
)


def _suspicion(line: str) -> int:
    """给一行代码打一个"像不像出错的那句"的分。

    为什么需要它：判别力（只在本类错题里出现）还不够 ——
    `def total(scores):` 和 `result = 0` 同样满足判别力，但它们是样板行。
    实测里靠这一项把 `for i in range(len(scores) - 1):` 顶到第一位、
    把 `def` / `return` 压下去，每个知识点只留最有嫌疑的一行。

    分数只用于**排序**，不做阈值判断 —— 判据越简单越不容易在数据变化时失效。
    """
    stripped = line.strip()
    score = 0
    if stripped.startswith(_STRUCTURAL_PREFIX) or stripped in _STRUCTURAL_EXACT:
        score -= 5
    if any(token in line for token in _CONTROL_TOKENS):
        score += 3
    if "[" in line or "]" in line:
        score += 2
    if any(ch in line for ch in _OPERATOR_CHARS):
        score += 1
    if stripped.startswith("return "):
        score -= 1
    # 「def / if / for 结尾没有冒号」正是语法错误的典型形态。
    # 这一条必须能盖过上面的 -5，否则真实案例会被埋掉：
    # 实测一组「括号不匹配」的错题，出错的就是 `def add(a, b)`（少了冒号），
    # 却因为 def 前缀被扣分而输给了同文件里无辜的 `return a - b`。
    if stripped.startswith(_NEEDS_TERMINATOR) and not stripped.endswith((":", "{", "}")):
        score += 7
    return score


def recurring_lines(
    rows: list, *, limit: int = RECURRING_LIMIT
) -> list[dict]:
    """找出"反复出错的具体语句"——**按知识点分组，每组留最有嫌疑的一行**。

    ## 为什么不能只看"这行出现了几次"

    第一版就是这么写的，结果是噪音：错题记录里存的是**整份代码**，
    学生把同一个文件反复提交时，文件里的每一行（包括 `def total(scores):`
    和 `result = 0`）都会出现在全部错题里，按次数排序等于把整份源码列出来。

    于是换成两级判据：

      1. **判别力**（硬条件）：这一行在"本知识点的错题"里出现得多、
         在"其它知识点的错题"里出现得少。实测里
         `for i in range(len(scores) - 1):` 出现在 3 条「循环边界」里、
         0 条其它错题里。
      2. **嫌疑度**（排序用，见 `_suspicion`）：控制流与含下标/运算符的语句
         排在前面，`def` / `import` / 光秃秃的 `return` 排后面。

    每个知识点只留**一行**，避免一份代码刷满半页。
    区分不出来时（例如所有错题都来自同一份代码，判据对每行都一样），
    返回空列表，由 `recurring_summary()` 如实说明，而不是硬凑几行出来。
    """
    # 每条错题里出现过的行（归一化），以及每行的全局出现次数
    per_record: list[tuple[str, set[str]]] = []
    line_total: Counter[str] = Counter()
    for row in rows:
        snippet = getattr(row, "code_snippet", "") or ""
        if not snippet:
            continue
        seen = {
            _normalize(line)
            for line in snippet.splitlines()
            if _worth_reporting(_normalize(line))
        }
        if not seen:
            continue
        point = getattr(row, "knowledge_point", "") or "未分类"
        per_record.append((point, seen))
        for line in seen:
            line_total[line] += 1

    if not per_record:
        return []

    group_records: Counter[str] = Counter(point for point, _ in per_record)
    group_lines: dict[str, Counter[str]] = {}
    for point, seen in per_record:
        bucket = group_lines.setdefault(point, Counter())
        for line in seen:
            bucket[line] += 1

    # 每个知识点先各自挑出最有嫌疑的一行
    best_per_group: list[dict] = []
    for point, bucket in group_lines.items():
        if group_records[point] < RECURRING_MIN_RECORDS:
            continue
        choices: list[tuple[int, int, int, str]] = []
        for line, in_group in bucket.items():
            if in_group < RECURRING_MIN_RECORDS:
                continue
            outside = line_total[line] - in_group
            # 别处也常见 → 是样板行（`return result`、`}`…），不是"这句一直在错"
            if outside >= in_group:
                continue
            choices.append((_suspicion(line), in_group - outside, len(line), line))
        if not choices:
            continue
        choices.sort(reverse=True)
        suspicion, margin, _, line = choices[0]
        best_per_group.append(
            {
                "line": line,
                "count": bucket[line],
                "group_records": group_records[point],
                "knowledge_point": point,
                "margin": margin,
                "suspicion": suspicion,
            }
        )

    best_per_group.sort(key=lambda item: (-item["margin"], -item["suspicion"], -item["count"]))

    out: list[dict] = []
    for entry in best_per_group[:limit]:
        line = entry["line"]
        out.append(
            {
                "line": line
                if len(line) <= RECURRING_MAX_CHARS
                else line[: RECURRING_MAX_CHARS - 1] + "…",
                "count": entry["count"],
                "knowledge_point": entry["knowledge_point"],
                "group_records": entry["group_records"],
            }
        )
    return out


def recurring_summary(items: list[dict], *, record_count: int = 0) -> str:
    """给「反复出错的语句」小节配一句话。"""
    if not items:
        if record_count == 0:
            return "本区间没有错题记录，暂时分析不出反复出错的语句。"
        return (
            "本区间没能定位到「只属于某一类错误的语句」——常见原因是这些错题来自"
            "同一份代码（每一行在所有错题里都出现，区分不出来），"
            "或者每次错的地方都不一样。后一种情况说明问题比较分散，"
            "建议先把出现次数最多的那一类知识点弄清楚（见上一节）。"
        )
    top = items[0]
    return (
        f"下表列出的是**只在与某一类错误相关的错题里反复出现**的语句："
        f"首行的 `{top['line']}` 在 {top['count']} 条「{top['knowledge_point']}」错题里出现，"
        "而其它类型的错题里没有。反复错同一行通常不是手误，而是某个概念还没真正理顺 —— "
        "把这行单独拎出来，用一组具体数字手工走一遍，比再刷十道新题有用。"
    )


__all__ = [
    "ADVICE",
    "LEVELS",
    "RADAR_GUIDE",
    "RECURRING_MIN_RECORDS",
    "WEAK_SCORE_THRESHOLD",
    "advice_for",
    "build_insights",
    "level_of",
    "recurring_lines",
    "recurring_summary",
    "summarize_causes",
]

"""规则模板房源指代消解 resolver（Story 6.2，OQ-5 选型前默认实现）。

手写纯 Python、同步、零 I/O 的纯函数 resolver（``CorefResolver`` 钩子
契约，filters/coreference.py）：先行词锚点仅取 ``envelope.house_id``
（信封必填字段，即会话单一房源），把消息中的房源指代表达（它/这套/
那套/该房/这套房/那套房子/这个房子 等）单趟扫描归一化为对齐 PRD
示例的全角显式引用"这套房（house_id: {house_id}）"——做的是归一化
而非开放式实体链接：不过问指代是否真指该房源（会话已锚定单一房源，
误命中代价低、漏命中走恒等降级）。

产出：``(resolved, refs)``——``resolved`` 为替换后文本（无命中逐字节
等于原文），``refs`` 为去重后的单条 house ``CorefRef``（同消息多处
命中只描述一个实体锚点，ref_count ∈ {0, 1}；``resolved_message`` /
``coref`` 仍由 coreference 过滤器经 ``model_copy(update=...)`` 唯一
写入，resolver 不碰信封）。resolver 任何异常由过滤器 6.1 fail-safe
契约承接（回落透传、resolver_error=True），本模块自身无状态、微秒级。

「上面说的」类前缀短语随其后的量词名词命中（如"上面说的那套"命中
"那套"），前缀自然保留；非房源实体独立指代（那层/那个价位 等价格、
楼层、位置表达）MVP 不做独立归一化——裸指示代词后紧跟非房源实体
名词时守卫跳过该处替换，保留原文走既有降级路径（CorefRef 的
price/floor/location 类型为 OQ-5 选型后扩展留口）。

已知限制（归一化而非开放式实体链接的固有退化形态）：
- 跨房源比较（"这套比那套贵吗"）两处命中均归一化为同一会话房源
  引用，形成自比较——spec Never 明确不做跨房源指代，MVP 对此类
  输入不做特殊处理；
- 输入已含显式引用标注（"这套房（house_id: x）"）时该处跳过替换，
  不双重标注，整条消息无新命中即恒等透传（不反推已标注引用的
  coref 元数据）。
"""

import re

from weiliao_agent.models.coref import CorefRef
from weiliao_agent.models.envelope import Envelope

# 房源指代表达模式表（覆盖"它/这个/这套/那套/该房/这套房/那套房子"
# 等）。替换点以 Golden 示例为准（spec Design Notes）："这套房"必须先于
# "这套"命中（按长度降序构造 alternation，同一起点最长模式优先），
# 否则 "这套房还有吗" 会被截成 "...显式引用）房还有吗"。
_HOUSE_PATTERNS: tuple[str, ...] = (
    "这套房子",
    "那套房子",
    "这个房子",
    "那个房子",
    "这套住房",
    "这间房",
    "那间房",
    "该房子",
    "该房屋",
    "本房源",
    "这套房",
    "那套房",
    "该房源",
    "这房子",
    "那房子",
    "此房",
    "此套",
    "这套",
    "那套",
    "该房",
    "这间",
    "那间",
    "这个",
    "那个",
    "它",
)

# 裸指示代词（这个/那个）守卫：其后紧跟非房源实体名词（价格/楼层/
# 位置/周末/小区/中介/合同/物业/餐 等）时该处指代按非房源独立指代
# 处理，跳过替换、保留原文——对齐 spec I/O 矩阵"那个价位呢"原文保留、
# coref=[] 的 MVP 降级口径。
_GUARDED_DEMONSTRATIVES: tuple[str, ...] = ("这个", "那个")
_NON_HOUSE_NOUNS: tuple[str, ...] = (
    "价格",
    "价位",
    "楼层",
    "层",
    "位置",
    "地段",
    "周末",
    "小区",
    "中介",
    "合同",
    "物业",
    "餐",
)

# 守卫判定前的 tail 规整：剥掉开头的「的」与常见标点/空白——「那个的
# 价位呢」类输入先规整再判名词，避免误判为房源指代。
_GUARD_STRIP_CHARS = "的，。．.、！!？?：:；; \t　"

# "其它/其他/它们" 中"它"字非房源指代：负向守卫——"其/他"居"它"前、
# "们"居"它"后，命中即原样保留。
_TA_NEGATED_PREFIX: tuple[str, ...] = ("其", "他")
_TA_NEGATED_SUFFIX: tuple[str, ...] = ("们",)

_PATTERN_RE = re.compile(
    "|".join(re.escape(pattern) for pattern in sorted(_HOUSE_PATTERNS, key=len, reverse=True))
)


def _explicit_ref(house_id: str) -> str:
    """全角显式引用标注（对齐 PRD 示例，逐字节口径，唯一允许格式）。"""
    return f"这套房（house_id: {house_id}）"


class RuleBasedCorefResolver:
    """可调用规则模板 resolver：``Envelope -> (resolved, refs)``。

    无构造依赖、无实例状态，组合根直接 ``RuleBasedCorefResolver()``
    注入 ``CoreferenceFilter(resolver=...)``；OQ-5 选型后以同签名
    实现整体替换。
    """

    def __call__(self, envelope: Envelope) -> tuple[str, list[CorefRef]]:
        """归一化消息中的房源指代；无命中返回 ``(原文, [])``。

        resolver 返回类型为 ``str``：``None`` 输入按防御口径归一为
        ``""``（过滤器 6.1 空白守卫保证 ``None`` 永不到达 resolver，
        与该守卫是两层独立防御，返回值口径差异无运行时影响）。
        """
        message = envelope.message
        # 防御纵深：过滤器 6.1 空白守卫保证不传入空白消息，resolver
        # 自身对 None/空串/纯空白亦返回恒等（不产引用、不改写文本）。
        if message is None or not message.strip():
            return message or "", []
        house_id = envelope.house_id  # 信封必填字段，即会话单一房源锚点
        hits = 0

        def _replace(match: re.Match[str]) -> str:
            nonlocal hits
            token = match.group(0)
            tail = message[match.end() :]
            if tail.startswith("（house_id:"):
                # 输入已含显式引用标注（如原文自带"这套房（house_id: x）"）：
                # 跳过该处替换，避免双重标注。
                return token
            if token in _GUARDED_DEMONSTRATIVES and any(
                tail.lstrip(_GUARD_STRIP_CHARS).startswith(noun)
                for noun in _NON_HOUSE_NOUNS
            ):
                # 非房源独立指代（那个价位/那个的价位/这个周末 等）：
                # 保留原文。
                return token
            if token == "它" and (
                any(
                    message[: match.start()].endswith(char)
                    for char in _TA_NEGATED_PREFIX
                )
                or any(tail.startswith(char) for char in _TA_NEGATED_SUFFIX)
            ):
                # 其它/其他/它们：非房源指代，原样保留。
                return token
            hits += 1
            return _explicit_ref(house_id)

        resolved = _PATTERN_RE.sub(_replace, message)
        if not hits:
            return message, []
        # 同消息多处命中去重为单条 house 引用（ref_count ∈ {0, 1}）。
        return resolved, [CorefRef(entity_type="house", entity_id=house_id)]

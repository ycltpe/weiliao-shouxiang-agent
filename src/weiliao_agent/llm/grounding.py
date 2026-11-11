"""确定性 grounding 校验（Story 4.3，AC 第 2 条）。

受控生成草稿写入信封前的 fail-closed 把守，纯 Python、无 I/O、无外部
依赖，可独立单测。检查口径：

1. 违禁表述清单——复用模板库（Story 4.1）的
   ``_FORBIDDEN_MATERIAL_FRAGMENTS`` 为底稿（「最」「第一」「价格可谈」
   「学区名额」及内部流程话术），扩充「我是这套房的经纪人」及等价
   自称条目；
2. 内部标注 / 分段标记泄漏——prompt 三段固定标记（【事实卡片】/
   【对话上下文】/【生成约束】）不得出现在买家向输出；
3. http(s) 链接——事实卡片无任何链接字段，输出出现链接必属无中生有。

``verify_grounding`` 返回首条命中问题的机器可读 reason；全部通过返回
``None``。空/空白输出按未通过处理（子图层另有 fail-fast，此处兜底
fail-closed）。
"""

import re

from weiliao_agent.filters.templates import _FORBIDDEN_MATERIAL_FRAGMENTS
from weiliao_agent.llm.prompts import (
    CONSTRAINT_SECTION_HEADER,
    DIALOGUE_SECTION_HEADER,
    FACT_SECTION_HEADER,
)

# 4.1 底稿之上的自称扩充：「我是这套房的经纪人」及等价自称变体。
_SELF_CLAIM_FRAGMENTS = (
    "我是这套房的经纪人",
    "我是本房的经纪人",
    "作为您的经纪人",
    "作为这套房的经纪人",
    "您的专属经纪人",
)

# 违禁表述清单：4.1 底稿 + 自称扩充（去重保序，只读引用模板常量）。
_FORBIDDEN_EXPRESSIONS: tuple[str, ...] = tuple(
    dict.fromkeys((*_FORBIDDEN_MATERIAL_FRAGMENTS, *_SELF_CLAIM_FRAGMENTS))
)

# 自称兜底正则：固定子串之外的变体（如「我是这套房子的经纪人」——
# 「这套房子的」超出手工枚举）由「我是/作为 + ≤8 字符 + 经纪人」收敛，
# 保持 fail-closed。
# DOTALL：`.` 须跨换行——「我是\n这套房的经纪人」类换行变体同属自称，
# fail-closed 确定性校验层不容缝隙（code review Epic 4 补丁）。
_SELF_CLAIM_PATTERN = re.compile(r"(?:我是|作为).{0,8}经纪人", re.DOTALL)
_SELF_CLAIM_REASON = "forbidden_expression:self_claim_regex"

# 内部标注 / 分段标记：prompt 三段固定标记泄漏即未通过。
_SECTION_MARKERS = (
    FACT_SECTION_HEADER,
    DIALOGUE_SECTION_HEADER,
    CONSTRAINT_SECTION_HEADER,
)

# 事实卡片无链接字段：输出出现任何 http(s) 链接必属无中生有。裸域名
# 同样拦截（www. 前缀或常见 TLD 结尾）；TLD 词表保守选取，普通中文
# 应答不含此类 ASCII 串，误伤面可控。
_COMMON_TLDS = (
    "com|cn|net|org|edu|gov|io|cc|co|me|tv|so|in|la|vip|top|xyz|club|"
    "tech|store|online|site|shop|link|live|news|media|biz|info|mobi|"
    "name|pro|red|fun|icu|art|wang|ren"
)
_BARE_DOMAIN_PATTERN = rf"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)*\.(?:{_COMMON_TLDS})\b"
_URL_PATTERN = re.compile(
    rf"https?://|www\.|{_BARE_DOMAIN_PATTERN}",
    re.IGNORECASE,
)

# 失控输出长度上限：超出即拦（正常应答 ≤200 字，留 5 倍余量仅挡 runaway）。
_MAX_OUTPUT_LENGTH = 1000


def verify_grounding(text: str) -> str | None:
    """确定性 grounding 校验：返回首条命中问题 reason，通过返回 None。

    检查顺序固定：失控长度 → 违禁表述 → 自称兜底 → 分段标记泄漏 →
    链接。全部 fail-closed，任一命中即返回，供过滤器丢弃草稿并把
    reason 记入 trace。
    """
    if not text or not text.strip():
        return "empty_output"
    if len(text) > _MAX_OUTPUT_LENGTH:
        return "overlong_output"
    for expression in _FORBIDDEN_EXPRESSIONS:
        if expression in text:
            return f"forbidden_expression:{expression}"
    if _SELF_CLAIM_PATTERN.search(text) is not None:
        return _SELF_CLAIM_REASON
    for marker in _SECTION_MARKERS:
        if marker in text:
            return f"section_marker_leak:{marker}"
    if _URL_PATTERN.search(text) is not None:
        return "forbidden_link"
    return None

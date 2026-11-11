"""硬触发评估模块（Story 5.1，AD-12 单一评估点）。

四类高风险信号集中判定：红线意图（价格谈判 / 贷款政策 / 投诉）、
非文本媒体输入（图片 / 语音不分类、不生成）、同一意图追问达阈值、
负面词命中。命中任一即触发转人工——判定结果（触发标记 + 命中条件
清单）由 intent_router 挂到信封 ``handoff`` 字段，编排器据此短路
下游（cache / llm / risk / assembler 零执行）。

条件集配置化且默认值权威在本模块：追问阈值缺省 3、红线意图缺省
NEGOTIATION / LOAN_POLICY / COMPLAINT（FR 值）；组合根经
``WEILIAO_HANDOFF_*`` env 灰度调整，非法值启动即 fail-fast。

负面词表为仓库内置静态文件（风控中台产出发布进仓库，决策 Q2-A），
``load_negative_words`` 启动只读加载：文件不可读或无任何词条即
fail-fast（启动即败，不静默放行——词表缺失会让负面触发永不命中）。

判定顺序固定（与配置一致，可审计）：红线意图 → 媒体类型 → 追问序号
→ 负面词；多条件同时命中时清单包含全部命中项。评估自身无 I/O，纯
内存计算，可独立单测；评估代码异常由调用方（intent_router）fail-closed
按触发处理。
"""

import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import get_args

from weiliao_agent.models.envelope import HandoffAssessment
from weiliao_agent.models.intent import IntentTag

# 默认追问阈值（FR 值）：record_followup 返回的该意图追问序号 ≥ 3 即命中
# ——与 Story 2.3 的 is_followup（第 2 次起）口径不同，勿复用。
DEFAULT_FOLLOWUP_THRESHOLD = 3
# 默认红线意图集合（FR 值）：命中任一即触发转人工。
DEFAULT_REDLINE_INTENTS: frozenset[IntentTag] = frozenset(
    {"NEGOTIATION", "LOAN_POLICY", "COMPLAINT"}
)

# 命中条件清单的标识词表（负面词命中记词条本身，见 assess 文档）。
CONDITION_REDLINE_INTENT = "redline_intent"
CONDITION_NON_TEXT_MEDIA = "non_text_media"
CONDITION_FOLLOWUP_THRESHOLD = "followup_threshold"
# fail-closed 归因：评估代码自身异常时按触发处理，命中条件记此标识。
CONDITION_ASSESSMENT_ERROR = "assessment_error"

# IntentTag 全量枚举（构造校验红线集合标签合法性用；str 集合口径同
# llm.model_router.KNOWN_INTENTS，env 注入路径免 cast）。
_ALL_TAGS: frozenset[str] = frozenset(get_args(IntentTag))

# ---- 负面词匹配归一化（2026-11-11 Epic 5 code review 裁决 A） ----
#
# 匹配语义保持子串包含不变；输入与词表双侧归一化，关闭两类绕过：
# 组合/兼容形（NFC + NFKC 折叠全半角）与零宽字符插入（\u200b 等剥除）。
# 简繁折叠经内置映射完成（繁体消息折叠为简体后命中简体词表）——映射
# 覆盖负面词域常见字符，不引第三方包；风控词表 owner 演进优先。
_ZERO_WIDTH_CHARS = frozenset("\u200b\u200c\u200d\u200e\u200f\ufeff\u2060")

# 简 → 繁（反向表在模块加载时生成；只列词表域高频字，宁缺毋滥）。
_SIMP_TO_TRAD: dict[str, str] = {
    "骗": "騙",
    "妈": "媽",
    "骂": "罵",
    "么": "麼",
    "脑": "腦",
    "残": "殘",
    "诉": "訴",
    "钱": "錢",
    "赔": "賠",
    "骚": "騷",
    "扰": "擾",
    "烂": "爛",
    "贱": "賤",
    "滚": "滾",
    "该": "該",
    "废": "廢",
    "无": "無",
    "耻": "恥",
    "恶": "惡",
    "气": "氣",
    "丢": "丟",
    "恼": "惱",
    "货": "貨",
    "毙": "斃",
}
_TRAD_TO_SIMP: dict[str, str] = {
    trad: simp for simp, trad in _SIMP_TO_TRAD.items()
}


def _normalize_for_matching(text: str) -> str:
    """负面词匹配前的输入归一化：NFC 组合 → NFKC 全半角折叠 → 剥
    零宽字符 → 简繁折叠（繁→简，经内置映射）。词表加载与消息评估
    双侧同法调用，保证两侧字节口径一致。"""
    folded = unicodedata.normalize(
        "NFC", unicodedata.normalize("NFKC", text)
    )
    stripped = "".join(ch for ch in folded if ch not in _ZERO_WIDTH_CHARS)
    return "".join(_TRAD_TO_SIMP.get(ch, ch) for ch in stripped)


class HardTriggerAssessor:
    """硬触发评估器（AD-12）：四类条件集中判定，返回判定结果。

    构造参数即全部判定配置（阈值 / 红线集合 / 负面词表），实例不可变、
    线程安全，纯内存计算。阈值非正整数、红线集合为空或含未知意图标签
    构造即 fail-fast（ValueError）——配置错误不得静默退化为永不触发。
    """

    def __init__(
        self,
        *,
        followup_threshold: int = DEFAULT_FOLLOWUP_THRESHOLD,
        redline_intents: Iterable[str] | None = None,
        negative_words: Iterable[str] = (),
    ) -> None:
        if isinstance(followup_threshold, bool) or not isinstance(
            followup_threshold, int
        ):
            msg = f"followup_threshold must be an int, got {followup_threshold!r}"
            raise ValueError(msg)
        if followup_threshold <= 0:
            msg = (
                f"followup_threshold must be a positive integer, "
                f"got {followup_threshold}"
            )
            raise ValueError(msg)
        redline = (
            DEFAULT_REDLINE_INTENTS
            if redline_intents is None
            else frozenset(redline_intents)
        )
        if not redline:
            msg = "redline_intents must be non-empty"
            raise ValueError(msg)
        unknown = sorted(set(redline) - _ALL_TAGS)
        if unknown:
            msg = f"redline_intents contains unknown intent tags: {unknown!r}"
            raise ValueError(msg)
        self._followup_threshold = followup_threshold
        self._redline_intents: frozenset[str] = redline
        # 词条归一化（评审补丁）：strip 并丢弃空/纯空白词条——空白
        # 词条是 '' 的子串，会命中所有消息导致全部转人工；词表文件
        # 加载侧已对行级空白做同样归一（load_negative_words），构造
        # 侧兜底防御编程式注入的脏词条。空集合语义不变（负面条件
        # 永不命中）。2026-11-11 裁决 A：词条另经匹配归一化（NFC/
        # NFKC/零宽/简繁），与消息侧同口径，防词表带全半角或繁体
        # 形态时双侧不一致。
        self._negative_words: frozenset[str] = frozenset(
            _normalize_for_matching(word.strip())
            for word in negative_words
            if word.strip()
        )

    @property
    def followup_threshold(self) -> int:
        """追问触发阈值（只读，组合根 env 回读验证用）。"""
        return self._followup_threshold

    @property
    def redline_intents(self) -> frozenset[str]:
        """红线意图集合（只读，组合根 env 回读验证用）。"""
        return self._redline_intents

    @property
    def negative_words(self) -> frozenset[str]:
        """负面词表（只读，组合根 env 回读验证用）。"""
        return self._negative_words

    def assess(
        self,
        *,
        intent: IntentTag | None,
        media_type: str | None,
        per_intent_followup_count: int,
        message: str | None,
    ) -> HandoffAssessment:
        """集中判定四类硬触发，返回触发标记 + 命中条件清单。

        判定顺序固定且与配置一致：红线意图 → 媒体类型 → 追问序号 →
        负面词，清单按此顺序追加。媒体口径：``media_type`` 为 None 或
        ``"text"`` 视为文本，其余任何值一律判触发（具体媒体枚举在接入
        适配器 Story 落地前不收紧校验）。追问口径：per-tag 聚合序号
        ≥ 阈值即命中（record_followup 返回值为权威，不读 L1 快照；
        Redis 故障 fail-safe 时调用方传 0，本条件不触发；2026-11-11
        裁决 B 起为 per-tag 聚合计数，「同一意图 ≥3」冻结口径）。
        负面词口径：对 message 归一化后做子串包含匹配（词条为连续
        字面量，无正则；归一化 = NFC/NFKC 全半角折叠 + 剥零宽字符 +
        简繁折叠，输入与词表双侧同法，2026-11-11 裁决 A），命中记
        该词条为命中条件；多词命中按词表排序追加，保证结果确定可
        审计。全部未命中返回 ``triggered=False`` 与空清单。
        """
        conditions: list[str] = []
        if intent is not None and intent in self._redline_intents:
            conditions.append(CONDITION_REDLINE_INTENT)
        if media_type is not None and media_type != "text":
            conditions.append(CONDITION_NON_TEXT_MEDIA)
        if per_intent_followup_count >= self._followup_threshold:
            conditions.append(CONDITION_FOLLOWUP_THRESHOLD)
        if message:
            normalized = _normalize_for_matching(message)
            for word in sorted(self._negative_words):
                if word in normalized:
                    conditions.append(word)
        return HandoffAssessment(triggered=bool(conditions), conditions=conditions)


def load_negative_words(path: str | Path) -> frozenset[str]:
    """启动只读加载内置负面词表：一行一条；空行与 # 注释行忽略。

    词表由风控中台产出发布进仓库（决策 Q2-A），env 仅覆盖文件路径。
    文件不可读或无任何词条即 fail-fast（ValueError）——加载失败启动
    即败，不静默放行。
    """
    try:
        # utf-8-sig：Windows/Excel 编辑保存的词表首行带 BOM——utf-8
        # 读取会使首词条永久失配（Epic 5 code review 补丁；同仓库
        # eval_coref_resolver.py 已用 utf-8-sig 口径）。
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        msg = f"negative words file unreadable: {path!s} ({exc})"
        raise ValueError(msg) from exc
    words = {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    if not words:
        msg = f"negative words file contains no entries: {path!s}"
        raise ValueError(msg)
    return frozenset(words)

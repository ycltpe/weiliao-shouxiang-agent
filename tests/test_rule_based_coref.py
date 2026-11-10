"""规则模板房源指代消解 resolver 单测（Story 6.2）。

覆盖 spec I/O & Edge-Case Matrix 全部场景：单指代命中、多指代共存、
指代嵌价格问句、无指代恒等、非房源独立指代降级、空串/None/纯空白
防御、全角括号显式引用逐字节断言（Golden 示例口径）、同消息去重为
单条 house 引用（ref_count ∈ {0, 1}）、house_id 直取信封字段。
"""

from typing import Any

from weiliao_agent.coref import RuleBasedCorefResolver
from weiliao_agent.models.coref import CorefRef
from weiliao_agent.models.envelope import Envelope

HOUSE_A = "house-001"


def _envelope(message: str | None, house_id: str = HOUSE_A) -> Envelope:
    payload: dict[str, Any] = {
        "session_id": "sess-001",
        "house_id": house_id,
        "timestamp": "2026-11-05T10:00:00+08:00",
        "message": message,
    }
    return Envelope.model_validate(payload)


def _resolve(message: str | None, house_id: str = HOUSE_A) -> tuple[str, list[CorefRef]]:
    resolver = RuleBasedCorefResolver()
    return resolver(_envelope(message, house_id))


# ---- Golden 示例（逐字节口径） ----


def test_ta_price_question_golden() -> None:
    resolved, refs = _resolve("它多少钱")
    assert resolved == "这套房（house_id: house-001）多少钱"
    assert refs == [CorefRef(entity_type="house", entity_id=HOUSE_A)]


def test_zhe_tao_lighting_question_golden() -> None:
    resolved, refs = _resolve("这套采光怎么样")
    assert resolved == "这套房（house_id: house-001）采光怎么样"
    assert refs == [CorefRef(entity_type="house", entity_id=HOUSE_A)]


def test_shangmian_shuode_prefix_preserved_golden() -> None:
    # 「上面说的」类前缀随其后量词名词命中："那套"替换、前缀自然保留。
    resolved, refs = _resolve("上面说的那套还有吗")
    assert resolved == "上面说的这套房（house_id: house-001）还有吗"
    assert refs == [CorefRef(entity_type="house", entity_id=HOUSE_A)]


def test_xiaowang_scenario_lighting() -> None:
    # PRD 小王场景端到端口径。
    resolved, refs = _resolve("它采光怎么样")
    assert resolved == "这套房（house_id: house-001）采光怎么样"
    assert refs == [CorefRef(entity_type="house", entity_id=HOUSE_A)]


# ---- 单指代命中 ----


def test_single_hit_produces_explicit_full_width_ref() -> None:
    resolved, refs = _resolve("那套房子今天能看房吗")
    assert resolved == "这套房（house_id: house-001）今天能看房吗"
    assert refs == [CorefRef(entity_type="house", entity_id=HOUSE_A)]


def test_longest_pattern_wins_no_partial_replacement() -> None:
    # "这套房"必须先于"这套"命中：不得截出"…显式引用）房还有吗"。
    resolved, refs = _resolve("这套房还有吗")
    assert resolved == "这套房（house_id: house-001）还有吗"
    assert refs == [CorefRef(entity_type="house", entity_id=HOUSE_A)]


def test_house_id_taken_from_envelope() -> None:
    # 锚点直取信封必填字段：不同 house_id 产出对应显式引用。
    resolved, refs = _resolve("它朝向呢", house_id="house-777")
    assert resolved == "这套房（house_id: house-777）朝向呢"
    assert refs == [CorefRef(entity_type="house", entity_id="house-777")]


# ---- 多指代共存与去重 ----


def test_multiple_hits_all_replaced_and_refs_deduplicated() -> None:
    resolved, refs = _resolve("这套采光好吗？它朝向呢")
    assert resolved == (
        "这套房（house_id: house-001）采光好吗？"
        "这套房（house_id: house-001）朝向呢"
    )
    # 同消息多处命中去重为单条 house 引用（ref_count=1）。
    assert refs == [CorefRef(entity_type="house", entity_id=HOUSE_A)]


# ---- 指代嵌价格问句（house 锚定覆盖复合问法） ----


def test_ta_nested_price_question_normalized() -> None:
    resolved, refs = _resolve("它的价格呢")
    assert resolved == "这套房（house_id: house-001）的价格呢"
    assert refs == [CorefRef(entity_type="house", entity_id=HOUSE_A)]


# ---- 无指代：正常恒等 ----


def test_no_coref_identity_byte_identical() -> None:
    message = "今天方便看房吗"
    resolved, refs = _resolve(message)
    assert resolved == message  # 逐字节等于原文
    assert refs == []


def test_no_coref_price_question_identity() -> None:
    # 直接问价无指代：不归一化（指代归一化不问开放式价格实体）。
    message = "总价多少"
    resolved, refs = _resolve(message)
    assert resolved == message
    assert refs == []


# ---- 非房源独立指代：MVP 降级，保留原文 ----


def test_floor_coref_not_normalized() -> None:
    message = "那层采光呢"
    resolved, refs = _resolve(message)
    assert resolved == message
    assert refs == []


def test_price_demonstrative_guarded() -> None:
    message = "那个价位呢"
    resolved, refs = _resolve(message)
    assert resolved == message
    assert refs == []


def test_location_demonstrative_guarded() -> None:
    message = "那个位置偏吗"
    resolved, refs = _resolve(message)
    assert resolved == message
    assert refs == []


def test_ta_negated_others_preserved() -> None:
    # 其它/其他/它们 中"它"字非房源指代：负向守卫原样保留。
    message = "其它楼层还有吗"
    resolved, refs = _resolve(message)
    assert resolved == message
    assert refs == []


# ---- 空白防御纵深 ----


def test_empty_string_returns_identity() -> None:
    resolved, refs = _resolve("")
    assert resolved == ""
    assert refs == []


def test_whitespace_only_returns_identity() -> None:
    resolved, refs = _resolve("   ")
    assert resolved == "   "
    assert refs == []


def test_none_message_returns_empty_identity() -> None:
    # 防御纵深：过滤器守卫保证不传入 None，resolver 自身亦不产引用。
    resolved, refs = _resolve(None)
    assert resolved == ""
    assert refs == []


# ---- 守卫增补（评审补丁）：非房源名词扩展 + 「的」/标点剥离 ----


def test_guard_weekend_not_house_coref() -> None:
    message = "这个周末方便看房吗"
    resolved, refs = _resolve(message)
    assert resolved == message
    assert refs == []


def test_guard_xiaoqu_not_house_coref() -> None:
    message = "那个小区环境如何"
    resolved, refs = _resolve(message)
    assert resolved == message
    assert refs == []


def test_guard_strips_leading_de_and_punctuation() -> None:
    # 守卫判定前剥掉 tail 开头的「的」：「那个的价位呢」不得误判。
    message = "那个的价位呢"
    resolved, refs = _resolve(message)
    assert resolved == message
    assert refs == []


# ---- 全长形式（评审补丁）：模式表补全长，不得截出游离「房」字 ----


def test_zhe_jian_fang_full_form_replaced_whole() -> None:
    resolved, refs = _resolve("这间房还有吗")
    assert resolved == "这套房（house_id: house-001）还有吗"
    assert refs == [CorefRef(entity_type="house", entity_id=HOUSE_A)]


def test_ci_fang_hits() -> None:
    resolved, refs = _resolve("此房真实在售吗")
    assert resolved == "这套房（house_id: house-001）真实在售吗"
    assert refs == [CorefRef(entity_type="house", entity_id=HOUSE_A)]


# ---- 已含显式引用标注：不双重标注 ----


def test_pre_annotated_explicit_ref_not_double_wrapped() -> None:
    message = "这套房（house_id: h9）还有吗"
    resolved, refs = _resolve(message)
    assert resolved == message  # 原样保留，不包第二层
    assert refs == []


# ---- 守卫与命中共存（评审补丁） ----


def test_guarded_token_coexists_with_hit_and_dedup() -> None:
    # 守卫跳过的 token 不计 hit；同消息命中去重仍为单条 house 引用。
    resolved, refs = _resolve("那个价位呢？它朝向呢")
    assert resolved == "那个价位呢？这套房（house_id: house-001）朝向呢"
    assert refs == [CorefRef(entity_type="house", entity_id=HOUSE_A)]


# ---- 跨房源比较退化形态锁定（评审补丁） ----


def test_cross_house_comparison_degenerates_to_self_comparison() -> None:
    # spec Never 明确不做跨房源指代；本实现是归一化而非开放式实体
    # 链接——「这套/那套」均锚定会话单一房源，两处替换形成自比较为
    # 已知退化形态（rule_based.py docstring 已知限制段），此处锁定
    # 当前行为防静默漂移。
    resolved, refs = _resolve("这套比那套贵吗")
    assert resolved == (
        "这套房（house_id: house-001）比这套房（house_id: house-001）贵吗"
    )
    assert refs == [CorefRef(entity_type="house", entity_id=HOUSE_A)]

"""确定性 grounding 校验单测（Story 4.3，AC 第 2 条）。

覆盖：违禁表述清单（自称经纪人变体/「最」「第一」「价格可谈」「学区
名额」）、内部标注/分段标记泄漏、http(s) 链接（大小写）、空输出
fail-closed；合规草稿返回 None。纯函数无 I/O，全部离线。
"""

import pytest

from weiliao_agent.llm.grounding import verify_grounding


@pytest.mark.parametrize(
    "text",
    [
        "您好，这套房源目前在售，价格信息以页面展示为准。",
        "南向两居室，采光良好，周边配套成熟，欢迎预约看房。",
    ],
)
def test_compliant_text_passes(text: str) -> None:
    assert verify_grounding(text) is None


@pytest.mark.parametrize(
    ("text", "expected_reason"),
    [
        ("我是这套房的经纪人，随时带您看房。", "forbidden_expression:我是这套房的经纪人"),
        ("作为您的经纪人，我建议您尽快出手。", "forbidden_expression:作为您的经纪人"),
        ("这套房性价比第一，错过不再有。", "forbidden_expression:第一"),
        ("价格可谈，您出个价吧。", "forbidden_expression:价格可谈"),
        ("这套房带学区名额，先到先得。", "forbidden_expression:学区名额"),
        ("我是经纪人，房源信息我最全。", "forbidden_expression:最"),
    ],
)
def test_forbidden_expressions_rejected(text: str, expected_reason: str) -> None:
    assert verify_grounding(text) == expected_reason


@pytest.mark.parametrize(
    "marker",
    ["【事实卡片】", "【对话上下文】", "【生成约束】"],
)
def test_section_marker_leak_rejected(marker: str) -> None:
    assert verify_grounding(f"您好，{marker}房源编号 house-001。") == (
        f"section_marker_leak:{marker}"
    )


@pytest.mark.parametrize(
    "text",
    [
        "详情见 https://example.com/house/1",
        "图片在这里 HTTP://EXAMPLE.COM/IMG",
        "详情见 www.example.com/house/1",
        "详情见 example.com/path",
        "更多资料见房源网 fang.com/docs",
    ],
)
def test_any_http_link_rejected(text: str) -> None:
    assert verify_grounding(text) == "forbidden_link"


def test_self_claim_regex_catches_unlisted_variants() -> None:
    # 固定子串之外的自称变体由兜底正则收敛（fail-closed）。
    assert verify_grounding("我是这套房子的经纪人，随时看房。") == (
        "forbidden_expression:self_claim_regex"
    )
    # code review Epic 4 补丁：换行变体同属自称（DOTALL），不得绕过。
    assert verify_grounding("我是\n这套房的经纪人，随时看房。") == (
        "forbidden_expression:self_claim_regex"
    )
    # 普通文本不误伤。
    assert verify_grounding("您好，房源信息可以咨询平台客服。") is None


def test_overlong_output_rejected() -> None:
    assert verify_grounding("好" * 1001) == "overlong_output"
    assert verify_grounding("好" * 1000) is None  # 边界内不拦


def test_empty_output_fails_closed() -> None:
    assert verify_grounding("") == "empty_output"
    assert verify_grounding("   \n\t ") == "empty_output"


def test_first_hit_wins_deterministic_order() -> None:
    # 同时命中违禁表述与链接：返回清单首条（检查顺序固定，可断言）。
    reason = verify_grounding("价格可谈，详情 https://example.com")
    assert reason == "forbidden_expression:价格可谈"

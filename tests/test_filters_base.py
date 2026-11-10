"""过滤器共享契约 helper 单测（Story 4.7 判空口径唯一出处直测）。

``is_blank_draft`` 是两个拦截点的唯一判空口径，两调用方均先自行判 None，
helper 对 None 返回 False（None 是常态无草稿，非拦截）这一条改反会静默
通过——必须本体直测钉死。
"""

import pytest

from weiliao_agent.filters.base import is_blank_draft


def test_none_is_not_blank() -> None:
    # None = 未生成/红线/熔断/审核不通过的常态无草稿：不算拦截。
    assert is_blank_draft(None) is False


@pytest.mark.parametrize("blank", ["", " ", "  \n\t ", "\t\n  ", "\n", "\u3000\u3000"])
def test_blank_variants_are_blank(blank: str) -> None:
    assert is_blank_draft(blank) is True


@pytest.mark.parametrize("non_blank", ["a", " 这套房在售", "\t在售 \n", "【AI 助手】"])
def test_non_blank_variants_are_not_blank(non_blank: str) -> None:
    assert is_blank_draft(non_blank) is False

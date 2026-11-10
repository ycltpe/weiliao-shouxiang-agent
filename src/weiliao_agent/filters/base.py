"""过滤器统一契约（AD-1）。

每个过滤器仅消费上游信封、产出下游信封，过滤器之间禁止直接函数
调用，全部状态经信封传递。
"""

from abc import ABC, abstractmethod

from weiliao_agent.models.envelope import Envelope


def is_blank_draft(draft: str | None) -> bool:
    """共享草稿判空口径（Story 4.7，两处拦截的唯一出处）。

    ``strip()`` 后为空串即判空（空串/纯空白草稿无买家可见正文）；``None``
    不判空——None 是「无草稿」常态（未生成/红线/熔断/审核不通过），与
    空草稿拦截是不同语义，不算拦截、不记 draft_empty 事件。本函数仅用于
    分支选择，调用方不得拿判空结果做清洗/改写后回用。
    """
    return draft is not None and not draft.strip()


class Filter(ABC):
    """过滤器抽象基类：单一职责、可替换、可独立测试。"""

    # 子类应覆写为过滤器名；缺省时降级归因退化为类名（管道兜底保护）。
    name: str = ""

    @abstractmethod
    def process(self, envelope: Envelope) -> Envelope:
        """消费上游信封，产出下游信封。"""

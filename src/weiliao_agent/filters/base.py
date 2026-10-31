"""过滤器统一契约（AD-1）。

每个过滤器仅消费上游信封、产出下游信封，过滤器之间禁止直接函数
调用，全部状态经信封传递。
"""

from abc import ABC, abstractmethod

from weiliao_agent.models.envelope import Envelope


class Filter(ABC):
    """过滤器抽象基类：单一职责、可替换、可独立测试。"""

    # 子类应覆写为过滤器名；缺省时降级归因退化为类名（管道兜底保护）。
    name: str = ""

    @abstractmethod
    def process(self, envelope: Envelope) -> Envelope:
        """消费上游信封，产出下游信封。"""

"""指代消解实现包（Story 6.2）：resolver 默认实现与可插拔替换留口。

OQ-5 选型（规则模板 / 序列标注 / LLM 改写）后组合根整体替换
``RuleBasedCorefResolver`` 注入即可，过滤器本体与信封契约不动。
"""

from weiliao_agent.coref.rule_based import RuleBasedCorefResolver

__all__ = ["RuleBasedCorefResolver"]

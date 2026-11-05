"""Kafka 消费者：房源事件同步、转人工指令。"""

from weiliao_agent.workers.house_sync import (
    STALE_AFTER,
    HouseSyncConsumer,
    KafkaMessageSource,
    Message,
    MessageSource,
)

__all__ = [
    "HouseSyncConsumer",
    "KafkaMessageSource",
    "Message",
    "MessageSource",
    "STALE_AFTER",
]

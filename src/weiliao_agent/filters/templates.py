"""占位模板注册表（Story 1.2）—— 错误兜底机制的独立验收载体。

9 类意图各一条安全文案 + 1 条通用兜底；intent 缺失或不在 9 类枚举内
一律回退通用兜底（fail-closed，与信封未知枚举口径一致）。

全部买家可见占位文案（含兜底应答）携带固定 AI 身份披露前缀（AD-10），
Story 4.1 完整模板库替换时继承该约定。
"""

from weiliao_agent.models.intent import IntentTag

# 固定 AI 身份披露前缀（AD-10）：买家可见文本 100% 携带。
AI_DISCLOSURE_PREFIX = "【AI 助手】"

# 9 类意图各一条预审核安全文案；均为不承诺具体事实的安抚/记录口径，
# 占位过滤器输出视为已审核，允许直接下发买家。
_INTENT_PLACEHOLDERS: dict[IntentTag, str] = {
    "GREETING": "您好，已收到您的消息。请问您对这套房想了解哪方面的信息？",
    "PRICE_INQUIRY": "您咨询的价格问题已为您记录，确认后第一时间回复您。",
    "AVAILABILITY": "房源在售情况正在为您核实，稍后回复您确认结果。",
    "LOCATION": "小区位置与周边配套信息正在为您整理，稍后回复您详情。",
    "PHOTO_REQUEST": "房源照片已为您向经纪人索取，稍后发送给您查看。",
    "NEGOTIATION": "您的出价意向已记录并转达，经纪人确认后回复您。",
    "LOAN_POLICY": "贷款政策相关问题已记录，稍后为您详细解答。",
    "COMPLAINT": "感谢您的反馈，已记录并会尽快跟进处理。",
    "UNKNOWN": "已收到您的消息，经纪人稍后回复您。",
}

# 通用兜底：intent 缺失或不在 9 类枚举内时使用。
_GENERIC_PLACEHOLDER = "系统繁忙，已为您记录消息，经纪人稍后回复您。"


def render_placeholder(intent: IntentTag | None) -> str:
    """按意图标签渲染买家可见占位文案，统一携带 AI 身份披露前缀。

    intent 为 None 时回退通用兜底文案；类型层已收窄为 9 类枚举
    （Story 2.1），运行时经 dict.get 仍对枚举外字符串 fail-closed
    到通用兜底（与信封未知枚举口径一致）。
    """
    body = _GENERIC_PLACEHOLDER
    if intent:
        body = _INTENT_PLACEHOLDERS.get(intent, _GENERIC_PLACEHOLDER)
    return f"{AI_DISCLOSURE_PREFIX}{body}"

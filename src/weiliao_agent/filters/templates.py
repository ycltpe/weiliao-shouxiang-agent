"""集中、同步的模板兜底应答库（Story 4.1）。

``IntentTag`` 的 9 类标签各有独立安全文案，通用兜底单独定义。
缺失或运行时未知意图一律 fail-closed 到通用兜底；只有图片请求会
消费调用方提供的素材链接，渲染过程不访问外部服务。
"""

from urllib.parse import urlsplit

from weiliao_agent.models.intent import IntentTag

# 固定 AI 身份披露前缀（AD-10）：买家可见文本 100% 携带。
AI_DISCLOSURE_PREFIX = "【AI 助手】"

_INTENT_FALLBACKS: dict[IntentTag, str] = {
    "GREETING": "您好，我可以协助您了解房源信息。请问您想咨询价格、位置、照片，还是其他内容？",
    "PRICE_INQUIRY": (
        "关于房源价格，我暂时无法确认相关信息。"
        "您可以说明关注的预算范围，后续答复应以核实结果为准。"
    ),
    "AVAILABILITY": (
        "我暂时无法确认房源当前状态，请以后续核实结果为准。"
        "您可以继续咨询位置、照片或贷款政策。"
    ),
    "LOCATION": (
        "我暂时无法确认该房源的位置和周边配套详情。"
        "您可以说明关注的交通或生活设施，方便后续核实。"
    ),
    "PHOTO_REQUEST": (
        "暂时没有可用的房源图片或相册链接。"
        "您可以先咨询房源位置、价格或贷款政策等其他信息。"
    ),
    "NEGOTIATION": (
        "关于议价，我无法确认价格是否可调整，也不能作出承诺。"
        "您可以说明出价意向，议价结果以双方后续沟通为准。"
    ),
    "LOAN_POLICY": (
        "贷款政策可能因地区、个人资质和时间而变化，我暂时无法确认适用条件。"
        "建议向贷款机构核实具体要求。"
    ),
    "COMPLAINT": (
        "感谢您的反馈。很抱歉这次体验未能满足您的预期，"
        "您可以继续说明具体问题，以便后续核实与处理。"
    ),
    "UNKNOWN": (
        "我暂时无法确定您想咨询的内容。"
        "您可以补充说明想了解的价格、位置、照片或贷款政策等信息。"
    ),
}

# 与 UNKNOWN 独立：缺失或运行时未知意图不得泄露素材或猜测买家意图。
_GENERIC_FALLBACK = "当前无法生成具体答复，请稍后重试，或补充您想咨询的房源问题。"

_PHOTO_IMAGE_FALLBACK = "以下是房源图片链接：{image_url}"
_PHOTO_ALBUM_FALLBACK = "您可以通过以下相册链接查看房源图片：{album_url}"

_MAX_MATERIAL_URL_LENGTH = 2048
_FORBIDDEN_MATERIAL_FRAGMENTS = (
    "最",
    "第一",
    "价格可谈",
    "学区名额",
    "我是经纪人",
    "我是您的经纪人",
    "已为您记录",
    "已转达",
    "已索取",
    "已发送",
)


def _validated_material_url(value: str | None) -> str | None:
    """返回可安全渲染的素材 URL；空白、非法或违禁输入按无素材处理。"""
    if value is None or not value.strip():
        return None
    if len(value) > _MAX_MATERIAL_URL_LENGTH:
        return None
    if any(char.isspace() or not char.isprintable() for char in value):
        return None
    if any(fragment in value for fragment in _FORBIDDEN_MATERIAL_FRAGMENTS):
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return value


def render_fallback(
    intent: IntentTag | None,
    image_url: str | None = None,
    album_url: str | None = None,
) -> str:
    """同步渲染安全兜底文案；图片链接优先于相册链接。

    空白素材按无素材处理。非图片请求忽略全部素材参数，运行时未知
    标签与 ``None`` 都返回通用兜底，且不渲染任何素材。
    """
    if intent == "PHOTO_REQUEST":
        image_link = _validated_material_url(image_url)
        if image_link is not None:
            body = _PHOTO_IMAGE_FALLBACK.format(image_url=image_link)
        else:
            album_link = _validated_material_url(album_url)
            body = (
                _PHOTO_ALBUM_FALLBACK.format(album_url=album_link)
                if album_link is not None
                else _INTENT_FALLBACKS["PHOTO_REQUEST"]
            )
    elif intent is None:
        body = _GENERIC_FALLBACK
    else:
        body = _INTENT_FALLBACKS.get(intent, _GENERIC_FALLBACK)
    return f"{AI_DISCLOSURE_PREFIX}{body}"


def render_placeholder(
    intent: IntentTag | None,
    image_url: str | None = None,
    album_url: str | None = None,
) -> str:
    """兼容旧调用方；完整委托给 ``render_fallback``。"""
    return render_fallback(intent, image_url=image_url, album_url=album_url)

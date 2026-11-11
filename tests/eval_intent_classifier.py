"""意图分类评测 harness（Story 2.2，≥90%·≥1000 真实验收门 deferred）。

用法：

- ``uv run python tests/eval_intent_classifier.py``：内置合成种子集
  （9 类，每类数十条）冒烟。无 ``OPENAI_API_KEY`` 时以离线规则冒烟
  替身跑 harness 机制（覆盖率/混淆矩阵/报告），有 key 时加 ``--live``
  以真实 LLM 分类器跑种子集。
- ``uv run python tests/eval_intent_classifier.py eval.csv [--live]``：
  读标注 CSV（列：text,label）逐条分类，输出 9 类覆盖率、准确率与
  混淆对。语料 ≥1000 条且覆盖 9 类时执行真实验收门（准确率 ≥90%，
  不达标 exit 1）；语料未就绪时打印 deferred 说明并 exit 0。
"""

import argparse
import csv
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any, get_args

from weiliao_agent.filters.intent_router import IntentClassifier
from weiliao_agent.models.intent import IntentClassification, IntentTag

# 9 类权威清单经 Literal 派生（评审补丁：手抄字面量曾与 2.1 模式漂移，
# IntentTag 增删类时覆盖率/门禁口径静默失效）。
ALL_TAGS: tuple[IntentTag, ...] = get_args(IntentTag)

MIN_EVAL_ROWS = 1000
GATE_ACCURACY = 0.9

# 合成种子集：每类数十条，冒烟 harness 机制与分类器接线（真实验收
# 以人工标注语料复跑本 harness 为准）。
SEEDS: dict[IntentTag, list[str]] = {
    "GREETING": [
        "你好",
        "您好",
        "在吗",
        "在吗在吗",
        "早上好",
        "晚上好",
        "hi",
        "hello",
        "在吗？回复一下",
        "你好，看到消息了吗",
        "您好，打扰了",
        "在？",
        "亲，在吗",
        "你好呀",
        "在不在",
    ],
    "PRICE_INQUIRY": [
        "这套房多少钱",
        "总价多少",
        "单价多少一平",
        "什么价位",
        "报价是多少",
        "多少钱一个月",
        "租金怎么算",
        "这个房子卖多少钱",
        "首付要多少",
        "全款多少能拿下",
        "这套房的底价是多少",
        "挂牌价是多少",
        "预算 300 万这个房子够吗",
        "物业费怎么收",
        "税费大概多少",
    ],
    "AVAILABILITY": [
        "房子还在吗",
        "现在还能看房吗",
        "这周方便看房吗",
        "房子卖出去了吗",
        "还在售吗",
        "现在租出去了吗",
        "什么时候可以入住",
        "今天能去看房吗",
        "房东现在方便签约吗",
        "这房源真实在售吗",
        "明天上午能看房吗",
        "这房子还空着吗",
        "有钥匙随时能看吗",
        "周末可以看房吗",
        "还在租吗",
    ],
    "LOCATION": [
        "房子在哪个位置",
        "离地铁远吗",
        "到陆家嘴通勤多久",
        "周边有学校吗",
        "附近有什么商场",
        "小区地址发我一下",
        "靠不靠江",
        "在几层",
        "朝向怎么样",
        "小区停车方便吗",
        "附近公交车多吗",
        "是哪个学区",
        "离医院近不近",
        "在哪个区",
        "周边环境吵不吵",
    ],
    "PHOTO_REQUEST": [
        "能发几张照片看看吗",
        "有户型图吗",
        "发个实拍视频",
        "房间照片发一下",
        "有室内图吗",
        "阳台照片有吗",
        "厨房和卫生间拍一下",
        "可以视频看房吗",
        "发一下房源图片",
        "VR 链接有吗",
        "想看下客厅的照片",
        "有没有装修好的图",
        "再发几张卧室照片",
        "小区环境照片有吗",
        "发几张白天的照片",
    ],
    "NEGOTIATION": [
        "价格能便宜点吗",
        "能不能再降十万",
        "房东好说话吗能谈吗",
        "最低什么价",
        "预算不够能优惠吗",
        "中介费能打折吗",
        "要是全款能便宜多少",
        "再便宜点就定了",
        "价格有点贵能商量吗",
        "能不能分期",
        "租金能优惠两百吗",
        "大刀小刀都行，能谈吗",
        "隔壁同户型便宜多了",
        "再降 5 万我今天签约",
        "诚心买，给个最低价",
    ],
    "LOAN_POLICY": [
        "贷款首付比例是多少",
        "能贷款吗",
        "利率现在多少",
        "支持公积金贷款吗",
        "商贷和公积金差多少",
        "二套房政策是什么",
        "月供大概多少",
        "贷款年限最长多久",
        "征信有逾期能贷吗",
        "组合贷可以办吗",
        "现在的房贷政策怎么样",
        "认房认贷吗",
        "外地户口能贷款吗",
        "提前还款有违约金吗",
        "三成首付贷款够吗",
    ],
    "COMPLAINT": [
        "你们服务太差了",
        "回复也太慢了吧",
        "说好安排看房根本没去",
        "信息都是假的吧",
        "价格跟挂的不一样，投诉",
        "什么态度啊",
        "再这样我投诉了",
        "说好昨天回电也没有回",
        "照片和实际差太多了吧",
        "中介太不靠谱了",
        "我要差评",
        "发布的信息半天没人理啊",
        "你们这是在浪费我时间",
        "房源都下架了还挂着骗人",
        "太让人失望了",
    ],
    "UNKNOWN": [
        "在吗？我家猫会后空翻",
        "今天天气怎么样",
        "你会写诗吗",
        "帮我算一下 1+1",
        "附近哪家火锅好吃",
        "你是真人吗",
        "给我讲个笑话",
        "房价以后会涨吗",
        "你觉得这个小区怎么样啊随便聊聊",
        "在忙吗",
        "你好厉害",
        "谢谢",
        "好的",
        "嗯嗯",
        "收到",
    ],
}


class _SmokeStubClassifier:
    """离线冒烟替身（非交付实现）：关键词规则，仅验证 harness 机制。

    真实分类验收必须 ``--live``（真实 LLM 分类器）或标注 CSV 复跑。
    """

    source = "smoke_stub"

    _KEYWORDS: Mapping[IntentTag, tuple[str, ...]] = {
        "GREETING": ("你好", "您好", "在吗", "早上好", "晚上好", "hi", "hello", "在？", "在不在"),
        # LOAN 先于 PRICE：「首付比例」「组合贷」类表述归贷款政策，价格
        # 问句不含贷款关键词时回落 PRICE。
        "LOAN_POLICY": (
            "贷款", "利率", "公积金", "商贷", "月供", "征信", "认贷", "还款", "政策", "组合贷",
        ),
        "NEGOTIATION": ("便宜", "降", "优惠", "打折", "分期", "商量", "最低", "大刀"),
        "PRICE_INQUIRY": (
            "多少钱", "价位", "价格", "首付", "单价", "租金", "报价", "税", "物业", "总价", "底价",
            "预算",
        ),
        "AVAILABILITY": (
            "在售", "在租", "看房", "卖出", "租出", "入住", "空着", "钥匙", "签约",
        ),
        "LOCATION": (
            "位置", "地铁", "通勤", "地址", "朝向", "层", "学校", "商场", "医院", "公交", "区",
            "停车", "江",
        ),
        "PHOTO_REQUEST": ("照片", "户型图", "视频", "图片", "图", "vr", "拍", "实拍"),
        "COMPLAINT": (
            "差", "慢", "投诉", "假", "态度", "不靠谱", "差评", "骗", "失望", "浪费", "回电",
            "没人理", "没去",
        ),
    }

    def classify(self, message: str, context: Mapping[str, Any]) -> IntentClassification:
        text = message.lower()
        for tag, keywords in self._KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return IntentClassification(tag=tag, confidence=0.8)
        return IntentClassification(tag="UNKNOWN", confidence=0.6)


class _LiveClassifier:
    """真实 LLM 分类器封装：单条失败 fail-closed UNKNOWN（置信度 0.0）。

    超时/网络/解析失败不中断整个评测——该条按 UNKNOWN 计（误判计入
    混淆矩阵，不抛异常逃逸）。
    """

    def __init__(self) -> None:
        from weiliao_agent.classifiers.llm_zero_shot import LlmZeroShotClassifier

        self._inner = LlmZeroShotClassifier()
        self.source = self._inner.source

    def classify(self, message: str, context: Mapping[str, Any]) -> IntentClassification:
        try:
            return self._inner.classify(message, context)
        except Exception:
            return IntentClassification(tag="UNKNOWN", confidence=0.0)


def _evaluate(classifier: IntentClassifier, rows: Sequence[tuple[str, IntentTag]]) -> int:
    """逐条分类并打印报告；返回进程退出码。"""
    per_tag: dict[str, dict[str, int]] = {
        tag: {"support": 0, "correct": 0} for tag in ALL_TAGS
    }
    confusion: dict[tuple[str, str], int] = {}
    correct = 0
    for text, expected in rows:
        per_tag[expected]["support"] += 1
        predicted = classifier.classify(text, {}).tag
        if predicted == expected:
            correct += 1
            per_tag[expected]["correct"] += 1
        else:
            key = (expected, predicted)
            confusion[key] = confusion.get(key, 0) + 1

    coverage = sum(1 for tag in ALL_TAGS if per_tag[tag]["support"] > 0)
    accuracy = correct / len(rows) if rows else 0.0

    print(f"classifier_source={_source_of(classifier)} rows={len(rows)}")
    print(f"coverage={coverage}/9 accuracy={accuracy:.3f} ({correct}/{len(rows)})")
    for tag in ALL_TAGS:
        stats = per_tag[tag]
        if stats["support"] == 0:
            print(f"  {tag:<15} support=0 (missing)")
            continue
        print(
            f"  {tag:<15} support={stats['support']:<4} "
            f"correct={stats['correct']:<4} "
            f"recall={stats['correct'] / stats['support']:.3f}"
        )
    if confusion:
        print("top_confusions:")
        for (expected, predicted), count in sorted(
            confusion.items(), key=lambda item: -item[1]
        )[:10]:
            print(f"  {expected} -> {predicted}: {count}")

    if len(rows) >= MIN_EVAL_ROWS and coverage == len(ALL_TAGS):
        # 真实验收门：规模 ≥1000 且 9 类全覆盖时执行。
        if accuracy >= GATE_ACCURACY:
            print(f"GATE PASS: accuracy {accuracy:.3f} >= {GATE_ACCURACY}")
            return 0
        print(f"GATE FAIL: accuracy {accuracy:.3f} < {GATE_ACCURACY}")
        return 1
    print(
        f"GATE DEFERRED: need >={MIN_EVAL_ROWS} rows and 9/9 coverage "
        f"to enforce accuracy >={GATE_ACCURACY} (spec OQ-1 选 B)"
    )
    return 0


def _source_of(classifier: IntentClassifier) -> str:
    source = getattr(classifier, "source", None)
    return source if isinstance(source, str) and source else type(classifier).__name__


def _load_csv(path: str) -> list[tuple[str, IntentTag]]:
    rows: list[tuple[str, IntentTag]] = []
    # utf-8-sig：容忍 Excel/WPS 另存 CSV 的 BOM（评审补丁；utf-8 下首
    # 列名解析为 "\ufefftext" 致列校验 SystemExit，报错具误导性）。
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not {"text", "label"} <= set(reader.fieldnames):
            msg = f"CSV must have columns text,label; got {reader.fieldnames}"
            raise SystemExit(msg)
        for line in reader:
            text = (line.get("text") or "").strip()
            label = (line.get("label") or "").strip().upper()
            if not text or label not in ALL_TAGS:
                continue  # 非法行跳过（fail-closed，不进评测基数）
            rows.append((text, label))  # type: ignore[arg-type]
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", nargs="?", help="标注语料 CSV（列：text,label）")
    parser.add_argument(
        "--live",
        action="store_true",
        help="以真实 LLM 分类器跑种子集/语料（需 OPENAI_API_KEY）",
    )
    args = parser.parse_args(argv)

    live = args.live or bool(args.csv)
    if live and not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set: cannot run live LLM evaluation", file=sys.stderr)
        return 2
    classifier: IntentClassifier = _LiveClassifier() if live else _SmokeStubClassifier()

    if args.csv:
        rows = _load_csv(args.csv)
        if not rows:
            print("no valid rows in CSV", file=sys.stderr)
            return 2
    else:
        rows = [(text, tag) for tag in ALL_TAGS for text in SEEDS[tag]]
        if not live:
            print("offline smoke mode (stub classifier; use --live for real LLM)")

    return _evaluate(classifier, rows)


if __name__ == "__main__":
    raise SystemExit(main())

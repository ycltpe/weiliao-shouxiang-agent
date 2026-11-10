"""FR-1 全链压测设施（Story 7.2）：coreference → intent_router 端到端
P99 < 50ms 实测验收（收口 6.1/6.2/6.3 同根挂账）。

全链口径：真实 CoreferenceFilter（Story 6.2 规则 resolver——单趟正则
扫描、微秒级）+ 真实 IntentRouterFilter（llm_zero_shot 分类器，45ms
硬超时守卫 + latency_ms 埋点既有）；编排器逐跳 deadline 不复用（本设施
测量热路径自身延迟，预算给足，超时兜底不计入 FR-1 口径）。

凭据门（spec Boundaries）：无 ``OPENAI_API_KEY`` 时显式 skip——打印
记录原因并以退出码 0 收场（同 ``tests/eval_intent_classifier.py`` 的
CLI/退出码风格），绝不虚构 P99 数据冒充验收门。凭据门内测真实延迟：
含消解指代句（「这套房」「它」类锚定）的消息集逐条过全链，量取
P99 并判定 < 50ms；分类失败 fail-closed（UNKNOWN）轮次照常计时
（FR-1 是端到端延迟口径，非分类质量口径——质量门在
``tests/eval_intent_classifier.py``）。

压测线程有界（编排器共享池 + 分类器自有有界池），超时守卫先行返回，
不悬挂进程；不触达真实 Redis / Kafka（写路径回调一律不注入）。

用法::

    uv run python scripts/loadtest_intent_chain.py [--concurrency 8] \
        [--requests 50]
"""

import argparse
import os
import sys
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from weiliao_agent.classifiers.llm_zero_shot import LlmZeroShotClassifier
from weiliao_agent.coref.rule_based import RuleBasedCorefResolver
from weiliao_agent.filters.coreference import CoreferenceFilter
from weiliao_agent.filters.intent_router import IntentRouterFilter
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.pipeline import PipelineOrchestrator

# FR-1 验收门：含消解端到端意图分类 P99 < 50ms。
FR1_P99_BUDGET_MS = 50.0

# 消息集：覆盖无指代 / 房源指代（「这套房」「它」锚定）/ 长句混合——
# 消解 resolver 的命中与透传路径都进延迟样本。
MESSAGES: tuple[str, ...] = (
    "这套房现在什么价位？",
    "它的朝向怎么样？",
    "这套房离我公司远吗？周边有地铁吗？",
    "那它的物业费怎么收？",
    "在吗？这套房这周方便看房吗？",
    "小区停车方便吗？这套房的楼层高不高？",
    "它的学区是哪里？",
    "这套房还在售吗？房东急不急？",
    "相比隔壁小区，这套房的优势是什么？",
    "它的挂牌价最近有变动吗？",
)


@dataclass(frozen=True)
class _ChainResult:
    """单请求全链读数：端到端延迟与分类 fail-closed 观测。"""

    latency_ms: float
    degraded: bool
    failed_filter: str | None


def _build_orchestrator() -> PipelineOrchestrator:
    """全链组装：coreference（规则 resolver）→ intent_router（真实分类器）。

    写路径回调（intent_writer/followup_recorder/coref_writer 等）一律
    不注入：压测不触达 Redis；编排器预算给足（10s）——本设施测热路径
    自身延迟，不测量兜底路径。
    """
    return PipelineOrchestrator(
        filters=[
            CoreferenceFilter(resolver=RuleBasedCorefResolver()),
            IntentRouterFilter(classifier=LlmZeroShotClassifier()),
        ],
        budget_seconds=10.0,
    )


def _percentile(sorted_values: list[float], pct: float) -> float:
    """最近秩 P 分位（与 loadtest_timeout_storm 同口径）。"""
    if not sorted_values:
        return 0.0
    rank = max(0, min(len(sorted_values) - 1, int(pct * len(sorted_values) + 0.5) - 1))
    return sorted_values[rank]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--concurrency", type=int, default=8, help="并发请求线程数")
    parser.add_argument(
        "--requests", type=int, default=50, help="每线程顺序请求数（总请求=乘积）"
    )
    args = parser.parse_args(argv)
    if args.concurrency <= 0 or args.requests <= 0:
        parser.error("--concurrency/--requests 必须为正整数")

    # 编排器组装先于凭据门执行（审查 G3）：全链构造（含
    # LlmZeroShotClassifier——凭据惰性、首次 classify 才建客户端）在
    # 正常验证路径被覆盖，kwarg 漂移无凭据时即暴露，不潜伏到有人带
    # 凭据复跑才炸。
    orchestrator = _build_orchestrator()

    # 凭据门（spec：不可用时显式 skip 并记录原因，不虚构数据）。
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "SKIP: OPENAI_API_KEY 未配置——FR-1 全链压测需真实 llm_zero_shot "
            "分类器（45ms 硬超时守卫内测真实延迟）。不虚构 P99 数据："
            "配置凭据后复跑 `uv run python scripts/loadtest_intent_chain.py` "
            "执行 P99 < 50ms 验收门。",
            file=sys.stderr,
        )
        return 0

    results: list[_ChainResult] = []
    results_lock = threading.Lock()

    def _issue(worker_index: int, request_index: int) -> None:
        envelope = Envelope.model_validate(
            {
                "session_id": f"fr1-loadtest-s{worker_index}",
                "house_id": "house-fr1-loadtest",
                "turn": request_index + 1,
                "timestamp": datetime.now(UTC).isoformat(),
                "message": MESSAGES[(worker_index + request_index) % len(MESSAGES)],
            }
        )
        started = time.monotonic()
        result = orchestrator.run(envelope)
        latency_ms = (time.monotonic() - started) * 1000
        with results_lock:
            results.append(
                _ChainResult(
                    latency_ms=latency_ms,
                    degraded=result.degraded,
                    failed_filter=result.failed_filter,
                )
            )

    started = time.monotonic()
    with ThreadPoolExecutor(
        max_workers=args.concurrency, thread_name_prefix="fr1-loadtest"
    ) as clients:
        list(
            clients.map(
                lambda i: _issue(i // args.requests, i % args.requests),
                range(args.concurrency * args.requests),
            )
        )
    elapsed = time.monotonic() - started

    latencies = sorted(r.latency_ms for r in results)
    p50 = _percentile(latencies, 50)
    p99 = _percentile(latencies, 99)
    fail_closed = sum(1 for r in results if r.failed_filter == "intent_router")

    print(f"过滤器链: {' -> '.join(orchestrator.filter_names)}（含消解全链）")
    print(f"请求总数: {len(results)}（fail-closed UNKNOWN 轮次={fail_closed}）")
    print(f"执行耗时: {elapsed:.2f}s")
    print(
        f"端到端延迟: p50={p50:.1f}ms p99={p99:.1f}ms "
        f"（FR-1 预算 {FR1_P99_BUDGET_MS:.0f}ms）"
    )

    if any(r.degraded for r in results):
        print(
            f"警告: {sum(1 for r in results if r.degraded)} 条应答走了降级兜底"
            "（预算给足下不应出现，测量口径受污染）",
            file=sys.stderr,
        )
    if p99 >= FR1_P99_BUDGET_MS:
        print(f"断言失败: P99 {p99:.1f}ms >= FR-1 预算 {FR1_P99_BUDGET_MS:.0f}ms", file=sys.stderr)
        return 1
    print(f"断言通过: FR-1 全链 P99 < {FR1_P99_BUDGET_MS:.0f}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())

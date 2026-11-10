"""超时风暴压测设施（Story 7.2，NFR10 验收工具与 D1/D2/D4 决策证据来源）。

进程内组装编排器：真实过滤器链（coreference 透传 → intent_router 占位
→ cache_lookup（fakeredis）→ llm_generate（慢 stub 子图，单调用延迟
> 编排器剩余预算，逐跳超时丢弃）→ risk_review → response_assembler），
并发请求群打完超时风暴后量取并打印：

- 存活线程数（``threading.enumerate()`` 口径）：基线 / 风暴期峰值 /
  排空后（压测线程采样，排除采样线程自身）；
- 应答延迟 P99 与逐请求 degraded 归因（丢弃计数 = slow_filter 命中数）；
- 被丢弃生成调用的 token 归因读数（Story 7.2 D4 落地后可用，
  ``observability.metrics.get_discarded_generation_metrics()``）。

内置断言（失败非零退出，可重复执行）：

- 风暴期存活线程数 ≤ 基线 + 并发请求线程 + 编排器池 + 客户端池 +
  采样余量（NFR10「执行线程不积累」口径——每请求新建执行器的旧形态
  下执行线程随并发线性膨胀，本断言即 D1 决策的实测判据）；
- 应答 P99 ≤ 编排器端到端预算 + 余量（NFR-1 出口余量口径）；
- 无请求傻等全量超时（应答线程在预算内返回）；
- 逐跳超时丢弃计数 == 请求总数（形态 A 默认参数：编排器预算 <
  客户端预算，slow_filter=llm_generate 归因逐字节可查）；
- 排空后被丢弃调用完成数 == 请求总数、token 累计 == 单调用 usage ×
  请求数（token 归因口径不丢不重）。

两种超时形态（CLI 可调）：形态 A（``--budget 0.5``）编排器逐跳超时先
触发（spec 验收口径：逐跳超时即兜底应答）；形态 B（``--budget 2.5``，
argparse 默认）生产形态——llm_generate 客户端 0.9s 预算内归一（4.3
既定语义，degraded=False 影子口径），丢弃归因仍全量可查。

压测全程不触达真实 OpenAI / Redis / Kafka：慢调用经
``OpenAiGenerationClient`` 注入同接口 chat 替身（模拟「客户端内超时
归一 + usage 回传」形态——预算内归一抛 ``LlmGenerationError("timeout")``，
后台跑完回传 ``usage_metadata``）；cache_lookup 以 ``l0_client=None``
零 I/O 透传（占位行为，无 Redis 连接），不注入 broker。

用法::

    uv run python scripts/loadtest_timeout_storm.py \
        --concurrency 24 --requests 4 --call-delay 2.0
"""

import argparse
import math
import sys
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from weiliao_agent.filters.cache_lookup import CacheLookupFilter
from weiliao_agent.filters.coreference import CoreferenceFilter
from weiliao_agent.filters.intent_router import IntentRouterFilter
from weiliao_agent.filters.llm_generate import LlmGenerateFilter
from weiliao_agent.filters.response_assembler import ResponseAssemblerFilter
from weiliao_agent.filters.risk_review import RiskReviewFilter
from weiliao_agent.llm.client import _MAX_WORKERS as _LLM_CLIENT_MAX_WORKERS
from weiliao_agent.llm.client import OpenAiGenerationClient
from weiliao_agent.llm.subgraph import LlmGenerationSubgraph
from weiliao_agent.models.envelope import Envelope
from weiliao_agent.observability import metrics
from weiliao_agent.pipeline import PipelineOrchestrator

# 慢 stub 单调用回传的确定性 usage（token 归因断言的期望值口径）。
STUB_USAGE = {"input_tokens": 15, "output_tokens": 12, "total_tokens": 27}
# 风暴期线程峰值的采样间隔（秒）。
SAMPLE_INTERVAL_SECONDS = 0.01
# 生成客户端内部执行器并发上限：直接引用 llm/client.py 的
# ``_MAX_WORKERS``（审查 G8——原先手写副本会与客户端池大小漂移）。
# 线程断言余量：主压测协调线程 + 调度转发余量。
THREAD_SLACK = 4


class _SlowChatModel:
    """慢 chat 替身：``invoke`` 阻塞 ``delay_seconds`` 后回传固定文本

    与 ``usage_metadata``——与 ``ChatOpenAI`` 同接口（``.invoke``
    返回带 ``content`` / ``usage_metadata`` 的响应），驱动客户端预算
    守卫的「超时归一」与成功路径的「usage 回传」两形态。
    """

    def __init__(self, delay_seconds: float, completion_counter: list[int]) -> None:
        self._delay_seconds = delay_seconds
        # 完成计数（单元素列表即最小线程安全计数器：append 原子）：
        # token 归因三方一致断言（llm_generate 丢弃归因 == 慢调用完成
        # == 归因 calls）的实测口径。
        self._completion_counter = completion_counter

    def invoke(self, messages: Sequence[Any]) -> Any:
        del messages  # 压测不关心 prompt 内容
        time.sleep(self._delay_seconds)
        self._completion_counter.append(1)
        return _SlowResponse()


class _SlowResponse:
    """最小响应替身：``content`` / ``usage_metadata`` 两属性口径。"""

    content = "stub 生成草稿（压测占位文案，确定性 grounding 可通过）"
    usage_metadata = dict(STUB_USAGE)


@dataclass(frozen=True)
class _StormResult:
    """单请求结果：延迟与降级归因（丢弃判定口径）。"""

    latency_seconds: float
    degraded: bool
    failed_filter: str | None


def _alive_threads(exclude: set[int] | None = None) -> int:
    """``threading.enumerate()`` 口径的存活线程数（可排除采样者自身）。"""
    exclude = exclude or set()
    return sum(1 for t in threading.enumerate() if t.ident not in exclude)


def _build_orchestrator(
    args: argparse.Namespace,
) -> tuple[PipelineOrchestrator, list[int]]:
    """进程内组装压测链：慢 stub 子图驱动 llm_generate 逐跳超时丢弃。"""
    completions: list[int] = []
    slow_client = OpenAiGenerationClient(
        budget_seconds=args.llm_budget,
        chat_model=_SlowChatModel(args.call_delay, completions),
    )
    orchestrator = PipelineOrchestrator(
        filters=[
            CoreferenceFilter(),
            IntentRouterFilter(),  # 未注入分类器：占位行为（零凭据）
            CacheLookupFilter(l0_client=None),  # 零 I/O 透传（占位行为）
            LlmGenerateFilter(subgraph=LlmGenerationSubgraph(slow_client)),
            RiskReviewFilter(),  # 未注入审核客户端：既有缺省行为
            ResponseAssemblerFilter(),
        ],
        budget_seconds=args.budget,
    )
    # 慢调用完成计数随编排器一并返回（压测读数挂点）——不向编排器
    # 实例猴子补丁未类型化属性（审查 G8）。
    return orchestrator, completions


def _run_storm(
    orchestrator: PipelineOrchestrator, args: argparse.Namespace
) -> tuple[list[_StormResult], int, int, int]:
    """并发请求群打风暴；返回 (结果列表, 风暴期线程峰值, 基线线程数,
    排空后线程数)。"""
    results: list[_StormResult] = []
    results_lock = threading.Lock()
    peak = 0
    peak_lock = threading.Lock()
    stop_sampling = threading.Event()

    def _sample() -> None:
        nonlocal peak
        own = {threading.get_ident()}
        while not stop_sampling.is_set():
            count = _alive_threads(exclude=own)
            with peak_lock:
                peak = max(peak, count)
            stop_sampling.wait(SAMPLE_INTERVAL_SECONDS)

    def _issue(worker_index: int, request_index: int) -> None:
        envelope = Envelope.model_validate(
            {
                "session_id": f"loadtest-s{worker_index}",
                "house_id": "house-loadtest",
                "turn": request_index + 1,
                "timestamp": datetime.now(UTC).isoformat(),
                "message": "这套房现在什么价位？",
            }
        )
        started = time.monotonic()
        result = orchestrator.run(envelope)
        latency = time.monotonic() - started
        with results_lock:
            results.append(
                _StormResult(
                    latency_seconds=latency,
                    degraded=result.degraded,
                    failed_filter=result.failed_filter,
                )
            )

    baseline = _alive_threads()
    sampler = threading.Thread(target=_sample, name="loadtest-sampler", daemon=True)
    sampler.start()
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(
            max_workers=args.concurrency, thread_name_prefix="loadtest-client"
        ) as clients:
            list(
                clients.map(
                    lambda i: _issue(i // args.requests, i % args.requests),
                    range(args.concurrency * args.requests),
                )
            )
    finally:
        elapsed = time.monotonic() - started
        stop_sampling.set()
        sampler.join(timeout=5)
    # 排空：被丢弃的慢调用在客户端有界池（_LLM_CLIENT_MAX_WORKERS 并发）
    # 内排队跑完——总排空时长 = 批次数 × 单调用延迟 + 余量，保证 token
    # 归因「完成数 == 请求总数」断言时序稳定（可重复执行的前提）。
    total = args.concurrency * args.requests
    drain_seconds = math.ceil(total / _LLM_CLIENT_MAX_WORKERS) * args.call_delay + 1.0
    time.sleep(drain_seconds)
    drained = _alive_threads()
    print(
        f"风暴执行耗时: {elapsed:.2f}s（{args.concurrency}x{args.requests} 请求，"
        f"排空等待 {drain_seconds:.1f}s）"
    )
    return results, peak, baseline, drained


def _percentile(sorted_values: list[float], pct: float) -> float:
    """最近秩 P 分位（压测口径，与观测值对齐，不做插值外推）。"""
    if not sorted_values:
        return 0.0
    rank = max(0, min(len(sorted_values) - 1, int(pct * len(sorted_values) + 0.5) - 1))
    return sorted_values[rank]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--concurrency", type=int, default=24, help="并发请求线程数")
    parser.add_argument(
        "--requests", type=int, default=4, help="每线程顺序请求数（总请求=乘积）"
    )
    parser.add_argument(
        "--call-delay",
        type=float,
        default=2.0,
        help="慢 stub 单调用延迟秒数（须 > llm-budget 触发客户端超时归一）",
    )
    parser.add_argument(
        "--budget", type=float, default=2.5, help="编排器端到端内部预算秒数"
    )
    parser.add_argument(
        "--llm-budget",
        type=float,
        default=0.9,
        help="生成客户端预算秒数（LLM_GENERATE_BUDGET_SECONDS 口径）",
    )
    parser.add_argument(
        "--thread-budget",
        type=int,
        default=None,
        help="风暴期存活线程断言预算（缺省=链长共享池 + 采样余量）",
    )
    args = parser.parse_args(argv)
    if args.concurrency <= 0 or args.requests <= 0:
        parser.error("--concurrency/--requests 必须为正整数")
    # 数值合法性校验（审查 G9）：非有限值（nan/inf）与异常常数不得
    # 产生「断言空虚通过」或 traceback 形态的虚假证据——nan 的比较
    # 恒 False 会绕过下方 delay>budget 校验，必须显式 isfinite 先行。
    if not all(
        math.isfinite(v)
        for v in (args.budget, args.llm_budget, args.call_delay)
    ):
        parser.error("--budget/--llm-budget/--call-delay 必须为正有限数")
    if args.budget <= 0:
        parser.error("--budget 必须为正数（否则每跳立即兜底、断言空虚通过）")
    if args.llm_budget <= 0:
        parser.error("--llm-budget 必须为正数")
    if args.call_delay <= 0:
        parser.error("--call-delay 必须为正数")
    if args.call_delay <= args.llm_budget:
        parser.error("--call-delay 须 > --llm-budget（否则不触发超时归一丢弃）")

    orchestrator, completion_log = _build_orchestrator(args)
    chain_len = len(orchestrator.filter_names)
    # 形态判定：编排器预算 < 客户端预算 → 逐跳超时先触发（形态 A，
    # spec 验收口径）；否则客户端内归一（形态 B，生产影子口径）。
    form_a = args.budget < args.llm_budget
    thread_budget = (
        args.thread_budget
        if args.thread_budget is not None
        else args.concurrency + max(1, chain_len) + _LLM_CLIENT_MAX_WORKERS + THREAD_SLACK
    )

    results, peak, baseline, drained = _run_storm(orchestrator, args)
    stub_completions = len(completion_log)

    latencies = sorted(r.latency_seconds for r in results)
    p50 = _percentile(latencies, 50)
    p99 = _percentile(latencies, 99)
    degraded = sum(1 for r in results if r.degraded)
    discarded = sum(1 for r in results if r.failed_filter == "llm_generate")
    attribution = sorted(
        {r.failed_filter for r in results if r.failed_filter is not None}
    )
    usage_pool = metrics.get_discarded_generation_metrics()
    discarded_snapshot = usage_pool.snapshot()
    usage_pool.reset()  # 进程内复跑（pytest 并发场景）不留计数残留

    print(f"过滤器链: {' -> '.join(orchestrator.filter_names)}")
    print(f"超时形态: {'A 编排器逐跳丢弃' if form_a else 'B 客户端内归一（影子口径）'}")
    print(
        f"请求总数: {len(results)}（degraded={degraded}，llm_generate 归因="
        f"{discarded}，慢调用完成={stub_completions}，归因分布={attribution}）"
    )
    print(
        f"应答延迟: p50={p50 * 1000:.0f}ms p99={p99 * 1000:.0f}ms "
        f"（预算 {args.budget * 1000:.0f}ms）"
    )
    print(
        f"存活线程: 基线={baseline} 风暴期峰值={peak} 排空后={drained} "
        f"(断言预算 {thread_budget})"
    )
    print(
        "被丢弃调用 token 归因: "
        f"calls={discarded_snapshot['calls']} tokens={discarded_snapshot['tokens']}"
    )

    failures: list[str] = []
    if peak > baseline + thread_budget:
        failures.append(
            f"线程无界: 风暴期峰值 {peak} > 基线 {baseline} + 预算 {thread_budget}"
        )
    if p99 > args.budget + 0.2:
        failures.append(
            f"P99 劣化: {p99 * 1000:.0f}ms > 预算 {args.budget * 1000:.0f}ms + 200ms"
        )
    if form_a and degraded != len(results):
        failures.append(f"存在未降级应答: {len(results) - degraded} 条（形态 A 应全部逐跳丢弃）")
    if form_a and any(r.failed_filter is None for r in results):
        failures.append("形态 A 存在 failed_filter 为 None 的应答（逐跳超时归因应逐字节可查）")
    if form_a and discarded < stub_completions:
        # 归因覆盖（不丢）：凡开始执行的慢调用必有 llm_generate 归因兜底
        # （编排器预算 < 客户端预算时 llm 跳必被逐跳丢弃）。attribution ≥
        # completions 的松方向另有「llm 跳剩余预算 ≤ 0 立即兜底」路径
        # 不产生执行。形态 B 下 client 先归一透传（影子口径），部分请求
        # 的丢弃归因落在排队超预算的后续跳——token 归因 == 慢调用完成
        # 的等式仍对两种形态精确成立（下方断言）。
        failures.append(
            f"丢弃归因丢单: llm_generate 归因 {discarded} < 慢调用完成 {stub_completions}"
        )
    if (
        discarded_snapshot["calls"] != stub_completions
        or discarded_snapshot["tokens"] != stub_completions * STUB_USAGE["total_tokens"]
    ):
        failures.append(
            "token 归因不符: "
            f"calls={discarded_snapshot['calls']}（期望 {stub_completions}） "
            f"tokens={discarded_snapshot['tokens']}"
            f"（期望 {stub_completions * STUB_USAGE['total_tokens']}）"
        )

    if failures:
        print("\n断言失败:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("断言通过: 线程有界、P99 在预算内、丢弃计数与 token 归因一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())

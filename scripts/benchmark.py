"""
Benchmark script for the TTS Caching Server.

A standalone client that quantifies caching performance by sending a mix
of duplicate and unique requests, measuring latencies, and verifying the
distributed lock works under concurrency.

Usage:
    uv run python scripts/benchmark.py --base-url http://localhost:8000
    uv run python scripts/benchmark.py --num-requests 30 --run-concurrent
    uv run python scripts/benchmark.py --concurrency-test
    uv run python scripts/benchmark.py --voice hi-IN-KavyaNeural --language hi-IN
"""

import argparse
import asyncio
import json
import random
import statistics
import time
from datetime import datetime, timezone

import httpx

# Default test sentences derived from real TTSPrompt file patterns.
DEFAULT_SENTENCES = [
    "am i speaking with pallavi",
    "am i speaking with a. abdul rahaman",
    "am i speaking with a. ajay",
    "am i speaking with a. geetha",
    "am i speaking with a. srinivas",
    "am i speaking with a balaji",
    "am i speaking with a dinesh kumar",
    "am i speaking with a gopal",
    "am i speaking with a hari prasad",
    "am i speaking with a ramesh",
]

# Cost constant (example: $0.01 per API call)
COST_PER_API_CALL = 0.01


async def run_sequential_benchmark(
    client: httpx.AsyncClient,
    base_url: str,
    num_requests: int,
    sentences: list[str],
    voice: str,
    language: str,
) -> dict:
    """Run the sequential benchmark with a mix of duplicate and unique requests.

    Args:
        client: httpx async client.
        base_url: Server base URL.
        num_requests: Total number of requests to send.
        sentences: List of unique sentences to cycle through.
        voice: Voice name to use.
        language: Language code to use.

    Returns:
        Dict with benchmark results.
    """
    requests_to_send = []
    for i in range(num_requests):
        sentence = sentences[i % len(sentences)]
        requests_to_send.append(sentence)
    random.shuffle(requests_to_send)

    results = []
    overall_start = time.perf_counter()

    for i, text in enumerate(requests_to_send, 1):
        start = time.perf_counter()

        try:
            response = await client.post(
                f"{base_url}/tts",
                json={
                    "text": text,
                    "voice": voice,
                    "language": language,
                    "format": "wav",
                },
                timeout=60.0,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            cache_status = response.headers.get("X-Cache-Status", "UNKNOWN")

            results.append({
                "request_num": i,
                "text": text[:50],
                "status_code": response.status_code,
                "cache_status": cache_status,
                "latency_ms": round(elapsed_ms, 2),
                "response_size": len(response.content),
            })

            print(
                f"  [{i:3d}/{num_requests}] {cache_status:4s} | "
                f"{elapsed_ms:8.1f}ms | {len(response.content):6d} bytes | "
                f"'{text[:40]}...'"
            )

        except Exception as e:
            elapsed_ms = (time.perf_counter() - start) * 1000
            results.append({
                "request_num": i,
                "text": text[:50],
                "status_code": 0,
                "cache_status": "ERROR",
                "latency_ms": round(elapsed_ms, 2),
                "response_size": 0,
                "error": str(e),
            })
            print(f"  [{i:3d}/{num_requests}] ERROR | {elapsed_ms:8.1f}ms | {str(e)[:60]}")

    overall_elapsed = time.perf_counter() - overall_start

    # Compute statistics
    hit_latencies = [r["latency_ms"] for r in results if r["cache_status"] == "HIT"]
    miss_latencies = [r["latency_ms"] for r in results if r["cache_status"] == "MISS"]

    hits = len(hit_latencies)
    misses = len(miss_latencies)
    errors = len([r for r in results if r["cache_status"] == "ERROR"])
    total = len(results)
    hit_rate = (hits / total * 100) if total > 0 else 0
    throughput = total / overall_elapsed if overall_elapsed > 0 else 0

    def _stats(latencies: list[float]) -> dict:
        if not latencies:
            return {"mean": 0, "median": 0, "p95": 0}
        sorted_l = sorted(latencies)
        p95_idx = int(len(sorted_l) * 0.95)
        return {
            "mean": round(statistics.mean(sorted_l), 2),
            "median": round(statistics.median(sorted_l), 2),
            "p95": round(sorted_l[min(p95_idx, len(sorted_l) - 1)], 2),
        }

    hit_stats = _stats(hit_latencies)
    miss_stats = _stats(miss_latencies)

    speedup = (miss_stats["mean"] / hit_stats["mean"]) if hit_stats["mean"] > 0 else 0
    api_calls_saved = total - misses
    cost_saved = api_calls_saved * COST_PER_API_CALL

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_requests": total,
        "hits": hits,
        "misses": misses,
        "errors": errors,
        "hit_rate_percent": round(hit_rate, 2),
        "throughput_req_sec": round(throughput, 2),
        "hit_latency": hit_stats,
        "miss_latency": miss_stats,
        "speedup_factor": round(speedup, 2),
        "total_wall_clock_seconds": round(overall_elapsed, 2),
        "api_calls_saved": api_calls_saved,
        "estimated_cost_saved_usd": round(cost_saved, 4),
        "cost_per_call_usd": COST_PER_API_CALL,
        "results": results,
    }

    return summary


async def run_concurrent_benchmark(
    client: httpx.AsyncClient,
    base_url: str,
    num_requests: int,
    concurrency: int,
    sentences: list[str],
    voice: str,
    language: str,
) -> dict:
    """Run a concurrent benchmark simulating multiple users sending requests in parallel.

    Args:
        client: httpx async client.
        base_url: Server base URL.
        num_requests: Total number of requests to send.
        concurrency: Batch size for concurrent execution.
        sentences: List of sentences to cycle through.
        voice: Voice name to use.
        language: Language code to use.

    Returns:
        Dict with concurrent benchmark results.
    """
    requests_to_send = []
    for i in range(num_requests):
        sentence = sentences[i % len(sentences)]
        requests_to_send.append(sentence)
    random.shuffle(requests_to_send)

    results = []
    overall_start = time.perf_counter()

    async def _send_request(idx: int, text: str) -> dict:
        start = time.perf_counter()
        try:
            response = await client.post(
                f"{base_url}/tts",
                json={
                    "text": text,
                    "voice": voice,
                    "language": language,
                    "format": "wav",
                },
                timeout=60.0,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            cache_status = response.headers.get("X-Cache-Status", "UNKNOWN")
            return {
                "request_num": idx,
                "text": text[:50],
                "status_code": response.status_code,
                "cache_status": cache_status,
                "latency_ms": round(elapsed_ms, 2),
                "response_size": len(response.content),
            }
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start) * 1000
            return {
                "request_num": idx,
                "text": text[:50],
                "status_code": 0,
                "cache_status": "ERROR",
                "latency_ms": round(elapsed_ms, 2),
                "response_size": 0,
                "error": str(e),
            }

    # Process in chunks of `concurrency`
    for i in range(0, len(requests_to_send), concurrency):
        batch = requests_to_send[i : i + concurrency]
        tasks = [_send_request(i + idx + 1, text) for idx, text in enumerate(batch)]
        batch_results = await asyncio.gather(*tasks)
        results.extend(batch_results)

        for r in batch_results:
            status = r["cache_status"]
            latency = r["latency_ms"]
            size = r["response_size"]
            text_part = r["text"]
            if status == "ERROR":
                print(f"  [{r['request_num']:3d}/{num_requests}] ERROR | {latency:8.1f}ms | {r.get('error', '')[:60]}")
            else:
                print(
                    f"  [{r['request_num']:3d}/{num_requests}] {status:4s} | "
                    f"{latency:8.1f}ms | {size:6d} bytes | "
                    f"'{text_part[:40]}...'"
                )

    overall_elapsed = time.perf_counter() - overall_start

    # Compute statistics
    hit_latencies = [r["latency_ms"] for r in results if r["cache_status"] == "HIT"]
    miss_latencies = [r["latency_ms"] for r in results if r["cache_status"] == "MISS"]

    hits = len(hit_latencies)
    misses = len(miss_latencies)
    errors = len([r for r in results if r["cache_status"] == "ERROR"])
    total = len(results)
    hit_rate = (hits / total * 100) if total > 0 else 0
    throughput = total / overall_elapsed if overall_elapsed > 0 else 0

    def _stats(latencies: list[float]) -> dict:
        if not latencies:
            return {"mean": 0, "median": 0, "p95": 0}
        sorted_l = sorted(latencies)
        p95_idx = int(len(sorted_l) * 0.95)
        return {
            "mean": round(statistics.mean(sorted_l), 2),
            "median": round(statistics.median(sorted_l), 2),
            "p95": round(sorted_l[min(p95_idx, len(sorted_l) - 1)], 2),
        }

    hit_stats = _stats(hit_latencies)
    miss_stats = _stats(miss_latencies)

    speedup = (miss_stats["mean"] / hit_stats["mean"]) if hit_stats["mean"] > 0 else 0
    api_calls_saved = total - misses
    cost_saved = api_calls_saved * COST_PER_API_CALL

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_requests": total,
        "hits": hits,
        "misses": misses,
        "errors": errors,
        "hit_rate_percent": round(hit_rate, 2),
        "throughput_req_sec": round(throughput, 2),
        "hit_latency": hit_stats,
        "miss_latency": miss_stats,
        "speedup_factor": round(speedup, 2),
        "total_wall_clock_seconds": round(overall_elapsed, 2),
        "api_calls_saved": api_calls_saved,
        "estimated_cost_saved_usd": round(cost_saved, 4),
        "cost_per_call_usd": COST_PER_API_CALL,
        "results": results,
    }

    return summary


async def run_concurrency_test(
    client: httpx.AsyncClient,
    base_url: str,
    concurrency: int,
    voice: str,
    language: str,
) -> dict:
    """Fire N simultaneous requests for the same uncached sentence.

    Verifies the distributed lock ensures only 1 external API call is made,
    not N.

    Args:
        client: httpx async client.
        base_url: Server base URL.
        concurrency: Number of parallel requests.
        voice: Voice name to use.
        language: Language code to use.

    Returns:
        Dict with concurrency test results.
    """
    test_text = f"Concurrency test sentence at {time.time_ns()}"

    print(f"\n{'='*60}")
    print(f"CONCURRENCY TEST: {concurrency} parallel requests, same sentence")
    print(f"Text: '{test_text}'")
    print(f"{'='*60}\n")

    async def _send_request(idx: int) -> dict:
        start = time.perf_counter()
        try:
            response = await client.post(
                f"{base_url}/tts",
                json={
                    "text": test_text,
                    "voice": voice,
                    "language": language,
                    "format": "wav",
                },
                timeout=60.0,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            cache_status = response.headers.get("X-Cache-Status", "UNKNOWN")
            return {
                "request_idx": idx,
                "cache_status": cache_status,
                "latency_ms": round(elapsed_ms, 2),
                "status_code": response.status_code,
            }
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start) * 1000
            return {
                "request_idx": idx,
                "cache_status": "ERROR",
                "latency_ms": round(elapsed_ms, 2),
                "status_code": 0,
                "error": str(e),
            }

    tasks = [_send_request(i) for i in range(concurrency)]
    results = await asyncio.gather(*tasks)

    misses = sum(1 for r in results if r["cache_status"] == "MISS")
    hits = sum(1 for r in results if r["cache_status"] == "HIT")
    errors = sum(1 for r in results if r["cache_status"] == "ERROR")

    for r in sorted(results, key=lambda x: x["request_idx"]):
        print(
            f"  Request {r['request_idx']:2d}: {r['cache_status']:5s} | "
            f"{r['latency_ms']:8.1f}ms"
        )

    lock_working = misses == 1
    print(f"\n  MISSes: {misses} (expected: 1)")
    print(f"  HITs:   {hits} (expected: {concurrency - 1})")
    print(f"  Errors: {errors}")
    print(f"  Lock working correctly: {'[YES]' if lock_working else '[NO]'}")

    return {
        "concurrency": concurrency,
        "total_requests": concurrency,
        "misses": misses,
        "hits": hits,
        "errors": errors,
        "lock_working": lock_working,
        "results": results,
    }


def print_summary(summary: dict, title: str = "BENCHMARK SUMMARY") -> None:
    """Print a formatted summary table of benchmark results."""
    print(f"\n{'='*60}")
    print(title)
    print(f"{'='*60}")

    print(f"\n  {'Metric':<35s} {'Value':>15s}")
    print(f"  {'-'*35} {'-'*15}")
    print(f"  {'Total Requests':<35s} {summary['total_requests']:>15d}")
    print(f"  {'Cache Hits':<35s} {summary['hits']:>15d}")
    print(f"  {'Cache Misses':<35s} {summary['misses']:>15d}")
    print(f"  {'Errors':<35s} {summary['errors']:>15d}")
    print(f"  {'Hit Rate':<35s} {summary['hit_rate_percent']:>14.1f}%")
    print(f"  {'Throughput':<35s} {summary['throughput_req_sec']:>11.2f} req/s")

    print(f"\n  {'Latency (ms)':<35s} {'HIT':>10s} {'MISS':>10s}")
    print(f"  {'-'*35} {'-'*10} {'-'*10}")
    print(
        f"  {'Mean':<35s} "
        f"{summary['hit_latency']['mean']:>10.1f} "
        f"{summary['miss_latency']['mean']:>10.1f}"
    )
    print(
        f"  {'Median':<35s} "
        f"{summary['hit_latency']['median']:>10.1f} "
        f"{summary['miss_latency']['median']:>10.1f}"
    )
    print(
        f"  {'P95':<35s} "
        f"{summary['hit_latency']['p95']:>10.1f} "
        f"{summary['miss_latency']['p95']:>10.1f}"
    )

    print(f"\n  {'Speedup Factor (miss/hit)':<35s} {summary['speedup_factor']:>14.1f}x")
    print(f"  {'Total Wall-Clock Time':<35s} {summary['total_wall_clock_seconds']:>13.1f}s")
    print(f"  {'API Calls Saved':<35s} {summary['api_calls_saved']:>15d}")
    print(
        f"  {'Estimated Cost Saved':<35s} "
        f"{'$' + str(summary['estimated_cost_saved_usd']):>15s}"
    )
    print(f"{'='*60}\n")


def make_beautiful_json_report(
    seq_summary: dict,
    concur_summary: dict | None,
    lock_summary: dict | None,
    voice: str,
    language: str,
) -> dict:
    """Combine test results into a beautifully structured, self-documenting JSON object."""
    exec_summary = (
        f"This report evaluates the caching efficiency of the TTS Caching Server using the integrated Azure TTS backend. "
        f"Under sequential load, the server achieved a cache hit rate of {seq_summary['hit_rate_percent']}% across {seq_summary['total_requests']} requests. "
        f"Cache hits were served in an average of {seq_summary['hit_latency']['mean']}ms, compared to {seq_summary['miss_latency']['mean']}ms for "
        f"cache misses requiring on-the-fly synthesis. This represents a latency speedup of {seq_summary['speedup_factor']}x, "
        f"saving {seq_summary['api_calls_saved']} external calls and protecting the Azure Speech service from redundant workload."
    )

    report = {
        "report_metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "description": "Benchmark performance results for TTS Caching Server (direct Azure Speech SDK integration).",
            "voice_configured": voice,
            "language_configured": language,
        },
        "executive_summary": exec_summary,
        "sequential_test_metrics": {
            "total_requests_processed": seq_summary["total_requests"],
            "cache_hits": seq_summary["hits"],
            "cache_misses_synthesized": seq_summary["misses"],
            "errors": seq_summary["errors"],
            "cache_hit_rate": f"{seq_summary['hit_rate_percent']}%",
            "overall_throughput_req_sec": seq_summary["throughput_req_sec"],
            "total_elapsed_seconds": seq_summary["total_wall_clock_seconds"],
        },
        "latency_analysis_ms": {
            "cache_hits": {
                "mean_average": seq_summary["hit_latency"]["mean"],
                "median": seq_summary["hit_latency"]["median"],
                "p95_percentile": seq_summary["hit_latency"]["p95"],
            },
            "cache_misses": {
                "mean_average": seq_summary["miss_latency"]["mean"],
                "median": seq_summary["miss_latency"]["median"],
                "p95_percentile": seq_summary["miss_latency"]["p95"],
            },
            "performance_gain": {
                "speedup_factor": f"{seq_summary['speedup_factor']}x faster hits",
            },
        },
        "cost_and_usage_savings": {
            "external_api_calls_saved": seq_summary["api_calls_saved"],
            "simulated_cost_per_call_usd": seq_summary["cost_per_call_usd"],
            "total_simulated_money_saved_usd": seq_summary["estimated_cost_saved_usd"],
        },
    }

    if concur_summary is not None:
        report["concurrent_load_test_metrics"] = {
            "description": "Intense parallel batch load testing simulating multiple concurrent users.",
            "metrics": {
                "total_requests": concur_summary["total_requests"],
                "cache_hits": concur_summary["hits"],
                "cache_misses": concur_summary["misses"],
                "errors": concur_summary["errors"],
                "throughput_req_sec": concur_summary["throughput_req_sec"],
                "cache_hit_rate": f"{concur_summary['hit_rate_percent']}%",
            },
        }

    if lock_summary is not None:
        report["concurrency_lock_verification"] = {
            "description": "Simultaneous duplicate requests for the exact same uncached sentence to verify Redis lock deduplication.",
            "metrics": {
                "parallel_requests": lock_summary["concurrency"],
                "total_misses_triggers": lock_summary["misses"],
                "total_hits_deduplicated": lock_summary["hits"],
                "errors": lock_summary["errors"],
                "lock_working_correctly": lock_summary["lock_working"],
            },
        }

    report["glossary_of_terms"] = {
        "cache_hits": "Requests resolved instantly from local disk storage because the audio was previously generated.",
        "cache_misses": "Requests requiring live calls to Azure Text-to-Speech because the audio was not in the local cache.",
        "cache_hit_rate": "Percentage of requests resolved locally. Higher hit rates save bandwidth, API costs, and lower overall response times.",
        "throughput_req_sec": "The number of requests the server successfully completed per second.",
        "speedup_factor": "How many times faster a cache hit is compared to calling Azure to synthesize the voice on-the-fly.",
        "p95_percentile": "Response time threshold under which 95% of requests fall. Represents worst-case user-experience latency.",
        "concurrency_lock_verification": "Confirms that when N users request the exact same uncached voice prompt at the exact same instant, the server only makes ONE call to Azure. All other requests block temporarily and get served from the cache, preventing redundant charges.",
    }

    return report


def main() -> None:
    """CLI entry point for the benchmark script."""
    parser = argparse.ArgumentParser(
        description="Benchmark the TTS Caching Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Base URL of the TTS server (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--num-requests",
        type=int,
        default=20,
        help="Total number of sequential requests (default: 20)",
    )
    parser.add_argument(
        "--run-concurrent",
        action="store_true",
        help="Run an intense concurrent load test in addition to sequential test",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Batch size for concurrent load testing (default: 10)",
    )
    parser.add_argument(
        "--concurrency-test",
        action="store_true",
        help="Run the concurrency/lock test (10 parallel requests for same text)",
    )
    parser.add_argument(
        "--voice",
        default="en-IN-KavyaNeural",
        help="Azure voice to use for testing (default: en-IN-KavyaNeural)",
    )
    parser.add_argument(
        "--language",
        default="en-IN",
        help="Azure language parameter to use for testing (default: en-IN)",
    )
    parser.add_argument(
        "--output-file",
        default="benchmark_results.json",
        help="Output file for formatted JSON results (default: benchmark_results.json)",
    )
    args = parser.parse_args()

    asyncio.run(_async_main(args))


async def _async_main(args: argparse.Namespace) -> None:
    """Async main runner for the benchmark."""
    async with httpx.AsyncClient() as client:
        seq_summary = None
        concur_summary = None
        lock_summary = None

        # 1. Sequential benchmark
        print(f"\n{'='*60}")
        print(f"1. SEQUENTIAL BENCHMARK: {args.num_requests} requests")
        print(f"Voice: {args.voice} | Language: {args.language}")
        print(f"Server: {args.base_url}")
        print(f"{'='*60}\n")

        seq_summary = await run_sequential_benchmark(
            client=client,
            base_url=args.base_url,
            num_requests=args.num_requests,
            sentences=DEFAULT_SENTENCES,
            voice=args.voice,
            language=args.language,
        )
        print_summary(seq_summary, "1. SEQUENTIAL BENCHMARK SUMMARY")

        # 2. Concurrent Load benchmark
        if args.run_concurrent:
            print(f"\n{'='*60}")
            print(f"2. CONCURRENT LOAD BENCHMARK: {args.num_requests} requests (concurrency={args.concurrency})")
            print(f"Voice: {args.voice} | Language: {args.language}")
            print(f"Server: {args.base_url}")
            print(f"{'='*60}\n")

            concur_summary = await run_concurrent_benchmark(
                client=client,
                base_url=args.base_url,
                num_requests=args.num_requests,
                concurrency=args.concurrency,
                sentences=DEFAULT_SENTENCES,
                voice=args.voice,
                language=args.language,
            )
            print_summary(concur_summary, "2. CONCURRENT LOAD BENCHMARK SUMMARY")

        # 3. Concurrency Lock test (optional)
        if args.concurrency_test:
            lock_summary = await run_concurrency_test(
                client=client,
                base_url=args.base_url,
                concurrency=10,
                voice=args.voice,
                language=args.language,
            )

        # 4. Generate beautiful, self-documenting JSON report
        beautiful_report = make_beautiful_json_report(
            seq_summary=seq_summary,
            concur_summary=concur_summary,
            lock_summary=lock_summary,
            voice=args.voice,
            language=args.language,
        )

        with open(args.output_file, "w", encoding="utf-8") as f:
            json.dump(beautiful_report, f, indent=2)
        print(f"Beautiful JSON report saved to: {args.output_file}")


if __name__ == "__main__":
    main()

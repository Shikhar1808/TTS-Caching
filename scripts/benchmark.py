"""
Benchmark script for the TTS Caching Server.

A standalone client that quantifies caching performance by sending a mix
of duplicate and unique requests, measuring latencies, and verifying the
distributed lock works under concurrency.

Usage:
    uv run python scripts/benchmark.py --base-url http://localhost:8000
    uv run python scripts/benchmark.py --concurrency-test
    uv run python scripts/benchmark.py --num-requests 30 --output-file results.json

This script is optional and controlled by the demo_mode_enabled config flag
on the server side (it relies on the /demo/synthesize endpoint for testing).
"""

import argparse
import asyncio
import json
import random
import statistics
import time
from datetime import datetime, timezone

import httpx

# Default test sentences: 10 unique, each repeated once = 20 requests
DEFAULT_SENTENCES = [
    "Welcome to our text to speech service.",
    "The quick brown fox jumps over the lazy dog.",
    "Today's weather forecast calls for sunny skies and warm temperatures.",
    "Please hold while we transfer your call to the next available agent.",
    "Your order has been confirmed and will be delivered within three business days.",
    "Attention passengers, flight seven two nine is now boarding at gate twelve.",
    "Thank you for choosing our restaurant. Your table will be ready in fifteen minutes.",
    "The meeting has been rescheduled to tomorrow at ten in the morning.",
    "For technical support, please press one. For billing inquiries, press two.",
    "Congratulations! You have successfully completed the registration process.",
]

# Cost constant (example: $0.01 per API call)
COST_PER_API_CALL = 0.01


async def run_sequential_benchmark(
    client: httpx.AsyncClient,
    base_url: str,
    num_requests: int,
    sentences: list[str],
) -> dict:
    """Run the sequential benchmark with a mix of duplicate and unique requests.

    Args:
        client: httpx async client.
        base_url: Server base URL.
        num_requests: Total number of requests to send.
        sentences: List of unique sentences to cycle through.

    Returns:
        Dict with benchmark results.
    """
    # Build request list: each sentence repeated, then shuffled
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
                    "voice": "default",
                    "language": "en",
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
    all_latencies = [r["latency_ms"] for r in results if r["cache_status"] != "ERROR"]

    hits = len(hit_latencies)
    misses = len(miss_latencies)
    errors = len([r for r in results if r["cache_status"] == "ERROR"])
    total = len(results)
    hit_rate = (hits / total * 100) if total > 0 else 0

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
    concurrency: int = 10,
) -> dict:
    """Fire N simultaneous requests for the same uncached sentence.

    Verifies the distributed lock ensures only 1 external API call is made,
    not N.

    Args:
        client: httpx async client.
        base_url: Server base URL.
        concurrency: Number of parallel requests.

    Returns:
        Dict with concurrency test results.
    """
    # Use a unique sentence unlikely to be cached
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
                    "voice": "default",
                    "language": "en",
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

    # Fire all requests simultaneously
    tasks = [_send_request(i) for i in range(concurrency)]
    results = await asyncio.gather(*tasks)

    # Analyze
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


def print_summary(summary: dict) -> None:
    """Print a formatted summary table of benchmark results."""
    print(f"\n{'='*60}")
    print("BENCHMARK SUMMARY")
    print(f"{'='*60}")

    print(f"\n  {'Metric':<35s} {'Value':>15s}")
    print(f"  {'-'*35} {'-'*15}")
    print(f"  {'Total Requests':<35s} {summary['total_requests']:>15d}")
    print(f"  {'Cache Hits':<35s} {summary['hits']:>15d}")
    print(f"  {'Cache Misses':<35s} {summary['misses']:>15d}")
    print(f"  {'Errors':<35s} {summary['errors']:>15d}")
    print(f"  {'Hit Rate':<35s} {summary['hit_rate_percent']:>14.1f}%")

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
        "--concurrency-test",
        action="store_true",
        help="Run the concurrency/lock test (10 parallel requests for same text)",
    )
    parser.add_argument(
        "--output-file",
        default="benchmark_results.json",
        help="Output file for JSON results (default: benchmark_results.json)",
    )
    args = parser.parse_args()

    asyncio.run(_async_main(args))


async def _async_main(args: argparse.Namespace) -> None:
    """Async main runner for the benchmark."""
    async with httpx.AsyncClient() as client:
        all_results = {}

        # Sequential benchmark
        print(f"\n{'='*60}")
        print(f"SEQUENTIAL BENCHMARK: {args.num_requests} requests")
        print(f"Server: {args.base_url}")
        print(f"{'='*60}\n")

        summary = await run_sequential_benchmark(
            client=client,
            base_url=args.base_url,
            num_requests=args.num_requests,
            sentences=DEFAULT_SENTENCES,
        )
        print_summary(summary)
        all_results["sequential"] = summary

        # Concurrency test (optional)
        if args.concurrency_test:
            concurrency_result = await run_concurrency_test(
                client=client,
                base_url=args.base_url,
                concurrency=10,
            )
            all_results["concurrency"] = concurrency_result

        # Save to JSON
        # Remove the detailed results for cleaner output
        output = {k: {kk: vv for kk, vv in v.items() if kk != "results"} for k, v in all_results.items()}
        output["raw"] = all_results

        with open(args.output_file, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, default=str)
        print(f"Results saved to: {args.output_file}")


if __name__ == "__main__":
    main()

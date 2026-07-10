# TTS Caching Server

A production-grade FastAPI server that accepts text input, generates speech audio via the integrated Azure Text-to-Speech (TTS) SDK directly, and caches the result on local disk using content-addressable storage (xxh3_128 hashing). Identical requests never hit the Azure API twice. The server includes a **local fallback layer** that serves pre-generated files (e.g., from `TTSPrompt/`) directly at zero API cost. Cache lookups are backed by Redis metadata with distributed locking for safe concurrent access.

## Prerequisites

- **Python 3.11+**
- **Docker** (for Redis)
- **uv** — Python package manager ([install guide](https://docs.astral.sh/uv/getting-started/installation/))

## Quick Start

### 1. Clone and enter the project

```bash
cd "TTS Server"
```

### 2. Copy the environment file

```bash
cp .env.example .env
```

Default settings work out-of-the-box for local development with the built-in demo TTS endpoint and pre-generated audio fallback. Edit `.env` to customize.

### 3. Start Redis (Docker)

```bash
docker run -d --name tts-redis -p 6379:6379 redis:7-alpine
```

Verify Redis is running:

```bash
docker exec -it tts-redis redis-cli ping
# → PONG
```

### 4. Install dependencies

```bash
uv sync
```

### 5. Start the server

```bash
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The server starts at `http://localhost:8000`. You'll see:

```
INFO | Starting TTS Caching Server...
INFO | Cache directory: ./tts_cache
INFO | Demo mode: True
INFO | Pre-generated dir: ./TTSPrompt
INFO | Redis connection established
INFO | Pre-generated lookup: 8628 files indexed
INFO | TTS Caching Server started successfully
```

## Alternative: Running with Docker Compose

You can spin up both the FastAPI application and the Redis database using Docker Compose:

```bash
# Start both services
docker compose up --build -d
```

Verify that they are up:

```bash
docker compose ps
```

The app will be available on `http://localhost:8000`. You can check the logs using:

```bash
docker compose logs -f
```

To stop the containers:

```bash
docker compose down
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CACHE_DIR` | `./tts_cache` | Root directory for cached audio files |
| `PRE_GENERATED_DIR` | `./TTSPrompt` | Directory containing pre-generated audio files for local fallback |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `MAX_CACHE_SIZE_GB` | `10.0` | Max cache size before eviction |
| `EVICTION_ENABLED` | `false` | Toggle automatic LRU eviction |
| `EVICTION_THRESHOLD` | `0.8` | Eviction trigger (fraction of max size) |
| `DEMO_MODE_ENABLED` | `true` | Toggle `/demo/synthesize` endpoint |
| `AZURE_SPEECH_KEY` | *Required* | Subscription key for the Azure Speech SDK |
| `AZURE_SPEECH_REGION` | `centralindia` | Azure service region for the Speech SDK |
| `TTS_API_BASE_URL` | `http://localhost:8000/demo` | External TTS API base URL (for demo endpoint integration fallback) |
| `TTS_API_KEY` | `demo-key` | API key for external TTS |
| `TTS_API_TIMEOUT` | `30.0` | API call timeout (seconds) |
| `TTS_API_MAX_RETRIES` | `3` | Max retry attempts |
| `LOCK_TIMEOUT_SECONDS` | `30` | Redis lock TTL |
| `LOG_LEVEL` | `INFO` | Logging level |

## API Endpoints

### POST /tts — Generate or serve cached audio

```bash
curl -X POST http://localhost:8000/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "am i speaking with a. ajay", "voice": "en-IN-KavyaNeural", "language": "en-IN"}' \
  --output hello.wav
```

**Request body:**

```json
{
  "text": "am i speaking with a. ajay",
  "voice": "en-IN-KavyaNeural",
  "language": "en-IN",
  "format": "wav"
}
```

*Note: The `language` parameter automatically normalizes full names (like `"english"`) to ISO codes (like `"en"`) internally.*

**Response:** Binary audio file with headers:
- `Content-Type: audio/wav`
- `X-Cache-Status: HIT`, `PRE_GENERATED`, or `MISS`

- `HIT`: Served from the local redis/disk cache.
- `PRE_GENERATED`: Match found in `PRE_GENERATED_DIR` local fallback, served directly (and written to cache for subsequent requests).
- `MISS`: Generated via external TTS API call.

### GET /health — Health check

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "healthy",
  "redis_connected": true,
  "cache_dir_exists": true,
  "cache_dir_writable": true
}
```

### GET /cache/stats — Cache statistics

```bash
curl http://localhost:8000/cache/stats
```

```json
{
  "total_entries": 42,
  "total_size_bytes": 1847296,
  "total_size_gb": 0.0017,
  "cache_hits": 156,
  "cache_misses": 42,
  "hit_rate_percent": 78.79,
  "max_cache_size_gb": 10.0
}
```

## Running the Benchmark

The benchmark script measures caching performance with real numbers.

### Sequential test (default: 20 requests)

```bash
uv run python scripts/benchmark.py --base-url http://localhost:8000
```

### With concurrency/lock test

```bash
uv run python scripts/benchmark.py --base-url http://localhost:8000 --concurrency-test
```

### Custom request count

```bash
uv run python scripts/benchmark.py --num-requests 50 --output-file my_results.json
```

### What the output means

The benchmark sends a shuffled mix of duplicate and unique sentences, then reports:

| Metric | Meaning |
|--------|---------|
| **Hit Rate %** | Percentage of requests served from cache |
| **HIT latency** (mean/median/p95) | How fast cached responses are |
| **MISS latency** (mean/median/p95) | How long external API calls take |
| **Speedup Factor** | `miss_latency / hit_latency` — how many times faster cache hits are |
| **API Calls Saved** | Requests that didn't need to call the external API |
| **Estimated Cost Saved** | Based on configurable per-call cost ($0.01 default) |

The **concurrency test** fires 10 parallel requests for the same uncached text and verifies only 1 external API call was made (proving the distributed lock works).

Results are saved to `benchmark_results.json` for comparison across runs.

## Manual Eviction

If eviction is disabled (default), you can run it manually:

```bash
uv run python -m app.eviction
```

To enable automatic eviction after each cache write, set in `.env`:

```
EVICTION_ENABLED=true
```

## Hashed Fallback Migration

Pre-generated files in the local fallback directory (`TTSPrompt/`) must be named after their `xxh3_128` cache key hashes (e.g. `{cache_key}.wav`). 

If you have human-readable files (named like `english_dipti_am_i_speaking_with___a._abdul_rahaman.wav`), you can migrate them to the hashed layout using the provided script:

```bash
# Run a dry-run migration to inspect changes
uv run python -m scripts.hash_prompts --dir ./TTSPrompt --dry-run

# Run the migration for real (renames files in-place)
uv run python -m scripts.hash_prompts --dir ./TTSPrompt
```

## Configuring Azure TTS API

The server is integrated with the Azure Text-to-Speech (TTS) SDK directly. To configure your credentials:

1. Update `.env` with your Azure credentials:
   ```env
   AZURE_SPEECH_KEY=your-actual-subscription-key
   AZURE_SPEECH_REGION=centralindia
   ```

2. Request audio by specifying an Azure-supported language and voice in the POST body:
   - **English (India)**: `language: "en-IN"`, `voice: "en-IN-KavyaNeural"`
   - **Hindi (India)**: `language: "hi-IN"`, `voice: "hi-IN-KavyaNeural"`
   - **Marathi (India)**: `language: "mr-IN"`, `voice: "mr-IN-AarohiNeural"`

## Project Structure

```
TTS Server/
├── app/              # FastAPI app package
│   ├── __init__.py
│   ├── main.py       # FastAPI app + endpoints
│   ├── cache.py      # CacheManager (hashing, Redis, disk I/O, locking)
│   ├── tts_client.py # Direct Azure Speech SDK client (non-blocking threading + retry)
│   ├── config.py     # Pydantic Settings
│   ├── models.py     # Request/response models
│   ├── pre_generated.py # Pre-generated local fallback index
│   └── eviction.py   # LRU eviction (optional)
├── scripts/          # Standalone client / benchmark tools
│   ├── AzureFaberTTs.py # Azure TTS standalone generator script
│   └── benchmark.py  # Benchmark client with concurrent load testing
├── TTSPrompt/        # Pre-generated WAV audio files for local fallback
├── pyproject.toml    # uv project dependencies
├── .env.example      # Environment template
├── README.md         # This file
├── HOW_IT_WORKS.md   # Architecture & debugging guide
└── FUTURE_IMPROVEMENTS.md # Architectural advice for dynamic prompts
```

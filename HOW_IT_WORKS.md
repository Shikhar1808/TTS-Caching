# How It Works — TTS Caching Server Architecture

This document is an internal reference for debugging and understanding the full
request lifecycle, cache key formula, disk layout, Redis schema, distributed
locking, and LRU eviction.

---

## Table of Contents

1. [Request Lifecycle](#request-lifecycle)
2. [Cache Key Formula](#cache-key-formula)
3. [Directory Layout on Disk](#directory-layout-on-disk)
4. [Redis Key Schema](#redis-key-schema)
5. [Distributed Lock Mechanics](#distributed-lock-mechanics)
6. [LRU Eviction](#lru-eviction)
7. [Common Failure Modes](#common-failure-modes)
8. [Quick Debug Checklist](#quick-debug-checklist)

---

## Request Lifecycle

### Cache HIT Path (fast — no external API call)

```
Client POST /tts {"text":"Hello world","voice":"default","language":"english"}
    │
    ▼
1. Normalize language: "english" → "en"
    │
    ▼
2. Compute cache_key = xxh3_128("en_default_Hello world" + "wav").hexdigest()
   → e.g. "a3f7b2c1e9d04856a1b2c3d4e5f60718"
    │
    ▼
3. Redis GET tts:meta:a3f7b2c1e9d04856a1b2c3d4e5f60718
   → Found! Metadata JSON returned
    │
    ▼
4. Verify file exists: tts_cache/a3/a3f7b2c1e9d04856a1b2c3d4e5f60718.wav
   → Exists ✓
    │
    ▼
5. Redis INCR tts:stats:hits
    │
    ▼
6. Stream FileResponse with X-Cache-Status: HIT
    │
    ▼
7. (Fire-and-forget) Update last_accessed in Redis metadata
```

### Pre-Generated HIT Path (local fallback — no external API call)

```
Client POST /tts {"text":"am i speaking with a. ajay","voice":"dipti","language":"english"}
    │
    ▼
1. Normalize language: "english" → "en"
    │
    ▼
2. Compute cache_key = xxh3_128("en_dipti_am i speaking with a. ajay" + "wav").hexdigest()
   → e.g. "3fe6e89c23922812f98563d4b33ef991"
    │
    ▼
3. Redis GET tts:meta:3fe6e89c...
   → Not found (MISS in Redis)
    │
    ▼
4. Pre-generated lookup check: Look for matching file in TTSPrompt/
   → Found (english_dipti_am_i_speaking_with___a._ajay.wav) ✓
    │
    ▼
5. Read file bytes and write to local cache (tts_cache/3f/3fe6e89c...)
   and store metadata in Redis (for subsequent fast hits)
    │
    ▼
6. Stream FileResponse with X-Cache-Status: PRE_GENERATED
```

### Cache MISS Path (slow — calls external TTS API)

```
Client POST /tts {"text":"New sentence","voice":"default","language":"en"}
    │
    ▼
1. Compute cache_key = xxh3_128("en_default_New sentence" + "wav").hexdigest()
   → e.g. "b1e2f3a4c5d60718fedcba9876543210"
    │
    ▼
2. Redis GET tts:meta:b1e2f3a4c5d60718fedcba9876543210
   → Not found (MISS)
    │
    ▼
3. Pre-generated lookup check: Look for matching file in TTSPrompt/
   → Not found
    │
    ▼
4. Acquire lock: SET lock:b1e2f3a4c5d60718fedcba9876543210 <uuid> NX PX 30000
   → Acquired ✓
    │
    ▼
5. Double-check: Redis GET tts:meta:b1e2...  (race condition guard)
   → Still not found
    │
    ▼
6. Call external TTS API: POST {base_url}/synthesize
   with retry + exponential backoff (tenacity)
   → Returns audio bytes
    │
    ▼
7. Write temp file:   tts_cache/b1/b1e2f3a4c5d60718fedcba9876543210.wav.tmp
    │
    ▼
8. Atomic rename:     tts_cache/b1/b1e2f3a4c5d60718fedcba9876543210.wav
    │
    ▼
9. Store metadata in Redis: SET tts:meta:b1e2f3a4c5d60718fedcba9876543210 <JSON>
   Redis INCR tts:stats:misses
    │
    ▼
10. Release lock: DEL lock:b1e2f3a4c5d60718fedcba9876543210
    (only if lock value matches — Lua script)
    │
    ▼
11. Stream FileResponse with X-Cache-Status: MISS
    │
    ▼
12. (Optional) Background task: check eviction threshold
```

---

## Cache Key Formula

The cache key is a **pure hash** — no human-readable prefix, no slug.

### Formula

```python
input_string = f"{language}_{voice}_{text}"
cache_key    = xxh3_128((input_string + format).encode("utf-8")).hexdigest()
filename     = f"{cache_key}.{format}"
```

### Worked Example

**Input:**
```
text     = "Hello, welcome to our service."
voice    = "default"
language = "en"
format   = "wav"
```

**Computation:**
```python
import xxhash

input_string = "en_default_Hello, welcome to our service."
hash_input   = input_string + "wav"
# hash_input = "en_default_Hello, welcome to our service.wav"

cache_key = xxhash.xxh3_128(hash_input.encode("utf-8")).hexdigest()
# cache_key = "a7c3e1f9b2d4068513579bdf2468ace0"  (example)

filename = "a7c3e1f9b2d4068513579bdf2468ace0.wav"
shard    = "a7"
path     = "tts_cache/a7/a7c3e1f9b2d4068513579bdf2468ace0.wav"
```

### What feeds into the hash

| Field | Included | Why |
|-------|----------|-----|
| `language` | ✓ | Different language = different pronunciation |
| `voice` | ✓ | Different voice = different audio |
| `text` | ✓ | Different text = different content |
| `format` | ✓ | Different format = different encoding |

### What does NOT feed into the hash

The hash uses only the four fields above. Any additional parameters (if added
later) would need to be added to the hash input string to avoid false cache hits.

---

## Directory Layout on Disk

Files are sharded into subdirectories based on the first 2 hex characters of
the cache key. This creates up to 256 directories (`00` through `ff`), preventing
any single directory from containing too many files.

```
tts_cache/
├── a3/
│   ├── a3f7b2c1e9d04856a1b2c3d4e5f60718.wav
│   └── a39e1c2b4d560f87e3a1b9c7d5f80246.wav
├── a7/
│   └── a7c3e1f9b2d4068513579bdf2468ace0.wav
├── b1/
│   ├── b1e2f3a4c5d60718fedcba9876543210.wav
│   └── b1e2f3a4c5d60718fedcba9876543210.wav.tmp   ← in-progress write
├── ff/
│   └── ff0a1b2c3d4e5f6789abcdef01234567.wav
└── ...
```

**Notes:**
- `.wav.tmp` files are in-progress writes; they get renamed to `.wav` atomically
- If you see stale `.tmp` files, they're from crashed/interrupted writes and can be safely deleted
- The shard directory (`a3/`, `b1/`, etc.) is created automatically on first write

---

## Redis Key Schema

### Metadata: `tts:meta:{cache_key}`

Each cached file has a metadata entry in Redis. The key is `tts:meta:` followed
by the full cache key (hex hash).

**Example key:** `tts:meta:a7c3e1f9b2d4068513579bdf2468ace0`

**Example value (JSON string):**

```json
{
  "path": "C:/Users/saxen/Desktop/work/Voxomos/TTS Server/tts_cache/a7/a7c3e1f9b2d4068513579bdf2468ace0.wav",
  "size_bytes": 44144,
  "created_at": "2026-07-09T13:30:00.123456+00:00",
  "last_accessed": "2026-07-09T14:15:42.789012+00:00",
  "language": "en",
  "voice": "default",
  "text": "Hello, welcome to our service.",
  "format": "wav"
}
```

This is where the full human-readable context lives — the filename itself is
just a hash, so this metadata is essential for debugging/auditing which hash
maps to which sentence.

### Lock: `lock:{cache_key}`

Distributed lock key. The value is a UUID owned by the lock holder.

**Example key:** `lock:a7c3e1f9b2d4068513579bdf2468ace0`
**Example value:** `"f47ac10b-58cc-4372-a567-0e02b2c3d479"`
**TTL:** 30 seconds (configurable via `LOCK_TIMEOUT_SECONDS`)

### Stats Counters

| Key | Type | Description |
|-----|------|-------------|
| `tts:stats:hits` | Integer (INCR) | Total cache hit count |
| `tts:stats:misses` | Integer (INCR) | Total cache miss count |

---

## Distributed Lock Mechanics

### Why it exists

Without the lock, if 10 simultaneous requests arrive for the same uncached text:

```
Request 1: MISS → calls external API → generates audio → writes to disk
Request 2: MISS → calls external API → generates audio → writes to disk  ← WASTED
Request 3: MISS → calls external API → generates audio → writes to disk  ← WASTED
... (10 redundant API calls for the same audio)
```

With the lock:

```
Request 1: MISS → acquires lock → calls external API → writes → releases lock
Request 2: MISS → lock busy, waits... → lock released → re-checks cache → HIT!
Request 3: MISS → lock busy, waits... → lock released → re-checks cache → HIT!
... (only 1 API call, 9 served from cache)
```

### Step-by-step race condition walkthrough

1. **Request A** arrives, computes `cache_key = "abc123..."`, checks Redis → MISS
2. **Request B** arrives 10ms later, same text, same `cache_key` → also MISS
3. **Request A** runs `SET lock:abc123... <uuid-A> NX PX 30000` → **acquired** ✓
4. **Request B** runs `SET lock:abc123... <uuid-B> NX PX 30000` → **denied** (key exists)
5. **Request B** sleeps 500ms, retries... still locked, sleeps again
6. **Request A** calls external API, gets audio bytes
7. **Request A** writes `abc123....wav.tmp` → renames to `abc123....wav`
8. **Request A** stores metadata in Redis under `tts:meta:abc123...`
9. **Request A** releases lock: Lua script checks `GET lock:abc123...` == `uuid-A` → `DEL` ✓
10. **Request B** retries lock... but instead of acquiring, it first **re-checks cache**
11. **Request B** finds `tts:meta:abc123...` in Redis → **HIT!** → serves cached file
12. **Request B** never acquired the lock, never called the external API

### Lock safety

- **Lock value is a UUID**: only the holder can release it (Lua script)
- **TTL prevents deadlocks**: if the holder crashes, the lock auto-expires after 30s
- **Double-check after lock**: prevents redundant generation even when lock is acquired

---

## LRU Eviction

### How it decides what to delete

1. Read `last_accessed` from every `tts:meta:*` entry in Redis
2. Sort all entries by `last_accessed`, **oldest first**
3. Delete files from the oldest until disk usage drops below 70% of `max_cache_size_gb`
4. For each deletion: remove file from disk + remove metadata from Redis

### When it runs

| Mode | Trigger | Config |
|------|---------|--------|
| **Automatic** | After each `put()` (cache write) | `EVICTION_ENABLED=true` |
| **Manual** | CLI command | `uv run python -m app.eviction` |
| **Disabled** (default) | Never | `EVICTION_ENABLED=false` |

### Thresholds

With default settings (`MAX_CACHE_SIZE_GB=10`, `EVICTION_THRESHOLD=0.8`):

- **Trigger at**: 8 GB (80% of 10 GB)
- **Target after eviction**: 7 GB (70% — creates headroom to avoid constant eviction)

### Manual eviction

```bash
uv run python -m app.eviction
```

Output:

```
INFO | Eviction check: usage=8589934592 bytes (8.00 GB), threshold=8589934592 bytes (8.00 GB)
INFO | Evicted: a3f7b2c1... (44144 bytes, last_accessed=2026-07-01T..., text='Old sentence...')
INFO | Evicted: b1e2f3a4... (44144 bytes, last_accessed=2026-07-02T..., text='Another old...')
INFO | Eviction complete: evicted 2 files, freed 88288 bytes (0.08 MB)
```

---

## Common Failure Modes

### 1. File in Redis metadata but missing from disk

**Symptom:** Cache reports a HIT, but returns an error or re-generates.

**Cause:** File was manually deleted, disk failure, or stale metadata.

**What happens:** The `get()` method checks `Path(metadata.path).exists()` — if the file
is gone, it deletes the Redis entry and returns a cache MISS. The file is regenerated.

**Debug:**
```bash
docker exec -it tts-redis redis-cli GET "tts:meta:a7c3e1f9b2d4068513579bdf2468ace0"
# Check if the "path" field points to an existing file
```

### 2. External API timeout

**Symptom:** `POST /tts` returns HTTP 502 after a delay.

**Cause:** The external TTS API is slow or unreachable.

**What happens:** tenacity retries 3 times with exponential backoff (1s, 2s, 4s).
After all retries fail, a `TTSGenerationError` is raised → HTTP 502 response.

**Debug:** Check logs for `TTS generation FAILED after Xms` messages. Verify
`TTS_API_BASE_URL` is correct and the API is reachable.

### 3. Lock never released (stuck lock)

**Symptom:** All requests for a specific text hang for 10 seconds then return 503.

**Cause:** The lock holder crashed or the process was killed mid-generation.

**What happens:** The lock has a TTL (default 30s) and auto-expires. After the TTL,
the next request can acquire the lock normally.

**Manual fix:**
```bash
# Find and delete the stuck lock
docker exec -it tts-redis redis-cli KEYS "lock:*"
docker exec -it tts-redis redis-cli DEL "lock:a7c3e1f9b2d4068513579bdf2468ace0"
```

### 4. Stale .tmp files on disk

**Symptom:** `.tmp` files accumulating in shard directories.

**Cause:** Server crashed between writing the temp file and renaming it.

**Fix:** Safe to delete any `.tmp` file manually. They are excluded from disk
usage calculations.

```bash
# Find and delete stale temp files
find tts_cache/ -name "*.tmp" -delete
```

### 5. Redis connection lost

**Symptom:** All requests fail with 500 errors.

**Debug:**
```bash
docker exec -it tts-redis redis-cli ping
# If PONG → Redis is fine, check REDIS_URL in .env
# If error → restart Redis: docker restart tts-redis
```

---

## Quick Debug Checklist

### Inspect a specific cache key's metadata

```bash
# 1. Compute the cache key by hand (with language normalization)
python -c "
import xxhash
text = 'am i speaking with a. ajay'
voice = 'dipti'
language = 'english'
fmt = 'wav'

# Normalize language
aliases = {'english': 'en', 'hindi': 'hi', 'tamil': 'ta', 'telugu': 'te'}
normalized_lang = aliases.get(language.strip().lower(), language.strip().lower())

input_str = f'{normalized_lang}_{voice}_{text}'
key = xxhash.xxh3_128((input_str + fmt).encode()).hexdigest()
print(f'normalized_lang: {normalized_lang}')
print(f'cache_key:       {key}')
print(f'shard:           {key[:2]}')
print(f'redis_key:       tts:meta:{key}')
"

# 2. Check Redis for the metadata
docker exec -it tts-redis redis-cli GET "tts:meta:<cache_key>"

# 3. Check if the file exists on disk
ls tts_cache/<shard>/<cache_key>.wav
```

### Check cache stats

```bash
# Via API
curl http://localhost:8000/cache/stats | python -m json.tool

# Via Redis directly
docker exec -it tts-redis redis-cli GET "tts:stats:hits"
docker exec -it tts-redis redis-cli GET "tts:stats:misses"
```

### List all cached entries

```bash
docker exec -it tts-redis redis-cli KEYS "tts:meta:*"
```

### Check for stuck locks

```bash
docker exec -it tts-redis redis-cli KEYS "lock:*"
# Any results here = active or stuck locks
# Check TTL:
docker exec -it tts-redis redis-cli TTL "lock:<cache_key>"
```

### Reset stats counters

```bash
docker exec -it tts-redis redis-cli DEL "tts:stats:hits" "tts:stats:misses"
```

### Flush entire cache (nuclear option)

```bash
# Delete all Redis metadata
docker exec -it tts-redis redis-cli KEYS "tts:meta:*" | xargs docker exec -i tts-redis redis-cli DEL
docker exec -it tts-redis redis-cli DEL "tts:stats:hits" "tts:stats:misses"

# Delete all disk files
rm -rf tts_cache/
```

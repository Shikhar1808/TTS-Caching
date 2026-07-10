# Architectural Guide: Dynamic Prompts & Future Improvements

This document outlines best practices for handling dynamic prompts (varying parameters like dates, names, ticket IDs) and highlights other future improvements for the TTS Caching Server.

---

## 1. Handling Dynamic Prompts

Since the server computes cache keys using a cryptographic hash of the entire text string, any dynamic parameter change triggers a cache miss. Below are two architectural patterns to handle this.

### Pattern A: Template Splitting & Stitched Audio (Recommended)
Instead of sending a fully combined sentence to the TTS server, split it into **static template segments** and **dynamic variables** at the client application level (e.g., your telephony platform, IVR script, or Asterisk dial plan).

```
                      +-----------------------------+
                      | "Your Ticket ID is"         |  --> [POST /tts] (Cache HIT <10ms)
                      +-----------------------------+
                                     |
                                  (Stitch)
                                     |
                      +-----------------------------+
                      | "1"  "0"  "0"  "2"  "4"       |  --> Play pre-cached digit wavs
                      +-----------------------------+
```

* **How it works**:
  1. Cache static parts ahead of time (e.g., `"Your Ticket ID is"`, `"Please hold while we transfer you to"`).
  2. Use DTMF or individual digit audio files (already cached locally) to read numbers, dates, or letters.
* **Benefits**:
  * **Near 100% Cache Hit Rate** for all requests.
  * Zero live API calls to Azure TTS during customer sessions.
  * Minimal disk cache space required.

---

### Pattern B: On-The-Fly Synthesis with LRU Eviction
If stitching audio is not possible (e.g., reading out full addresses or complex names), you must synthesize the prompt dynamically. To prevent infinite disk growth, enable the server's **Least Recently Used (LRU) Eviction**.

```env
# Enable automatic eviction on disk threshold
EVICTION_ENABLED=true
MAX_CACHE_SIZE_GB=5.0
EVICTION_THRESHOLD=0.8
```

* **How it works**:
  1. The server generates a unique cache file for the text on a miss.
  2. When the folder size reaches `80%` of `5.0 GB` (i.e. `4.0 GB`), a background worker evaluates Redis metadata and deletes the oldest, least accessed files.
* **Benefits**:
  * Safeguards disk space from filling up.
  * Handles infinite variations of dynamic texts.
  * Duplicate hits (e.g. if the same user repeats the address prompt) are still served instantly.

---

## 2. Recommended Future Improvements

### Audio Transcoding (Pydub Integration)
* **Goal**: Support multiple audio output formats dynamically.
* **Details**: Currently, the server maps the `wav` format to Azure's `Riff8Khz16BitMonoPcm`. If a client requests `.mp3` or `.ogg`, we should use `pydub` (which is already a project dependency) to transcode the WAV bytes in-memory before returning them.

### Cache Warming (Pre-fetching)
* **Goal**: Minimize first-load latency for key prompts.
* **Details**: Add a startup task that reads standard prompts (from a static JSON config or database) and pre-synthesizes them on startup. This ensures that the very first callers of the day experience `HIT` level latency for standard prompts.

### Metrics Monitoring (Prometheus/Grafana)
* **Goal**: Provide dashboard visibility into caching health.
* **Details**: Export Redis statistics (`tts:stats:hits`, `tts:stats:misses`) as Prometheus metrics. Stakeholders can then monitor:
  * Overall hit rate trends.
  * Hourly throughput.
  * Dollar savings from cached API requests.

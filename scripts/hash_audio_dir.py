"""
Standalone Python script to batch-process audio files from an input directory (e.g. ./TTSPrompt_test)
and export them to an output directory with xxHash128 (XXH3_128) hashed filenames.

Usage:
  python scripts/hash_audio_dir.py --input-dir ./TTSPrompt_test --output-dir ./TTSPrompt_hashed
"""

import argparse
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

try:
    import xxhash
except ImportError:
    raise ImportError("xxhash library is required. Install via 'pip install xxhash' or 'uv add xxhash'.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger("hash_audio_dir")

# BCP-47 / Language aliases mapping
LANGUAGE_MAP: dict[str, str] = {
    "english": "english", "en": "english", "en-in": "english",
    "hindi": "hindi", "hi": "hindi", "hi-in": "hindi",
    "tamil": "tamil", "ta": "tamil", "ta-in": "tamil",
    "telugu": "telugu", "te": "telugu", "te-in": "telugu",
    "kannada": "kannada", "kn": "kannada", "kn-in": "kannada",
    "malayalam": "malayalam", "ml": "malayalam", "ml-in": "malayalam",
    "bengali": "bengali", "bn": "bengali", "bn-in": "bengali",
    "gujarati": "gujarati", "gu": "gujarati", "gu-in": "gujarati",
    "marathi": "marathi", "mr": "marathi", "mr-in": "marathi",
    "punjabi": "punjabi", "pa": "punjabi", "pa-in": "punjabi",
    "urdu": "urdu", "ur": "urdu", "ur-in": "urdu",
    "odia": "odia", "or": "odia", "or-in": "odia",
    "assamese": "assamese", "as": "assamese", "as-in": "assamese",
}


def is_already_hashed(filename: str) -> bool:
    name, _, ext = filename.rpartition(".")
    return len(name) == 32 and all(c in "0123456789abcdefABCDEF" for c in name) if ext else False


def parse_filename(filename: str) -> Optional[tuple[str, str, str, str]]:
    name, _, ext = filename.rpartition(".")
    if not name or not ext or ext.lower() not in ("wav", "mp3", "ogg", "flac"):
        return None

    parts = name.split("_", 2)
    if len(parts) < 3:
        return None

    raw_lang, voice, raw_text = parts
    lang = LANGUAGE_MAP.get(raw_lang.strip().lower(), raw_lang.strip().lower())
    text = " ".join(raw_text.replace("___", " ").replace("_", " ").split())

    return (lang, voice, text, ext.lower()) if text else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone batch audio file xxHash128 exporter.")
    parser.add_argument("--input-dir", "-i", default="./TTSPrompt_test", help="Source directory (default: ./TTSPrompt_test)")
    parser.add_argument("--output-dir", "-o", default="./TTSPrompt_hashed", help="Target directory (default: ./TTSPrompt_hashed)")
    parser.add_argument("--move", action="store_true", help="Move files instead of copying")
    args = parser.parse_args()

    input_path = Path(args.input_dir).resolve()
    output_path = Path(args.output_dir).resolve()

    if not input_path.exists() or not input_path.is_dir():
        logger.error("Input directory not found: %s", input_path)
        return

    logger.info("Scanning '%s' -> '%s' [Mode: %s]", input_path, output_path, "MOVE" if args.move else "COPY")

    output_path.mkdir(parents=True, exist_ok=True)

    total, processed, already_hashed, skipped, errors = 0, 0, 0, 0, 0

    for entry in os.scandir(input_path):
        if not entry.is_file():
            continue

        total += 1
        filename = entry.name

        if is_already_hashed(filename):
            cache_key, _, ext = filename.rpartition(".")
            hashed_filename = filename
        else:
            parsed = parse_filename(filename)
            if parsed is None:
                skipped += 1
                continue

            lang, voice, text, fmt = parsed
            try:
                cache_key = xxhash.xxh3_128(f"{lang}_{voice}_{text}{fmt}".encode("utf-8")).hexdigest()
                hashed_filename = f"{cache_key}.{fmt}"
            except Exception as e:
                errors += 1
                logger.warning("Hash error for %s: %s", filename, e)
                continue

        target_file = output_path / hashed_filename

        if target_file.exists():
            already_hashed += 1
            continue

        try:
            if args.move:
                shutil.move(entry.path, target_file)
            else:
                shutil.copy2(entry.path, target_file)
            processed += 1
        except Exception as e:
            errors += 1
            logger.error("Failed to process %s: %s", filename, e)

    logger.info("Complete: Total=%d | Exported=%d | Existing/AlreadyHashed=%d | Skipped=%d | Errors=%d", total, processed, already_hashed, skipped, errors)


if __name__ == "__main__":
    main()

"""
Migration script to hash all pre-generated audio filenames in a directory.

Parses human-readable filenames matching the pattern:
    {language}_{voice}_{prompt_text}___{dynamic_variable}.{ext}
computes their xxh3_128 cache key hashes, and renames the files in place to
    {cache_key}.{ext} (e.g. 2128bfe33529608c971421b8fcd1c72d.wav).

Usage:
    uv run python scripts/hash_prompts.py --dir ./TTSPrompt
    uv run python scripts/hash_prompts.py --dir ./TTSPrompt --dry-run
"""

import argparse
import logging
import os
import re
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("hash_prompts")


def is_already_hashed(filename: str) -> bool:
    """Check if the filename is already a 32-character hex hash with extension.

    Examples:
        2128bfe33529608c971421b8fcd1c72d.wav -> True
    """
    name, _, ext = filename.rpartition(".")
    if not name or not ext:
        return False
    return bool(re.match(r"^[0-9a-f]{32}$", name))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rename pre-generated files to their cache-key hashes"
    )
    parser.add_argument(
        "--dir",
        default="./TTSPrompt",
        help="Path to the directory containing pre-generated files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the renaming without modifying files",
    )
    args = parser.parse_args()

    directory = Path(args.dir)
    if not directory.exists() or not directory.is_dir():
        logger.error("Directory not found or is not a directory: %s", directory)
        return

    # Import app modules. Ensure we are running from project root so python path works.
    try:
        from app.cache import CacheManager
        from app.models import TTSRequest
        from app.pre_generated import _parse_filename
    except ImportError as e:
        logger.error(
            "Failed to import app modules: %s. Run this script from the project root directory.",
            e,
        )
        return

    logger.info("Scanning directory: %s", directory.resolve())
    if args.dry_run:
        logger.info("=== DRY RUN MODE — No files will be modified ===")

    total = 0
    renamed = 0
    skipped_hashed = 0
    skipped_invalid = 0
    errors = 0

    for entry in os.scandir(directory):
        if not entry.is_file():
            continue

        total += 1

        # Check if already hashed
        if is_already_hashed(entry.name):
            skipped_hashed += 1
            continue

        # Parse filename components
        parsed = _parse_filename(entry.name)
        if parsed is None:
            skipped_invalid += 1
            logger.debug("Skipped non-matching filename: %s", entry.name)
            continue

        language, voice, text, fmt = parsed

        try:
            # Recreate TTSRequest to compute hash key exactly like CacheManager
            request = TTSRequest(
                text=text,
                voice=voice,
                language=language,
                format=fmt,
            )
            cache_key = CacheManager.compute_key(request)
        except Exception as e:
            errors += 1
            logger.warning("Error generating request model for %s: %s", entry.name, e)
            continue

        old_path = Path(entry.path)
        new_filename = f"{cache_key}.{fmt}"
        new_path = directory / new_filename

        # Print migration action
        logger.info(
            "Rename: %s -> %s (lang=%s, voice=%s, text='%s...')",
            entry.name,
            new_filename,
            language,
            voice,
            text[:30],
        )

        if not args.dry_run:
            try:
                # Handle potential duplicate hashes (multiple source files map to same request properties)
                if new_path.exists() and new_path.resolve() != old_path.resolve():
                    logger.warning(
                        "Destination file already exists (duplicate key): %s. Removing duplicate source file: %s",
                        new_filename,
                        entry.name,
                    )
                    os.remove(old_path)
                else:
                    os.rename(old_path, new_path)
                renamed += 1
            except Exception as e:
                errors += 1
                logger.error("Failed to rename %s to %s: %s", entry.name, new_filename, e)
        else:
            renamed += 1

    logger.info("Migration complete:")
    logger.info("  Total scanned:      %d", total)
    logger.info("  Renamed (migrated): %d", renamed)
    logger.info("  Skipped (already hashed): %d", skipped_hashed)
    logger.info("  Skipped (invalid/other):  %d", skipped_invalid)
    logger.info("  Errors:             %d", errors)


if __name__ == "__main__":
    main()

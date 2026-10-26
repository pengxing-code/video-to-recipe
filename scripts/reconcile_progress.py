import argparse
import csv
import io
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".mts", ".m2ts"}
SOURCE_ID_RE = re.compile(r"(?:来源编号|source[_ ]?id)\s*[:：]\s*`?([^`\r\n]+)", re.IGNORECASE)
SOURCE_LINE_RE = re.compile(r"来源|原视频|视频路径|source", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild a source-based progress ledger for cooking videos, transcripts, and recipes."
    )
    parser.add_argument("videos", help="Video file or directory")
    parser.add_argument("--transcripts", required=True, help="Directory containing transcript text files")
    parser.add_argument("--recipes", required=True, help="Directory containing Markdown recipe files")
    parser.add_argument("--output", default=None, help="Progress CSV path")
    parser.add_argument("--batch-size", type=int, choices=(3, 4), default=4)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Exit nonzero unless every source maps one-to-one")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding=encoding)
    os.replace(temporary, path)


def normalize_alias(value: str) -> str:
    value = value.strip().strip("`\"'")
    value = value.replace("_", " ")
    return " ".join(value.split()).casefold()


def leaf_stem(value: str) -> str:
    cleaned = value.strip().strip("`\"'")
    cleaned = cleaned.replace("/", "\\")
    name = PureWindowsPath(cleaned).name
    suffix = PureWindowsPath(name).suffix.lower()
    return PureWindowsPath(name).stem if suffix in VIDEO_EXTENSIONS else name


def collect_videos(path: Path, recursive: bool) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(f"Unsupported video type: {path.suffix}")
        return [path.resolve()]
    if not path.is_dir():
        raise FileNotFoundError(f"Video input does not exist: {path}")
    iterator = path.rglob("*") if recursive else path.glob("*")
    return sorted(
        (item.resolve() for item in iterator if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda item: str(item).casefold(),
    )


def build_source_lookup(videos: list[Path]):
    names = [video.stem.casefold() for video in videos]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate source IDs: video filename stems must be unique, including nested folders and different extensions.")
    exact = {video.stem.casefold(): video.stem for video in videos}
    aliases = defaultdict(list)
    for video in videos:
        aliases[normalize_alias(video.stem)].append(video.stem)
    return exact, aliases


def resolve_source(value: str, exact: dict[str, str], aliases: dict[str, list[str]]) -> str | None:
    candidate = leaf_stem(value)
    direct = exact.get(candidate.casefold())
    if direct:
        return direct
    matches = aliases.get(normalize_alias(candidate), [])
    return matches[0] if len(matches) == 1 else None


def recipe_sources(text: str, videos: list[Path], exact, aliases) -> set[str]:
    matches = set()
    for value in SOURCE_ID_RE.findall(text):
        source = resolve_source(value, exact, aliases)
        if source:
            matches.add(source)

    source_lines = [line for line in text.splitlines() if SOURCE_LINE_RE.search(line)]
    for line in source_lines:
        normalized_line = normalize_alias(line)
        for video in videos:
            exact_name = video.name.casefold()
            exact_stem = video.stem.casefold()
            alias_stem = normalize_alias(video.stem)
            if exact_name in line.casefold() or exact_stem in line.casefold() or alias_stem in normalized_line:
                matches.add(video.stem)

    if not matches:
        folded = text.casefold()
        normalized_text = normalize_alias(text)
        for video in videos:
            if video.name.casefold() in folded or normalize_alias(video.name) in normalized_text:
                matches.add(video.stem)
    return matches


def transcript_source(path: Path, exact, aliases) -> str | None:
    stem = path.stem
    if stem.casefold().endswith(".transcript"):
        stem = stem[: -len(".transcript")]
    return resolve_source(stem, exact, aliases)


def status_for(transcripts: list[Path], recipes: list[Path], empty_recipes: set[Path]) -> str:
    issues = []
    if not transcripts:
        issues.append("missing-transcript")
    elif len(transcripts) > 1:
        issues.append("duplicate-transcript")
    if not recipes:
        issues.append("missing-recipe")
    elif len(recipes) > 1:
        issues.append("duplicate-recipe")
    if any(recipe in empty_recipes for recipe in recipes):
        issues.append("empty-recipe")
    return "+".join(issues) if issues else "done"


def main() -> int:
    # Keep Chinese source IDs readable even with legacy Windows output encodings.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parse_args()
    video_input = Path(args.videos).expanduser()
    transcript_dir = Path(args.transcripts).expanduser().resolve()
    recipe_dir = Path(args.recipes).expanduser().resolve()
    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else recipe_dir.parent / "recipe-progress.csv"
    )
    batch_path = output_path.with_suffix(".next-batch.json")

    try:
        videos = collect_videos(video_input, args.recursive)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not transcript_dir.is_dir():
        print(f"Transcript directory does not exist: {transcript_dir}", file=sys.stderr)
        return 2
    if not recipe_dir.is_dir():
        print(f"Recipe directory does not exist: {recipe_dir}", file=sys.stderr)
        return 2

    try:
        exact, aliases = build_source_lookup(videos)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    transcript_files = sorted(transcript_dir.glob("*.txt"), key=lambda path: path.name.casefold())
    recipe_files = sorted(recipe_dir.glob("*.md"), key=lambda path: path.name.casefold())
    transcripts_by_source = defaultdict(list)
    recipes_by_source = defaultdict(list)
    orphan_transcripts = []
    orphan_recipes = []
    ambiguous_recipes = []
    empty_recipes = set()

    for transcript in transcript_files:
        source = transcript_source(transcript, exact, aliases)
        if source:
            transcripts_by_source[source].append(transcript)
        else:
            orphan_transcripts.append(transcript)

    for recipe in recipe_files:
        text = recipe.read_text(encoding="utf-8-sig", errors="replace")
        if not text.strip():
            empty_recipes.add(recipe)
        sources = recipe_sources(text, videos, exact, aliases)
        if len(sources) == 1:
            recipes_by_source[next(iter(sources))].append(recipe)
        elif len(sources) > 1:
            ambiguous_recipes.append((recipe, sorted(sources)))
        else:
            orphan_recipes.append(recipe)

    rows = []
    next_batch = []
    frames_root = transcript_dir.parent / "frames"
    for video in videos:
        source = video.stem
        transcripts = transcripts_by_source[source]
        recipes = recipes_by_source[source]
        status = status_for(transcripts, recipes, empty_recipes)
        row = {
            "source_id": source,
            "video_path": str(video),
            "transcript_path": ";".join(str(path) for path in transcripts),
            "recipe_path": ";".join(str(path) for path in recipes),
            "status": status,
        }
        rows.append(row)
        if len(transcripts) == 1 and not recipes and len(next_batch) < args.batch_size:
            next_batch.append(
                {
                    "source_id": source,
                    "video_path": str(video),
                    "transcript_path": str(transcripts[0]),
                    "frames_path": str(frames_root / source),
                }
            )

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=("source_id", "video_path", "transcript_path", "recipe_path", "status"),
    )
    writer.writeheader()
    writer.writerows(rows)
    atomic_write_text(output_path, buffer.getvalue(), encoding="utf-8-sig")

    done = sum(row["status"] == "done" for row in rows)
    duplicate_recipe_sources = sum(len(paths) > 1 for paths in recipes_by_source.values())
    duplicate_transcript_sources = sum(len(paths) > 1 for paths in transcripts_by_source.values())
    summary = {
        "generated_at": utc_now(),
        "video_count": len(videos),
        "transcript_file_count": len(transcript_files),
        "recipe_file_count": len(recipe_files),
        "done_count": done,
        "pending_count": len(videos) - done,
        "orphan_transcript_count": len(orphan_transcripts),
        "orphan_recipe_count": len(orphan_recipes),
        "ambiguous_recipe_count": len(ambiguous_recipes),
        "duplicate_transcript_source_count": duplicate_transcript_sources,
        "duplicate_recipe_source_count": duplicate_recipe_sources,
        "empty_recipe_count": len(empty_recipes),
        "progress_csv": str(output_path),
        "next_batch": next_batch,
        "anomalies": {
            "orphan_transcripts": [str(path) for path in orphan_transcripts],
            "orphan_recipes": [str(path) for path in orphan_recipes],
            "ambiguous_recipes": [
                {"recipe": str(path), "source_ids": sources} for path, sources in ambiguous_recipes
            ],
        },
    }
    atomic_write_text(batch_path, json.dumps(summary, ensure_ascii=False, indent=2))

    print(
        "Progress: "
        f"videos={len(videos)}, transcripts={len(transcript_files)}, recipes={len(recipe_files)}, "
        f"done={done}, pending={len(videos) - done}",
        flush=True,
    )
    print(
        "Anomalies: "
        f"orphan_transcripts={len(orphan_transcripts)}, orphan_recipes={len(orphan_recipes)}, "
        f"ambiguous_recipes={len(ambiguous_recipes)}, duplicate_transcript_sources={duplicate_transcript_sources}, "
        f"duplicate_recipe_sources={duplicate_recipe_sources}, empty_recipes={len(empty_recipes)}",
        flush=True,
    )
    if next_batch:
        print("Next batch:", flush=True)
        for item in next_batch:
            print(f"  {item['source_id']} -> {Path(item['video_path']).name}", flush=True)
    else:
        print("Next batch: none", flush=True)
    print(f"Progress CSV: {output_path}", flush=True)
    print(f"Batch JSON: {batch_path}", flush=True)

    strict_ok = (
        len(videos) > 0
        and len(transcript_files) == len(videos)
        and len(recipe_files) == len(videos)
        and done == len(videos)
        and not orphan_transcripts
        and not orphan_recipes
        and not ambiguous_recipes
        and not duplicate_transcript_sources
        and not duplicate_recipe_sources
        and not empty_recipes
    )
    return 1 if args.strict and not strict_ok else 0


if __name__ == "__main__":
    raise SystemExit(main())

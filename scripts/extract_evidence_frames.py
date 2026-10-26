import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract precise evidence frames from a cooking video.")
    parser.add_argument("video")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--times", nargs="*", type=float, default=[])
    parser.add_argument("--start", type=float, default=None)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--contact-sheet", action="store_true")
    parser.add_argument("--columns", type=int, default=5)
    parser.add_argument("--rows", type=int, default=7)
    parser.add_argument("--ffmpeg", default=None)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def find_ffmpeg(explicit: str | None) -> Path | None:
    candidates = [explicit, os.environ.get("FFMPEG_PATH"), shutil.which("ffmpeg")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    roots = [Path.cwd(), *list(Path(__file__).resolve().parents)[:5]]
    for root in roots:
        tools_dir = root / "tools"
        if tools_dir.is_dir():
            matches = sorted(path for name in ("ffmpeg.exe", "ffmpeg") for path in tools_dir.glob(f"**/{name}") if path.is_file())
            if matches:
                return matches[0].resolve()
    return None


def run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "FFmpeg failed without an error message.")


def time_label(seconds: float) -> str:
    return f"{seconds:010.3f}".replace(".", "p")


def output_flag(overwrite: bool) -> str:
    return "-y" if overwrite else "-n"


def main() -> int:
    args = parse_args()
    video = Path(args.video).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if not video.is_file():
        print(f"Video does not exist: {video}", file=sys.stderr)
        return 2
    if not args.times and args.start is None:
        print("Provide --times or --start.", file=sys.stderr)
        return 2
    if args.duration <= 0 or args.fps <= 0 or args.width <= 0 or args.columns <= 0 or args.rows <= 0:
        print("Duration, fps, width, columns, and rows must be greater than zero.", file=sys.stderr)
        return 2
    if any(value < 0 for value in args.times) or (args.start is not None and args.start < 0):
        print("Timestamps cannot be negative.", file=sys.stderr)
        return 2
    if args.contact_sheet and args.start is None:
        print("--contact-sheet requires --start and a range.", file=sys.stderr)
        return 2

    ffmpeg = find_ffmpeg(args.ffmpeg)
    if ffmpeg is None:
        print("FFmpeg was not found. Pass --ffmpeg or set FFMPEG_PATH.", file=sys.stderr)
        return 2
    output_dir.mkdir(parents=True, exist_ok=True)
    produced = []

    try:
        for timestamp in sorted(set(args.times)):
            target = output_dir / f"sec-{time_label(timestamp)}.jpg"
            if target.exists() and not args.overwrite:
                produced.append(target)
                continue
            command = [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                output_flag(args.overwrite),
                "-ss",
                str(timestamp),
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(target),
            ]
            run(command)
            produced.append(target)

        if args.start is not None:
            range_pattern = output_dir / "range-%05d.jpg"
            existing_range = sorted(output_dir.glob("range-*.jpg"))
            if args.overwrite or not existing_range:
                command = [
                    str(ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    output_flag(args.overwrite),
                    "-ss",
                    str(args.start),
                    "-t",
                    str(args.duration),
                    "-i",
                    str(video),
                    "-vf",
                    f"fps={args.fps},scale={args.width}:-2",
                    "-q:v",
                    "3",
                    str(range_pattern),
                ]
                run(command)
                existing_range = sorted(output_dir.glob("range-*.jpg"))
            produced.extend(existing_range)

            if args.contact_sheet:
                contact_pattern = output_dir / "contact-%03d.jpg"
                existing_contacts = sorted(output_dir.glob("contact-*.jpg"))
                if args.overwrite or not existing_contacts:
                    sheet_count = max(1, math.ceil(args.duration * args.fps / (args.columns * args.rows)))
                    filter_value = (
                        f"fps={args.fps},scale={args.width}:-2,"
                        f"tile={args.columns}x{args.rows}:padding=2:margin=2"
                    )
                    command = [
                        str(ffmpeg),
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        output_flag(args.overwrite),
                        "-ss",
                        str(args.start),
                        "-t",
                        str(args.duration),
                        "-i",
                        str(video),
                        "-vf",
                        filter_value,
                        "-frames:v",
                        str(sheet_count),
                        "-q:v",
                        "2",
                        str(contact_pattern),
                    ]
                    run(command)
                    existing_contacts = sorted(output_dir.glob("contact-*.jpg"))
                produced.extend(existing_contacts)
    except RuntimeError as exc:
        print(f"Frame extraction failed: {exc}", file=sys.stderr)
        return 1

    unique_files = sorted({path.resolve() for path in produced}, key=lambda path: path.name.casefold())
    manifest = {
        "generated_at": utc_now(),
        "source_id": video.stem,
        "source": str(video),
        "requested_times": sorted(set(args.times)),
        "range": (
            {"start": args.start, "duration": args.duration, "fps": args.fps}
            if args.start is not None
            else None
        ),
        "files": [str(path) for path in unique_files],
    }
    manifest_path = output_dir / "evidence-manifest.json"
    temporary = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, manifest_path)
    print(f"Extracted {len(unique_files)} evidence file(s) to {output_dir}", flush=True)
    print(f"Evidence manifest: {manifest_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

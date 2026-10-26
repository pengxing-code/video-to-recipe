import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".mts", ".m2ts"}
DEFAULT_MODEL = Path(os.environ.get("FASTER_WHISPER_MODEL", "models/faster-whisper-medium"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare cooking videos with resumable local transcription and evidence frames."
    )
    parser.add_argument("input", help="A video file or a folder containing videos")
    parser.add_argument("--output-dir", default="video-to-recipe-output")
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--language", default="zh")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--frame-interval", type=float, default=10.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=30.0)
    parser.add_argument("--ffmpeg", default=None)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--skip-frames", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def write_json(path: Path, payload: dict) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


class Heartbeat:
    def __init__(self, path: Path, interval: float, **state) -> None:
        self.path = path
        self.interval = max(5.0, interval) if interval > 0 else 0.0
        self.state = dict(state)
        self.state["started_at"] = utc_now()
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None

    def update(self, **changes) -> None:
        with self.lock:
            self.state.update(changes)

    def emit(self) -> None:
        with self.lock:
            payload = dict(self.state)
        payload["updated_at"] = utc_now()
        payload["elapsed_seconds"] = round(time.monotonic() - self.started, 1)
        write_json(self.path, payload)
        detail = payload.get("detail") or payload.get("video") or "working"
        print(f"[heartbeat] {payload.get('phase', 'working')}: {detail} ({payload['elapsed_seconds']}s)", flush=True)

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            self.emit()

    def __enter__(self):
        self.emit()
        if self.interval:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.update(result="failed" if exc else "complete")
        self.emit()


def collect_videos(input_path: Path, recursive: bool) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(f"Unsupported video type: {input_path.suffix}")
        return [input_path.resolve()]
    if not input_path.is_dir():
        raise FileNotFoundError(f"Input does not exist: {input_path}")

    iterator = input_path.rglob("*") if recursive else input_path.glob("*")
    return sorted(
        (path.resolve() for path in iterator if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda path: str(path).casefold(),
    )


def find_ffmpeg(explicit: str | None) -> Path | None:
    candidates: list[str | Path | None] = [explicit, os.environ.get("FFMPEG_PATH"), shutil.which("ffmpeg")]
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


def format_time(seconds: float) -> str:
    hours, remainder = divmod(max(0.0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours >= 1:
        return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"
    return f"{int(minutes):02d}:{secs:06.3f}"


def extract_frames(ffmpeg: Path, video: Path, frame_dir: Path, interval: float, overwrite: bool) -> int:
    frame_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(frame_dir.glob("frame_*.jpg"))
    if existing and not overwrite:
        return len(existing)

    output_pattern = frame_dir / "frame_%05d.jpg"
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
        "-i",
        str(video),
        "-vf",
        f"fps=1/{interval},scale=960:-2",
        "-q:v",
        "3",
        str(output_pattern),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed for {video.name}: {result.stderr.strip()}")
    return len(list(frame_dir.glob("frame_*.jpg")))


def transcribe_video(model, video: Path, transcript_path: Path, metadata_path: Path, args, heartbeat) -> None:
    started = time.perf_counter()
    segments, info = model.transcribe(
        str(video),
        language=args.language,
        vad_filter=True,
        beam_size=args.beam_size,
    )

    rows = []
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = transcript_path.with_name(f".{transcript_path.name}.{os.getpid()}.partial")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            for segment in segments:
                text = segment.text.strip()
                row = {"start": segment.start, "end": segment.end, "text": text}
                rows.append(row)
                handle.write(f"[{format_time(segment.start)} --> {format_time(segment.end)}] {text}\n")
                handle.flush()
                heartbeat.update(media_seconds=round(segment.end, 3), segments=len(rows))
        os.replace(temporary, transcript_path)
    finally:
        if temporary.exists():
            temporary.unlink()

    metadata = {
        "source_id": video.stem,
        "source": str(video),
        "duration_seconds": info.duration,
        "language": info.language,
        "language_probability": info.language_probability,
        "model": args.model,
        "device": args.device,
        "compute_type": args.compute_type,
        "processing_seconds": round(time.perf_counter() - started, 3),
        "segments": rows,
    }
    write_json(metadata_path, metadata)


def existing_record_map(manifest_path: Path) -> dict[str, dict]:
    if not manifest_path.is_file():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records = {}
    for record in payload.get("videos", []):
        source = record.get("source")
        if source:
            records[str(Path(source).resolve()).casefold()] = record
    return records


def main() -> int:
    args = parse_args()
    if args.frame_interval <= 0:
        print("--frame-interval must be greater than zero.", file=sys.stderr)
        return 2

    input_path = Path(args.input).expanduser()
    output_dir = Path(args.output_dir).expanduser().resolve()
    model_path = Path(args.model).expanduser()
    manifest_path = output_dir / "manifest.json"
    heartbeat_path = output_dir / "preparation-heartbeat.json"

    try:
        videos = collect_videos(input_path, args.recursive)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not videos:
        print("No supported videos found.", file=sys.stderr)
        return 2
    sources: dict[str, Path] = {}
    for video in videos:
        key = video.stem.casefold()
        if key in sources:
            print(f"Duplicate source ID '{video.stem}': {sources[key]} and {video}. Rename one video before preparation.", file=sys.stderr)
            return 2
        sources[key] = video

    output_dir.mkdir(parents=True, exist_ok=True)
    previous = existing_record_map(manifest_path)
    records = []
    for video in videos:
        transcript_path = output_dir / "transcripts" / f"{video.stem}.transcript.txt"
        metadata_path = output_dir / "metadata" / f"{video.stem}.json"
        frame_dir = output_dir / "frames" / video.stem
        old = previous.get(str(video).casefold(), {})
        records.append(
            {
                "source_id": video.stem,
                "source": str(video),
                "transcript": str(transcript_path),
                "metadata": str(metadata_path),
                "frames": str(frame_dir),
                "status": old.get("status", "pending"),
                "error": None,
            }
        )

    manifest = {
        "schema_version": 2,
        "input": str(input_path.resolve()),
        "output_dir": str(output_dir),
        "model": str(model_path),
        "updated_at": utc_now(),
        "videos": records,
    }

    def save_manifest() -> None:
        manifest["updated_at"] = utc_now()
        write_json(manifest_path, manifest)

    save_manifest()
    needs_transcription = any(
        args.overwrite
        or not Path(record["transcript"]).is_file()
        or not Path(record["metadata"]).is_file()
        for record in records
    )

    model = None
    if needs_transcription:
        if not model_path.exists():
            print(f"Model not found: {model_path}", file=sys.stderr)
            return 2
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            print(f"faster-whisper is not installed in this Python: {exc}", file=sys.stderr)
            return 2
        print(f"Loading model once for {len(videos)} video(s): {model_path}", flush=True)
        with Heartbeat(
            heartbeat_path,
            args.heartbeat_seconds,
            phase="load-model",
            detail=str(model_path),
            total=len(videos),
        ):
            model = WhisperModel(str(model_path), device=args.device, compute_type=args.compute_type)
    else:
        print("All transcripts and metadata are reusable; model loading skipped.", flush=True)

    ffmpeg = None if args.skip_frames else find_ffmpeg(args.ffmpeg)
    if not args.skip_frames and ffmpeg is None:
        print("Warning: FFmpeg was not found; transcription will continue without key frames.", file=sys.stderr)

    failures = 0
    for index, (video, record) in enumerate(zip(videos, records), start=1):
        transcript_path = Path(record["transcript"])
        metadata_path = Path(record["metadata"])
        frame_dir = Path(record["frames"])
        print(f"[{index}/{len(videos)}] {video.name}", flush=True)
        record.update(status="processing", started_at=utc_now(), error=None)
        save_manifest()

        try:
            with Heartbeat(
                heartbeat_path,
                args.heartbeat_seconds,
                phase="prepare-video",
                video=video.name,
                detail=f"{index}/{len(videos)} {video.name}",
                index=index,
                total=len(videos),
            ) as heartbeat:
                if args.overwrite or not transcript_path.is_file() or not metadata_path.is_file():
                    if model is None:
                        raise RuntimeError("Transcription is required but the model was not loaded.")
                    heartbeat.update(stage="transcribing")
                    transcribe_video(model, video, transcript_path, metadata_path, args, heartbeat)
                    record["transcript_status"] = "created"
                else:
                    print(f"  Reusing transcript: {transcript_path}", flush=True)
                    record["transcript_status"] = "reused"

                frame_count = len(list(frame_dir.glob("frame_*.jpg")))
                if ffmpeg is not None:
                    heartbeat.update(stage="extracting-frames")
                    frame_count = extract_frames(ffmpeg, video, frame_dir, args.frame_interval, args.overwrite)
                    record["frame_status"] = "ready"
                elif args.skip_frames:
                    record["frame_status"] = "skipped"
                else:
                    record["frame_status"] = "ffmpeg-unavailable"

                record.update(
                    status="complete",
                    frame_count=frame_count,
                    completed_at=utc_now(),
                )
        except Exception as exc:
            failures += 1
            record.update(status="failed", error=str(exc), completed_at=utc_now())
            print(f"  FAILED: {exc}", file=sys.stderr, flush=True)
            save_manifest()
            if args.fail_fast:
                break
            continue

        save_manifest()

    complete = sum(record.get("status") == "complete" for record in records)
    pending = len(records) - complete - failures
    manifest["summary"] = {"total": len(records), "complete": complete, "failed": failures, "pending": pending}
    save_manifest()
    print(
        f"Preparation finished: total={len(records)}, complete={complete}, failed={failures}, pending={pending}. "
        f"Manifest: {manifest_path}",
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

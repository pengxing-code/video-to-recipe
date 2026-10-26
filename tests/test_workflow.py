import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


prepare = module("prepare_videos")
reconcile = module("reconcile_progress")
evidence = module("extract_evidence_frames")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.videos = self.root / "videos"
        self.transcripts = self.root / "work" / "transcripts"
        self.recipes = self.root / "recipes"
        for directory in (self.videos, self.transcripts, self.recipes):
            directory.mkdir(parents=True)
        self.output = self.root / "progress.csv"

    def run_script(self, script, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *map(str, args)], capture_output=True, encoding="utf-8", errors="replace", timeout=15)

    def audit(self, *extra):
        result = self.run_script("reconcile_progress.py", self.videos, "--transcripts", self.transcripts, "--recipes", self.recipes, "--output", self.output, *extra)
        report_path = self.output.with_suffix(".next-batch.json")
        return result, json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None

    def source(self, name="香煎豆腐", recipe=True):
        video = self.videos / f"{name}.mp4"
        video.touch()
        (self.transcripts / f"{name}.transcript.txt").write_text("[00:00 --> 00:03] 放入豆腐。", encoding="utf-8")
        if recipe:
            (self.recipes / f"{name}.md").write_text(f"# 菜名\n\n## 来源\n- 来源编号：`{name}`\n- 原视频：`{video}`\n\n## 食材\n豆腐，视频未明确用量\n## 做法\n放入豆腐。", encoding="utf-8")
        return video

    def test_all_cli_help_without_model_or_ffmpeg(self):
        for script in ("prepare_videos.py", "extract_evidence_frames.py", "reconcile_progress.py"):
            result = self.run_script(script, "--help")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage:", result.stdout)

    def test_complete_one_to_one_passes_strict(self):
        self.source()
        result, report = self.audit("--strict")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["done_count"], 1)
        self.assertEqual(report["pending_count"], 0)
        self.assertEqual(report["next_batch"], [])

    def test_next_batch_is_bounded_and_never_includes_completed_sources(self):
        self.source("已完成")
        for i in range(6):
            self.source(f"来源 {i}", recipe=False)
        result, report = self.audit("--batch-size", "3")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["done_count"], 1)
        self.assertEqual(report["pending_count"], 6)
        self.assertEqual(len(report["next_batch"]), 3)
        self.assertNotIn("已完成", [item["source_id"] for item in report["next_batch"]])
        result, _ = self.audit("--strict")
        self.assertEqual(result.returncode, 1)

    def test_duplicate_and_orphan_recipes_fail_strict(self):
        self.source()
        (self.recipes / "重复.md").write_text("# 菜谱\n来源编号：`香煎豆腐`", encoding="utf-8")
        (self.recipes / "孤立.md").write_text("# 无来源", encoding="utf-8")
        result, report = self.audit("--strict")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["duplicate_recipe_source_count"], 1)
        self.assertEqual(report["orphan_recipe_count"], 1)

    def test_empty_recipe_never_counts_done(self):
        self.source()
        (self.recipes / "香煎豆腐.md").write_text("", encoding="utf-8")
        result, report = self.audit("--strict")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["empty_recipe_count"], 1)
        self.assertEqual(report["done_count"], 0)

    def test_legacy_alias_is_used_only_if_unique(self):
        paths = [Path("recipe A.mp4"), Path("recipe_A.mp4")]
        exact, aliases = reconcile.build_source_lookup(paths)
        self.assertIsNone(reconcile.resolve_source("recipe__A", exact, aliases))
        exact, aliases = reconcile.build_source_lookup(paths[:1])
        self.assertEqual(reconcile.resolve_source("recipe_A", exact, aliases), "recipe A")

    def test_duplicate_stems_stop_before_preparation_can_overwrite(self):
        (self.videos / "same.mp4").touch()
        nested = self.videos / "nested"
        nested.mkdir()
        (nested / "same.mov").touch()
        target = self.root / "prepared"
        result = self.run_script("prepare_videos.py", self.videos, "--recursive", "--output-dir", target)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Duplicate source ID", result.stderr)
        self.assertFalse(target.exists())
        result, report = self.audit("--recursive", "--strict")
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(report)

    def test_ffmpeg_explicit_parameter_and_environment_are_respected(self):
        executable = self.root / "ffmpeg-placeholder"
        executable.touch()
        for helper in (prepare, evidence):
            self.assertEqual(helper.find_ffmpeg(str(executable)), executable.resolve())
            with patch.dict(os.environ, {"FFMPEG_PATH": str(executable)}):
                self.assertEqual(helper.find_ffmpeg(None), executable.resolve())

    def test_resume_skips_model_loading_with_complete_transcript_and_metadata(self):
        video = self.source()
        output = self.root / "prepared"
        (output / "transcripts").mkdir(parents=True)
        (output / "metadata").mkdir()
        (output / "transcripts" / f"{video.stem}.transcript.txt").write_text("已有转写", encoding="utf-8")
        (output / "metadata" / f"{video.stem}.json").write_text("{}", encoding="utf-8")
        result = self.run_script("prepare_videos.py", video, "--output-dir", output, "--model", self.root / "nonexistent-model", "--skip-frames", "--heartbeat-seconds", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("model loading skipped", result.stdout)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["videos"][0]["transcript_status"], "reused")
        self.assertEqual(manifest["videos"][0]["frame_status"], "skipped")

    def test_invalid_evidence_time_exits_before_ffmpeg(self):
        video = self.source()
        target = self.root / "evidence"
        result = self.run_script("extract_evidence_frames.py", video, "--output-dir", target, "--times", "-1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("negative", result.stderr)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()

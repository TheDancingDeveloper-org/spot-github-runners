import base64
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.runner_assets import (CLEAN_TARGET, DOCKER_WRAPPER, JOB_COMPLETED,
                               RUNNER_DOCKERFILE, RUNNER_ENTRYPOINT, STARTUP_SCRIPT)
from lib.runner_manager import args_parser, render_startup


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("clean_target", ROOT / "runner/clean_target.py")
clean_target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clean_target)


class JobCacheCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name) / "work"
        self.workspace = self.work / "oxidex" / "oxidex"
        self.workspace.mkdir(parents=True)
        self.env = {"OXIDEX_CLEAN_TARGET": "1",
                    "GITHUB_REPOSITORY": "swack-tools/oxidex",
                    "GITHUB_WORKSPACE": str(self.workspace)}

    def test_only_current_runner_default_target_is_removed(self):
        target = self.workspace / "target"
        target.mkdir()
        (target / "build-output").write_text("regenerable")
        (self.workspace / "source.rs").write_text("preserve")
        other = self.work / "other-runner" / "oxidex" / "oxidex" / "target"
        other.mkdir(parents=True)
        (other / "build-output").write_text("preserve")
        shared = self.work / "_tool"
        shared.mkdir()
        (shared / "cache").write_text("preserve")

        self.assertTrue(clean_target.cleanup(self.work, self.env))
        self.assertFalse(target.exists())
        self.assertTrue((self.workspace / "source.rs").exists())
        self.assertTrue((other / "build-output").exists())
        self.assertTrue((shared / "cache").exists())

    def test_opt_in_repository_and_workspace_are_required(self):
        target = self.workspace / "target"
        target.mkdir()
        for key, value in (("OXIDEX_CLEAN_TARGET", "0"),
                           ("GITHUB_REPOSITORY", "another/repo"),
                           ("GITHUB_WORKSPACE", str(self.work))):
            with self.subTest(key=key):
                env = self.env | {key: value}
                self.assertFalse(clean_target.cleanup(self.work, env))
                self.assertTrue(target.exists())

    def test_target_symlink_is_refused_without_following_it(self):
        external = Path(self.temp.name) / "external"
        external.mkdir()
        (external / "keep").write_text("preserve")
        (self.workspace / "target").symlink_to(external, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            clean_target.cleanup(self.work, self.env)
        self.assertTrue((external / "keep").exists())

    def test_workspace_symlink_is_refused(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        (other / "target").mkdir()
        self.workspace.rmdir()
        self.workspace.symlink_to(other, target_is_directory=True)
        with self.assertRaises(OSError):
            clean_target.cleanup(self.work, self.env)
        self.assertTrue((other / "target").exists())

    def test_nested_symlink_is_unlinked_without_following(self):
        external = Path(self.temp.name) / "external"
        external.mkdir()
        (external / "keep").write_text("preserve")
        target = self.workspace / "target"
        target.mkdir()
        (target / "external").symlink_to(external, target_is_directory=True)
        self.assertTrue(clean_target.cleanup(self.work, self.env))
        self.assertTrue((external / "keep").exists())

    def test_removal_failure_is_reported_and_does_not_touch_other_paths(self):
        target = self.workspace / "target"
        target.mkdir()
        with patch.object(clean_target.shutil, "rmtree", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                clean_target.cleanup(self.work, self.env)
        self.assertTrue(target.exists())

    def test_missing_target_is_ok_and_assets_match_templates(self):
        self.assertFalse(clean_target.cleanup(self.work, self.env))
        for content, path in ((CLEAN_TARGET, "runner/clean_target.py"),
                              (DOCKER_WRAPPER, "runner/docker-wrapper.py"),
                              (JOB_COMPLETED, "runner/job-completed.sh"),
                              (RUNNER_DOCKERFILE, "runner/Dockerfile"),
                              (RUNNER_ENTRYPOINT, "runner/entrypoint.sh"),
                              (STARTUP_SCRIPT, "cloud-init/startup.sh")):
            with self.subTest(path=path):
                self.assertEqual(content, (ROOT / path).read_text())
        self.assertIn("{{CLEAN_TARGET_B64}}", STARTUP_SCRIPT)
        self.assertIn("{{JOB_COMPLETED_B64}}", STARTUP_SCRIPT)

    def test_rendered_startup_carries_both_hook_files(self):
        args = args_parser().parse_args(["--max-cost", ".5"])
        rendered = render_startup(args, "token", "group", "deployment")
        for content in (CLEAN_TARGET, DOCKER_WRAPPER, JOB_COMPLETED):
            self.assertIn(base64.b64encode(content.encode()).decode(), rendered)
        self.assertNotIn("{{", rendered)


if __name__ == "__main__":
    unittest.main()

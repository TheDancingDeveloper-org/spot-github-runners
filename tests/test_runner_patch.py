"""Verify repository runner patch manifests, config-repo fetching, ISA filtering, and live hot-patching."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib import runner_manager as manager
from lib.compute_api import Offer
from lib.launch_candidates import LaunchInventory, candidate_key, launchable_candidates
from lib.machine_profiles import platform_preferences
from lib.runner_assets import RUNNER_DOCKERFILE
from lib.runner_fleet import GceVm
from lib.runner_patch import (
    RunnerPatch,
    classify_patch_diff,
    fetch_patch_from_config_repo,
    parse_manifest_text,
    render_dockerfile,
    render_host_patch_script,
    render_live_hot_patch_script,
    resolve_repo_default_branch,
    validate_patch_dict,
)
from pricing_fixtures import storage_prices_for
from lib import spot_runner_autoscaler as controller


YAML_NNTMUX_MANIFEST = """
# Repository runner customization for xbmc4lyfe/newznab-tmux
schema: spot-runner-patch-v1
repository: xbmc4lyfe/newznab-tmux

hardware:
  arch: x86_64
  isa:
    - avx512-vbmi2
  extra_labels:
    - avx512-vbmi2
    - nntmux-simd
  min_vcpu: 16
  min_ram_gib: 64
  qualify_command: |
    grep -q avx512_vbmi2 /proc/cpuinfo

host:
  apt_packages:
    - linux-perf
  sysctl:
    net.core.rmem_max: 134217728
    net.core.wmem_max: 134217728
    vm.max_map_count: 262144
  boot_mkdirs:
    - /mnt/runner-data/nntmux-cache
  files:
    - path: /etc/security/limits.d/99-nntmux.conf
      mode: "0644"
      owner: root:root
      content: |
        * soft nofile 1048576
        * hard nofile 1048576
  boot_script: |
    chown 1001:1001 /mnt/runner-data/nntmux-cache

container:
  apt_packages:
    - php-cli
    - php-ffi
    - composer
  sysctl:
    net.ipv4.tcp_rmem: "4096 87380 67108864"
    net.ipv4.tcp_wmem: "4096 65536 67108864"
  mounts:
    - source: /mnt/runner-data/nntmux-cache
      target: /work/.nntmux-cache
      read_only: false
  files:
    - path: /etc/php-ffi-runner.ini
      mode: "0644"
      content: |
        ffi.enable=true
        memory_limit=1G
  dockerfile_run: |
    php -v && composer --version
"""


class RunnerPatchTests(unittest.TestCase):
    def setUp(self):
        self.real_subprocess_run = subprocess.run
        blocker = patch("subprocess.run", side_effect=AssertionError("Unexpected external command"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_yaml_and_json_manifests_parse_to_identical_patch(self):
        parsed_yaml = validate_patch_dict(parse_manifest_text(YAML_NNTMUX_MANIFEST))
        json_text = json.dumps(parsed_yaml.to_canonical_dict())
        parsed_json = validate_patch_dict(parse_manifest_text(json_text))
        self.assertEqual(parsed_yaml, parsed_json)
        self.assertEqual(parsed_yaml.patch_sha256, parsed_json.patch_sha256)
        self.assertEqual(parsed_yaml.hardware.isa, ("avx512-vbmi2",))
        self.assertEqual(parsed_yaml.container.apt_packages, ("php-cli", "php-ffi", "composer"))
        self.assertEqual(parsed_yaml.host.sysctl["vm.max_map_count"], "262144")
        self.assertEqual(parsed_yaml.container.sysctl["net.ipv4.tcp_rmem"], "4096 87380 67108864")

    def test_dynamic_default_branch_resolution_handles_custom_branches(self):
        for repo, branch, sha in (
            ("xbmc4lyfe/newznab-tmux", "master", "a" * 40),
            ("swack-tools/oxidex", "main", "b" * 40),
            ("Appz4Fun/nzbget", "dupe-article-fallback", "c" * 40),
        ):
            calls = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                endpoint = cmd[2]
                if endpoint == f"repos/{repo}":
                    return SimpleNamespace(returncode=0, stdout=json.dumps({"default_branch": branch}), stderr="")
                if endpoint == f"repos/{repo}/commits/{branch}":
                    return SimpleNamespace(returncode=0, stdout=json.dumps({"sha": sha}), stderr="")
                raise AssertionError(f"Unexpected endpoint {endpoint}")

            with patch("lib.runner_patch.subprocess.run", side_effect=fake_run):
                resolved_branch, resolved_sha = resolve_repo_default_branch(repo)
            self.assertEqual((resolved_branch, resolved_sha), (branch, sha))

    def test_fetch_patch_from_config_repo_merges_defaults_and_repo_manifest(self):
        sha = "d" * 40
        defaults_yaml = """
schema: spot-runner-patch-v1
host:
  apt_packages:
    - htop
  sysctl:
    vm.swappiness: 10
container:
  apt_packages:
    - strace
"""
        files = {
            f"repos/swack-tools/spot-runners-config/contents/defaults.yaml?ref={sha}": defaults_yaml,
            f"repos/swack-tools/spot-runners-config/contents/repos/xbmc4lyfe/newznab-tmux.yaml?ref={sha}": YAML_NNTMUX_MANIFEST,
        }

        def fake_run(cmd, **kwargs):
            endpoint = cmd[2]
            if endpoint == "repos/swack-tools/spot-runners-config":
                return SimpleNamespace(returncode=0, stdout=json.dumps({"default_branch": "main"}), stderr="")
            if endpoint == "repos/swack-tools/spot-runners-config/commits/main":
                return SimpleNamespace(returncode=0, stdout=json.dumps({"sha": sha}), stderr="")
            if endpoint in files:
                encoded = base64.b64encode(files[endpoint].encode()).decode("ascii")
                return SimpleNamespace(returncode=0, stdout=json.dumps({"encoding": "base64", "content": encoded}), stderr="")
            raise AssertionError(f"Unexpected endpoint {endpoint}")

        with patch("lib.runner_patch.subprocess.run", side_effect=fake_run):
            patch_obj = fetch_patch_from_config_repo("xbmc4lyfe/newznab-tmux", org="swack-tools")

        self.assertEqual(patch_obj.source_commit, sha)
        self.assertEqual(patch_obj.host.apt_packages, ("htop", "linux-perf"))
        self.assertEqual(patch_obj.host.sysctl["vm.swappiness"], "10")
        self.assertEqual(patch_obj.host.sysctl["vm.max_map_count"], "262144")
        self.assertEqual(patch_obj.container.apt_packages, ("strace", "php-cli", "php-ffi", "composer"))

    def test_validation_rejects_forbidden_mounts_and_mismatched_isa(self):
        with self.assertRaisesRegex(ValueError, "forbidden"):
            validate_patch_dict({
                "schema": "spot-runner-patch-v1",
                "container": {
                    "mounts": [{"source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}]
                },
            })
        with self.assertRaisesRegex(ValueError, "requires arch 'x86_64'"):
            validate_patch_dict({
                "schema": "spot-runner-patch-v1",
                "hardware": {"arch": "arm64", "isa": ["avx512-vbmi2"]},
            })
        with self.assertRaisesRegex(ValueError, "requires arch 'arm64'"):
            validate_patch_dict({
                "schema": "spot-runner-patch-v1",
                "hardware": {"arch": "x86_64", "isa": ["neon"]},
            })

    def test_isa_filtering_excludes_milan_and_cascade_lake_for_avx512_vbmi2(self):
        # nntmux kernel 3 requires avx512-vbmi2:
        # Allowed: AMD Turin (c4d/n4d), Intel Granite/Emerald Rapids (c4/n4), Intel Ice Lake (n2).
        # Rejected: AMD Milan (c2d/n2d) and Intel Cascade Lake (n2).
        self.assertEqual(
            [p for _, p in platform_preferences("c2d-standard-32", required_isa=("avx512-vbmi2",))],
            [],
        )
        self.assertEqual(
            [p for _, p in platform_preferences("n2-standard-32", required_isa=("avx512-vbmi2",))],
            ["Intel Ice Lake"],
        )
        self.assertEqual(
            [p for _, p in platform_preferences("c4-standard-32", "asia-south2-a", required_isa=("avx10.1",))],
            ["Intel Granite Rapids"],
        )
        self.assertEqual(
            [p for _, p in platform_preferences("c4-standard-32", "us-central1-a", required_isa=("avx10.1",))],
            [],
        )

        # Verify launchable_candidates filters out Cascade Lake when avx512-vbmi2 is required
        n2 = Offer("region", "n2-standard-32", 32, 128, 0.05, zones=("region-a",))
        ranked = controller.eligible_offers([n2], controller.Decimal("10"), storage_rates=storage_prices_for([n2]))
        inv = LaunchInventory(
            platforms_by_zone={"region-a": ["Intel Ice Lake", "Intel Cascade Lake"]},
            disk_types_by_zone={"region-a": {"pd-standard", "pd-balanced", "pd-ssd"}},
            region_quotas={"region": {"CPUS": (200.0, 0.0), "N2_CPUS": (200.0, 0.0), "INSTANCES": (10.0, 0.0)}},
            global_quotas={"CPUS_ALL_REGIONS": (200.0, 0.0)},
        )
        selected, skipped = launchable_candidates(ranked, inv, 100, required_isa=("avx512-vbmi2",))
        self.assertEqual([candidate_key(c) for c in selected], ["region-a/n2-standard-32/Intel Ice Lake"])
        self.assertTrue(any("Intel Cascade Lake" in s for s in skipped))

    def test_arm64_provisioning_selects_axion_and_debian_arm64_image(self):
        patch_obj = validate_patch_dict({
            "schema": "spot-runner-patch-v1",
            "hardware": {"arch": "arm64", "isa": ["neon", "sve"], "extra_labels": ["arm64-simd"]},
        })
        args = manager.args_parser().parse_args([
            "--max-cost", "0.2",
            "--instance-type", "c4a-standard-32",
            "--cpu", "32",
            "--ram", "128",
        ])
        args.runner_patch = patch_obj
        manager.select_sizing(args)
        self.assertEqual(args.min_cpu_platform, "Google Axion")
        self.assertEqual(args.boot_disk_type, "hyperdisk-balanced")

        calls = []

        def cloud(command, **kwargs):
            calls.append(command)
            return "[]" if command[:3] == ["compute", "disks", "list"] else ""

        with patch("lib.runner_manager.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="")), \
             patch("lib.runner_manager.gcloud", side_effect=cloud):
            manager.apply_startup(args, "us-central1-a", "startup-script-body")

        create_vm = next(c for c in calls if c[:3] == ["compute", "instances", "create"])
        self.assertIn("--image-family=debian-13-arm64", create_vm)
        self.assertIn("--min-cpu-platform=Google Axion", create_vm)

    def test_rendered_startup_and_dockerfile_contain_patch_and_pass_bash_syntax_check(self):
        patch_obj = validate_patch_dict(parse_manifest_text(YAML_NNTMUX_MANIFEST))
        dockerfile = render_dockerfile(RUNNER_DOCKERFILE, patch_obj)
        self.assertIn("apt-get install -y --no-install-recommends php-cli php-ffi composer", dockerfile)
        self.assertIn("/etc/php-ffi-runner.ini", dockerfile)
        self.assertLess(dockerfile.index("php-cli"), dockerfile.index("USER runner\nENV CARGO_HOME=/work/.cargo"))

        args = manager.args_parser().parse_args([
            "--max-cost", "0.3",
            "--instance-type", "c4d-standard-32",
            "--cpu", "32",
            "--ram", "128",
        ])
        args.runner_patch = patch_obj
        manager.select_sizing(args)
        startup = manager.render_startup(args, "token-123", "group-1", "deploy-1")
        self.assertIn("RUNNER_LABELS='oxidex-spot-7x27,c4d-standard-32,asia-south2,avx512-vbmi2,nntmux-simd'", startup)
        self.assertIn("/opt/gha-runners/repo-host-patch.sh", startup)
        self.assertIn("'--sysctl=net.ipv4.tcp_rmem=4096 87380 67108864'", startup)
        self.assertIn("-v /mnt/runner-data/nntmux-cache:/work/.nntmux-cache", startup)
        self.assertNotIn("{{", startup)

        host_patch = render_host_patch_script(patch_obj)
        self.assertIn("grep -Eiwq avx512_vbmi2 /proc/cpuinfo", host_patch)
        self.assertIn("/etc/sysctl.d/99-spot-runner-patch.conf", host_patch)
        self.assertIn("/var/lib/gha-runner-patch/state.json", host_patch)

        for script_text in (startup, host_patch):
            with tempfile.NamedTemporaryFile("w", suffix=".sh") as f:
                f.write(script_text)
                f.flush()
                self.real_subprocess_run(["bash", "-n", f.name], check=True)

    def test_classify_and_apply_live_hot_patch(self):
        initial = validate_patch_dict({
            "schema": "spot-runner-patch-v1",
            "hardware": {"arch": "x86_64", "isa": ["avx512-vbmi2"]},
            "host": {"sysctl": {"vm.max_map_count": 262144}},
            "container": {"apt_packages": ["php-cli"]},
        })
        updated = validate_patch_dict({
            "schema": "spot-runner-patch-v1",
            "hardware": {"arch": "x86_64", "isa": ["avx512-vbmi2"], "extra_labels": ["hot-patched"]},
            "host": {"sysctl": {"vm.max_map_count": 524288}},
            "container": {
                "apt_packages": ["php-cli", "jq"],
                "sysctl": {"net.ipv4.tcp_rmem": "4096 87380 67108864"},
            },
        })
        self.assertEqual(classify_patch_diff(initial.to_canonical_dict(), initial), "noop")
        self.assertEqual(classify_patch_diff(initial.to_canonical_dict(), updated), "hot_patch")

        mount_change = validate_patch_dict({
            "schema": "spot-runner-patch-v1",
            "hardware": {"arch": "x86_64", "isa": ["avx512-vbmi2"]},
            "container": {"mounts": [{"source": "/mnt/runner-data/a", "target": "/work/a"}]},
        })
        self.assertEqual(classify_patch_diff(initial.to_canonical_dict(), mount_change), "recreate_containers")

        isa_change = validate_patch_dict({
            "schema": "spot-runner-patch-v1",
            "hardware": {"arch": "x86_64", "isa": ["avx512-vbmi2", "amx"]},
        })
        self.assertEqual(classify_patch_diff(initial.to_canonical_dict(), isa_change), "replace_vm")

        args = manager.args_parser().parse_args([
            "--max-cost", "0.3",
            "--instance-type", "c4d-standard-32",
            "--cpu", "32",
            "--ram", "128",
            "--patch-live",
        ])
        args.runner_patch = updated
        manager.select_sizing(args)
        vm = GceVm(args.instance_name, "asia-south2-a", "RUNNING", "c4d-standard-32", True,
                   instance_id="123", cpu_platform="AMD Turin")

        ssh_commands = []

        def fake_gcloud(cmd, **kwargs):
            ssh_commands.append(cmd)
            if "cat /var/lib/gha-runner-patch/state.json" in cmd[-1]:
                return json.dumps({"manifest": initial.to_canonical_dict()})
            return f"LIVE_PATCH_APPLIED:{updated.patch_sha256}\n"

        with patch("lib.runner_manager.list_gcloud_vms", return_value=[vm]), \
             patch("lib.runner_manager.gcloud", side_effect=fake_gcloud), \
             patch("lib.runner_manager.sync_runner_labels") as sync_labels:
            result = manager.apply_live_patch(args, "asia-south2-a")

        self.assertEqual(result, "hot_patch")
        self.assertEqual(len(ssh_commands), 2)
        sync_labels.assert_called_once_with(args)

        live_script = render_live_hot_patch_script(updated, base_dockerfile=RUNNER_DOCKERFILE, runner_prefix="oxidex-asia-")
        self.assertIn('docker exec -i -u root "$cid" bash -euo pipefail', live_script)
        self.assertIn('nsenter -t "$pid" -n sysctl -w', live_script)
        self.assertIn("docker build --pull -t oxidex-self-hosted-runner:2.337.0 /opt/gha-runners/build", live_script)

    def test_path_normalization_and_arm64_storage_and_yaml_chomping(self) -> None:
        from decimal import Decimal
        from lib.storage_selection import cheapest_storage

        for bad_mount in ("/var/run", "/run", "/var", "/run/gha-admission", "/var/run/./docker.sock", "/var//run/docker.sock"):
            with self.assertRaises(ValueError):
                validate_patch_dict({
                    "schema": "spot-runner-patch-v1",
                    "container": {"mounts": [{"source": bad_mount, "target": "/work/mnt"}]},
                })
        for bad_path in ("/etc/foo\nRUN id", "/tmp//bar", "/tmp/./bar", "/tmp/bar/"):
            with self.assertRaises(ValueError):
                validate_patch_dict({
                    "schema": "spot-runner-patch-v1",
                    "container": {"files": [{"path": bad_path, "content": "x"}]},
                })

        parsed = parse_manifest_text(YAML_NNTMUX_MANIFEST)
        self.assertEqual(parsed["host"]["boot_script"], "chown 1001:1001 /mnt/runner-data/nntmux-cache\n")
        self.assertEqual(parsed["container"]["dockerfile_run"], "php -v && composer --version\n")

        rates = {
            "hyperdisk-balanced:capacity": Decimal("0.08"),
            "hyperdisk-balanced:iops": Decimal("0.005"),
            "hyperdisk-balanced:throughput": Decimal("0.04"),
        }
        boot, data, _ = cheapest_storage("c4a-standard-32", 200, rates, arch="arm64")
        self.assertEqual((boot, data), ("hyperdisk-balanced", "hyperdisk-balanced"))


if __name__ == "__main__":
    unittest.main()


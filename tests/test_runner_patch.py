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
from lib.machine_profiles import platform_preferences, machine_profile, configure_storage
from lib.runner_assets import RUNNER_DOCKERFILE
from lib.runner_fleet import GceVm
import urllib.error
from lib import runner_patch as runner_patch_mod
from lib.runner_patch import (
    RunnerPatch,
    classify_patch_diff,
    fetch_patch_from_config_repo,
    list_config_repo_targets,
    parse_manifest_text,
    render_dockerfile,
    render_host_patch_script,
    render_live_hot_patch_script,
    resolve_repo_default_branch,
    validate_patch_dict,
    SCHEMA_VERSION,
)
try:
    from pricing_fixtures import storage_prices_for
except ModuleNotFoundError:
    from tests.pricing_fixtures import storage_prices_for
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
        gh_present = patch("lib.runner_patch.shutil.which", return_value="/usr/bin/gh")
        gh_present.start()
        self.addCleanup(gh_present.stop)

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
        self.assertEqual(patch_obj.source_branch, "main")
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

    def test_schema_and_field_validations(self) -> None:
        # Schema version mismatch
        with self.assertRaisesRegex(ValueError, "Unsupported runner patch schema"):
            validate_patch_dict({"schema": "spot-runner-patch-v2"})

        # Booleans rejected for min_vcpu and min_ram_gib
        with self.assertRaisesRegex(ValueError, "hardware.min_vcpu must be a positive integer"):
            validate_patch_dict({"hardware": {"min_vcpu": True}})
        with self.assertRaisesRegex(ValueError, "hardware.min_ram_gib must be a positive integer"):
            validate_patch_dict({"hardware": {"min_ram_gib": False}})

        # Directory traversal in file paths rejected
        with self.assertRaisesRegex(ValueError, "Invalid absolute file path"):
            validate_patch_dict({"host": {"files": [{"path": "/etc/../passwd", "content": "bad"}]}})

        # Duplicate mount targets rejected
        with self.assertRaisesRegex(ValueError, "Duplicate mount target"):
            validate_patch_dict({
                "container": {
                    "mounts": [
                        {"source": "/mnt/runner-data/a", "target": "/work/target"},
                        {"source": "/mnt/runner-data/b", "target": "/work/target"},
                    ]
                }
            })

        # Duplicate file paths rejected
        with self.assertRaisesRegex(ValueError, "Duplicate file path"):
            validate_patch_dict({
                "host": {
                    "files": [
                        {"path": "/opt/file.txt", "content": "1"},
                        {"path": "/opt/file.txt", "content": "2"},
                    ]
                }
            })

        # Sysctl newline injection rejected
        with self.assertRaisesRegex(ValueError, "Invalid sysctl value"):
            validate_patch_dict({"host": {"sysctl": {"net.core.rmem_max": "134217728\nkernel.panic=1"}}})
        with self.assertRaisesRegex(ValueError, "Invalid sysctl value"):
            validate_patch_dict({"host": {"sysctl": {"net.core.rmem_max": "134217728\rkernel.panic=1"}}})

        # Runner label regex validation
        with self.assertRaisesRegex(ValueError, "Invalid runner label"):
            validate_patch_dict({"hardware": {"extra_labels": ["invalid label with spaces"]}})
        with self.assertRaisesRegex(ValueError, "Invalid runner label"):
            validate_patch_dict({"hardware": {"extra_labels": ["-leading-dash"]}})

    def test_canonical_dict_and_sha256_stability(self) -> None:
        p1 = validate_patch_dict({"hardware": {"arch": "x86_64"}}, source_commit="sha1", source_branch="branch1")
        p2 = validate_patch_dict({"hardware": {"arch": "x86_64"}}, source_commit="sha2", source_branch="branch2")
        self.assertEqual(p1.patch_sha256, p2.patch_sha256)
        self.assertNotIn("source_commit", p1.to_canonical_dict())
        self.assertNotIn("source_branch", p1.to_canonical_dict())

    def test_toml_manifest_parsing(self) -> None:
        toml_text = """
schema = "spot-runner-patch-v1"
repository = "swack-tools/oxidex"

[hardware]
arch = "x86_64"
isa = ["avx2"]
min_vcpu = 4
min_ram_gib = 16

[host.sysctl]
"net.core.rmem_max" = "134217728"
"""
        parsed = parse_manifest_text(toml_text)
        patch_obj = validate_patch_dict(parsed)
        self.assertEqual(patch_obj.schema, SCHEMA_VERSION)
        self.assertEqual(patch_obj.repository, "swack-tools/oxidex")
        self.assertEqual(patch_obj.hardware.min_vcpu, 4)
        self.assertEqual(patch_obj.hardware.isa, ("avx2",))

    def test_hardware_sizing_enforcement_in_select_sizing(self) -> None:
        patch_obj = validate_patch_dict({
            "schema": "spot-runner-patch-v1",
            "hardware": {"arch": "x86_64", "min_vcpu": 16, "min_ram_gib": 64},
        })
        args_too_small_cpu = manager.args_parser().parse_args([
            "--max-cost", "0.2",
            "--instance-type", "n2-standard-8",
            "--cpu", "8",
            "--ram", "64",
        ])
        args_too_small_cpu.runner_patch = patch_obj
        with self.assertRaisesRegex(RuntimeError, "Host vCPU 8 is below patch minimum 16"):
            manager.select_sizing(args_too_small_cpu)

        args_too_small_ram = manager.args_parser().parse_args([
            "--max-cost", "0.2",
            "--instance-type", "n2-standard-16",
            "--cpu", "16",
            "--ram", "32",
        ])
        args_too_small_ram.runner_patch = patch_obj
        with self.assertRaisesRegex(RuntimeError, "Host RAM 32 GiB is below patch minimum 64 GiB"):
            manager.select_sizing(args_too_small_ram)

    def test_machine_profiles_arm64_and_storage(self) -> None:
        c4a = machine_profile("c4a-standard-16", arch="arm64")
        self.assertIsNotNone(c4a)
        self.assertEqual(c4a.quota_metric, "C4A_CPUS")
        self.assertEqual(c4a.disk_type, "hyperdisk-balanced")
        self.assertEqual(c4a.disk_interface, "NVME")

        t2a = machine_profile("t2a-standard-16", arch="arm64")
        self.assertIsNotNone(t2a)
        self.assertEqual(t2a.quota_metric, "T2A_CPUS")
        self.assertEqual(t2a.disk_type, "pd-standard")
        self.assertEqual(t2a.disk_interface, "SCSI")

        args = manager.args_parser().parse_args([
            "--max-cost", "0.2",
            "--instance-type", "c4a-standard-16",
            "--cpu", "16",
            "--ram", "64",
        ])
        args.arch = "arm64"
        profile = configure_storage(args)
        self.assertEqual(profile.disk_type, "hyperdisk-balanced")
        self.assertEqual(args.disk_type, "hyperdisk-balanced")
        self.assertEqual(args.disk_interface, "NVME")


class _FakeUrlResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()
        self.status = 200

    def read(self, _limit=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class GhApiFallbackTests(unittest.TestCase):
    """runner_patch._gh_api_json uses urllib against api.github.com when the gh CLI is absent."""

    def setUp(self):
        blocker = patch("subprocess.run", side_effect=AssertionError("gh must not run when it is not on PATH"))
        blocker.start()
        self.addCleanup(blocker.stop)
        no_gh = patch("lib.runner_patch.shutil.which", return_value=None)
        no_gh.start()
        self.addCleanup(no_gh.stop)

    def test_urllib_fallback_sends_token_and_parses_json(self):
        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append((request.full_url, dict(request.header_items()), timeout))
            return _FakeUrlResponse({"default_branch": "main"})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            data = runner_patch_mod._gh_api_json("repos/o/r", env={"SPOT_CONFIG_GH_TOKEN": "cfg-token", "GH_TOKEN": "other"})
        self.assertEqual(data, {"default_branch": "main"})
        url, headers, timeout = seen[0]
        self.assertEqual(url, "https://api.github.com/repos/o/r")
        self.assertEqual(headers.get("Authorization"), "Bearer cfg-token")
        self.assertGreater(timeout, 0)

    def test_urllib_fallback_maps_404_for_missing_files(self):
        def not_found(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

        with patch("urllib.request.urlopen", side_effect=not_found):
            self.assertIsNone(runner_patch_mod._fetch_repo_file_at_ref("o/r", "defaults.yaml", "a" * 40, env={"GH_TOKEN": "t"}))
            with self.assertRaisesRegex(RuntimeError, "404"):
                runner_patch_mod._gh_api_json("repos/o/r", env={"GH_TOKEN": "t"})

    def test_urllib_fallback_rejects_path_traversal(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("no request expected")):
            with self.assertRaises(RuntimeError):
                runner_patch_mod._gh_api_json("repos/../../evil", env={})

    def _tree_responses(self, tree_payload):
        sha = "e" * 40

        def fake_urlopen(request, timeout=None):
            url = request.full_url
            if url.endswith("/repos/swack-tools/spot-runners-config"):
                return _FakeUrlResponse({"default_branch": "main"})
            if url.endswith("/repos/swack-tools/spot-runners-config/commits/main"):
                return _FakeUrlResponse({"sha": sha})
            if url.endswith(f"/repos/swack-tools/spot-runners-config/git/trees/{sha}?recursive=1"):
                return _FakeUrlResponse(tree_payload)
            raise AssertionError(f"unexpected URL {url}")

        return sha, fake_urlopen

    def test_list_config_repo_targets(self):
        sha, fake = self._tree_responses({"truncated": False, "tree": [
            {"path": "defaults.yaml", "type": "blob"},
            {"path": "repos", "type": "tree"},
            {"path": "repos/swack-tools", "type": "tree"},
            {"path": "repos/swack-tools/oxidex.yaml", "type": "blob"},
            {"path": "repos/xbmc4lyfe/newznab-tmux.yaml", "type": "blob"},
            {"path": "repos/README.md", "type": "blob"},
        ]})
        with patch("urllib.request.urlopen", side_effect=fake):
            targets, got_sha = list_config_repo_targets(env={"GH_TOKEN": "t"})
        self.assertEqual(targets, ["swack-tools/oxidex", "xbmc4lyfe/newznab-tmux"])
        self.assertEqual(got_sha, sha)

    def test_list_config_repo_targets_fails_closed(self):
        for payload in (
            {"truncated": True, "tree": [{"path": "repos/a/b.yaml", "type": "blob"}]},
            {"truncated": False, "tree": [{"path": "repos/-bad/x.yaml", "type": "blob"}]},
            {"message": "Not Found"},
        ):
            _sha, fake = self._tree_responses(payload)
            with patch("urllib.request.urlopen", side_effect=fake):
                with self.assertRaises(RuntimeError, msg=payload):
                    list_config_repo_targets(env={"GH_TOKEN": "t"})


if __name__ == "__main__":
    unittest.main()

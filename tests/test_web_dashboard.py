"""Unit and HTTP integration tests for the Spot Fleet & Rust Builder Web UI Dashboard."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request

from lib.dashboard_models import (
    DEFAULT_RUNNER_CLOUD_INIT,
    compute_cpu_benchmark_score,
    disk_category,
    filter_regions,
    format_gcloud_ssh_command,
    parse_cloud_init,
    qualify_instances,
)
from lib.dashboard_scaling import (
    build_github_oom_deep_link,
    classify_github_ci_failure,
    evaluate_builder_scaling_decision,
    evaluate_cpu_scope,
    evaluate_scaling_decision,
    parse_github_oom_deep_link,
)
from lib.dashboard_server import create_dashboard_server, main as dashboard_main
from lib.rust_build_telemetry import (
    classify_ip_and_network,
    compute_cpu_points,
    enrich_rust_build_record,
)


class DashboardModelsTests(unittest.TestCase):
    def test_cpu_benchmark_scores_order_generations(self):
        turin_16 = compute_cpu_benchmark_score("AMD Turin", 16, "c4d-standard-16")
        emerald_16 = compute_cpu_benchmark_score("Intel Emerald Rapids", 16, "n4-standard-16")
        icelake_8 = compute_cpu_benchmark_score("Intel Ice Lake", 8, "n2-standard-8")
        cascade_8 = compute_cpu_benchmark_score("Intel Cascade Lake", 8, "n2-standard-8")
        self.assertGreater(turin_16, emerald_16)
        self.assertGreaterEqual(icelake_8, 1000)
        self.assertLess(cascade_8, 1000)

    def test_qualify_instances_runner_sizing_and_benchmark_floor(self):
        # 4 GitHub runners each with 4 vCPU and 16 GiB RAM, benchmark >= 1000
        profile = {
            "role": "cicd_runner",
            "min_runners": 4,
            "min_instances": 1,
            "max_instances": 8,
            "runner_vcpu": 4,
            "runner_ram_gib": 16,
            "host_overhead_gib": 0,
            "min_instance_vcpu": 8,
            "min_instance_ram_gib": 32,
            "min_benchmark_score": 1000,
            "disk_category": "any",
            "disk_type": "hyperdisk-balanced",
            "boot_disk_gb": 40,
            "data_disk_gb": 100,
            "budget": {
                "active_period": "day",
                "max_hourly_usd": 5.00,
                "max_daily_usd": 10.00,
                "max_monthly_usd": 300.00,
                "include_disk_costs": True,
                "include_taxes": False,
            },
        }
        regions = {"us-south1": True, "africa-south1": True, "europe-west1": True}
        result = qualify_instances(profile, regions)
        self.assertGreater(result["hardware_qualifying_count"], 0)
        self.assertGreater(result["spot_price_qualifying_count"], 0)

        for candidate in result["hardware_qualifying"]:
            self.assertGreaterEqual(candidate["benchmark_score"], 1000)
            self.assertGreaterEqual(candidate["vcpu"], 8)
            self.assertGreaterEqual(candidate["ram_gib"], 32)
            # An 8 vCPU / 32 GiB instance holds 2 runners; a 16 vCPU / 64 GiB holds 4
            if candidate["vcpu"] == 8 and candidate["ram_gib"] == 32:
                self.assertEqual(candidate["units_per_instance"], 2)
                self.assertEqual(candidate["instances_needed_for_floor"], 2)
            elif candidate["vcpu"] == 16 and candidate["ram_gib"] == 64:
                self.assertEqual(candidate["units_per_instance"], 4)
                self.assertEqual(candidate["instances_needed_for_floor"], 1)

    def test_hyperdrive_vs_normal_drive_filtering(self):
        self.assertEqual(disk_category("hyperdisk-balanced"), "hyperdrive")
        self.assertEqual(disk_category("hyperdisk-throughput"), "hyperdrive")
        self.assertEqual(disk_category("pd-ssd"), "normal")
        self.assertEqual(disk_category("pd-balanced"), "normal")

        profile = {
            "role": "rust_builder",
            "min_runners": 1,
            "runner_vcpu": 16,
            "runner_ram_gib": 28,
            "min_instance_vcpu": 16,
            "min_instance_ram_gib": 32,
            "min_benchmark_score": 1400,
            "disk_category": "hyperdrive",
            "disk_type": "hyperdisk-balanced",
            "budget": {
                "max_hourly_usd": 5.0,
                "max_daily_usd": 20.0,
                "max_monthly_usd": 500.0,
                "include_disk_costs": True,
            },
        }
        res = qualify_instances(profile, {"us-south1": True})
        self.assertTrue(all(c["disk_category"] == "hyperdrive" for c in res["hardware_qualifying"]))

    def test_cloud_init_parser_and_incomplete_chmod_warning(self):
        broken_yaml = DEFAULT_RUNNER_CLOUD_INIT + "\n  - chmod\n"
        parsed = parse_cloud_init(broken_yaml)
        self.assertEqual(parsed["format"], "cloud-config")
        self.assertTrue(parsed["package_update"])
        self.assertTrue(parsed["package_upgrade"])
        self.assertIn("docker", parsed["groups"])
        self.assertIn("build-essential", parsed["packages"])
        self.assertEqual(len(parsed["stages"]), 5)
        self.assertFalse(parsed["valid"])
        self.assertTrue(any("chmod" in w for w in parsed["warnings"]))

    def test_region_filtering_by_latency_and_capabilities(self):
        state = {"us-south1": True, "africa-south1": True, "europe-west1": False}
        low_lat = filter_regions(state, max_latency_ms=50)
        self.assertTrue(all(r["latency_ms"] <= 50 for r in low_lat))
        turin_only = filter_regions(state, required_cpu_platform="AMD Turin")
        self.assertTrue(all("AMD Turin" in r["cpu_platforms"] for r in turin_only))

    def test_ssh_command_formatting(self):
        cmd = format_gcloud_ssh_command("oxidex-runners-za-jnb-01", "africa-south1-a", "homelab-424523")
        self.assertEqual(
            cmd,
            "gcloud compute ssh oxidex-runners-za-jnb-01 --zone=africa-south1-a --project=homelab-424523",
        )


class DashboardScalingTests(unittest.TestCase):
    def test_github_oom_deep_link_build_and_parse(self):
        url = build_github_oom_deep_link(
            "Appz4Fun/nzbget",
            38596411473,
            115964282184,
            2,
            50,
        )
        self.assertEqual(
            url,
            "https://github.com/Appz4Fun/nzbget/actions/runs/38596411473/job/115964282184#step:2:50",
        )
        parsed = parse_github_oom_deep_link(url)
        self.assertEqual(parsed["repository"], "Appz4Fun/nzbget")
        self.assertEqual(parsed["run_id"], 38596411473)
        self.assertEqual(parsed["job_id"], 115964282184)
        self.assertEqual(parsed["step_number"], 2)
        self.assertEqual(parsed["line_number"], 50)
        timeout_cls = classify_github_ci_failure(
            "2026-10-14T16:25:26.2656514Z ##[error]Timeout reached, aborting! (Canceling Pages deployment)"
        )
        self.assertEqual(timeout_cls["failure_classification"], "TIMEOUT")
        self.assertFalse(timeout_cls["is_oom"])
        oom_cls = classify_github_ci_failure(
            "LLVM ERROR: out of memory / Allocation failed during LTO link pass (exit code 137)",
            runner_name="gha-oxidex-runners-asianortheast2-a-1791098074-2",
        )
        self.assertEqual(oom_cls["failure_classification"], "OOM")
        self.assertTrue(oom_cls["is_oom"])
        self.assertTrue(oom_cls["is_self_hosted_runner"])

        external_oom_cls = classify_github_ci_failure(
            "LLVM ERROR: out of memory / Allocation failed during LTO link pass (exit code 137)",
            runner_name="external-laptop-runner-01",
        )
        self.assertEqual(external_oom_cls["failure_classification"], "EXTERNAL_RUNNER_OOM")
        self.assertFalse(external_oom_cls["is_oom"])
        self.assertFalse(external_oom_cls["is_self_hosted_runner"])

        missing_runner_cls = classify_github_ci_failure(
            "LLVM ERROR: out of memory / Allocation failed during LTO link pass (exit code 137)",
            runner_name=None,
        )
        self.assertEqual(missing_runner_cls["failure_classification"], "EXTERNAL_RUNNER_OOM")
        self.assertFalse(missing_runner_cls["is_oom"])

    def test_cpu_scope_all_vs_majority(self):
        samples = [62.0, 58.0, 35.0]
        maj_ok, _ = evaluate_cpu_scope(samples, 50.0, "majority")
        all_ok, _ = evaluate_cpu_scope(samples, 50.0, "all")
        self.assertTrue(maj_ok)
        self.assertFalse(all_ok)
        all_hot, _ = evaluate_cpu_scope([55.0, 71.0, 64.0], 50.0, "all")
        self.assertTrue(all_hot)

    def test_scale_down_when_only_5_jobs_ran_in_past_hour(self):
        policy = {
            "min_runners": 4,
            "max_runners": 20,
            "runners_per_instance": 2,
            "enable_hourly_runs_scale_down": True,
            "scale_down_max_jobs_past_hour": 5,
            "scale_down_target_runners": 2,
            "enable_github_ci_oom_trigger": False,
            "enable_journal_oom_trigger": False,
            "enable_cpu_utilization_trigger": False,
        }
        telemetry = {
            "current_runners": 8,
            "jobs_ran_past_hour": 5,
            "provisioned_jobs": 1,
            "queued_jobs": 0,
            "instance_cpu_percentages": [20.0, 18.0],
            "journal_oom_events": [],
            "github_oom_runs": [],
        }
        decision = evaluate_scaling_decision(policy, telemetry)
        self.assertEqual(decision["direction"], "scale_down")
        # Target is clamped to min_runners=4 even when scale_down_target_runners=2
        self.assertEqual(decision["desired_runners"], 4)
        self.assertEqual(decision["desired_instances"], 2)
        self.assertEqual(decision["scale_down_reasons"][0]["trigger"], "hourly_runs_low")

        # Preserve capacity for active provisioned jobs when provisioned_jobs > min_runners
        busy_telemetry = {**telemetry, "provisioned_jobs": 5}
        busy_decision = evaluate_scaling_decision(policy, busy_telemetry)
        self.assertEqual(busy_decision["direction"], "scale_down")
        self.assertEqual(busy_decision["desired_runners"], 5)
        self.assertEqual(busy_decision["desired_instances"], 3)

    def test_scale_up_on_oom_and_budget_cap_enforcement(self):
        policy = {
            "min_runners": 4,
            "max_runners": 24,
            "runners_per_instance": 2,
            "scale_up_step_runners": 6,
            "max_scale_up_hourly_usd": 5.00,
            "max_scale_up_daily_usd": 10.00,
            "max_scale_up_monthly_usd": 12.00,  # Strict monthly cap (~$0.0164/hr)
        }
        telemetry = {
            "current_runners": 4,
            "jobs_ran_past_hour": 25,
            "provisioned_jobs": 8,
            "queued_jobs": 4,
            "instance_cpu_percentages": [75.0, 82.0],
            "github_oom_runs": [
                {
                    "repository": "swack-tools/oxidex",
                    "run_id": 37192272062,
                    "job_id": 111407847874,
                    "runner_name": "gha-oxidex-runners-us-south1-a-01",
                    "step_number": 8,
                    "line_number": 106,
                    "error_excerpt": "Process completed with exit code 137 (OOMKilled)",
                }
            ],
        }
        decision = evaluate_scaling_decision(policy, telemetry, per_instance_hourly_usd=0.20)
        self.assertTrue(decision["budget_capped"])
        # Blocked scale-up must hold at current fleet (4 runners / 2 instances), never scale down
        self.assertEqual(decision["direction"], "hold")
        self.assertEqual(decision["desired_instances"], 2)
        self.assertEqual(decision["desired_runners"], 4)

    def test_stale_oom_events_expire_after_lookback_window(self):
        policy = {
            "min_runners": 4,
            "max_runners": 16,
            "runners_per_instance": 4,
            "oom_lookback_minutes": 60,
            "enable_hourly_runs_scale_down": False,
            "enable_cpu_utilization_trigger": False,
        }
        telemetry = {
            "current_runners": 4,
            "jobs_ran_past_hour": 2,
            "provisioned_jobs": 1,
            "queued_jobs": 0,
            "instance_cpu_percentages": [25.0],
            "journal_oom_events": [
                {
                    "instance_name": "oxidex-runners-dallas-01",
                    "zone": "us-south1-a",
                    "process": "rustc",
                    "pid": 1234,
                    "observed_at": "2020-01-01T00:00:00Z",
                }
            ],
            "github_oom_runs": [
                {
                    "repository": "swack-tools/oxidex",
                    "run_id": 37192272062,
                    "job_id": 111407847874,
                    "runner_name": "gha-oxidex-runners-asianortheast2-a-1791098074-2",
                    "step_number": 8,
                    "line_number": 106,
                    "failed_at": "2020-01-01T00:00:00Z",
                    "error_excerpt": "##[error]Process completed with exit code 137.",
                }
            ],
        }
        decision = evaluate_scaling_decision(policy, telemetry)
        self.assertEqual(decision["direction"], "hold")
        self.assertEqual(decision["scale_up_reasons"], [])


class RustBuildTelemetryTests(unittest.TestCase):
    def test_ip_geolocation_and_protonvpn_tor_classification(self):
        proton = classify_ip_and_network("185.159.157.42")
        self.assertEqual(proton["provider_label"], "ProtonVPN")
        self.assertTrue(proton["is_vpn"])
        self.assertFalse(proton["is_tor"])
        self.assertEqual(proton["geolocation"]["country_code"], "CH")

        tor = classify_ip_and_network("185.220.101.99")
        self.assertEqual(tor["provider_label"], "Tor")
        self.assertTrue(tor["is_tor"])
        self.assertIn("Tor", tor["badge_text"])

        direct = classify_ip_and_network("73.162.44.19", "m4air-workstation.local")
        self.assertEqual(direct["provider_label"], "Direct")
        self.assertFalse(direct["is_vpn"])
        self.assertFalse(direct["is_tor"])
        self.assertEqual(direct["hostname"], "m4air-workstation.local")

        unmatched = classify_ip_and_network("8.8.8.8")
        self.assertEqual(unmatched["provider_label"], "Unknown")
        self.assertEqual(unmatched["network_type"], "Unknown")
        self.assertEqual(unmatched["geolocation"]["city"], "Unknown")

        invalid_ip = classify_ip_and_network("not-an-ip")
        self.assertEqual(invalid_ip["provider_label"], "Unknown")
        self.assertEqual(invalid_ip["network_type"], "Invalid IP")

    def test_cpu_points_and_rust_build_enrichment(self):
        pts = compute_cpu_points(1000.0, 2485)
        self.assertEqual(pts, 2485000.0)

        enriched = enrich_rust_build_record(
            {
                "build_id": "rb-test-1",
                "project_name": "oxidex",
                "requested_at": "2026-10-15T00:00:00Z",
                "finished_at": "2026-10-15T00:01:40Z",
                "delivered_at": "2026-10-15T00:01:50Z",
                "cache_hits": 400,
                "total_crates": 420,
                "instance_name": "builder-oxidex-turin-01",
                "zone": "us-south1-b",
                "machine_type": "c4d-highcpu-16",
                "vcpu": 16,
                "cpu_platform": "AMD Turin",
                "instance_benchmark_score": 2500,
                "origin_ip": "185.159.157.12",
                "peak_ram_gib": 22.5,
                "container_ram_cap_gib": 28.0,
                "cpu_time_seconds": 800.0,
            }
        )
        self.assertEqual(enriched["total_duration_seconds"], 110.0)
        self.assertEqual(enriched["compile_duration_seconds"], 100.0)
        self.assertEqual(enriched["delivery_duration_seconds"], 10.0)
        self.assertEqual(enriched["network_security"]["provider_label"], "ProtonVPN")
        self.assertEqual(enriched["peak_ram_gib"], 22.5)
        self.assertEqual(enriched["cpu_points"], 2000000.0)

        with self.assertRaises(ValueError):
            enrich_rust_build_record(
                {
                    "build_id": "rb-bad-order",
                    "requested_at": "2026-10-15T00:05:00Z",
                    "finished_at": "2026-10-15T00:01:00Z",
                    "delivered_at": "2026-10-15T00:02:00Z",
                }
            )


class DashboardCodexRegressionTests(unittest.TestCase):
    def test_qualify_instances_enforces_max_instances_and_fleet_budget(self):
        # 8 runners of 4vCPU/16GiB with max_instances=1: 8-vCPU/32-GiB VMs need 4 instances -> rejected
        profile = {
            "role": "cicd_runner",
            "min_runners": 8,
            "min_instances": 1,
            "max_instances": 1,
            "runner_vcpu": 4,
            "runner_ram_gib": 16,
            "min_instance_vcpu": 8,
            "min_instance_ram_gib": 32,
            "min_benchmark_score": 1000,
            "disk_category": "any",
            "budget": {
                "max_hourly_usd": 10.0,
                "max_daily_usd": 100.0,
                "max_monthly_usd": 3000.0,
            },
        }
        res = qualify_instances(profile, {"us-south1": True})
        self.assertGreater(res["disqualified_counts"]["exceeds_max_instances"], 0)
        for cand in res["hardware_qualifying"]:
            self.assertLessEqual(cand["instances_needed_for_floor"], 1)

        # Fleet floor budget: 2 instances needed at ~$0.19/hr each -> fleet hourly > $0.30
        tight_fleet_profile = {
            **profile,
            "min_runners": 4,
            "max_instances": 4,
            "budget": {
                "max_hourly_usd": 0.30,
                "max_daily_usd": 100.0,
                "max_monthly_usd": 3000.0,
            },
        }
        tight_res = qualify_instances(tight_fleet_profile, {"us-south1": True})
        for cand in tight_res["spot_price_qualifying"]:
            self.assertLessEqual(cand["fleet_floor_hourly_usd"], 0.30)

    def test_cloud_init_syntax_validation_rejects_broken_yaml_and_shell(self):
        bad_yaml = "#cloud-config\npackages:\n  - curl\n\t- git\nunbalanced: [1, 2\n"
        parsed_yaml = parse_cloud_init(bad_yaml)
        self.assertFalse(parsed_yaml["valid"])
        self.assertGreater(len(parsed_yaml["errors"]), 0)

        bad_shell = "#!/usr/bin/env bash\nif [ -f /tmp/foo ; then\n  echo hi\n"
        parsed_sh = parse_cloud_init(bad_shell)
        self.assertFalse(parsed_sh["valid"])
        self.assertGreater(len(parsed_sh["errors"]), 0)


class DashboardServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = Path(self.tmp.name) / "dashboard-state.json"
        self.server, self.store = create_dashboard_server(
            host="127.0.0.1",
            port=0,
            state_path=self.state_file,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_index_html_and_overview_endpoints(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("Spot GitHub Runners &amp; Rust Remote Builders Control Plane", html)
        self.assertIn("africa-south1", html)
        self.assertIn("function esc(val)", html)

        overview = self._get("/api/overview?include_deleted=true&window_hours=24")
        self.assertEqual(overview["project"], "homelab-424523")
        self.assertIn("rust_builder", overview["scaling_evaluations"])
        self.assertGreaterEqual(len(overview["instances_view"]["instances"]), 5)
        # Verify South Africa instance is present with 4 runners of 4vCPU / 8GB RAM
        za = next(
            i for i in overview["instances_view"]["instances"] if i["region"] == "africa-south1"
        )
        self.assertEqual(za["runners_count"], 4)
        self.assertEqual(za["runner_vcpu"], 4)
        self.assertEqual(za["runner_ram_gib"], 8)

    def test_edit_budget_and_save_as_cloud_init_template(self):
        budget_res = self._post(
            "/api/budget",
            {
                "target": "global",
                "active_period": "day",
                "max_hourly_usd": 4.50,
                "max_daily_usd": 12.00,
                "max_monthly_usd": 280.00,
                "include_disk_costs": False,
            },
        )
        self.assertEqual(budget_res["budget"]["max_daily_usd"], 12.00)
        # Global budget applies to both cicd_runner and rust_builder profiles and scaling policies
        self.assertEqual(self.store.state["profiles"]["rust_builder"]["budget"]["max_daily_usd"], 12.00)
        self.assertEqual(self.store.state["scaling"]["rust_builder"]["max_scale_up_daily_usd"], 12.00)
        self.assertFalse(self.store.state["scaling"]["rust_builder"]["include_disk_in_scale_budget"])

        custom_yaml = DEFAULT_RUNNER_CLOUD_INIT + "\n  - wget -qO /tmp/setup.sh https://example.com/setup.sh\n"
        ci_res = self._post(
            "/api/cloud-init/save",
            {
                "mode": "save_as",
                "role": "cicd_runner",
                "name": "Wget Custom Runner Cloud-Init",
                "content": custom_yaml,
                "assign_to_profile": True,
            },
        )
        new_id = ci_res["template"]["id"]
        self.assertEqual(self.store.state["profiles"]["cicd_runner"]["cloud_init_template_id"], new_id)

    def test_create_vm_template_scale_region_and_delete_instance(self):
        # Create a VM template from the 32-vCPU Hong Kong instance (8 runners) and assign it
        tpl_res = self._post(
            "/api/vm-templates/from-instance",
            {
                "instance_name": "oxidex-runners-hk-bulky-02",
                "template_name": "HK Bulky 32-vCPU Snapshot Template",
                "assign_to_profile": True,
            },
        )
        self.assertEqual(tpl_res["vm_template"]["source_instance"], "oxidex-runners-hk-bulky-02")
        self.assertEqual(self.store.state["scaling"]["cicd_runner"]["runners_per_instance"], 8)

        # Scaling up a c4d template in africa-south1 (only n2/e2/t2d) is rejected with 400
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/instances/scale-region",
                {"region": "africa-south1", "role": "cicd_runner", "target_instances": 2},
            )
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

        # Scaling up in us-south1 (which supports c4d / AMD Turin / hyperdisk-balanced) honors the 32-vCPU template (8 runners)
        scale_res = self._post(
            "/api/instances/scale-region",
            {"region": "us-south1", "role": "cicd_runner", "target_instances": 2},
        )
        self.assertEqual(scale_res["current_count"], 2)
        newest = next(i for i in scale_res["running_instances"] if i["name"] != "oxidex-runners-us-south1-03")
        self.assertEqual(newest["machine_type"], "c4d-standard-32")
        self.assertEqual(newest["runners_count"], 8)
        self.assertEqual(self.store.state["scaling_telemetry"]["current_runners"], 18)

        # Disabling a region blocks scale-up into that region
        self._post("/api/regions", {"regions_enabled": {"asia-east2": False}})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/instances/scale-region",
                {"region": "asia-east2", "role": "cicd_runner", "target_instances": 1},
            )
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

        # Deleting an already-deleted instance does not rewrite its historical deleted_at or runtime_hours
        already_del_before = next(
            dict(i) for i in self.store.state["instances"] if i["name"] == "oxidex-runners-iowa-04"
        )
        repeat_del = self._post(
            "/api/instances/delete",
            {"instance_name": "oxidex-runners-iowa-04", "reason": "Accidental second delete"},
        )
        self.assertEqual(repeat_del["status"], "already_deleted")
        self.assertEqual(repeat_del["instance"]["deleted_at"], already_del_before["deleted_at"])
        self.assertEqual(repeat_del["instance"]["runtime_hours"], already_del_before["runtime_hours"])
        self.assertEqual(repeat_del["instance"]["deletion_trigger"], already_del_before["deletion_trigger"])

        del_res = self._post(
            "/api/instances/delete",
            {
                "instance_name": "oxidex-runners-za-jnb-01",
                "reason": "Scheduled scale-down: lack of GitHub runner demand",
            },
        )
        self.assertEqual(del_res["instance"]["status"], "DELETED")
        self.assertEqual(self.store.state["scaling_telemetry"]["current_runners"], 14)

    def test_rust_builder_scaling_and_project_override_on_reload(self):
        sc_res = self._post(
            "/api/scaling",
            {
                "role": "rust_builder",
                "policy": {"min_instances": 2, "max_instances": 4, "cpu_threshold_percent": 60.0},
            },
        )
        self.assertEqual(sc_res["evaluation"]["role"], "rust_builder")
        self.assertGreaterEqual(sc_res["evaluation"]["desired_instances"], 2)

        # Reload store from existing state_file with a new project & org
        from lib.dashboard_server import DashboardStore

        reloaded = DashboardStore(
            state_path=self.state_file,
            project="custom-gcp-proj-99",
            org="custom-org",
        )
        self.assertEqual(reloaded.state["project"], "custom-gcp-proj-99")
        self.assertEqual(reloaded.state["org"], "custom-org")
        self.assertTrue(
            all("--project=custom-gcp-proj-99" in i["ssh_command"] for i in reloaded.state["instances"])
        )
        self.assertTrue(
            all("--project=custom-gcp-proj-99" in b["ssh_command"] for b in reloaded.state["rust_builds"])
        )

    def test_record_rust_build_and_cli_dump_overview(self):
        import contextlib
        import io

        rb_res = self._post(
            "/api/rust-builds",
            {
                "project_name": "oxidex",
                "origin_ip": "185.220.101.44",
                "instance_name": "builder-oxidex-turin-01",
                "cache_hits": 410,
                "total_crates": 430,
                "peak_ram_gib": 23.1,
                "cpu_time_seconds": 900.0,
            },
        )
        self.assertEqual(rb_res["build"]["network_security"]["provider_label"], "Tor")
        self.assertGreater(rb_res["build"]["cpu_points"], 0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = dashboard_main(["--dump-overview", "--state-file", str(self.state_file)])
        self.assertEqual(rc, 0)
        dumped = json.loads(buf.getvalue())
        self.assertEqual(dumped["project"], "homelab-424523")

    def test_round2_codex_regressions(self):
        # 1. YAML structure validation rejects inconsistent indentation and nesting under scalars
        bad_indent = parse_cloud_init("#cloud-config\nusers:\n  - name: foo\n   shell: /bin/bash\n")
        self.assertFalse(bad_indent["valid"])
        bad_scalar_nest = parse_cloud_init("#cloud-config\npackage_update: true\n  nested: 1\n")
        self.assertFalse(bad_scalar_nest["valid"])

        # 2. Tor hostname matching uses delimited labels, not substrings
        tor_host = classify_ip_and_network("8.8.8.8", "tor-exit-01.zwiebelfreunde.de")
        self.assertTrue(tor_host["is_tor"])
        benign_host = classify_ip_and_network("8.8.8.8", "refactor-worker.toronto.corp.example")
        self.assertFalse(benign_host["is_tor"])

        # 3. Profile updates propagate budgets to scaling caps, sync builder resource keys, and reject cross-role templates
        prof_res = self._post(
            "/api/profile/rust_builder",
            {
                "min_instances": 2,
                "max_instances": 5,
                "runner_vcpu": 24,
                "runner_ram_gib": 48,
                "budget": {
                    "max_hourly_usd": 2.25,
                    "max_daily_usd": 18.00,
                    "max_monthly_usd": 120.00,
                    "include_disk_costs": False,
                },
            },
        )
        self.assertEqual(prof_res["status"], "ok")
        b_sc = self.store.state["scaling"]["rust_builder"]
        self.assertEqual(b_sc["max_scale_up_hourly_usd"], 2.25)
        self.assertEqual(b_sc["max_scale_up_daily_usd"], 18.00)
        self.assertEqual(b_sc["max_scale_up_monthly_usd"], 120.00)
        self.assertFalse(b_sc["include_disk_in_scale_budget"])
        self.assertEqual(b_sc["container_vcpu"], 24)
        self.assertEqual(b_sc["builder_vcpu"], 24)
        self.assertEqual(b_sc["container_ram_gib"], 48.0)
        self.assertEqual(b_sc["builder_ram_gib"], 48.0)

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/profile/cicd_runner",
                {"use_vm_template": True, "vm_template_id": "tpl-builder-c4d-16"},
            )
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

        # 4. Unsupported roles are rejected in scale-region, scaling, and qualify
        for endpoint, body in (
            ("/api/instances/scale-region", {"region": "us-south1", "role": "invalid_role", "target_instances": 1}),
            ("/api/scaling", {"role": "invalid_role", "policy": {}}),
            ("/api/qualify", {"role": "invalid_role"}),
        ):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post(endpoint, body)
            self.assertEqual(ctx.exception.code, 400)
            ctx.exception.close()

        # 5. Rust build telemetry derives omitted total_crates from hits+misses and deduplicates build_id
        before_count = self._get("/api/rust-builds")["total_builds"]
        b1 = self._post(
            "/api/rust-builds",
            {"build_id": "rb-idempotent-01", "cache_hits": 10, "cache_misses": 2},
        )
        self.assertEqual(b1["build"]["total_crates"], 12)
        self.assertEqual(b1["build"]["cache_hit_rate_percent"], 83.3)
        b2 = self._post(
            "/api/rust-builds",
            {"build_id": "rb-idempotent-01", "cache_hits": 11, "cache_misses": 1},
        )
        self.assertEqual(b2["build"]["total_crates"], 12)
        self.assertEqual(b2["build"]["cache_hits"], 11)
        after_count = self._get("/api/rust-builds")["total_builds"]
        self.assertEqual(after_count, before_count + 1)

        # 6. Cross-origin POSTs and non-JSON POSTs are rejected
        cross_req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/budget",
            data=json.dumps({"max_daily_usd": 999}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(cross_req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

        text_req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/budget",
            data=json.dumps({"max_daily_usd": 999}).encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(text_req, timeout=10)
        self.assertEqual(ctx.exception.code, 415)
        ctx.exception.close()

    def test_round3_codex_regressions(self):
        # 1. Malformed YAML flow sequences and mappings are rejected
        for bad_flow in (
            "#cloud-config\npackages: [curl,,git]\n",
            "#cloud-config\npackages: [,curl]\n",
            "#cloud-config\npackages: [curl,]\n",
            "#cloud-config\npackages: [curl] trailing\n",
            "#cloud-config\nruncmd: [{a: 1,, b: 2}]\n",
            "#cloud-config\nruncmd: [{bare_key}]\n",
        ):
            res = parse_cloud_init(bad_flow)
            self.assertFalse(res["valid"], f"Expected invalid flow syntax for: {bad_flow!r}")

        # 2. Zero or sub-unit budgets prohibit scaling from 0 -> 1 instance
        zero_runner_eval = evaluate_scaling_decision(
            policy={"min_runners": 0, "max_runners": 16, "runners_per_instance": 4, "max_scale_up_hourly_usd": 0.0, "max_scale_up_daily_usd": 0.0, "max_scale_up_monthly_usd": 0.0},
            telemetry={"current_runners": 0, "current_instances": 0, "queued_jobs": 5},
            per_instance_hourly_usd=0.35,
        )
        self.assertEqual(zero_runner_eval["desired_instances"], 0)
        self.assertEqual(zero_runner_eval["desired_runners"], 0)
        self.assertTrue(zero_runner_eval["budget_capped"])

        zero_builder_eval = evaluate_builder_scaling_decision(
            policy={"min_instances": 0, "max_instances": 4, "max_scale_up_hourly_usd": 0.0, "max_scale_up_daily_usd": 0.0, "max_scale_up_monthly_usd": 0.0},
            telemetry={"current_builder_instances": 0, "queued_rust_builds": 3},
            per_instance_hourly_usd=0.30,
        )
        self.assertEqual(zero_builder_eval["desired_instances"], 0)
        self.assertTrue(zero_builder_eval["budget_capped"])

        # 3. Unknown budget target is rejected with 400 instead of mutating global budget
        prev_global_daily = self.store.state["global_budget"]["max_daily_usd"]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/budget", {"target": "rust_bulder", "max_daily_usd": 999.0})
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()
        self.assertEqual(self.store.state["global_budget"]["max_daily_usd"], prev_global_daily)

        # 4. Invalid profile update does not poison persisted state and preserves existing budget inclusion flags
        self._post(
            "/api/budget",
            {"target": "cicd_runner", "include_disk_costs": False, "include_taxes": True, "tax_rate_percent": 9.5},
        )
        prev_min_runners = self.store.state["profiles"]["cicd_runner"]["min_runners"]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(
                "/api/profile/cicd_runner",
                {"min_runners": 99, "runner_vcpu": "not-a-number"},
            )
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()
        self.assertEqual(self.store.state["profiles"]["cicd_runner"]["min_runners"], prev_min_runners)

        # Partial budget update in profile preserves include_disk_costs / include_taxes / tax_rate_percent
        self._post(
            "/api/profile/cicd_runner",
            {"min_runners": 6, "budget": {"max_daily_usd": 14.0}},
        )
        r_budget = self.store.state["profiles"]["cicd_runner"]["budget"]
        self.assertFalse(r_budget["include_disk_costs"])
        self.assertTrue(r_budget["include_taxes"])
        self.assertEqual(r_budget["tax_rate_percent"], 9.5)

        # 5. Excessive or negative regional scale targets are rejected before allocating records
        for bad_target in (-1, 25, 100000):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post(
                    "/api/instances/scale-region",
                    {"region": "us-south1", "role": "cicd_runner", "target_instances": bad_target},
                )
            self.assertEqual(ctx.exception.code, 400)
            ctx.exception.close()

        # 6. Overview includes unfiltered fleet_summary even when instances_view is role-filtered
        filtered_ov = self._get("/api/overview?role=rust_builder&include_deleted=false")
        self.assertEqual(len(filtered_ov["instances_view"]["instances"]), 2)
        self.assertTrue(all(i["role"] == "rust_builder" for i in filtered_ov["instances_view"]["instances"]))
        self.assertEqual(filtered_ov["fleet_summary"]["running_runner_vms"], 3)
        self.assertEqual(filtered_ov["fleet_summary"]["running_runner_slots"], 10)
        self.assertEqual(filtered_ov["fleet_summary"]["running_builder_vms"], 2)


if __name__ == "__main__":
    unittest.main()

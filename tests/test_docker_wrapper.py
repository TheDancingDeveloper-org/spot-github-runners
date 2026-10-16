"""Unit and regression tests for runner/docker-wrapper.py."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("docker_wrapper", ROOT / "runner/docker-wrapper.py")
docker_wrapper = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["docker_wrapper"] = docker_wrapper
SPEC.loader.exec_module(docker_wrapper)


class FakeDockerDaemon:
    """In-memory fake Docker daemon for testing docker-wrapper.py CLI calls."""

    def __init__(self, runner_id: str = "160bb3de045e6d3234fa30400028627418c30c5b382cf5412f30ba3407014685") -> None:
        self.runner_id = runner_id
        self.runner_name = "newznab-newznab-spot-runners-asia-3"
        self.calls: list[list[str]] = []
        self.networks: dict[str, dict] = {
            "bridge": {
                "Id": "3e30ad2a69b3701f19e2c7a8f30ee5f2d73924ec658e01b7a55c41dc7f5b4b7e",
                "Gateway": "172.17.0.1",
            },
            "github_network_1ee58a9eaf37488d9860bf85282c700f": {
                "Id": "a8a0c917941ad925a5bc1b5899c660c0f45500e58432985de0a23f96d509d99c",
                "Gateway": "172.18.0.1",
            },
        }
        self.containers: dict[str, dict] = {
            self.runner_id: {
                "Id": self.runner_id,
                "Name": f"/{self.runner_name}",
                "NetworkSettings": {
                    "Networks": {
                        "bridge": {
                            "NetworkID": self.networks["bridge"]["Id"],
                            "Gateway": "172.17.0.1",
                            "IPAddress": "172.17.0.4",
                            "GwPriority": 0,
                        }
                    },
                    "Ports": {},
                },
            }
        }
        self.fail_connect: bool = False
        self.drop_bridge_on_connect: bool = False

    def add_service_container(
        self,
        cid: str,
        name: str,
        network: str,
        ip: str = "172.18.0.2",
        cport: int = 3306,
        ephemeral_hport: int = 49153,
    ) -> None:
        self.containers[cid] = {
            "Id": cid,
            "Name": f"/{name}",
            "NetworkSettings": {
                "Networks": {
                    network: {
                        "NetworkID": self.networks.get(network, {}).get("Id", f"id-{network}"),
                        "Gateway": "172.18.0.1",
                        "IPAddress": ip,
                    }
                },
                "Ports": {
                    f"{cport}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(ephemeral_hport)}]
                },
            },
        }

    def run_docker(self, global_args: list[str], sub_args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(sub_args))
        cmd = sub_args[0]
        rest = sub_args[1:]

        if cmd == "inspect":
            fmt = None
            if rest and rest[0] == "--format":
                fmt = rest[1]
                target = rest[2]
            else:
                target = rest[0]
            matched = self._find_container(target)
            if not matched:
                res = subprocess.CompletedProcess([cmd, *rest], 1, "", f"Error: No such object: {target}\n")
            elif fmt == "{{.Id}}":
                res = subprocess.CompletedProcess([cmd, *rest], 0, f"{matched['Id']}\n", "")
            else:
                res = subprocess.CompletedProcess([cmd, *rest], 0, json.dumps([matched]), "")
            if check and res.returncode != 0:
                raise RuntimeError(res.stderr.strip())
            return res

        if cmd == "network":
            action = rest[0]
            if action == "inspect":
                net_name = rest[-1]
                net_id = self.networks.get(net_name, {}).get("Id", f"id-{net_name}")
                return subprocess.CompletedProcess(sub_args, 0, f"{net_id}\n", "")
            if action == "connect":
                if self.fail_connect:
                    res = subprocess.CompletedProcess(sub_args, 1, "", "permission denied connecting network\n")
                    if check:
                        raise RuntimeError(res.stderr.strip())
                    return res
                net_name = rest[-2]
                cid = rest[-1]
                c = self._find_container(cid)
                assert c is not None
                if self.drop_bridge_on_connect:
                    c["NetworkSettings"]["Networks"].pop("bridge", None)
                c["NetworkSettings"]["Networks"][net_name] = {
                    "NetworkID": self.networks.get(net_name, {}).get("Id", f"id-{net_name}"),
                    "Gateway": "172.18.0.1",
                    "IPAddress": "172.18.0.10",
                    "GwPriority": -1,
                }
                return subprocess.CompletedProcess(sub_args, 0, "", "")
            if action == "disconnect":
                net_name = rest[-2]
                cid = rest[-1]
                c = self._find_container(cid)
                if c:
                    c["NetworkSettings"]["Networks"].pop(net_name, None)
                return subprocess.CompletedProcess(sub_args, 0, "", "")
            if action == "rm":
                for net_target in rest:
                    for k, v in list(self.networks.items()):
                        if k == net_target or v.get("Id", "").startswith(net_target):
                            self.networks.pop(k, None)
                return subprocess.CompletedProcess(sub_args, 0, f"{rest[0]}\n", "")

        if cmd == "create":
            name = docker_wrapper.extract_option_value(rest, ("--name",)) or "service-1"
            net = docker_wrapper.extract_option_value(rest, ("--network", "--net")) or "bridge"
            cid = "05bae9a60656e571df71484f85a1571bec1fc74dda5ac87d716034252cdf034c"
            self.add_service_container(cid, name, net)
            return subprocess.CompletedProcess(sub_args, 0, f"{cid}\n", "")

        if cmd in ("start", "stop", "rm", "kill"):
            return subprocess.CompletedProcess(sub_args, 0, f"{rest[-1]}\n", "")

        if cmd == "port":
            target = rest[0]
            c = self._find_container(target)
            if not c:
                return subprocess.CompletedProcess(sub_args, 1, "", "No such container\n")
            lines = []
            for cport_proto, bindings in c["NetworkSettings"]["Ports"].items():
                for b in bindings:
                    lines.append(f"{cport_proto} -> {b['HostIp']}:{b['HostPort']}")
            return subprocess.CompletedProcess(sub_args, 0, "\n".join(lines) + "\n", "")

        return subprocess.CompletedProcess(sub_args, 0, "", "")

    def _find_container(self, ref: str) -> dict | None:
        if ref in self.containers:
            return self.containers[ref]
        for cid, info in self.containers.items():
            if cid.startswith(ref) or info["Name"].lstrip("/") == ref:
                return info
        return None


class DockerWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / "state"
        self.daemon = FakeDockerDaemon()
        self.active_relays: dict[int, dict] = {}
        self._next_pid = 40000

        def fake_start_relay(local_port: int, target_ip: str, target_port: int) -> int:
            self._next_pid += 1
            pid = self._next_pid
            self.active_relays[pid] = {
                "local_port": local_port,
                "target_ip": target_ip,
                "target_port": target_port,
            }
            return pid

        def fake_terminate_relay(relay: dict) -> None:
            pid = int(relay.get("pid") or 0)
            info = self.active_relays.get(pid)
            if info and (
                info["local_port"] == relay.get("local_port")
                and info["target_ip"] == relay.get("target_ip")
                and info["target_port"] == relay.get("target_port")
            ):
                self.active_relays.pop(pid, None)

        self.patches = [
            patch.object(docker_wrapper, "STATE_DIR", self.state_dir),
            patch.object(docker_wrapper, "run_docker", side_effect=self.daemon.run_docker),
            patch.object(docker_wrapper, "start_owned_relay", side_effect=fake_start_relay),
            patch.object(docker_wrapper, "terminate_owned_relay", side_effect=fake_terminate_relay),
            patch.object(docker_wrapper, "wait_for_tcp_handshake", return_value=None),
            patch.dict(os.environ, {"HOSTNAME": self.daemon.runner_id[:12]}, clear=False),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_rewrite_publish_args_only_rewrites_github_network_containers(self) -> None:
        job_args = [
            "create",
            "--name",
            "mariadb-svc",
            "--network",
            "github_network_1ee58a9eaf37488d9860bf85282c700f",
            "-p",
            "3306:3306",
            "--publish=127.0.0.1:6379:6379",
            "mariadb:11.4.8",
        ]
        rewritten, requested = docker_wrapper.rewrite_publish_args(job_args)
        self.assertEqual(requested, {3306: 3306, 6379: 6379})
        self.assertIn("127.0.0.1::3306", rewritten)
        self.assertIn("--publish=127.0.0.1::6379", rewritten)

        bridge_args = ["run", "-d", "--network", "bridge", "-p", "3306:3306", "mariadb:11.4.8"]
        rewritten_bridge, requested_bridge = docker_wrapper.rewrite_publish_args(bridge_args)
        self.assertEqual(rewritten_bridge, bridge_args)
        self.assertEqual(requested_bridge, {})

    def test_create_start_port_stop_and_network_rm_lifecycle(self) -> None:
        net_name = "github_network_1ee58a9eaf37488d9860bf85282c700f"
        cid = "05bae9a60656e571df71484f85a1571bec1fc74dda5ac87d716034252cdf034c"

        # 1. docker create
        out = io.StringIO()
        with redirect_stdout(out):
            rc = docker_wrapper.main(
                [
                    "create",
                    "--name",
                    "donor-mariadb",
                    "--network",
                    net_name,
                    "-p",
                    "3306:3306",
                    "mariadb:11.4.8",
                ]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), cid)

        # 2. docker start attaches runner with --gw-priority -1 and starts owned relays
        out = io.StringIO()
        with redirect_stdout(out):
            rc = docker_wrapper.main(["start", cid])
        self.assertEqual(rc, 0)
        self.assertIn(
            ["network", "connect", "--gw-priority", "-1", net_name, self.daemon.runner_id],
            self.daemon.calls,
        )
        runner_nets = self.daemon.containers[self.daemon.runner_id]["NetworkSettings"]["Networks"]
        self.assertEqual(runner_nets["bridge"]["Gateway"], "172.17.0.1")
        self.assertIn(net_name, runner_nets)
        relay_ports = {r["local_port"] for r in self.active_relays.values()}
        self.assertEqual(relay_ports, {3306, 49153})

        # 3. docker port reports 0.0.0.0:3306 for GitHub Actions service port discovery
        out = io.StringIO()
        with redirect_stdout(out):
            rc = docker_wrapper.main(["port", cid])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "3306/tcp -> 0.0.0.0:3306")

        # 4. docker stop cleans up owned relays and disconnects runner from job network
        out = io.StringIO()
        with redirect_stdout(out):
            rc = docker_wrapper.main(["stop", cid])
        self.assertEqual(rc, 0)
        self.assertEqual(self.active_relays, {})
        self.assertNotIn(net_name, runner_nets)
        self.assertIn(["network", "disconnect", net_name, self.daemon.runner_id], self.daemon.calls)

    def test_network_rm_cleans_up_remaining_state_when_stop_was_skipped(self) -> None:
        net_name = "github_network_1ee58a9eaf37488d9860bf85282c700f"
        cid = "05bae9a60656e571df71484f85a1571bec1fc74dda5ac87d716034252cdf034c"
        with redirect_stdout(io.StringIO()):
            docker_wrapper.main(["create", "--name", "donor-mariadb", "--network", net_name, "-p", "3306:3306", "mariadb:11.4.8"])
            docker_wrapper.main(["start", cid])
        self.assertTrue(self.active_relays)

        net_id = self.daemon.networks[net_name]["Id"]
        with redirect_stdout(io.StringIO()):
            rc = docker_wrapper.main(["network", "rm", net_id])
        self.assertEqual(rc, 0)
        self.assertEqual(self.active_relays, {})
        runner_nets = self.daemon.containers[self.daemon.runner_id]["NetworkSettings"]["Networks"]
        self.assertNotIn(net_name, runner_nets)

    def test_handshake_failure_fails_closed_and_rolls_back_network_and_relays(self) -> None:
        net_name = "github_network_1ee58a9eaf37488d9860bf85282c700f"
        cid = "05bae9a60656e571df71484f85a1571bec1fc74dda5ac87d716034252cdf034c"
        with redirect_stdout(io.StringIO()):
            docker_wrapper.main(["create", "--name", "donor-mariadb", "--network", net_name, "-p", "3306:3306", "mariadb:11.4.8"])

        err = io.StringIO()
        with patch.object(docker_wrapper, "wait_for_tcp_handshake", side_effect=RuntimeError("upstream 172.18.0.2:3306 unreachable")), redirect_stderr(err):
            rc = docker_wrapper.main(["start", cid])
        self.assertEqual(rc, 1)
        self.assertIn("upstream 172.18.0.2:3306 unreachable", err.getvalue())
        self.assertEqual(self.active_relays, {})
        runner_nets = self.daemon.containers[self.daemon.runner_id]["NetworkSettings"]["Networks"]
        self.assertNotIn(net_name, runner_nets)

    def test_bridge_gateway_loss_fails_closed(self) -> None:
        net_name = "github_network_1ee58a9eaf37488d9860bf85282c700f"
        cid = "05bae9a60656e571df71484f85a1571bec1fc74dda5ac87d716034252cdf034c"
        self.daemon.drop_bridge_on_connect = True
        with redirect_stdout(io.StringIO()):
            docker_wrapper.main(["create", "--name", "donor-mariadb", "--network", net_name, "-p", "3306:3306", "mariadb:11.4.8"])

        err = io.StringIO()
        with redirect_stderr(err):
            rc = docker_wrapper.main(["start", cid])
        self.assertEqual(rc, 1)
        self.assertIn("default bridge gateway was lost", err.getvalue())
        self.assertEqual(self.active_relays, {})

    def test_concurrent_runners_isolate_state_and_do_not_cross_disconnect(self) -> None:
        state_a = Path(self.tmp.name) / "state-a"
        state_b = Path(self.tmp.name) / "state-b"
        runner_b_id = "f3e525692ad22ca4ebe07bf95b25ea1f8474b8d354a8018974e8bed8174c6b7b"
        self.daemon.containers[runner_b_id] = {
            "Id": runner_b_id,
            "Name": "/newznab-newznab-spot-runners-asia-4",
            "NetworkSettings": {
                "Networks": {
                    "bridge": {
                        "NetworkID": self.daemon.networks["bridge"]["Id"],
                        "Gateway": "172.17.0.1",
                        "IPAddress": "172.17.0.5",
                    }
                },
                "Ports": {},
            },
        }
        net_a = "github_network_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        net_b = "github_network_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        self.daemon.networks[net_a] = {"Id": "id-a", "Gateway": "172.19.0.1"}
        self.daemon.networks[net_b] = {"Id": "id-b", "Gateway": "172.20.0.1"}
        cid_a = "aaaa" * 16
        cid_b = "bbbb" * 16
        self.daemon.add_service_container(cid_a, "svc-a", net_a, ip="172.19.0.2", ephemeral_hport=49201)
        self.daemon.add_service_container(cid_b, "svc-b", net_b, ip="172.20.0.2", ephemeral_hport=49202)

        with patch.object(docker_wrapper, "STATE_DIR", state_a), patch.dict(os.environ, {"HOSTNAME": self.daemon.runner_id[:12]}), redirect_stdout(io.StringIO()):
            self.assertEqual(docker_wrapper.main(["start", cid_a]), 0)

        with patch.object(docker_wrapper, "STATE_DIR", state_b), patch.dict(os.environ, {"HOSTNAME": runner_b_id[:12]}), redirect_stdout(io.StringIO()):
            self.assertEqual(docker_wrapper.main(["start", cid_b]), 0)

        with patch.object(docker_wrapper, "STATE_DIR", state_a), patch.dict(os.environ, {"HOSTNAME": self.daemon.runner_id[:12]}), redirect_stdout(io.StringIO()):
            self.assertEqual(docker_wrapper.main(["stop", cid_a]), 0)

        self.assertNotIn(net_a, self.daemon.containers[self.daemon.runner_id]["NetworkSettings"]["Networks"])
        self.assertIn(net_b, self.daemon.containers[runner_b_id]["NetworkSettings"]["Networks"])
        remaining_ips = {r["target_ip"] for r in self.active_relays.values()}
        self.assertEqual(remaining_ips, {"172.20.0.2"})

    def test_real_tcp_handshake_timeout_and_success(self) -> None:
        self.patches[4].stop()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]

            def accept_once() -> None:
                conn, _ = srv.accept()
                conn.close()

            t = threading.Thread(target=accept_once, daemon=True)
            t.start()
            docker_wrapper.wait_for_tcp_handshake("127.0.0.1", port, timeout_sec=1.0)
            t.join(timeout=1.0)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unused:
            unused.bind(("127.0.0.1", 0))
            closed_port = unused.getsockname()[1]
        with self.assertRaises(RuntimeError):
            docker_wrapper.wait_for_tcp_handshake("127.0.0.1", closed_port, timeout_sec=0.15)


if __name__ == "__main__":
    unittest.main()

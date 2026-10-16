#!/usr/bin/env python3
"""Per-runner Docker CLI wrapper for GitHub Actions service-container reachability."""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REAL_DOCKER = os.environ.get("GHA_REAL_DOCKER", "/usr/bin/docker")
STATE_DIR = Path(os.environ.get("GHA_DOCKER_WRAPPER_STATE_DIR", "/tmp/gha-docker-wrapper"))
HANDSHAKE_TIMEOUT_SEC = float(os.environ.get("GHA_DOCKER_WRAPPER_HANDSHAKE_TIMEOUT", "15.0"))
NETWORK_NAME_RE = re.compile(r"^github_network_[0-9a-fA-F]+$")
GLOBAL_VALUE_FLAGS = frozenset({"-H", "--host", "--config", "-c", "--context", "-l", "--log-level"})


def find_subcommand(args: list[str]) -> tuple[str | None, int | None]:
    i = 0
    while i < len(args):
        token = args[i]
        if token in GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if any(token.startswith(f"{flag}=") for flag in GLOBAL_VALUE_FLAGS):
            i += 1
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token, i
    return None, None


def split_global_and_sub_args(args: list[str]) -> tuple[list[str], str | None, list[str]]:
    subcmd, idx = find_subcommand(args)
    if subcmd is None or idx is None:
        return list(args), None, []
    return list(args[:idx]), subcmd, list(args[idx + 1 :])


def parse_publish_spec(spec: str) -> tuple[str | None, int, int, str] | None:
    """Parse fixed TCP port publish specs like 3306:3306 or 127.0.0.1:3306:3306."""
    proto = "tcp"
    body = spec
    if "/" in body:
        body, proto = body.rsplit("/", 1)
        proto = proto.lower()
    if proto != "tcp":
        return None
    parts = body.split(":")
    if len(parts) == 2:
        host_ip = None
        host_port_str, container_port_str = parts
    elif len(parts) == 3:
        host_ip, host_port_str, container_port_str = parts
    else:
        return None
    if not host_port_str.isdigit() or not container_port_str.isdigit():
        return None
    host_port = int(host_port_str)
    container_port = int(container_port_str)
    if not (1 <= host_port <= 65535 and 1 <= container_port <= 65535):
        return None
    return host_ip, host_port, container_port, proto


def extract_option_value(sub_args: list[str], flags: tuple[str, ...]) -> str | None:
    i = 0
    while i < len(sub_args):
        tok = sub_args[i]
        if tok in flags and i + 1 < len(sub_args):
            return sub_args[i + 1]
        for flag in flags:
            if tok.startswith(f"{flag}="):
                return tok.split("=", 1)[1]
        i += 1
    return None


def rewrite_publish_args(args: list[str]) -> tuple[list[str], dict[int, int]]:
    """Rewrite fixed TCP -p HPORT:CPORT on github_network_* containers to 127.0.0.1::CPORT."""
    subcmd, sub_idx = find_subcommand(args)
    if subcmd not in ("create", "run") or sub_idx is None:
        return list(args), {}

    network_name = extract_option_value(args[sub_idx + 1 :], ("--network", "--net"))
    if not network_name or not NETWORK_NAME_RE.match(network_name):
        return list(args), {}

    rewritten = list(args)
    requested_map: dict[int, int] = {}
    i = sub_idx + 1
    while i < len(rewritten):
        tok = rewritten[i]
        if tok in ("-p", "--publish") and i + 1 < len(rewritten):
            parsed = parse_publish_spec(rewritten[i + 1])
            if parsed is not None:
                _, hport, cport, _ = parsed
                rewritten[i + 1] = f"127.0.0.1::{cport}"
                requested_map[cport] = hport
            i += 2
            continue
        if tok.startswith("-p=") or tok.startswith("--publish="):
            prefix, val = tok.split("=", 1)
            parsed = parse_publish_spec(val)
            if parsed is not None:
                _, hport, cport, _ = parsed
                rewritten[i] = f"{prefix}=127.0.0.1::{cport}"
                requested_map[cport] = hport
        i += 1
    return rewritten, requested_map


class StateStore:
    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = state_dir or STATE_DIR
        self.lock_path = self.state_dir / "state.lock"
        self.state_path = self.state_dir / "state.json"

    def __enter__(self) -> dict[str, Any]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(self.lock_path, "a+", encoding="utf-8")
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX)
        self._data = self._read()
        return self._data

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if exc_type is None:
                self._write(self._data)
        finally:
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_file.close()

    def _read(self) -> dict[str, Any]:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                data.setdefault("runner_id", None)
                data.setdefault("networks", {})
                data.setdefault("containers", {})
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"[docker-wrapper] warning: resetting corrupt state {self.state_path}: {exc}\n")
        return {"runner_id": None, "networks": {}, "containers": {}}

    def _write(self, data: dict[str, Any]) -> None:
        fd, tmp_path = tempfile.mkstemp(dir=str(self.state_dir), prefix="state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.state_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def run_docker(global_args: list[str], sub_args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = [REAL_DOCKER, *global_args, *sub_args]
    res = subprocess.run(cmd, text=True, capture_output=True)
    if check and res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        joined = " ".join(sub_args)
        raise RuntimeError(f"docker {joined} failed (rc={res.returncode}): {detail}")
    return res


def detect_self_container_id(global_args: list[str]) -> str | None:
    override = os.environ.get("GHA_RUNNER_CONTAINER_ID")
    if override:
        return override.strip()
    candidates: list[str] = []
    hostname = os.environ.get("HOSTNAME") or socket.gethostname()
    if hostname:
        candidates.append(hostname.strip())
    runner_name = os.environ.get("RUNNER_NAME")
    if runner_name and runner_name not in candidates:
        candidates.append(runner_name.strip())
    for cand in candidates:
        res = run_docker(global_args, ["inspect", "--format", "{{.Id}}", cand], check=False)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().splitlines()[0].strip()
    return None


def verify_pid_is_owned_socat(pid: int, local_port: int, target_ip: str, target_port: int) -> bool:
    if pid <= 1:
        return False
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if proc_cmdline.exists():
        try:
            raw = proc_cmdline.read_bytes()
            if not raw:
                return False
            parts = [p.decode("utf-8", errors="replace") for p in raw.split(b"\x00") if p]
            cmdline_str = " ".join(parts)
            return (
                bool(parts)
                and ("socat" in Path(parts[0]).name)
                and f"TCP-LISTEN:{local_port}," in cmdline_str
                and f"TCP:{target_ip}:{target_port}" in cmdline_str
            )
        except OSError:
            return False
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return (
            "socat" in out
            and f"TCP-LISTEN:{local_port}," in out
            and f"TCP:{target_ip}:{target_port}" in out
        )
    except (OSError, subprocess.SubprocessError):
        return False


def terminate_owned_relay(relay: dict[str, Any]) -> None:
    pid = int(relay.get("pid") or 0)
    local_port = int(relay.get("local_port") or 0)
    target_ip = str(relay.get("target_ip") or "")
    target_port = int(relay.get("target_port") or 0)
    if pid <= 1 or not verify_pid_is_owned_socat(pid, local_port, target_ip, target_port):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        sys.stderr.write(f"[docker-wrapper] warning: could not SIGTERM relay pid={pid}: {exc}\n")
        return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not verify_pid_is_owned_socat(pid, local_port, target_ip, target_port):
            return
        time.sleep(0.02)
    if verify_pid_is_owned_socat(pid, local_port, target_ip, target_port):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def start_owned_relay(local_port: int, target_ip: str, target_port: int) -> int:
    proc = subprocess.Popen(
        [
            "socat",
            f"TCP-LISTEN:{local_port},bind=127.0.0.1,reuseaddr,fork",
            f"TCP:{target_ip}:{target_port}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    time.sleep(0.05)
    if proc.poll() is not None:
        err = (proc.stderr.read() if proc.stderr else "").strip()
        raise RuntimeError(
            f"socat relay for 127.0.0.1:{local_port} -> {target_ip}:{target_port} exited immediately "
            f"(rc={proc.returncode}): {err}"
        )
    if proc.stderr is not None:
        proc.stderr.close()
    return proc.pid


def wait_for_tcp_handshake(host: str, port: int, timeout_sec: float = HANDSHAKE_TIMEOUT_SEC) -> None:
    if timeout_sec <= 0:
        return
    deadline = time.monotonic() + timeout_sec
    last_err: Exception | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"Timed out after {timeout_sec:.1f}s waiting for TCP reachability to {host}:{port} "
                f"(last_error={last_err})"
            )
        attempt_timeout = min(1.0, max(0.05, remaining))
        try:
            with socket.create_connection((host, port), timeout=attempt_timeout):
                return
        except OSError as exc:
            last_err = exc
            time.sleep(min(0.1, max(0.01, deadline - time.monotonic())))


def ensure_runner_attached_to_network(
    global_args: list[str],
    state: dict[str, Any],
    network_name: str,
) -> str:
    runner_id = state.get("runner_id") or detect_self_container_id(global_args)
    if not runner_id:
        raise RuntimeError("Could not determine self runner container ID for network attachment")
    state["runner_id"] = runner_id

    inspect_res = run_docker(global_args, ["inspect", runner_id], check=True)
    runner_info = json.loads(inspect_res.stdout)[0]
    attached_nets = (runner_info.get("NetworkSettings") or {}).get("Networks") or {}
    if "bridge" not in attached_nets:
        raise RuntimeError(f"Runner container {runner_id[:12]} is not attached to default 'bridge' network")

    net_entry = state["networks"].get(network_name)
    if network_name not in attached_nets:
        conn_res = run_docker(
            global_args,
            ["network", "connect", "--gw-priority", "-1", network_name, runner_id],
            check=False,
        )
        if conn_res.returncode != 0:
            recheck = run_docker(global_args, ["inspect", runner_id], check=True)
            recheck_nets = (json.loads(recheck.stdout)[0].get("NetworkSettings") or {}).get("Networks") or {}
            if network_name not in recheck_nets:
                detail = (conn_res.stderr or conn_res.stdout or "").strip()
                raise RuntimeError(
                    f"Failed to attach runner {runner_id[:12]} to job network {network_name}: {detail}"
                )
        post_res = run_docker(global_args, ["inspect", runner_id], check=True)
        post_nets = (json.loads(post_res.stdout)[0].get("NetworkSettings") or {}).get("Networks") or {}
        if "bridge" not in post_nets or not (post_nets["bridge"].get("Gateway") or ""):
            raise RuntimeError(
                f"Runner {runner_id[:12]} default bridge gateway was lost after attaching {network_name}"
            )
        if net_entry is None:
            net_id_res = run_docker(
                global_args,
                ["network", "inspect", "--format", "{{.Id}}", network_name],
                check=False,
            )
            net_id = net_id_res.stdout.strip() if net_id_res.returncode == 0 else network_name
            state["networks"][network_name] = {
                "network_id": net_id,
                "runner_id": runner_id,
                "owned_by_wrapper": True,
                "containers": [],
            }
    else:
        if net_entry is None:
            net_id_res = run_docker(
                global_args,
                ["network", "inspect", "--format", "{{.Id}}", network_name],
                check=False,
            )
            net_id = net_id_res.stdout.strip() if net_id_res.returncode == 0 else network_name
            state["networks"][network_name] = {
                "network_id": net_id,
                "runner_id": runner_id,
                "owned_by_wrapper": False,
                "containers": [],
            }
    return runner_id


def detach_runner_from_network_if_unused(
    global_args: list[str],
    state: dict[str, Any],
    network_name: str,
    *,
    force: bool = False,
) -> None:
    net_entry = state["networks"].get(network_name)
    if not net_entry:
        return
    active_cids = [cid for cid in net_entry.get("containers", []) if cid in state["containers"]]
    net_entry["containers"] = active_cids
    if active_cids and not force:
        return
    if net_entry.get("owned_by_wrapper"):
        runner_id = net_entry.get("runner_id") or state.get("runner_id") or detect_self_container_id(global_args)
        if runner_id:
            disc = run_docker(global_args, ["network", "disconnect", network_name, runner_id], check=False)
            if disc.returncode != 0:
                err_txt = (disc.stderr or disc.stdout or "").lower()
                if (
                    "is not connected" not in err_txt
                    and "not found" not in err_txt
                    and "no such network" not in err_txt
                ):
                    run_docker(global_args, ["network", "disconnect", "-f", network_name, runner_id], check=False)
    state["networks"].pop(network_name, None)


def cleanup_container_state(
    global_args: list[str],
    state: dict[str, Any],
    cid_or_name: str,
) -> None:
    matched_cids: list[str] = []
    for cid, cinfo in list(state["containers"].items()):
        if cid == cid_or_name or cid.startswith(cid_or_name) or cinfo.get("name") == cid_or_name:
            matched_cids.append(cid)
    if not matched_cids:
        res = run_docker(global_args, ["inspect", "--format", "{{.Id}}", cid_or_name], check=False)
        if res.returncode == 0 and res.stdout.strip():
            full_id = res.stdout.strip().splitlines()[0].strip()
            if full_id in state["containers"]:
                matched_cids.append(full_id)

    for cid in matched_cids:
        cinfo = state["containers"].pop(cid, {})
        for relay in cinfo.get("relays", []):
            terminate_owned_relay(relay)
        for net_name in cinfo.get("networks", []):
            if net_name in state["networks"]:
                c_list = state["networks"][net_name].get("containers", [])
                if cid in c_list:
                    c_list.remove(cid)
                detach_runner_from_network_if_unused(global_args, state, net_name)


def setup_service_container(
    global_args: list[str],
    state: dict[str, Any],
    cid_or_name: str,
    requested_ports_override: dict[int, int] | None = None,
) -> None:
    inspect_res = run_docker(global_args, ["inspect", cid_or_name], check=True)
    info = json.loads(inspect_res.stdout)[0]
    cid = str(info["Id"])
    cname = str(info.get("Name") or "").lstrip("/")
    net_settings = info.get("NetworkSettings") or {}
    networks = net_settings.get("Networks") or {}

    job_networks = [n for n in networks if NETWORK_NAME_RE.match(n)]
    if not job_networks:
        return

    existing_entry = state["containers"].get(cid, {})
    requested_ports: dict[str, int] = dict(existing_entry.get("requested_ports") or {})
    if requested_ports_override:
        for cport, hport in requested_ports_override.items():
            requested_ports[str(cport)] = int(hport)

    for old_relay in existing_entry.get("relays", []):
        terminate_owned_relay(old_relay)

    attached_job_networks: list[str] = []
    target_ip = ""
    try:
        for net_name in job_networks:
            ensure_runner_attached_to_network(global_args, state, net_name)
            attached_job_networks.append(net_name)
            net_entry = state["networks"][net_name]
            if cid not in net_entry["containers"]:
                net_entry["containers"].append(cid)
            ip_candidate = (networks.get(net_name) or {}).get("IPAddress") or ""
            if ip_candidate and not target_ip:
                target_ip = ip_candidate

        if not target_ip:
            raise RuntimeError(f"Service container {cid[:12]} on {job_networks} has no IPv4 address")

        ports = net_settings.get("Ports") or {}
        new_relays: list[dict[str, Any]] = []
        for cport_proto, bindings in ports.items():
            if not cport_proto.endswith("/tcp") or not bindings:
                continue
            cport = int(cport_proto.split("/", 1)[0])
            local_ports: set[int] = set()
            for binding in bindings:
                hport = int(binding.get("HostPort") or 0)
                if hport > 0:
                    local_ports.add(hport)
            req_hport = requested_ports.get(str(cport))
            if req_hport:
                local_ports.add(int(req_hport))
            elif cport == 3306:
                local_ports.add(3306)

            if not local_ports:
                continue

            wait_for_tcp_handshake(target_ip, cport, timeout_sec=HANDSHAKE_TIMEOUT_SEC)
            for lport in sorted(local_ports):
                for other_cid, other_info in list(state["containers"].items()):
                    if other_cid == cid:
                        continue
                    kept_relays = []
                    for r in other_info.get("relays", []):
                        if int(r.get("local_port") or 0) == lport:
                            terminate_owned_relay(r)
                        else:
                            kept_relays.append(r)
                    other_info["relays"] = kept_relays

                pid = start_owned_relay(lport, target_ip, cport)
                new_relays.append(
                    {
                        "pid": pid,
                        "local_port": lport,
                        "target_ip": target_ip,
                        "target_port": cport,
                    }
                )
                wait_for_tcp_handshake("127.0.0.1", lport, timeout_sec=5.0)
    except BaseException:
        for relay in locals().get("new_relays", []):
            terminate_owned_relay(relay)
        state["containers"].pop(cid, None)
        for net_name in attached_job_networks:
            if net_name in state["networks"] and cid in state["networks"][net_name].get("containers", []):
                state["networks"][net_name]["containers"].remove(cid)
            detach_runner_from_network_if_unused(global_args, state, net_name, force=True)
        raise

    state["containers"][cid] = {
        "name": cname,
        "networks": attached_job_networks,
        "target_ip": target_ip,
        "requested_ports": requested_ports,
        "relays": new_relays,
    }


def record_created_container(
    global_args: list[str],
    state: dict[str, Any],
    cid: str,
    sub_args: list[str],
    requested_map: dict[int, int],
) -> None:
    if not requested_map:
        return
    cname = extract_option_value(sub_args, ("--name",)) or ""
    net_name = extract_option_value(sub_args, ("--network", "--net")) or ""
    state["containers"][cid] = {
        "name": cname.lstrip("/"),
        "networks": [net_name] if NETWORK_NAME_RE.match(net_name) else [],
        "target_ip": "",
        "requested_ports": {str(k): int(v) for k, v in requested_map.items()},
        "relays": [],
    }


def handle_port_command(global_args: list[str], sub_args: list[str]) -> int:
    proc = run_docker(global_args, ["port", *sub_args], check=False)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0 or not sub_args:
        sys.stdout.write(proc.stdout)
        return proc.returncode

    target_ref = sub_args[0]
    requested_ports: dict[str, int] = {}
    with StateStore() as state:
        for cid, cinfo in state["containers"].items():
            if cid == target_ref or cid.startswith(target_ref) or cinfo.get("name") == target_ref:
                requested_ports = dict(cinfo.get("requested_ports") or {})
                break

    lines: list[str] = []
    for line in proc.stdout.splitlines():
        if " -> " in line:
            cport_proto, _ = line.split(" -> ", 1)
            if cport_proto.endswith("/tcp"):
                cport_str = cport_proto.split("/", 1)[0]
                if cport_str in requested_ports:
                    lines.append(f"{cport_proto} -> 0.0.0.0:{requested_ports[cport_str]}")
                    continue
                if cport_str == "3306":
                    lines.append("3306/tcp -> 0.0.0.0:3306")
                    continue
        lines.append(line)
    if lines:
        sys.stdout.write("\n".join(lines) + "\n")
    return proc.returncode


def extract_positional_targets(sub_args: list[str], flags_with_value: frozenset[str] = frozenset()) -> list[str]:
    targets: list[str] = []
    i = 0
    while i < len(sub_args):
        tok = sub_args[i]
        if tok == "--":
            targets.extend(sub_args[i + 1 :])
            break
        if tok in flags_with_value:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        targets.append(tok)
        i += 1
    return targets


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    rewritten_args, requested_map = rewrite_publish_args(raw_args)
    global_args, subcmd, sub_args = split_global_and_sub_args(rewritten_args)

    if subcmd == "port":
        return handle_port_command(global_args, sub_args)

    if subcmd == "create":
        proc = run_docker(global_args, ["create", *sub_args], check=False)
        if proc.returncode == 0 and requested_map and proc.stdout.strip():
            cid = proc.stdout.strip().splitlines()[-1].strip()
            with StateStore() as state:
                record_created_container(global_args, state, cid, sub_args, requested_map)
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode

    if subcmd == "start" or (subcmd == "run" and any(a in ("-d", "--detach") for a in sub_args)):
        proc = run_docker(global_args, [subcmd, *sub_args], check=False)
        if proc.returncode != 0:
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            return proc.returncode

        targets: list[str] = []
        if subcmd == "run":
            out_lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            if out_lines:
                targets.append(out_lines[-1])
        else:
            targets.extend(extract_positional_targets(sub_args, frozenset({"--detach-keys"})))

        try:
            with StateStore() as state:
                for target in targets:
                    setup_service_container(
                        global_args,
                        state,
                        target,
                        requested_ports_override=requested_map if subcmd == "run" else None,
                    )
        except Exception as exc:
            sys.stderr.write(f"[docker-wrapper] error setting up service container reachability: {exc}\n")
            return 1

        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode

    if subcmd in ("stop", "rm", "kill"):
        value_flags = frozenset({"-t", "--time", "-s", "--signal"})
        targets = extract_positional_targets(sub_args, value_flags)
        with StateStore() as state:
            for target in targets:
                cleanup_container_state(global_args, state, target)
        proc = run_docker(global_args, [subcmd, *sub_args], check=False)
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode

    if subcmd == "network" and sub_args:
        net_action = sub_args[0]
        net_rest = sub_args[1:]
        if net_action == "rm":
            targets = extract_positional_targets(net_rest)
            with StateStore() as state:
                for net_target in targets:
                    for net_name, net_entry in list(state["networks"].items()):
                        if net_name == net_target or net_entry.get("network_id", "").startswith(net_target):
                            for cid in list(net_entry.get("containers", [])):
                                cleanup_container_state(global_args, state, cid)
                            detach_runner_from_network_if_unused(global_args, state, net_name, force=True)
            proc = run_docker(global_args, ["network", *sub_args], check=False)
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            return proc.returncode

    os.execv(REAL_DOCKER, [REAL_DOCKER, *rewritten_args])


if __name__ == "__main__":
    raise SystemExit(main())

"""Declarative repository runner patching, config repo loader, and live hot-patcher."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "spot-runner-patch-v1"
DEFAULT_CONFIG_REPO_NAME = "spot-runners-config"

# Maps canonical ISA requirement names to:
# - arch: required CPU architecture ('x86_64' or 'arm64')
# - cpuinfo_flags: Linux /proc/cpuinfo flags (all must be present)
# - gce_platforms: supported GCE (machine_family, cpu_platform) pairs
ISA_CAPABILITIES: dict[str, dict[str, Any]] = {
    "avx2": {
        "arch": "x86_64",
        "cpuinfo_flags": ("avx2",),
        "gce_platforms": (
            ("c4d", "AMD Turin"),
            ("n4d", "AMD Turin"),
            ("c4", "Intel Granite Rapids"),
            ("c4", "Intel Emerald Rapids"),
            ("n4", "Intel Emerald Rapids"),
            ("c2d", "AMD Milan"),
            ("n2d", "AMD Milan"),
            ("n2", "Intel Ice Lake"),
            ("n2", "Intel Cascade Lake"),
        ),
    },
    "avx512": {
        "arch": "x86_64",
        "cpuinfo_flags": ("avx512f", "avx512bw", "avx512vl"),
        "gce_platforms": (
            ("c4d", "AMD Turin"),
            ("n4d", "AMD Turin"),
            ("c4", "Intel Granite Rapids"),
            ("c4", "Intel Emerald Rapids"),
            ("n4", "Intel Emerald Rapids"),
            ("n2", "Intel Ice Lake"),
            ("n2", "Intel Cascade Lake"),
        ),
    },
    "avx512-vbmi2": {
        "arch": "x86_64",
        "cpuinfo_flags": ("avx512f", "avx512_vbmi2"),
        "gce_platforms": (
            ("c4d", "AMD Turin"),
            ("n4d", "AMD Turin"),
            ("c4", "Intel Granite Rapids"),
            ("c4", "Intel Emerald Rapids"),
            ("n4", "Intel Emerald Rapids"),
            ("n2", "Intel Ice Lake"),
        ),
    },
    "amx": {
        "arch": "x86_64",
        "cpuinfo_flags": ("amx_tile", "amx_bf16", "amx_int8"),
        "gce_platforms": (
            ("c4", "Intel Granite Rapids"),
            ("c4", "Intel Emerald Rapids"),
            ("n4", "Intel Emerald Rapids"),
        ),
    },
    "avx10.1": {
        "arch": "x86_64",
        "cpuinfo_flags": ("avx512f", "avx512_vbmi2", "amx_tile"),
        "gce_platforms": (
            ("c4", "Intel Granite Rapids"),
        ),
    },
    "neon": {
        "arch": "arm64",
        "cpuinfo_flags": ("asimd",),
        "gce_platforms": (
            ("c4a", "Google Axion"),
            ("t2a", "Ampere Altra"),
        ),
    },
    "sve": {
        "arch": "arm64",
        "cpuinfo_flags": ("asimd", "sve"),
        "gce_platforms": (
            ("c4a", "Google Axion"),
        ),
    },
    "sve2": {
        "arch": "arm64",
        "cpuinfo_flags": ("asimd", "sve", "sve2"),
        "gce_platforms": (
            ("c4a", "Google Axion"),
        ),
    },
}

PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*(?:=[0-9A-Za-z.+:~-]+)?$")
SYSCTL_KEY_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_-]+)+$")
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
MODE_RE = re.compile(r"^[0-7]{3,4}$")
FORBIDDEN_MOUNT_SOURCES = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/gha-admission",
    "/var/lib/gha-admission",
    "/etc",
    "/root",
    "/proc",
    "/sys",
    "/dev",
)


@dataclass(frozen=True)
class PatchFileSpec:
    path: str
    content: str
    mode: str = "0644"
    owner: str = "root:root"


@dataclass(frozen=True)
class PatchMountSpec:
    source: str
    target: str
    read_only: bool = False


@dataclass(frozen=True)
class HardwarePatch:
    arch: str = "x86_64"
    isa: tuple[str, ...] = ()
    extra_labels: tuple[str, ...] = ()
    min_vcpu: int | None = None
    min_ram_gib: int | None = None
    qualify_command: str = ""


@dataclass(frozen=True)
class HostPatch:
    apt_packages: tuple[str, ...] = ()
    sysctl: dict[str, str] = field(default_factory=dict)
    files: tuple[PatchFileSpec, ...] = ()
    boot_mkdirs: tuple[str, ...] = ()
    boot_script: str = ""


@dataclass(frozen=True)
class ContainerPatch:
    apt_packages: tuple[str, ...] = ()
    sysctl: dict[str, str] = field(default_factory=dict)
    mounts: tuple[PatchMountSpec, ...] = ()
    files: tuple[PatchFileSpec, ...] = ()
    dockerfile_run: str = ""


@dataclass(frozen=True)
class RunnerPatch:
    schema: str = SCHEMA_VERSION
    repository: str = ""
    source_commit: str = ""
    hardware: HardwarePatch = field(default_factory=HardwarePatch)
    host: HostPatch = field(default_factory=HostPatch)
    container: ContainerPatch = field(default_factory=ContainerPatch)

    @property
    def is_empty(self) -> bool:
        return (
            self.hardware == HardwarePatch()
            and self.host == HostPatch()
            and self.container == ContainerPatch()
        )

    @property
    def has_container_image_changes(self) -> bool:
        return bool(
            self.container.apt_packages
            or self.container.files
            or self.container.dockerfile_run.strip()
        )

    @property
    def has_host_changes(self) -> bool:
        return bool(
            self.hardware.isa
            or self.hardware.qualify_command.strip()
            or self.host.apt_packages
            or self.host.sysctl
            or self.host.files
            or self.host.boot_mkdirs
            or self.host.boot_script.strip()
            or self.container.mounts
        )

    def to_canonical_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("source_commit", None)
        return payload

    @property
    def patch_sha256(self) -> str:
        raw = json.dumps(self.to_canonical_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def required_cpuinfo_flags(self) -> tuple[str, ...]:
        flags: list[str] = []
        for feature in self.hardware.isa:
            for flag in ISA_CAPABILITIES[feature]["cpuinfo_flags"]:
                if flag not in flags:
                    flags.append(flag)
        return tuple(flags)

    def allowed_gce_platforms(self) -> set[tuple[str, str]] | None:
        if not self.hardware.isa:
            return None
        allowed: set[tuple[str, str]] | None = None
        for feature in self.hardware.isa:
            feature_set = set(ISA_CAPABILITIES[feature]["gce_platforms"])
            allowed = feature_set if allowed is None else (allowed & feature_set)
        return allowed or set()

    def runner_labels(self) -> tuple[str, ...]:
        labels: list[str] = []
        for item in (*self.hardware.isa, *self.hardware.extra_labels):
            if item and item not in labels:
                labels.append(item)
        return tuple(labels)


def _parse_scalar(text: str) -> Any:
    val = text.strip()
    if not val:
        return ""
    if val in ("true", "True"):
        return True
    if val in ("false", "False"):
        return False
    if val in ("null", "None", "~"):
        return None
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        if val.startswith('"'):
            return json.loads(val)
        return val[1:-1]
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in inner.split(",")]
    if val.startswith("{") and val.endswith("}"):
        inner = val[1:-1].strip()
        if not inner:
            return {}
        out: dict[str, Any] = {}
        for part in inner.split(","):
            k, v = part.split(":", 1)
            out[str(_parse_scalar(k))] = _parse_scalar(v)
        return out
    if re.fullmatch(r"-?[0-9]+", val):
        return int(val)
    return val


def _strip_yaml_comment(line: str) -> str:
    in_single = False
    in_double = False
    for idx, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx].rstrip()
    return line.rstrip()


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse JSON or the YAML subset used by spot-runner-patch-v1 using only stdlib."""
    stripped = text.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        loaded = json.loads(stripped)
        if not isinstance(loaded, dict):
            raise ValueError("Manifest root must be a mapping")
        return loaded

    raw_lines = text.splitlines()
    idx = 0
    n = len(raw_lines)

    def next_nonempty(start: int) -> tuple[int, int, str] | None:
        i = start
        while i < n:
            cleaned = _strip_yaml_comment(raw_lines[i])
            if cleaned.strip() and cleaned.strip() != "---":
                indent = len(cleaned) - len(cleaned.lstrip(" "))
                return i, indent, cleaned.strip()
            i += 1
        return None

    def parse_block(start: int, base_indent: int) -> tuple[Any, int]:
        peek = next_nonempty(start)
        if peek is None or peek[1] < base_indent:
            return {}, start
        if peek[2].startswith("- ") or peek[2] == "-":
            items: list[Any] = []
            i = start
            while True:
                nxt = next_nonempty(i)
                if nxt is None or nxt[1] < base_indent:
                    break
                line_idx, indent, content = nxt
                if indent != base_indent or not (content.startswith("- ") or content == "-"):
                    break
                after_dash = content[1:].strip()
                i = line_idx + 1
                if not after_dash:
                    sub_peek = next_nonempty(i)
                    if sub_peek and sub_peek[1] > indent:
                        val, i = parse_block(i, sub_peek[1])
                        items.append(val)
                    else:
                        items.append(None)
                elif ":" in after_dash and not after_dash.startswith(('"', "'")):
                    k, v_str = after_dash.split(":", 1)
                    item_dict: dict[str, Any] = {}
                    v_str = v_str.strip()
                    if v_str in ("|", "|-"):
                        block_lines: list[str] = []
                        block_indent: int | None = None
                        while i < n:
                            raw = raw_lines[i]
                            if not raw.strip():
                                block_lines.append("")
                                i += 1
                                continue
                            cur_indent = len(raw) - len(raw.lstrip(" "))
                            if block_indent is None:
                                if cur_indent <= indent:
                                    break
                                block_indent = cur_indent
                            if cur_indent < block_indent:
                                break
                            block_lines.append(raw[block_indent:])
                            i += 1
                        while block_lines and block_lines[-1] == "":
                            block_lines.pop()
                        joined = "\n".join(block_lines)
                        item_dict[k.strip()] = joined if v_str == "|-" else (joined + "\n" if joined else "")
                    elif v_str:
                        item_dict[k.strip()] = _parse_scalar(v_str)
                    else:
                        sub_peek = next_nonempty(i)
                        if sub_peek and sub_peek[1] > indent:
                            val, i = parse_block(i, sub_peek[1])
                            item_dict[k.strip()] = val
                        else:
                            item_dict[k.strip()] = None
                    while True:
                        sub_peek = next_nonempty(i)
                        if sub_peek is None or sub_peek[1] <= indent:
                            break
                        sub_map, i = parse_block(i, sub_peek[1])
                        if isinstance(sub_map, dict):
                            item_dict.update(sub_map)
                        else:
                            break
                    items.append(item_dict)
                else:
                    items.append(_parse_scalar(after_dash))
            return items, i

        mapping: dict[str, Any] = {}
        i = start
        while True:
            nxt = next_nonempty(i)
            if nxt is None or nxt[1] < base_indent:
                break
            line_idx, indent, content = nxt
            if indent != base_indent or content.startswith("-"):
                break
            if ":" not in content:
                raise ValueError(f"Invalid YAML line {line_idx + 1}: {raw_lines[line_idx]}")
            key, val_part = content.split(":", 1)
            key = str(_parse_scalar(key.strip()))
            val_part = val_part.strip()
            i = line_idx + 1
            if val_part in ("|", "|-"):
                block_lines = []
                block_indent = None
                while i < n:
                    raw = raw_lines[i]
                    if not raw.strip():
                        block_lines.append("")
                        i += 1
                        continue
                    cur_indent = len(raw) - len(raw.lstrip(" "))
                    if block_indent is None:
                        if cur_indent <= indent:
                            break
                        block_indent = cur_indent
                    if cur_indent < block_indent:
                        break
                    block_lines.append(raw[block_indent:])
                    i += 1
                while block_lines and block_lines[-1] == "":
                    block_lines.pop()
                joined = "\n".join(block_lines)
                mapping[key] = joined if val_part == "|-" else (joined + "\n" if joined else "")
            elif val_part:
                mapping[key] = _parse_scalar(val_part)
            else:
                sub_peek = next_nonempty(i)
                if sub_peek and sub_peek[1] > indent:
                    val, i = parse_block(i, sub_peek[1])
                    mapping[key] = val
                else:
                    mapping[key] = None
        return mapping, i

    first = next_nonempty(0)
    if first is None:
        return {}
    result, _ = parse_block(0, first[1])
    if not isinstance(result, dict):
        raise ValueError("Manifest root must be a key-value mapping")
    return result


def parse_manifest_text(text: str) -> dict[str, Any]:
    """Parse JSON, YAML, or TOML manifest text into a dictionary."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("Runner patch manifest is empty")
    return _parse_simple_yaml(stripped)


def _is_valid_abs_path(p: Any) -> bool:
    if not isinstance(p, str) or not p.startswith("/") or p == "/" or "//" in p:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in p):
        return False
    return posixpath.normpath(p) == p


def _validate_packages(packages: Any, section: str) -> tuple[str, ...]:
    if packages is None:
        return ()
    if not isinstance(packages, (list, tuple)):
        raise ValueError(f"{section}.apt_packages must be a list of package names")
    out: list[str] = []
    for pkg in packages:
        if not isinstance(pkg, str) or not PACKAGE_RE.fullmatch(pkg):
            raise ValueError(f"Invalid apt package name in {section}.apt_packages: {pkg!r}")
        if pkg not in out:
            out.append(pkg)
    return tuple(out)


def _validate_sysctl(sysctl_map: Any, section: str) -> dict[str, str]:
    if sysctl_map is None:
        return {}
    if not isinstance(sysctl_map, dict):
        raise ValueError(f"{section}.sysctl must be a mapping")
    out: dict[str, str] = {}
    for key, value in sorted(sysctl_map.items()):
        if not isinstance(key, str) or not SYSCTL_KEY_RE.fullmatch(key):
            raise ValueError(f"Invalid sysctl key in {section}.sysctl: {key!r}")
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ValueError(f"Invalid sysctl value for {key} in {section}.sysctl: {value!r}")
        val_str = str(value).strip()
        if not val_str or "\n" in val_str or "\r" in val_str:
            raise ValueError(f"Invalid sysctl value for {key} in {section}.sysctl")
        out[key] = val_str
    return out


def _validate_files(files_list: Any, section: str) -> tuple[PatchFileSpec, ...]:
    if files_list is None:
        return ()
    if not isinstance(files_list, (list, tuple)):
        raise ValueError(f"{section}.files must be a list")
    out: list[PatchFileSpec] = []
    seen_paths: set[str] = set()
    for item in files_list:
        if not isinstance(item, dict):
            raise ValueError(f"Each entry in {section}.files must be a mapping")
        path = item.get("path")
        content = item.get("content", "")
        mode = str(item.get("mode", "0644"))
        owner = str(item.get("owner", "root:root"))
        if not _is_valid_abs_path(path):
            raise ValueError(f"Invalid absolute file path in {section}.files: {path!r}")
        if path in seen_paths:
            raise ValueError(f"Duplicate file path in {section}.files: {path}")
        seen_paths.add(path)
        if not isinstance(content, str):
            raise ValueError(f"Content for {path} in {section}.files must be a string")
        if not MODE_RE.fullmatch(mode):
            raise ValueError(f"Invalid file mode {mode!r} for {path} in {section}.files")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+:[a-zA-Z0-9_.-]+", owner):
            raise ValueError(f"Invalid owner {owner!r} for {path} in {section}.files")
        out.append(PatchFileSpec(path=path, content=content, mode=mode, owner=owner))
    return tuple(out)


def _validate_mounts(mounts_list: Any) -> tuple[PatchMountSpec, ...]:
    if mounts_list is None:
        return ()
    if not isinstance(mounts_list, (list, tuple)):
        raise ValueError("container.mounts must be a list")
    out: list[PatchMountSpec] = []
    seen_targets: set[str] = set()
    for item in mounts_list:
        if not isinstance(item, dict):
            raise ValueError("Each entry in container.mounts must be a mapping")
        source = item.get("source")
        target = item.get("target")
        read_only = bool(item.get("read_only", False))
        for label, p in (("source", source), ("target", target)):
            if not _is_valid_abs_path(p) or ":" in p:
                raise ValueError(f"Invalid mount {label} path in container.mounts: {p!r}")
        for forbidden in FORBIDDEN_MOUNT_SOURCES:
            if source == forbidden or source.startswith(forbidden + "/") or forbidden.startswith(source + "/"):
                raise ValueError(f"Mount source {source!r} is forbidden")
        if target in seen_targets:
            raise ValueError(f"Duplicate mount target in container.mounts: {target}")
        seen_targets.add(target)
        out.append(PatchMountSpec(source=source, target=target, read_only=read_only))
    return tuple(out)


def validate_patch_dict(data: dict[str, Any], *, source_commit: str = "") -> RunnerPatch:
    """Validate a parsed dictionary against spot-runner-patch-v1 and return a RunnerPatch."""
    if not isinstance(data, dict):
        raise ValueError("Runner patch manifest must be a mapping")
    schema = data.get("schema", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise ValueError(f"Unsupported runner patch schema {schema!r}; expected {SCHEMA_VERSION!r}")
    repository = data.get("repository", "") or ""
    if repository and (not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)):
        raise ValueError(f"Invalid repository identifier: {repository!r}")

    hw_raw = data.get("hardware") or {}
    if not isinstance(hw_raw, dict):
        raise ValueError("hardware section must be a mapping")
    arch = str(hw_raw.get("arch", "x86_64")).lower()
    if arch in ("x64", "amd64"):
        arch = "x86_64"
    elif arch == "aarch64":
        arch = "arm64"
    if arch not in ("x86_64", "arm64"):
        raise ValueError(f"Unsupported hardware.arch {arch!r}; expected 'x86_64' or 'arm64'")

    isa_raw = hw_raw.get("isa") or []
    if not isinstance(isa_raw, (list, tuple)):
        raise ValueError("hardware.isa must be a list of instruction set identifiers")
    isa_list: list[str] = []
    for item in isa_raw:
        norm = str(item).strip().lower().replace("_", "-")
        if norm == "avx-512":
            norm = "avx512"
        if norm not in ISA_CAPABILITIES:
            raise ValueError(f"Unsupported instruction set {item!r} in hardware.isa; supported: {sorted(ISA_CAPABILITIES)}")
        expected_arch = ISA_CAPABILITIES[norm]["arch"]
        if expected_arch != arch:
            raise ValueError(f"Instruction set {norm!r} requires arch {expected_arch!r}, but hardware.arch is {arch!r}")
        if norm not in isa_list:
            isa_list.append(norm)

    extra_labels_raw = hw_raw.get("extra_labels") or []
    if not isinstance(extra_labels_raw, (list, tuple)):
        raise ValueError("hardware.extra_labels must be a list")
    extra_labels: list[str] = []
    for label in extra_labels_raw:
        if not isinstance(label, str) or not LABEL_RE.fullmatch(label):
            raise ValueError(f"Invalid runner label in hardware.extra_labels: {label!r}")
        if label not in extra_labels:
            extra_labels.append(label)

    min_vcpu = hw_raw.get("min_vcpu")
    if min_vcpu is not None and (not isinstance(min_vcpu, int) or isinstance(min_vcpu, bool) or min_vcpu < 1):
        raise ValueError("hardware.min_vcpu must be a positive integer")
    min_ram_gib = hw_raw.get("min_ram_gib")
    if min_ram_gib is not None and (not isinstance(min_ram_gib, int) or isinstance(min_ram_gib, bool) or min_ram_gib < 1):
        raise ValueError("hardware.min_ram_gib must be a positive integer")
    qualify_command = str(hw_raw.get("qualify_command") or "")

    host_raw = data.get("host") or {}
    if not isinstance(host_raw, dict):
        raise ValueError("host section must be a mapping")
    boot_mkdirs_raw = host_raw.get("boot_mkdirs") or []
    if not isinstance(boot_mkdirs_raw, (list, tuple)):
        raise ValueError("host.boot_mkdirs must be a list")
    boot_mkdirs: list[str] = []
    for d in boot_mkdirs_raw:
        if not _is_valid_abs_path(d):
            raise ValueError(f"Invalid directory path in host.boot_mkdirs: {d!r}")
        if d not in boot_mkdirs:
            boot_mkdirs.append(d)
    boot_script = str(host_raw.get("boot_script") or "")

    container_raw = data.get("container") or {}
    if not isinstance(container_raw, dict):
        raise ValueError("container section must be a mapping")
    dockerfile_run = str(container_raw.get("dockerfile_run") or "")

    patch = RunnerPatch(
        schema=SCHEMA_VERSION,
        repository=repository,
        source_commit=source_commit,
        hardware=HardwarePatch(
            arch=arch,
            isa=tuple(isa_list),
            extra_labels=tuple(extra_labels),
            min_vcpu=min_vcpu,
            min_ram_gib=min_ram_gib,
            qualify_command=qualify_command,
        ),
        host=HostPatch(
            apt_packages=_validate_packages(host_raw.get("apt_packages"), "host"),
            sysctl=_validate_sysctl(host_raw.get("sysctl"), "host"),
            files=_validate_files(host_raw.get("files"), "host"),
            boot_mkdirs=tuple(boot_mkdirs),
            boot_script=boot_script,
        ),
        container=ContainerPatch(
            apt_packages=_validate_packages(container_raw.get("apt_packages"), "container"),
            sysctl=_validate_sysctl(container_raw.get("sysctl"), "container"),
            mounts=_validate_mounts(container_raw.get("mounts")),
            files=_validate_files(container_raw.get("files"), "container"),
            dockerfile_run=dockerfile_run,
        ),
    )
    allowed_platforms = patch.allowed_gce_platforms()
    if allowed_platforms is not None and not allowed_platforms:
        raise ValueError(f"No GCE CPU platform satisfies combined hardware.isa requirements: {list(patch.hardware.isa)}")
    return patch


def merge_patch_dicts(defaults: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge an organization defaults.yaml dict with a repository-specific patch dict."""
    if not defaults:
        return dict(override)
    merged: dict[str, Any] = {
        "schema": override.get("schema", defaults.get("schema", SCHEMA_VERSION)),
        "repository": override.get("repository", defaults.get("repository", "")),
    }
    hw_def = defaults.get("hardware") or {}
    hw_ovr = override.get("hardware") or {}
    merged["hardware"] = {
        "arch": hw_ovr.get("arch", hw_def.get("arch", "x86_64")),
        "isa": list(dict.fromkeys([*(hw_def.get("isa") or []), *(hw_ovr.get("isa") or [])])),
        "extra_labels": list(dict.fromkeys([*(hw_def.get("extra_labels") or []), *(hw_ovr.get("extra_labels") or [])])),
        "min_vcpu": hw_ovr.get("min_vcpu", hw_def.get("min_vcpu")),
        "min_ram_gib": hw_ovr.get("min_ram_gib", hw_def.get("min_ram_gib")),
        "qualify_command": hw_ovr.get("qualify_command") or hw_def.get("qualify_command") or "",
    }
    host_def = defaults.get("host") or {}
    host_ovr = override.get("host") or {}
    host_files = {f["path"]: f for f in (host_def.get("files") or []) if isinstance(f, dict) and "path" in f}
    for f in (host_ovr.get("files") or []):
        if isinstance(f, dict) and "path" in f:
            host_files[f["path"]] = f
    host_scripts = [s for s in (host_def.get("boot_script", ""), host_ovr.get("boot_script", "")) if s and s.strip()]
    merged["host"] = {
        "apt_packages": list(dict.fromkeys([*(host_def.get("apt_packages") or []), *(host_ovr.get("apt_packages") or [])])),
        "sysctl": {**(host_def.get("sysctl") or {}), **(host_ovr.get("sysctl") or {})},
        "files": list(host_files.values()),
        "boot_mkdirs": list(dict.fromkeys([*(host_def.get("boot_mkdirs") or []), *(host_ovr.get("boot_mkdirs") or [])])),
        "boot_script": "\n".join(host_scripts),
    }
    cnt_def = defaults.get("container") or {}
    cnt_ovr = override.get("container") or {}
    cnt_files = {f["path"]: f for f in (cnt_def.get("files") or []) if isinstance(f, dict) and "path" in f}
    for f in (cnt_ovr.get("files") or []):
        if isinstance(f, dict) and "path" in f:
            cnt_files[f["path"]] = f
    cnt_mounts = {m["target"]: m for m in (cnt_def.get("mounts") or []) if isinstance(m, dict) and "target" in m}
    for m in (cnt_ovr.get("mounts") or []):
        if isinstance(m, dict) and "target" in m:
            cnt_mounts[m["target"]] = m
    cnt_runs = [s for s in (cnt_def.get("dockerfile_run", ""), cnt_ovr.get("dockerfile_run", "")) if s and s.strip()]
    merged["container"] = {
        "apt_packages": list(dict.fromkeys([*(cnt_def.get("apt_packages") or []), *(cnt_ovr.get("apt_packages") or [])])),
        "sysctl": {**(cnt_def.get("sysctl") or {}), **(cnt_ovr.get("sysctl") or {})},
        "mounts": list(cnt_mounts.values()),
        "files": list(cnt_files.values()),
        "dockerfile_run": "\n".join(cnt_runs),
    }
    return merged


def load_patch_from_file(path: Path) -> RunnerPatch:
    """Load and validate a RunnerPatch from a local YAML/JSON manifest file."""
    raw = path.read_text(encoding="utf-8")
    return validate_patch_dict(parse_manifest_text(raw))


def _gh_api_json(endpoint: str, *, env: dict[str, str] | None = None) -> Any:
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    token = run_env.get("SPOT_CONFIG_GH_TOKEN") or run_env.get("GH_TOKEN")
    if token:
        run_env["GH_TOKEN"] = token
    result = subprocess.run(
        ["gh", "api", endpoint],
        text=True,
        capture_output=True,
        env=run_env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {endpoint} failed ({result.returncode}): {(result.stderr or result.stdout).strip()}")
    return json.loads(result.stdout)


def resolve_repo_default_branch(repo_full_name: str, *, env: dict[str, str] | None = None) -> tuple[str, str]:
    """Query GitHub settings for the repository's default_branch and its current HEAD SHA."""
    meta = _gh_api_json(f"repos/{repo_full_name}", env=env)
    default_branch = meta.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch.strip():
        raise RuntimeError(f"Repository {repo_full_name} does not report a valid default_branch")
    commit = _gh_api_json(f"repos/{repo_full_name}/commits/{default_branch}", env=env)
    sha = commit.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError(f"Could not resolve HEAD commit SHA for {repo_full_name}@{default_branch}")
    return default_branch, sha


def _fetch_repo_file_at_ref(repo_full_name: str, file_path: str, ref: str, *, env: dict[str, str] | None = None) -> str | None:
    try:
        payload = _gh_api_json(f"repos/{repo_full_name}/contents/{file_path}?ref={ref}", env=env)
    except RuntimeError as exc:
        if "404" in str(exc) or "Not Found" in str(exc):
            return None
        raise
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        raise RuntimeError(f"Unexpected content payload for {repo_full_name}:{file_path}@{ref}")
    return base64.b64decode(payload.get("content", "")).decode("utf-8")


def fetch_patch_from_config_repo(
    target_repository: str,
    *,
    config_repo: str | None = None,
    org: str = "swack-tools",
    env: dict[str, str] | None = None,
) -> RunnerPatch:
    """Fetch and validate a repository's patch from <org>/spot-runners-config on its default branch."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", target_repository):
        raise ValueError(f"target_repository must be in OWNER/REPO format, got {target_repository!r}")
    resolved_config_repo = config_repo or f"{org}/{DEFAULT_CONFIG_REPO_NAME}"
    _, commit_sha = resolve_repo_default_branch(resolved_config_repo, env=env)

    defaults_text = _fetch_repo_file_at_ref(resolved_config_repo, "defaults.yaml", commit_sha, env=env)
    defaults_dict = parse_manifest_text(defaults_text) if defaults_text else {}

    repo_path = f"repos/{target_repository}.yaml"
    repo_text = _fetch_repo_file_at_ref(resolved_config_repo, repo_path, commit_sha, env=env)
    if repo_text is None:
        raise RuntimeError(f"No runner patch manifest found at {resolved_config_repo}:{repo_path}@{commit_sha}")
    repo_dict = parse_manifest_text(repo_text)
    merged = merge_patch_dicts(defaults_dict, repo_dict)
    if not merged.get("repository"):
        merged["repository"] = target_repository
    return validate_patch_dict(merged, source_commit=commit_sha)


def render_dockerfile(base_dockerfile: str, patch: RunnerPatch | None) -> str:
    """Inject container-level customizations into the base runner Dockerfile."""
    if patch is None or not patch.has_container_image_changes:
        return base_dockerfile

    commands: list[str] = []
    if patch.container.apt_packages:
        pkg_str = " ".join(shlex.quote(p) for p in patch.container.apt_packages)
        commands.append(
            f"apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {pkg_str} && rm -rf /var/lib/apt/lists/*"
        )
    for file_spec in patch.container.files:
        b64 = base64.b64encode(file_spec.content.encode("utf-8")).decode("ascii")
        parent = str(Path(file_spec.path).parent)
        commands.append(
            f"install -d -m 0755 {shlex.quote(parent)} && "
            f"printf '%s' '{b64}' | base64 -d >{shlex.quote(file_spec.path)} && "
            f"chown {shlex.quote(file_spec.owner)} {shlex.quote(file_spec.path)} && "
            f"chmod {shlex.quote(file_spec.mode)} {shlex.quote(file_spec.path)}"
        )
    if patch.container.dockerfile_run.strip():
        b64_run = base64.b64encode(patch.container.dockerfile_run.encode("utf-8")).decode("ascii")
        commands.append(f"printf '%s' '{b64_run}' | base64 -d | bash -euo pipefail")

    joined = " \\\n    && ".join(commands)
    layer = f"# Repo patch {patch.patch_sha256[:12]}\nRUN {joined}\n"

    anchor = "USER runner\nENV CARGO_HOME=/work/.cargo\n"
    if anchor not in base_dockerfile:
        raise RuntimeError("Base runner Dockerfile is missing the expected USER runner anchor")
    return base_dockerfile.replace(anchor, layer + anchor, 1)


def render_host_patch_script(patch: RunnerPatch | None) -> str:
    """Render an idempotent host-level bash script for startup.sh and live hot-patching."""
    if patch is None or patch.is_empty:
        return ""

    lines = [
        "#!/usr/bin/env bash",
        "set -Eeuo pipefail",
        f"# Repo runner patch: repo={patch.repository or 'unspecified'} sha256={patch.patch_sha256}",
    ]

    expected_uname = "aarch64" if patch.hardware.arch == "arm64" else "x86_64"
    lines.append(f'actual_arch="$(uname -m)"')
    lines.append(
        f'[[ "$actual_arch" == "{expected_uname}" ]] || {{ echo "Host architecture $actual_arch does not match required {expected_uname}" >&2; exit 1; }}'
    )

    req_flags = patch.required_cpuinfo_flags()
    if req_flags:
        for flag in req_flags:
            lines.append(
                f"grep -Eiwq {shlex.quote(flag)} /proc/cpuinfo || {{ echo 'Host CPU is missing required instruction set flag: {flag}' >&2; exit 1; }}"
            )

    for directory in patch.host.boot_mkdirs:
        lines.append(f"install -d -m 0755 {shlex.quote(directory)}")

    for mount in patch.container.mounts:
        lines.append(f"install -d -m 0755 {shlex.quote(mount.source)}")

    if patch.host.sysctl:
        sysctl_body = "".join(f"{k} = {v}\n" for k, v in sorted(patch.host.sysctl.items()))
        sysctl_b64 = base64.b64encode(sysctl_body.encode("utf-8")).decode("ascii")
        lines.append(f"printf '%s' '{sysctl_b64}' | base64 -d >/etc/sysctl.d/99-spot-runner-patch.conf")
        lines.append("chmod 0644 /etc/sysctl.d/99-spot-runner-patch.conf")
        lines.append("sysctl --system >/dev/null")

    for file_spec in patch.host.files:
        b64 = base64.b64encode(file_spec.content.encode("utf-8")).decode("ascii")
        parent = str(Path(file_spec.path).parent)
        lines.append(f"install -d -m 0755 {shlex.quote(parent)}")
        lines.append(f"printf '%s' '{b64}' | base64 -d >{shlex.quote(file_spec.path)}")
        lines.append(f"chown {shlex.quote(file_spec.owner)} {shlex.quote(file_spec.path)}")
        lines.append(f"chmod {shlex.quote(file_spec.mode)} {shlex.quote(file_spec.path)}")

    if patch.host.apt_packages:
        pkgs = " ".join(shlex.quote(p) for p in patch.host.apt_packages)
        lines.append(f"apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {pkgs}")
        lines.append("apt-get clean && rm -rf /var/lib/apt/lists/*")

    if patch.host.boot_script.strip():
        boot_b64 = base64.b64encode(patch.host.boot_script.encode("utf-8")).decode("ascii")
        lines.append(f"printf '%s' '{boot_b64}' | base64 -d | bash -euo pipefail")

    if patch.hardware.qualify_command.strip():
        qual_b64 = base64.b64encode(patch.hardware.qualify_command.encode("utf-8")).decode("ascii")
        lines.append(f"printf '%s' '{qual_b64}' | base64 -d | bash -euo pipefail")

    receipt = {
        "schema": patch.schema,
        "repository": patch.repository,
        "source_commit": patch.source_commit,
        "patch_sha256": patch.patch_sha256,
        "manifest": patch.to_canonical_dict(),
    }
    receipt_b64 = base64.b64encode(json.dumps(receipt, sort_keys=True).encode("utf-8")).decode("ascii")
    lines.append("install -d -m 0755 /var/lib/gha-runner-patch")
    lines.append(f"printf '%s' '{receipt_b64}' | base64 -d >/var/lib/gha-runner-patch/state.json")
    lines.append("chmod 0644 /var/lib/gha-runner-patch/state.json")
    return "\n".join(lines) + "\n"


def render_docker_run_args(patch: RunnerPatch | None) -> list[str]:
    """Return additional docker run flags (--sysctl and -v mounts) for a RunnerPatch."""
    if patch is None or patch.is_empty:
        return []
    flags: list[str] = []
    for k, v in sorted(patch.container.sysctl.items()):
        flags.append(f"--sysctl={k}={v}")
    for mount in patch.container.mounts:
        spec = f"{mount.source}:{mount.target}"
        if mount.read_only:
            spec += ":ro"
        flags.extend(["-v", spec])
    return flags


def inject_startup_patch(startup_script: str, patch: RunnerPatch | None) -> str:
    """Inject host bootstrap script and container docker_args into a rendered startup.sh."""
    if patch is None or patch.is_empty:
        return startup_script

    host_script = render_host_patch_script(patch)
    if host_script:
        host_b64 = base64.b64encode(host_script.encode("utf-8")).decode("ascii")
        host_hook = (
            f"printf '%s' '{host_b64}' | base64 -d >/opt/gha-runners/repo-host-patch.sh\n"
            "chmod 0755 /opt/gha-runners/repo-host-patch.sh\n"
            "/opt/gha-runners/repo-host-patch.sh\n"
        )
        build_anchor = "chmod 0755 /opt/gha-runners/build/entrypoint.sh\n"
        if build_anchor not in startup_script:
            raise RuntimeError("Startup script is missing build entrypoint anchor for host patch injection")
        startup_script = startup_script.replace(build_anchor, host_hook + build_anchor, 1)

    extra_docker_flags = render_docker_run_args(patch)
    if extra_docker_flags:
        quoted = " ".join(shlex.quote(flag) for flag in extra_docker_flags)
        mount_anchor = '  docker_args+=(--mount "type=volume,src=$RUST_VOLUME,dst=/opt/runner-rust,readonly")\n'
        if mount_anchor not in startup_script:
            raise RuntimeError("Startup script is missing rust volume mount anchor for docker_args injection")
        startup_script = startup_script.replace(
            mount_anchor,
            mount_anchor + f"  docker_args+=({quoted})\n",
            1,
        )
    return startup_script


def classify_patch_diff(current_manifest: dict[str, Any] | None, target: RunnerPatch) -> str:
    """Classify the transition from a deployed manifest to target into noop, hot_patch, recreate_containers, or replace_vm."""
    if current_manifest is not None:
        current = validate_patch_dict(current_manifest)
        if current.patch_sha256 == target.patch_sha256:
            return "noop"
        if current.hardware.arch != target.hardware.arch or set(target.hardware.isa) - set(current.hardware.isa):
            return "replace_vm"
        if current.container.mounts != target.container.mounts:
            return "recreate_containers"
        return "hot_patch"

    if target.hardware.arch != "x86_64" or target.hardware.isa:
        return "replace_vm"
    if target.container.mounts:
        return "recreate_containers"
    return "hot_patch"


def render_live_hot_patch_script(
    patch: RunnerPatch,
    *,
    base_dockerfile: str,
    runner_version: str = "2.337.0",
    runner_prefix: str = "",
) -> str:
    """Build a self-contained root shell script that hot-patches a running VM and its active runner containers."""
    host_script = render_host_patch_script(patch)
    updated_dockerfile = render_dockerfile(base_dockerfile, patch)
    dockerfile_b64 = base64.b64encode(updated_dockerfile.encode("utf-8")).decode("ascii")

    container_cmds: list[str] = []
    if patch.container.apt_packages:
        pkgs = " ".join(shlex.quote(p) for p in patch.container.apt_packages)
        container_cmds.append(
            f"apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {pkgs} && rm -rf /var/lib/apt/lists/*"
        )
    for file_spec in patch.container.files:
        b64 = base64.b64encode(file_spec.content.encode("utf-8")).decode("ascii")
        parent = str(Path(file_spec.path).parent)
        container_cmds.append(
            f"install -d -m 0755 {shlex.quote(parent)} && "
            f"printf '%s' '{b64}' | base64 -d >{shlex.quote(file_spec.path)} && "
            f"chown {shlex.quote(file_spec.owner)} {shlex.quote(file_spec.path)} && "
            f"chmod {shlex.quote(file_spec.mode)} {shlex.quote(file_spec.path)}"
        )
    if patch.container.dockerfile_run.strip():
        run_b64 = base64.b64encode(patch.container.dockerfile_run.encode("utf-8")).decode("ascii")
        container_cmds.append(f"printf '%s' '{run_b64}' | base64 -d | bash -euo pipefail")

    container_exec_b64 = base64.b64encode("\n".join(container_cmds).encode("utf-8")).decode("ascii") if container_cmds else ""

    lines = [
        "#!/usr/bin/env bash",
        "set -Eeuo pipefail",
        host_script.strip(),
        f"if [[ -d /opt/gha-runners/build ]]; then",
        f"  printf '%s' '{dockerfile_b64}' | base64 -d >/opt/gha-runners/build/Dockerfile",
        f"  docker build --pull -t oxidex-self-hosted-runner:{shlex.quote(runner_version)} /opt/gha-runners/build",
        "fi",
        f"RUNNER_FILTER={shlex.quote(runner_prefix)}",
        'for cid in $(docker ps --format "{{.Names}}"); do',
        '  if [[ -n "$RUNNER_FILTER" && "$cid" != "$RUNNER_FILTER"* ]]; then continue; fi',
    ]
    if container_exec_b64:
        lines.append(
            f"  printf '%s' '{container_exec_b64}' | base64 -d | docker exec -i -u root \"$cid\" bash -euo pipefail"
        )
    for k, v in sorted(patch.container.sysctl.items()):
        lines.append('  pid="$(docker inspect -f "{{.State.Pid}}" "$cid")"' )
        lines.append(f'  nsenter -t "$pid" -n sysctl -w {shlex.quote(f"{k}={v}")} >/dev/null')
    lines.extend([
        "done",
        f"echo 'LIVE_PATCH_APPLIED:{patch.patch_sha256}'",
    ])
    return "\n".join(lines) + "\n"

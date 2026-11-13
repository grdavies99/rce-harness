"""`rce`: start, list, sideload to, exit and stop Roku Cloud Emulator devices.

Data goes to stdout; progress, warnings and errors go to stderr.
Exit codes: 0 success, 1 API/runtime failure, 2 usage/config error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import CONFIG_NAME, FIELDS, LOCAL_NAME, ConfigError, RCEConfig
from .core import ACTIVE, PENDING, RUNNING, SHUTDOWN, CoreClient, RCEError
from .device import DEV_PASSWORD_HINT, DeviceClient, Key, SideloadError
from .packaging import zip_channel

API_DOCS = "https://docs.rce.roku.com/core-api/index.html#/"

CONFIG_TEMPLATE = f"""\
# rce.toml (commit this file). Secrets go in rce.local.toml.
# API reference: {API_DOCS}

[device]
id = 74                 # RCE device id (find it with `rce list --all`)
snapshot = "live"       # snapshot name, numeric id, "live" (state at last stop) or "base" (factory reset; see `rce snapshots`)
firmware = "prod"       # exact firmware_version_id, or a substring of id/display name (`rce firmware`); empty = first listed
max_runtime = 1800      # seconds; RCE force-stops the emulator after this (cost cap). Org max: `rce config --check`

[channel]
path = "."              # .zip file or channel source directory, relative to this file
exclude = [".git", ".github", "node_modules", "out", "*.zip", "rce.toml", "rce.local.toml", ".DS_Store"]

# [api]                 # optional overrides
# core_url = "https://api.rce.roku.com"
# device_url = "https://device.rce.roku.com"   # only used if a running device has no instance_api_url
"""

LOCAL_TEMPLATE = """\
# rce.local.toml (gitignored; never commit)
token = ""         # Cloud Emulator → Token tab → Add token
dev_password = ""  # dev-mode password set on the virtual device
"""


# -- output helpers -------------------------------------------------------
def err(*parts: Any, end: str = "\n") -> None:
    print(*parts, file=sys.stderr, end=end, flush=True)


def print_table(headers: list[str], rows: list[list[Any]]) -> None:
    cells = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)]
    for r in [headers, *cells]:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def parse_time(value: str | None) -> datetime | None:
    """RFC 3339 -> aware datetime (fromisoformat can't take a trailing Z before 3.11)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def auto_stop_at(device: dict) -> datetime | None:
    """Approximate auto-stop: running_device.started_at (else created_at) + max_runtime."""
    rd = device.get("running_device") or {}
    start = parse_time(rd.get("started_at")) or parse_time(rd.get("created_at"))
    if start is None or not rd.get("max_runtime"):
        return None
    return start + timedelta(seconds=rd["max_runtime"])


def remaining(device: dict, now: datetime | None = None) -> str:
    stop = auto_stop_at(device) if device.get("status") == RUNNING else None
    if stop is None:
        return "-"
    secs = (stop - (now or datetime.now(timezone.utc))).total_seconds()
    return f"~{max(0, round(secs / 60))}m"


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    out = (f"{h}h" if h else "") + (f"{m}m" if m else "") + (f"{s}s" if s else "")
    return out or "0s"


def mask(value: str | None, secret: bool = False) -> str:
    if not value:
        return "(not set)"
    if secret or len(value) <= 8:
        return "****"
    return f"{value[:4]}…{value[-4:]}"


def fw_name(device: dict) -> str:
    return device.get("firmware_version_display_name") or device.get("firmware_version_id") or "-"


# -- commands -------------------------------------------------------------
def cmd_init(cfg: RCEConfig | None, args: argparse.Namespace) -> int:
    here = Path.cwd()
    cfg_file, local_file, gitignore = here / CONFIG_NAME, here / LOCAL_NAME, here / ".gitignore"
    if cfg_file.exists() and not args.force:
        raise ConfigError(f"{cfg_file} already exists (use --force to overwrite)")
    cfg_file.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    err(f"wrote {cfg_file}")

    if local_file.exists():
        err(f"kept existing {local_file}")  # never clobber a pasted token, even with --force
    else:
        local_file.write_text(LOCAL_TEMPLATE, encoding="utf-8")
        if os.name != "nt":
            local_file.chmod(0o600)
        err(f"wrote {local_file}")

    lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    if LOCAL_NAME not in (line.strip() for line in lines):
        text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        gitignore.write_text(text + LOCAL_NAME + "\n", encoding="utf-8")
        err(f"added {LOCAL_NAME} to {gitignore}")

    err("\nnext steps:\n"
        f"  1. paste your token into {LOCAL_NAME} (Cloud Emulator → Token tab → Add token)\n"
        "  2. rce config --check\n"
        "  3. rce list --all   # find your device id, then set [device].id in rce.toml")
    return 0


def cmd_config(cfg: RCEConfig, args: argparse.Namespace) -> int:
    if cfg.config_path:
        print(f"config: {cfg.config_path}")
    else:
        print(f"config: no {CONFIG_NAME} found (searched from {Path.cwd()} up)")
    width = max(len(n) for n in FIELDS)
    for name in FIELDS:
        value = getattr(cfg, name)
        if name == "token":
            shown = mask(value)
        elif name == "dev_password":
            shown = "****" if value else "(not set)"
        elif value is None:
            shown = "(not set)"
        elif isinstance(value, list):
            shown = ", ".join(value)
        else:
            shown = str(value)
        print(f"{name.ljust(width)} = {shown} ({cfg.sources.get(name, 'default')})")

    if args.check:
        with CoreClient(cfg) as core:
            me = core.whoami()
        org = me.get("organisation") or {}
        who = me.get("username") or me.get("email") or me.get("id")
        print(f"token ok: {who}, org {org.get('name', '?')}, max runtime {org.get('max_project_runtime', '?')}s")
    return 0


def cmd_firmware(cfg: RCEConfig, args: argparse.Namespace) -> int:
    with CoreClient(cfg) as core:
        versions = core.firmware_versions(args.type)
    if not versions:
        err("no firmware versions")
        return 0
    print_table(["ID", "TYPE", "NAME"],
                [[f["firmware_version_id"], f.get("device_type", "-"), f.get("display_name") or "-"]
                 for f in versions])
    return 0


def cmd_snapshots(cfg: RCEConfig, args: argparse.Namespace) -> int:
    device_id = cfg.require_device_id()
    with CoreClient(cfg) as core:
        snaps = core.snapshots(device_id)
    if not snaps:
        err(f"no snapshots on device {device_id}")
        return 0
    rows = []
    for s in snaps:
        flags = [f for f in ("base", "live") if s.get(f)] + ([] if s.get("ready", True) else ["not-ready"])
        created = parse_time(s.get("created_at"))
        rows.append([s.get("id"), s.get("name"), ",".join(flags) or "-",
                     s.get("firmware_version_display_name") or "-",
                     created.astimezone().strftime("%Y-%m-%d %H:%M") if created else "-"])
    print_table(["ID", "NAME", "FLAGS", "FIRMWARE", "CREATED"], rows)
    return 0


START_ERRORS: dict[int, Callable[[RCEError], str]] = {
    409: lambda e: ("snapshot is not ready yet (still uploading); try again shortly"
                    if e.code == "snapshot_not_ready"
                    else f"already running, locked by another operation, or snapshot not ready ({e.message})"),
    429: lambda e: ("trial accounts can run one device at a time; stop the other one (rce list)"
                    if e.code == "trial_device_run_limit" else e.message),
    402: lambda e: "free trial exhausted; a paid plan is required",
    400: lambda e: e.message + ("; your org's cap is shown by `rce config --check`"
                                if "max_runtime" in e.message or e.code == "trial_max_runtime_exceeded" else ""),
    503: lambda e: f"service temporarily unavailable; retry later ({e.message})",
}


def cmd_start(cfg: RCEConfig, args: argparse.Namespace) -> int:
    device_id = cfg.require_device_id()
    with CoreClient(cfg) as core:
        dev = core.get_device(device_id)
        status = dev.get("status")
        if status == RUNNING:
            rd = dev.get("running_device") or {}
            print(f"device {device_id} already running (instance {rd.get('instance_uuid', '?')})")
            return 0
        if status == PENDING:
            err(f"device {device_id} is already starting")
            if args.no_wait:
                return 0
        else:
            fw = core.pick_firmware(dev["device_type"], cfg.firmware)
            if not cfg.firmware:
                err(f"no firmware configured; using {fw.get('display_name') or fw['firmware_version_id']} "
                    f"({fw['firmware_version_id']})")
            snap = core.pick_snapshot(device_id, cfg.snapshot)
            err(f"starting device {device_id}: snapshot {snap.get('name')} (#{snap['id']}), "
                f"firmware {fw.get('display_name') or fw['firmware_version_id']}, "
                f"max runtime {fmt_duration(cfg.max_runtime)}")
            try:
                core.start_device(device_id, snap["id"], fw["firmware_version_id"], cfg.max_runtime)
            except RCEError as e:
                friendly = START_ERRORS.get(e.status)
                if friendly is None:
                    raise
                raise RCEError(e.status, friendly(e), e.code) from e
            if args.no_wait:
                print(f"starting {device_id} (not waiting; check with 'rce list')")
                return 0

        progress = _StatusLine()
        try:
            dev = core.wait_until_running(device_id, on_status=progress)
        except KeyboardInterrupt:
            progress.end()
            err("still starting; run 'rce stop' to cancel")
            return 130
        finally:
            progress.end()

    rd = dev.get("running_device") or {}
    max_rt = rd.get("max_runtime") or cfg.max_runtime
    stop = auto_stop_at(dev)
    print(f"device {device_id} running")
    print(f"  firmware   {fw_name(dev)} ({rd.get('firmware_version_id') or dev.get('firmware_version_id')})")
    print(f"  snapshot   {rd.get('snapshot_name') or '-'} (#{rd.get('snapshot_id')})")
    print(f"  instance   {rd.get('instance_uuid')}")
    print(f"  max runtime {fmt_duration(max_rt)}"
          + (f" (auto-stops ~{stop.astimezone().strftime('%H:%M')})" if stop else ""))
    return 0


class _StatusLine:
    """Prints status changes to stderr as `pending… → running`."""

    def __init__(self) -> None:
        self.shown = False

    def __call__(self, status: str) -> None:
        if not self.shown and status == SHUTDOWN:
            return  # the 202 from /start can arrive before the status flips
        suffix = "" if status == RUNNING else "…"
        err(("" if not self.shown else " → ") + f"{status}{suffix}", end="")
        self.shown = True

    def end(self) -> None:
        if self.shown:
            err("")
            self.shown = False


def cmd_list(cfg: RCEConfig, args: argparse.Namespace) -> int:
    with CoreClient(cfg) as core:
        devices = core.list_devices() or []
    if not args.all:
        devices = [d for d in devices if d.get("status") in ACTIVE]
    if args.json:
        print(json.dumps(devices, indent=2))
        return 0
    if not devices:
        print("no devices in this project" if args.all
              else "no active emulators (rce list --all to see every device)")
        return 0
    rows = []
    for d in devices:
        rd = d.get("running_device") or {}
        rows.append([
            "*" if d.get("id") == cfg.device_id else "",
            d.get("id"), d.get("name", "-"), d.get("device_type", "-"), d.get("status", "-"),
            fw_name(d),
            (rd.get("snapshot_name") if d.get("status") == RUNNING else d.get("last_snapshot_name")) or "-",
            remaining(d),
            (rd.get("instance_uuid") or "-")[:8],
        ])
    print_table(["", "ID", "NAME", "TYPE", "STATUS", "FIRMWARE", "SNAPSHOT", "REMAINING", "INSTANCE"], rows)
    return 0


def _device_client(cfg: RCEConfig) -> DeviceClient:
    device_id = cfg.require_device_id()
    with CoreClient(cfg) as core:
        rd = core.running_device(device_id)
    return DeviceClient.from_running(cfg, rd)


def cmd_sideload(cfg: RCEConfig, args: argparse.Namespace) -> int:
    # Validate everything local before any network call.
    if args.path:
        path = Path(args.path).expanduser().resolve()  # CLI paths are relative to the cwd
    elif cfg.channel_path:
        path = cfg.channel_path  # already resolved against the rce.toml directory
    else:
        raise ConfigError("no channel path: pass PATH or set [channel].path in rce.toml")
    if not (path.is_dir() or (path.is_file() and path.suffix.lower() == ".zip")):
        raise ConfigError(f"{path} is not a .zip file or a channel directory")
    if path.is_dir() and not (path / "manifest").is_file():
        raise ConfigError(f"no 'manifest' at {path}; is channel.path the channel root?")
    if not cfg.dev_password:
        raise SideloadError(DEV_PASSWORD_HINT)
    cfg.require_device_id()
    cfg.require_token()

    tmp: Path | None = None
    try:
        if path.is_dir():
            fd, name = tempfile.mkstemp(prefix="rce-channel-", suffix=".zip")
            os.close(fd)
            tmp = zip_channel(path, cfg.channel_exclude, Path(name))
            err(f"zipped {path} ({tmp.stat().st_size // 1024} KiB)")
        with _device_client(cfg) as device:
            message = device.sideload(tmp or path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    print(f"installed: {message}")
    return 0


def cmd_exit(cfg: RCEConfig, args: argparse.Namespace) -> int:
    if args.remove and not cfg.dev_password:
        raise SideloadError(DEV_PASSWORD_HINT)
    with _device_client(cfg) as device:
        if args.remove:
            device.remove_dev_app()
            print("dev channel removed")
        else:
            device.press(Key.HOME, delay=0)
            print("channel exited (Home)")
    return 0


def cmd_stop(cfg: RCEConfig, args: argparse.Namespace) -> int:
    with CoreClient(cfg) as core:
        if args.all:
            targets = [d for d in core.list_devices() or [] if d.get("status") in ACTIVE]
            if not targets:
                print("no active emulators")
                return 0
            if not args.yes:
                if not sys.stdin.isatty():
                    raise ConfigError(f"refusing to stop {len(targets)} emulators without --yes "
                                      "(stdin is not a terminal)")
                for d in targets:
                    err(f"  {d.get('id')}  {d.get('name', '')}")
                err(f"Stop {len(targets)} emulators? [y/N] ", end="")
                if input().strip().lower() not in ("y", "yes"):
                    err("aborted")
                    return 1
            ids = [d["id"] for d in targets]
        else:
            device_id = cfg.require_device_id()
            if core.get_device(device_id).get("status") == SHUTDOWN:
                print(f"device {device_id} is not running")
                return 0
            ids = [device_id]

        for device_id in ids:
            core.stop_device(device_id)
            if not args.wait:
                print(f"stopping {device_id}")
        if args.wait:
            for device_id in ids:
                core.wait_until_stopped(device_id)
                print(f"stopped {device_id}")
    return 0


COMMANDS: dict[str, Callable[[Any, argparse.Namespace], int]] = {
    "init": cmd_init,
    "config": cmd_config,
    "firmware": cmd_firmware,
    "snapshots": cmd_snapshots,
    "start": cmd_start,
    "list": cmd_list,
    "ls": cmd_list,
    "sideload": cmd_sideload,
    "exit": cmd_exit,
    "stop": cmd_stop,
}


# -- argument parsing -----------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rce", description=f"Roku Cloud Emulator CLI. API reference: {API_DOCS}")
    p.add_argument("--config", type=Path, metavar="PATH", help=f"use this {CONFIG_NAME} instead of searching for one")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging, including HTTP requests")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    dev = argparse.ArgumentParser(add_help=False)
    dev.add_argument("--device-id", type=int, metavar="N", help="device id (default: [device].id)")

    s = sub.add_parser("init", help=f"create {CONFIG_NAME} + {LOCAL_NAME} and gitignore the local file")
    s.add_argument("--force", action="store_true", help=f"overwrite an existing {CONFIG_NAME}")

    s = sub.add_parser("config", help="print the resolved config and where each value came from")
    s.add_argument("--check", action="store_true", help="verify the token via /user/me")

    s = sub.add_parser("firmware", help="list firmware versions")
    s.add_argument("--type", choices=["tv", "stb", "streambar"], help="only this device type")

    sub.add_parser("snapshots", parents=[dev], help="list the device's snapshots")

    s = sub.add_parser("start", parents=[dev], help="start the emulator")
    s.add_argument("--snapshot", metavar="NAME|ID|live|base")
    s.add_argument("--firmware", metavar="ID|SUBSTR")
    s.add_argument("--max-runtime", type=int, metavar="SECS")
    s.add_argument("--no-wait", action="store_true", help="return as soon as the start is accepted")

    s = sub.add_parser("list", aliases=["ls"], help="list active emulators (--all: every device)")
    s.add_argument("--all", action="store_true", help="include shut-down devices")
    s.add_argument("--json", action="store_true", help="print the raw device list")

    s = sub.add_parser("sideload", parents=[dev], help="install a channel (.zip or directory)")
    s.add_argument("path", nargs="?", metavar="PATH", help="default: [channel].path")

    s = sub.add_parser("exit", parents=[dev], help="exit the running channel (Home)")
    s.add_argument("--remove", action="store_true", help="uninstall the dev channel instead")

    s = sub.add_parser("stop", parents=[dev], help="stop the emulator")
    s.add_argument("--all", action="store_true", help="stop every active device in the project")
    s.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation with --all")
    s.add_argument("--wait", action="store_true", help="wait until the device is shut down")
    return p


def overrides_from(args: argparse.Namespace) -> dict[str, Any]:
    return {name: getattr(args, name, None) for name in ("device_id", "snapshot", "firmware", "max_runtime")}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s" if args.verbose else "warning: %(message)s")
    try:
        cfg = None if args.cmd == "init" else RCEConfig.load(config_path=args.config,
                                                             overrides=overrides_from(args))
        return COMMANDS[args.cmd](cfg, args)
    except ConfigError as e:
        return _fail(e, 2, args.verbose)
    except (RCEError, SideloadError, TimeoutError) as e:
        return _fail(e, 1, args.verbose)
    except httpx.HTTPStatusError as e:
        return _fail(f"HTTP {e.response.status_code} from {e.request.url}: {e.response.text[:300]}", 1, args.verbose)
    except httpx.HTTPError as e:
        return _fail(f"network error: {e}", 1, args.verbose)
    except KeyboardInterrupt:
        err("interrupted")
        return 130


def _fail(e: Exception | str, code: int, verbose: bool) -> int:
    if verbose:
        traceback.print_exc()
    err(f"error: {e}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

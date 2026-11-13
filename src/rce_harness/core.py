"""Client for the RCE Core API (devices, firmware, snapshots, lifecycle).

Spec: https://docs.rce.roku.com/core-api/openapi.json (v5.8.2).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from .config import RCEConfig

log = logging.getLogger("rce_harness")

# DeviceOut.status (the spec's DeviceStatus enum)
SHUTDOWN, PENDING, RUNNING = "shutdown", "pending", "running"
ACTIVE = {PENDING, RUNNING}
# DeviceHistoryOut.status values that mean a run did not come up / died
RUN_FAILED = {"failed", "crashed", "unknown"}

AUTH_HINT = "check token in rce.local.toml (or RCE_TOKEN); verify with 'rce config --check'"


class RCEError(RuntimeError):
    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    @classmethod
    def from_response(cls, resp: httpx.Response) -> "RCEError":
        """Build from an `ApiError {detail: string | {code, message}}` body."""
        code = None
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        if isinstance(detail, dict) and detail.get("message"):
            code = detail.get("code")
            text = detail["message"] + (f" [{code}]" if code else "")
        elif isinstance(detail, str) and detail:
            text = detail
        else:
            text = resp.text[:300] or resp.reason_phrase
        message = f"{text} (HTTP {resp.status_code})"
        if resp.status_code == 401:
            message += f"; {AUTH_HINT}"
        return cls(resp.status_code, message, code)


class DeviceStartFailed(RCEError):
    def __init__(self, message: str, run_status: str | None = None):
        super().__init__(0, message)
        self.run_status = run_status


class CoreClient:
    def __init__(self, config: RCEConfig, client: httpx.Client | None = None):
        self.config = config
        self._http = client or httpx.Client(
            base_url=config.core_base_url,
            headers={**config.auth_headers(), "Content-Type": "application/json"},
            timeout=config.timeout,
        )

    def __enter__(self) -> "CoreClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- plumbing ---------------------------------------------------------
    def _request(self, method: str, path: str, **kw: Any) -> Any:
        resp = self._http.request(method, path, **kw)
        if resp.status_code >= 400:
            raise RCEError.from_response(resp)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def close(self) -> None:
        self._http.close()

    # -- account ----------------------------------------------------------
    def whoami(self) -> dict:
        return self._request("GET", "/api/v1/user/me")

    # -- devices ----------------------------------------------------------
    def list_devices(self) -> list[dict]:
        """Every device in the token's project, newest first (items=0 = no page limit)."""
        return self._request("GET", "/api/v1/devices", params={"items": 0})

    def create_device(self, name: str, device_type: str = "tv") -> dict:
        if device_type not in ("tv", "stb"):  # the spec's CreatableDeviceType
            raise ValueError("device_type must be 'tv' or 'stb'")
        return self._request("POST", "/api/v1/devices", json={"name": name, "device_type": device_type})

    def get_device(self, device_id: int) -> dict:
        return self._request("GET", f"/api/v1/devices/{device_id}")

    def device_runs(self, device_id: int) -> list[dict]:
        """Run history (DeviceHistoryOut[]), newest first."""
        return self._request("GET", f"/api/v1/devices/{device_id}/runs")

    def running_device(self, device_id: int) -> dict:
        """The `running_device` of a running device, or RCEError(409) if it isn't running."""
        dev = self.get_device(device_id)
        rd = dev.get("running_device") or {}
        if dev.get("status") != RUNNING or not rd.get("instance_uuid"):
            raise RCEError(409, f"device {device_id} is not running (status={dev.get('status')}). "
                                "Run 'rce start'.")
        return rd

    def start_device(self, device_id: int, snapshot_id: int, firmware_version_id: str,
                     max_runtime: int = 1800) -> dict:
        """POST /start. Returns 202 immediately; the device boots in the background."""
        return self._request("POST", f"/api/v1/devices/{device_id}/start", json={
            "snapshot_id": snapshot_id,
            "firmware_version_id": firmware_version_id,
            "max_runtime": max_runtime,
        })

    def stop_device(self, device_id: int) -> Any:
        """POST /stop. Returns 202 immediately; the device moves to shutdown in the background."""
        return self._request("POST", f"/api/v1/devices/{device_id}/stop")

    def wait_until_running(self, device_id: int, timeout: float = 600, poll: float = 5,
                           on_status: Callable[[str], None] | None = None,
                           sleep: Callable[[float], None] = time.sleep,
                           grace: float = 30) -> dict:
        """Poll until the device is running; returns the DeviceOut.

        `shutdown` counts as a failed start only after `pending` was seen or after `grace`
        seconds (the 202 from /start can arrive before the status flips). On timeout the
        device is left alone: it may still come up.
        """
        started = time.monotonic()
        deadline = started + timeout
        seen_pending = False
        last: str | None = None
        while True:
            dev = self.get_device(device_id)
            status = dev.get("status")
            if status != last:
                last = status
                if on_status:
                    on_status(status)
            if status == RUNNING and (dev.get("running_device") or {}).get("instance_uuid"):
                return dev
            if status == PENDING:
                seen_pending = True
            elif status == SHUTDOWN and (seen_pending or time.monotonic() - started >= grace):
                runs = self.device_runs(device_id) or []
                run_status = runs[0].get("status") if runs else None
                raise DeviceStartFailed(
                    f"device {device_id} failed to start (last run status: {run_status or 'unknown'})",
                    run_status)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"still not running after {_duration(timeout)} (status={status}); "
                                   "it may still come up. Check 'rce list'")
            sleep(poll)

    def wait_until_stopped(self, device_id: int, timeout: float = 120, poll: float = 2,
                           sleep: Callable[[float], None] = time.sleep) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            dev = self.get_device(device_id)
            if dev.get("status") == SHUTDOWN:
                return dev
            if time.monotonic() >= deadline:
                raise TimeoutError(f"device {device_id} still {dev.get('status')} after {_duration(timeout)}")
            sleep(poll)

    # -- firmware ---------------------------------------------------------
    def firmware_versions(self, device_type: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"items": 0}
        if device_type:
            params["device_type"] = device_type
        return self._request("GET", "/api/v1/firmwareVersions", params=params)

    def pick_firmware(self, device_type: str, wanted: str | None = None) -> dict:
        """Pick a FirmwareVersionOut for `device_type`.

        Empty `wanted` -> first listed. Otherwise an exact firmware_version_id match, else the
        first case-insensitive substring match on id or display_name.
        """
        options = [f for f in self.firmware_versions(device_type)
                   if f.get("device_type") in (None, device_type)]
        if not options:
            raise RCEError(404, f"no firmware available for device_type={device_type}")
        if not wanted:
            return options[0]
        for f in options:
            if f["firmware_version_id"] == wanted:
                return f
        needle = wanted.lower()
        matches = [f for f in options
                   if needle in f["firmware_version_id"].lower() or needle in (f.get("display_name") or "").lower()]
        if matches:
            if len(matches) > 1:
                log.debug("firmware %r also matched: %s", wanted,
                          ", ".join(m["firmware_version_id"] for m in matches[1:]))
            return matches[0]
        available = ", ".join(f"{f.get('display_name') or f['firmware_version_id']} ({f['firmware_version_id']})"
                              for f in options)
        raise RCEError(404, f"no {device_type} firmware matches {wanted!r}; available: {available}")

    # -- snapshots --------------------------------------------------------
    def snapshots(self, device_id: int) -> list[dict]:
        return self._request("GET", f"/api/v1/devices/{device_id}/snapshots", params={"items": 0})

    def pick_snapshot(self, device_id: int, wanted: str | int = "live") -> dict:
        """Resolve a snapshot by id, exact name, or the `live`/`base` keyword (in that order)."""
        snaps = self.snapshots(device_id) or []
        match = None
        if isinstance(wanted, int) or (isinstance(wanted, str) and wanted.isdigit()):
            match = next((s for s in snaps if s.get("id") == int(wanted)), None)
        if match is None:
            match = next((s for s in snaps if s.get("name") == str(wanted)), None)
        if match is None and str(wanted) in ("live", "base"):
            match = next((s for s in snaps if s.get(str(wanted))), None)
        if match is None:
            names = ", ".join(f"{s.get('name')} (#{s.get('id')})" for s in snaps) or "none"
            raise RCEError(404, f"no snapshot {wanted!r} on device {device_id}; available: {names}")
        if not match.get("ready", True):
            raise RCEError(409, f"snapshot '{match.get('name')}' is not ready yet (still uploading); "
                                "try again shortly")
        if match.get("base"):
            log.warning("booting from the base snapshot: the device starts at guided setup (factory reset)")
        return match

    def create_snapshot(self, device_id: int, name: str) -> dict:
        return self._request("POST", f"/api/v1/devices/{device_id}/snapshots", json={"name": name})


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"

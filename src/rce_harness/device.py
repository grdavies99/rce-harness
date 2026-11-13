"""Client for the RCE Device API (ECP input, ECP queries, sideload).

The Device API has no public spec (device-api/openapi.json returns 403), so the calls
here are kept as they were and are unverified against a spec.
"""

from __future__ import annotations

import re
import urllib.parse
import time
import xml.etree.ElementTree as ET
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

from .config import RCEConfig


class Key(str, Enum):
    HOME = "Home"
    BACK = "Back"
    SELECT = "Select"
    UP = "Up"
    DOWN = "Down"
    LEFT = "Left"
    RIGHT = "Right"
    PLAY = "Play"
    REV = "Rev"
    FWD = "Fwd"
    INFO = "Info"  # the * / Options button
    INSTANT_REPLAY = "InstantReplay"
    BACKSPACE = "Backspace"
    ENTER = "Enter"


class SideloadError(RuntimeError):
    pass


DEV_PASSWORD_HINT = "dev-mode password required (set dev_password in rce.local.toml or RCE_DEV_PASSWORD)"


class DeviceClient:
    """Talks to one running virtual device, e.g. https://device.rce.roku.com/instance/{uuid}"""

    def __init__(self, config: RCEConfig, instance_uuid: str, instance_api_url: str | None = None,
                 client: httpx.Client | None = None):
        self.config = config
        self.instance_uuid = instance_uuid
        if instance_api_url:
            # The spec gives instance_api_url without a scheme (device.rce.roku.com/instance/<uuid>).
            url = instance_api_url if "://" in instance_api_url else "https://" + instance_api_url
            self.base_url = url.rstrip("/")
        else:
            self.base_url = f"{config.device_base_url}/instance/{instance_uuid}"
        self._http = client or httpx.Client(
            base_url=self.base_url, headers=config.auth_headers(), timeout=config.timeout
        )

    @classmethod
    def from_running(cls, config: RCEConfig, running_device: dict, client: httpx.Client | None = None) -> "DeviceClient":
        """Build from a DeviceOut.running_device (see CoreClient.running_device)."""
        return cls(config, running_device["instance_uuid"], running_device.get("instance_api_url"), client)

    def __enter__(self) -> "DeviceClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- remote input -----------------------------------------------------
    def _input(self, action: str, key: str | Key) -> None:
        k = key.value if isinstance(key, Key) else key
        resp = self._http.post(f"/api/v0/input/{action}/{k}")
        resp.raise_for_status()

    def press(self, *keys: str | Key, delay: float = 0.4) -> None:
        for k in keys:
            self._input("keypress", k)
            if delay:
                time.sleep(delay)

    def key_down(self, key: str | Key) -> None:
        self._input("keydown", key)

    def key_up(self, key: str | Key) -> None:
        self._input("keyup", key)

    def type_text(self, text: str, delay: float = 0.15) -> None:
        """Types via ECP literal keys (Lit_<char>)."""
        for ch in text:
            self._input("keypress", "Lit_" + urllib.parse.quote(ch, safe=""))
            if delay:
                time.sleep(delay)

    # -- ECP passthrough (needs dev mode + 'Control by mobile apps') ------
    def ecp(self, method: str, command: str, **kw) -> httpx.Response:
        resp = self._http.request(method, f"/api/v0/port/8060/http/{command.lstrip('/')}", **kw)
        resp.raise_for_status()
        return resp

    def device_info(self) -> dict[str, str]:
        return _xml_to_dict(self.ecp("GET", "query/device-info").text)

    def active_app(self) -> dict[str, str]:
        root = ET.fromstring(self.ecp("GET", "query/active-app").text)
        app = root.find("app")
        if app is None:
            return {}
        return {"id": app.get("id", ""), "name": (app.text or "").strip(), "version": app.get("version", "")}

    def launch(self, app_id: str = "dev", **params: str) -> None:
        self.ecp("POST", f"launch/{app_id}", params=params or None)

    def wait_for_active_app(self, app_id: str = "dev", timeout: float = 30, poll: float = 1) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            app = self.active_app()
            if app.get("id") == app_id:
                return app
            if time.monotonic() >= deadline:
                raise TimeoutError(f"active app is {app!r}, expected id={app_id}")
            time.sleep(poll)

    # -- sideload ---------------------------------------------------------
    # Unverified (no public Device API spec): the RCE token goes in X-Authorization because
    # the Authorization header carries digest auth (user rokudev, the dev-mode password) here.
    def _installer(self, password: str | None, timeout: float) -> httpx.Client:
        password = password or self.config.dev_password
        if not password:
            raise SideloadError(DEV_PASSWORD_HINT)
        return httpx.Client(timeout=timeout, auth=httpx.DigestAuth("rokudev", password),
                            headers={"X-Authorization": f"Bearer {self.config.require_token()}"})

    def sideload(self, zip_path: str | Path, password: str | None = None) -> str:
        """Install a dev channel .zip; returns Roku's message. Roku launches it after install."""
        path = Path(zip_path)
        with self._installer(password, 120) as c, path.open("rb") as fh:
            resp = c.post(f"{self.base_url}/sideload/plugin_install",
                          data={"mysubmit": "Install"},
                          files={"archive": (path.name, fh, "application/zip")})
        # RCE returns 200 even on functional failure; inspect the HTML body.
        body = resp.text
        resp.raise_for_status()
        if not _sideload_ok(body):
            raise SideloadError(_sideload_message(body) or "sideload failed (see HTML response)")
        return _sideload_message(body)

    def remove_dev_app(self, password: str | None = None) -> str:
        """Uninstall the dev channel; returns Roku's message."""
        with self._installer(password, 60) as c:
            resp = c.post(f"{self.base_url}/sideload/plugin_install", data={"mysubmit": "Delete"})
        body = resp.text
        resp.raise_for_status()
        message = _sideload_message(body)
        if "fail" in message.lower():
            raise SideloadError(message)
        return message


# -- helpers --------------------------------------------------------------
def _xml_to_dict(xml_text: str) -> dict[str, str]:
    root = ET.fromstring(xml_text)
    return {child.tag: (child.text or "").strip() for child in root}


_MSG_RE = re.compile(r"<font color=\"red\">(.*?)</font>", re.S | re.I)
_JSON_MSG_RE = re.compile(r'"text"\s*:\s*"([^"]+)"')


def _sideload_message(html: str) -> str:
    msgs = _MSG_RE.findall(html) or _JSON_MSG_RE.findall(html)
    return " | ".join(m.strip() for m in msgs)


def _sideload_ok(html: str) -> bool:
    lower = html.lower()
    return "install success" in lower or "identical to previous version" in lower

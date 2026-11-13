"""Configuration: rce.toml + rce.local.toml, overridden by env vars and CLI flags.

Precedence (highest first): CLI flag > env var > rce.local.toml > rce.toml > default.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10
    import tomli as tomllib

CONFIG_NAME = "rce.toml"
LOCAL_NAME = "rce.local.toml"
SECRETS = ("token", "dev_password")

DEFAULT_EXCLUDE = [".git", ".github", "node_modules", "out", "*.zip",
                   CONFIG_NAME, LOCAL_NAME, ".DS_Store"]

# field name -> (TOML table or None for top level, TOML key, env var or None)
FIELDS: dict[str, tuple[str | None, str, str | None]] = {
    "token": (None, "token", "RCE_TOKEN"),
    "dev_password": (None, "dev_password", "RCE_DEV_PASSWORD"),
    "device_id": ("device", "id", "RCE_DEVICE_ID"),
    "snapshot": ("device", "snapshot", "RCE_SNAPSHOT"),
    "firmware": ("device", "firmware", "RCE_FIRMWARE"),
    "max_runtime": ("device", "max_runtime", "RCE_MAX_RUNTIME"),
    "channel_path": ("channel", "path", "RCE_CHANNEL_PATH"),
    "channel_exclude": ("channel", "exclude", None),
    "core_base_url": ("api", "core_url", "RCE_CORE_URL"),
    "device_base_url": ("api", "device_url", "RCE_DEVICE_URL"),
}

TOKEN_HINT = ("no API token: set token in rce.local.toml or RCE_TOKEN "
              "(Cloud Emulator → Token tab → Add token)")
DEVICE_ID_HINT = "no device id: set [device].id in rce.toml or pass --device-id (see 'rce list --all')"


class ConfigError(ValueError):
    """Bad or missing configuration (CLI exit code 2)."""


@dataclass
class RCEConfig:
    token: str | None = None
    dev_password: str | None = None
    device_id: int | None = None
    snapshot: str | int = "live"
    firmware: str | None = None
    max_runtime: int = 1800
    channel_path: Path | None = None
    channel_exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    core_base_url: str = "https://api.rce.roku.com"
    device_base_url: str = "https://device.rce.roku.com"
    timeout: float = 30.0
    config_path: Path | None = None  # rce.toml that was loaded, if any
    sources: dict[str, str] = field(default_factory=dict)  # field -> "flag" / "env:X" / "rce.toml" / ...

    # -- loading ----------------------------------------------------------
    @classmethod
    def load(cls, *, config_path: Path | str | None = None, overrides: Mapping[str, Any] | None = None,
             cwd: Path | None = None, environ: Mapping[str, str] | None = None) -> "RCEConfig":
        cwd = Path(cwd) if cwd is not None else Path.cwd()
        environ = os.environ if environ is None else environ
        cfg = cls()
        cfg.sources = {name: "default" for name in FIELDS}

        explicit = config_path or environ.get("RCE_CONFIG")
        if explicit:
            path = Path(explicit)
            if not path.is_absolute():
                path = cwd / path
            if not path.is_file():
                raise ConfigError(f"config file not found: {path}")
        else:
            path = find_config(cwd)

        if path is not None:
            path = path.resolve()
            cfg.config_path = path
            base = path.parent
            cfg._apply_file(_read_toml(path), CONFIG_NAME, base, secrets_allowed=False)
            local = base / LOCAL_NAME
            if local.is_file():
                cfg._apply_file(_read_toml(local), LOCAL_NAME, base, secrets_allowed=True)

        for name, (_, _, env) in FIELDS.items():
            if env and environ.get(env, "") != "":
                cfg._set(name, environ[env], f"env:{env}", cwd, from_text=True)

        for name, value in (overrides or {}).items():
            if name not in FIELDS:
                raise ConfigError(f"unknown config override {name!r}")
            if value is not None:
                cfg._set(name, value, "flag", cwd)
        return cfg

    def _apply_file(self, data: dict, label: str, base: Path, *, secrets_allowed: bool) -> None:
        known_tables = {t for t, _, _ in FIELDS.values() if t}
        for key, value in data.items():
            if isinstance(value, dict):
                if key not in known_tables:
                    _warn(f"unknown table [{key}] in {label}")
                    continue
                for sub, subval in value.items():
                    name = _field_for(key, sub)
                    if name is None:
                        _warn(f"unknown key '{key}.{sub}' in {label}")
                        continue
                    self._set(name, subval, label, base)
            else:
                name = _field_for(None, key)
                if name is None:
                    _warn(f"unknown key '{key}' in {label}")
                    continue
                if key in SECRETS and not secrets_allowed and value:
                    _warn(f"secret '{key}' found in {label}, which is usually committed; move it to {LOCAL_NAME}")
                self._set(name, value, label, base)

    def _set(self, name: str, value: Any, source: str, base: Path, *, from_text: bool = False) -> None:
        table, key, _ = FIELDS[name]
        where = f"{table + '.' if table else ''}{key} ({source})"

        if name in ("token", "dev_password", "firmware", "core_base_url", "device_base_url"):
            if not isinstance(value, str):
                raise ConfigError(f"{where} must be a string")
            if value == "":
                return  # empty means "not set"; keep the lower-precedence value
            if name.endswith("_url"):
                value = value.rstrip("/")
        elif name == "device_id":
            value = _as_int(value, where, from_text)
            if value < 0:
                raise ConfigError(f"{where} must be an integer >= 0, got {value}")
        elif name == "max_runtime":
            value = _as_int(value, where, from_text)
            if value <= 0:
                raise ConfigError(f"{where} must be an integer > 0 (seconds), got {value}")
        elif name == "snapshot":
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ConfigError(f"{where} must be a string or an integer")
            if value == "":
                return
        elif name == "channel_path":
            if not isinstance(value, (str, Path)) or str(value) == "":
                raise ConfigError(f"{where} must be a non-empty string")
            p = Path(value).expanduser()
            value = p if p.is_absolute() else (base / p)
            value = value.resolve()
        elif name == "channel_exclude":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"{where} must be a list of strings")
            value = list(value)

        setattr(self, name, value)
        self.sources[name] = source

    # -- accessors --------------------------------------------------------
    def require_token(self) -> str:
        if not self.token:
            raise ConfigError(TOKEN_HINT)
        return self.token

    def require_device_id(self) -> int:
        if self.device_id is None:
            raise ConfigError(DEVICE_ID_HINT)
        return self.device_id

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.require_token()}"}


def find_config(start: Path) -> Path | None:
    """Walk up from `start` to the filesystem root and return the first rce.toml (like git's .git search)."""
    start = Path(start).resolve()
    for d in (start, *start.parents):
        candidate = d / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


# -- helpers --------------------------------------------------------------
def _field_for(table: str | None, key: str) -> str | None:
    for name, (t, k, _) in FIELDS.items():
        if t == table and k == key:
            return name
    return None


def _as_int(value: Any, where: str, from_text: bool) -> int:
    if from_text and isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            raise ConfigError(f"{where} must be an integer, got {value!r}") from None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} must be an integer, got {value!r}")
    return value


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)

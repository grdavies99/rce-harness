# rce-harness

`rce` is a small CLI for the **Roku Cloud Emulator (RCE)**. It can:

1. **start** an emulator with a chosen snapshot and firmware,
2. **list** emulators (active ones by default, or `--all`),
3. **sideload** a channel (a `.zip`, or a directory it zips for you),
4. **exit** the running channel (or uninstall the dev channel),
5. **stop** an emulator.

API reference: <https://docs.rce.roku.com/core-api/index.html#/>

## Install

```bash
pip install -e .        # Python 3.10+
```

## Setup

```bash
rce init                # writes rce.toml + rce.local.toml, gitignores rce.local.toml
# paste your token into rce.local.toml (Cloud Emulator → Token tab → Add token)
rce config --check      # verifies the token, shows your org's max runtime
rce list --all          # find your device id → [device].id in rce.toml
rce firmware            # pick a firmware → [device].firmware
rce snapshots           # pick a snapshot → [device].snapshot
```

**One-time device prep.** A fresh device boots to guided setup. Do this once by hand, then reuse it: run the device in the web UI, complete activation, enable **Dev Mode** (and set the dev password you'll put in `rce.local.toml`), and disable auto power savings. Enabling **Settings → System → Advanced → Control by mobile apps** is needed for ECP queries (not for `rce exit`). Then save a snapshot named `activated` and set `snapshot = "activated"`, or just use `live` (the state at last stop).

## Config

| File | Commit? | Holds |
|---|---|---|
| `rce.toml` | yes | `[device]` id, snapshot, firmware, max_runtime · `[channel]` path, exclude · optional `[api]` core_url, device_url |
| `rce.local.toml` | **no** | `token`, `dev_password`, plus any personal overrides of the keys above |

`rce` finds `rce.toml` by walking up from the current directory (like git), or uses `--config PATH` / `RCE_CONFIG`. `rce.local.toml` is read from the same directory, and relative paths in either file are resolved against it.

**Precedence:** CLI flag > env var > `rce.local.toml` > `rce.toml` > default. `rce config` shows each value and where it came from.

**Env vars:** `RCE_TOKEN`, `RCE_DEV_PASSWORD`, `RCE_DEVICE_ID`, `RCE_SNAPSHOT`, `RCE_FIRMWARE`, `RCE_MAX_RUNTIME`, `RCE_CHANNEL_PATH`, `RCE_CORE_URL`, `RCE_DEVICE_URL`, `RCE_CONFIG`.

**Snapshots:** a name, a numeric id, or a keyword. `live` (the default) is the device's state when it was last stopped. `base` is a factory reset: the device boots to guided setup, and the API warns this can make the device's other snapshots behave unpredictably. An exact snapshot name beats a keyword.

**Firmware:** an exact `firmware_version_id`, or a case-insensitive substring of an id or display name. If it's empty, the first listed version is used.

## Commands

```
rce init [--force]
rce config [--check]
rce firmware [--type tv|stb|streambar]
rce snapshots [--device-id N]
rce start  [--device-id N] [--snapshot NAME|ID|live|base] [--firmware ID|SUBSTR] [--max-runtime SECS] [--no-wait]
rce list   [--all] [--json]          # alias: rce ls
rce sideload [PATH] [--device-id N]  # PATH defaults to [channel].path
rce exit   [--device-id N] [--remove]
rce stop   [--device-id N] [--all] [--yes] [--wait]
```

Global flags go before the command: `--config PATH`, `-v/--verbose` (debug logging, including HTTP requests). Exit codes: `0` ok, `1` API/runtime failure, `2` usage/config error.

## Cost

RCE bills per device-minute. **An emulator keeps running after `rce start` returns** until you run `rce stop` or `max_runtime` (default 1800 s, always sent) runs out. Ctrl-C during `rce start` does not stop the device. Trial accounts can run one device at a time.

## Known gaps

- The Device API (sideload, ECP, input) has no public spec, so its auth header (`X-Authorization` plus digest auth) and the parsing of sideload HTML responses are unverified.
- No screenshots.

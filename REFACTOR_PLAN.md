# Refactor plan: simplify `rce` into a device-control CLI

This plan is for the Claude Code session that will do the work. Read it all before you edit anything. Every decision below is already made. If something here conflicts with what you find in the code or the RCE API, stop and ask instead of guessing.

**API reference:** the authoritative Core API docs are at https://docs.rce.roku.com/core-api/index.html#/. The raw spec is at https://docs.rce.roku.com/core-api/openapi.json (version 5.8.2 when this plan was written). Use the spec, not the old code, whenever they disagree. §0 lists the facts from it that this plan depends on. The Device API (sideload, ECP, input) has no public spec: `https://docs.rce.roku.com/device-api/openapi.json` returns 403. So don't change the existing Device API calls beyond what §3 says.

## Goal

Turn `rce-harness`, currently a pytest and CI test harness, into a small CLI that does five things against the Roku Cloud Emulator (RCE):

1. **Start** an emulator with a chosen snapshot and firmware (device OS version).
2. **List** emulators: by default only the active ones (`rce list`), or every device in the project in any state (`rce list --all`).
3. **Sideload** a channel (a `.zip` file, or a directory that the CLI zips).
4. **Exit** the running channel (and optionally uninstall the dev channel).
5. **Stop** an emulator.

The settings (device id, snapshot, firmware, runtime cap, channel path, access token, dev password) come from a config file in the repo the user runs `rce` from. Env vars and CLI flags override the file.

**Out of scope:** the pytest plugin, the CI workflow, example tests, the session context manager, websocket log streaming and debug consoles, the device-state WebSocket (polling is enough), renaming the package, and supporting several devices or profiles in one config.

---

## 0. Core API facts this plan relies on (spec v5.8.2)

Base URL is `https://api.rce.roku.com`. Auth is `Authorization: Bearer <token>`, using a personal access token. The spec has no `X-Authorization` for the Core API.

| Need | Endpoint | Notes |
|---|---|---|
| List all devices | `GET /api/v1/devices?items=0` | `items=0` means no page limit (the default is 100). Newest first. `order=asc\|desc`. The token's project decides which devices come back. Returns `DeviceOut[]`. |
| One device | `GET /api/v1/devices/{id}` | `DeviceOut` |
| Start | `POST /api/v1/devices/{id}/start` | The body needs **all three**: `snapshot_id` (int), `firmware_version_id` (an opaque string, passed back unchanged), and `max_runtime` (int seconds, **> 0**). Returns **202** right away and the device boots in the background. |
| Stop | `POST /api/v1/devices/{id}/stop` | Returns **202** right away and the device moves to `shutdown` in the background. |
| Run history | `GET /api/v1/devices/{id}/runs` | `DeviceHistoryOut[]`, newest first. Each run has a `status` (one of `created`, `pending`, `running`, `completed`, `failed`, `crashed`, `unknown`), `runtime` and `max_runtime`. |
| Firmware | `GET /api/v1/firmwareVersions?items=0&device_type=tv` | `FirmwareVersionOut {firmware_version_id, device_type, display_name\|null}`. The spec says to treat the id as opaque. |
| Snapshots | `GET /api/v1/devices/{id}/snapshots?items=0` | `SnapshotOut {id, name, ready, live, base, firmware_version_display_name, ...}` |
| Who am I | `GET /api/v1/user/me` | `UserOut`. Its organisation includes `max_project_runtime` (the server-side cap for `max_runtime`). |

**`DeviceOut` fields used by the CLI:** `id`, `name`, `device_type` (`tv`, `stb` or `streambar`), `status`, `firmware_version_id`, `firmware_version_display_name`, `last_snapshot_id`, `last_snapshot_name`, `created_at`, and `running_device` (null when not running). When present, `running_device` has `instance_uuid`, `instance_api_url` (for example `device.rce.roku.com/instance/<uuid>`, with no scheme), `created_at`, `started_at`, `running_at`, `snapshot_id`, `snapshot_name`, `firmware_version_id` and `max_runtime`.

**The device `status` enum is only `shutdown | pending | running`.** The current code checks for `failed`, `completed` and `starting`, and none of those exist on a device. A failed boot simply goes back to `shutdown`, and the reason is only in the latest entry of `/runs` (`failed` or `crashed`).

**Snapshots.** Each device has exactly one `base` snapshot and one `live` snapshot.
- **Base** contains only the device identity. Booting from it is like a factory reset: the device starts at guided setup. The spec warns that guided setup on a device that has other snapshots "may cause those snapshots to act unpredictably".
- **Live** is the device's state when it was last stopped.
- A snapshot is only usable once `ready: true`.

**Start errors** come back as `ApiError {detail: string | {code, message}}`:
- `400`: the firmware is unknown or doesn't support the device type, or `max_runtime` is over the allowed maximum. Trial users get the code `trial_max_runtime_exceeded`.
- `402`: `trial_exhausted`, meaning a paid plan is required.
- `409`: the device is already running or locked, or the snapshot is still uploading (`snapshot_not_ready`).
- `429`: `trial_device_run_limit`, because trial accounts can only run one device at a time.
- `503`: a service is temporarily unavailable. Retry later.

**The spec has no delete-device endpoint.** Remove `CoreClient.delete_device`.

---

## 1. Target command surface

```
rce init                         # create rce.toml + rce.local.toml, add rce.local.toml to .gitignore
rce config [--check]             # print the resolved config and where each value came from; --check verifies the token via /user/me
rce firmware [--type tv|stb|streambar]   # list firmware versions (helps fill in device.firmware)
rce snapshots [--device-id N]    # list snapshots (helps fill in device.snapshot)

rce start  [--device-id N] [--snapshot NAME|ID|live|base] [--firmware ID|SUBSTR] [--max-runtime SECS] [--no-wait]
rce list   [--all] [--json]      # alias: rce ls
rce sideload [PATH] [--device-id N]
rce exit   [--device-id N] [--remove]
rce stop   [--device-id N] [--all] [--yes] [--wait]
```

Global flags, accepted before the subcommand: `--config PATH` (use this rce.toml instead of searching for one) and `-v/--verbose` (debug logging, which includes HTTP requests).

The current `status`, `create`, `smoke` and `logs` commands are removed. `rce list` replaces `status`.

### Behaviour of each command

**`rce init`**
- Writes `./rce.toml` from the template in §2. If the file already exists, it refuses unless `--force` is passed.
- Writes `./rce.local.toml` with empty `token` and `dev_password` values and sets its mode to `0600` (skip the chmod on Windows).
- Appends `rce.local.toml` to `./.gitignore` if it isn't there already. Creates `.gitignore` if needed.
- Prints the next steps: paste the token, run `rce config --check`, then run `rce list --all` to find the device id.

**`rce config [--check]`**
- Prints each resolved key with its value and source (`flag`, `env:RCE_TOKEN`, `rce.local.toml`, `rce.toml`, `default`).
- Shows the token as `abcd…wxyz`, or `(not set)` when empty. The dev password always shows as `****`.
- Prints the path of the config file it found, or "no rce.toml found (searched from <cwd> up)".
- `--check` calls `GET /api/v1/user/me` and prints `token ok: <username or email>, org <name>, max runtime <max_project_runtime>s`. A 401 prints the token hint and exits 1.

**`rce firmware [--type T]`**
- Calls `GET /firmwareVersions?items=0`, adding `&device_type=T` when `--type` is given.
- Prints a table of `ID`, `TYPE` and `NAME` (`display_name`, or `-` when null).

**`rce snapshots [--device-id N]`**
- Calls `GET /devices/{id}/snapshots?items=0`.
- Prints a table of `ID`, `NAME`, `FLAGS` (`base`, `live`, `not-ready`), `FIRMWARE` (`firmware_version_display_name`) and `CREATED`.

**`rce start`**
1. Resolve `device.id`. If it is missing, exit with code 2 and the message: `no device id: set [device].id in rce.toml or pass --device-id (see 'rce list --all')`.
2. `GET` the device.
   - If the status is `running`, print `device 74 already running (instance <uuid>)` and exit 0.
   - If it is `pending`, print `device 74 is already starting` and go straight to waiting (step 6), or exit 0 with `--no-wait`.
   - Never post `/start` twice. The server would answer 409 anyway.
3. **Firmware.** Use the device's own `device_type` from the `GET`. The config does not set the type. Call `firmware_versions(device_type=...)` and run `pick_firmware` (see §3 for the matching rules). If no firmware is configured, use the first version in the list (the API returns a stable order) and print which one was used.
4. **Snapshot.** Resolve it with `pick_snapshot` (see §3). The default is `live`. The resolved snapshot must be `ready`.
5. `POST /start` with `{snapshot_id, firmware_version_id, max_runtime}`. **The emulator is not stopped when the CLI exits**, so `max_runtime` is now the only automatic cost cap. Default it to 1800 and always send it. Map these error codes to friendly messages:
   - `409`: "already running, locked by another operation, or snapshot not ready".
   - `429` `trial_device_run_limit`: "trial accounts can run one device at a time; stop the other one (rce list)".
   - `402`: "free trial exhausted; a paid plan is required".
   - `400`: include the server's message. If it is about `max_runtime`, add "your org's cap is shown by `rce config --check`".
6. Unless `--no-wait` is passed, poll with `wait_until_running` (see §3 for the state handling) and print each status change to stderr (`pending… → running`). On success, print to stdout:
   ```
   device 74 running
     firmware   <firmware_version_display_name> (<firmware_version_id>)
     snapshot   <snapshot_name> (#<snapshot_id>)
     instance   <instance_uuid>
     max runtime 30m (auto-stops ~14:32)
   ```
   Compute the auto-stop time as `running_device.started_at` (falling back to `created_at`) plus `max_runtime`, and label it approximate with `~`. The spec doesn't say exactly which moment the runtime counts from.
7. Ctrl-C while waiting stops polling but does **not** stop the device. Print `still starting; run 'rce stop' to cancel`.

**`rce list`** (alias `ls`)
- Calls `GET /api/v1/devices?items=0`. That's a single request, with no paging loop needed.
- By default it shows **active** devices only (`status` of `pending` or `running`). `--all` shows every device in the project, including `shutdown` ones.
- Table columns:
  - a marker: `*` for the device in the config
  - `ID`, `NAME`, `TYPE`, `STATUS`
  - `FIRMWARE`: `firmware_version_display_name`, falling back to `firmware_version_id`
  - `SNAPSHOT`: `running_device.snapshot_name` when running, otherwise `last_snapshot_name`
  - `REMAINING`: `~Nm`, computed as in `start` step 6, only for running devices; `-` otherwise
  - `INSTANCE`: the first 8 characters of `running_device.instance_uuid`, or `-`
- Sort order is the API's (newest first).
- `--json` prints the raw `DeviceOut` list, filtered the same way (`--all` gives everything).
- With no active devices it prints `no active emulators (rce list --all to see every device)`. With `--all` and no devices at all, it prints `no devices in this project`.

**`rce sideload [PATH]`**
- `PATH` defaults to `channel.path` from config. If neither is set, exit 2.
- Find the running device with `CoreClient.running_device(device_id)` (§3). If it isn't running, exit 1 with `device 74 is not running (status=shutdown). Run 'rce start'.`
- Build the `DeviceClient` from `running_device.instance_api_url` (§3).
- If `PATH` is a `.zip`, upload it as is. If it is a directory, zip it into a temp file with `packaging.zip_channel` (§3) and upload that, then delete the temp file. Anything else exits 2.
- Requires `dev_password`. Use the existing `SideloadError` message if it's missing.
- On success, print `installed: <message from Roku>`. The Roku launches the dev channel automatically after install, so don't call `launch` again.

**`rce exit`**
- Resolve the running device and `DeviceClient` as in `sideload`.
- By default, send keypress `Home`. This uses `/api/v0/input`, which does not need "Control by mobile apps". Print `channel exited (Home)`.
- With `--remove`, call `DeviceClient.remove_dev_app()`, which uninstalls the dev channel. It needs `dev_password`. Print `dev channel removed`.

**`rce stop`**
- Stops the configured device. If it's already `shutdown`, print `device 74 is not running` and exit 0 (stop is idempotent). Stopping a `pending` device is allowed.
- The stop is asynchronous (the server returns 202). Without `--wait`, print `stopping 74` and exit. With `--wait`, poll until `status == shutdown` (2 s interval, 120 s timeout), then print `stopped 74`.
- `--all` stops every active device from `list_devices()`. It asks `Stop 3 emulators? [y/N]` first, listing their ids and names, unless `--yes` is passed or stdin is not a TTY. In the non-TTY case without `--yes`, refuse with exit code 2.

### Exit codes and errors
- `0` success, `1` an API or runtime failure, `2` a usage or config error.
- `RCEError`, `SideloadError`, `TimeoutError` and `ConfigError` are caught once in `main()` and printed as `error: <message>` to stderr. No tracebacks unless `-v` is passed.
- `RCEError` messages come from the `ApiError` body: use `detail.message` (plus `[detail.code]`) when `detail` is an object, `detail` when it is a string, and the first 300 characters of the raw text otherwise.
- A 401 from the Core API gets an extra hint: `check token in rce.local.toml (or RCE_TOKEN); verify with 'rce config --check'`.

---

## 2. Config file design

### Files
| File | Committed? | Holds |
|---|---|---|
| `rce.toml` | yes | device id, snapshot, firmware, max runtime, channel path and excludes, optional API URL overrides |
| `rce.local.toml` | **no** (gitignored) | `token`, `dev_password`, plus any personal overrides of the keys above |

The two files are merged: `rce.local.toml` overrides `rce.toml` key by key. Putting `token` in `rce.local.toml` means a secret never gets committed by accident. If `token` or `dev_password` turns up in `rce.toml` itself, the CLI still uses it but prints this warning once to stderr: `warning: secret 'token' found in rce.toml, which is usually committed; move it to rce.local.toml`.

### Discovery
1. If `--config PATH` or `RCE_CONFIG` is set, use that file. A missing file is an error.
2. Otherwise, walk up from the current directory to the filesystem root and use the first `rce.toml` found. This works the way git finds `.git`, so `rce` works from any subdirectory of the repo.
3. `rce.local.toml` is always read from **the same directory** as the `rce.toml` that was found.
4. Relative paths in the config, such as `channel.path`, are resolved **against the directory that holds `rce.toml`**, not the current directory.
5. No config file is fine. Everything can come from env vars and flags. Only the commands that need a missing value fail.

### Precedence (highest first)
CLI flag > env var > `rce.local.toml` > `rce.toml` > built-in default

### Schema (this is also the `rce init` template)

```toml
# rce.toml (commit this file). Secrets go in rce.local.toml.
# API reference: https://docs.rce.roku.com/core-api/index.html#/

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
```

```toml
# rce.local.toml (gitignored; never commit)
token = ""         # Cloud Emulator → Token tab → Add token
dev_password = ""  # dev-mode password set on the virtual device
```

The config has no `device.type`. The type comes from the device itself (`DeviceOut.device_type`), so the two can never disagree.

### Env vars
`RCE_TOKEN`, `RCE_DEV_PASSWORD`, `RCE_DEVICE_ID`, `RCE_SNAPSHOT`, `RCE_FIRMWARE`, `RCE_MAX_RUNTIME`, `RCE_CHANNEL_PATH`, `RCE_CORE_URL`, `RCE_DEVICE_URL`, `RCE_CONFIG`.

### Validation (raise `ConfigError`, which exits with code 2)
- `device.id` must be an int ≥ 0.
- `device.max_runtime` must be an int > 0. That is the spec's only client-side rule. The real upper bound is the org's `max_project_runtime` (or the remaining trial time), which the server enforces with a 400. Don't hard-code a maximum.
- `device.snapshot` must be a string or an int.
- `channel.exclude` must be a list of strings.
- Unknown top-level tables or keys print a warning, not an error, so a typo like `snapshop` gets noticed.
- `token` is checked lazily: only commands that call the API need it. `init`, `config` (without `--check`) and `--help` work without one.

---

## 3. File-by-file changes

### Delete
- `src/rce_harness/pytest_plugin.py`
- `src/rce_harness/session.py`
- `examples/` (the whole directory)
- `.github/workflows/rce.yml`, and `.github/` if it ends up empty
- `tests/test_core.py::test_session_always_stops_device` and its `session` import

### `pyproject.toml`
- Change the description to "CLI for starting, sideloading to, and stopping Roku Cloud Emulator devices".
- Change dependencies to `httpx>=0.27` and `tomli>=2; python_version < "3.11"`. Remove `websockets`.
- Remove the `[project.entry-points.pytest11]` table.
- Remove the `markers` line from `[tool.pytest.ini_options]`. Keep `testpaths`.
- Keep `dev = ["pytest>=8", "respx>=0.21"]`. These are offline unit tests for the library, not test-harness integration (see §6).

### `.gitignore`
- Add `rce.local.toml`. Remove `rce-artifacts/`.

### `src/rce_harness/config.py`: rewrite
```python
class ConfigError(ValueError): ...

@dataclass
class RCEConfig:
    token: str | None = None
    dev_password: str | None = None
    device_id: int | None = None
    snapshot: str | int = "live"
    firmware: str | None = None
    max_runtime: int = 1800
    channel_path: Path | None = None
    channel_exclude: list[str] = field(default_factory=lambda: [...defaults from template...])
    core_base_url: str = "https://api.rce.roku.com"
    device_base_url: str = "https://device.rce.roku.com"
    timeout: float = 30.0
    config_path: Path | None = None              # rce.toml that was loaded, if any
    sources: dict[str, str] = field(default_factory=dict)  # key -> "flag"/"env:X"/"rce.toml"/...

    @classmethod
    def load(cls, *, config_path: Path | None = None, overrides: dict | None = None,
             cwd: Path | None = None, environ: Mapping[str, str] | None = None) -> "RCEConfig": ...

    def require_token(self) -> str: ...          # raises ConfigError with the "how to get a token" hint
    def require_device_id(self) -> int: ...
    def auth_headers(self) -> dict[str, str]:    # {"Authorization": f"Bearer {require_token()}"}
```
- `find_config(start: Path) -> Path | None` implements the walk-up search.
- `load()` merges, in order: defaults → `rce.toml` → `rce.local.toml` → env → `overrides`, which are CLI flags (`None` values are skipped). It records the source of each key in `sources` as it goes.
- Map TOML tables to flat fields: `[device].id` → `device_id`, `[channel].path` → `channel_path`, `[api].core_url` → `core_base_url`, and so on. Keep the mapping in a single dict so `rce config` can iterate over it.
- Take `environ` and `cwd` as parameters so tests don't need to monkeypatch.
- Remove `from_env()` and `extra_headers`. The spec confirms `Authorization: Bearer` for the Core API, so the config needs no header option.

### `src/rce_harness/core.py`: align with the spec
- **Status constants.** Replace `RUNNING`/`TERMINAL_FAILURE`/`STOPPED` with `SHUTDOWN, PENDING, RUNNING = "shutdown", "pending", "running"`, `ACTIVE = {PENDING, RUNNING}`, and `RUN_FAILED = {"failed", "crashed", "unknown"}` (that last set is for `/runs` entries).
- **`RCEError`.** Parse the `ApiError` body as described in §1 "Exit codes". Keep `.status` and add `.code` (from `detail.code`, or `None`).
- **`list_devices() -> list[dict]`.** `GET /api/v1/devices` with `params={"items": 0}`.
- **`device_runs(device_id) -> list[dict]`.** `GET /api/v1/devices/{id}/runs`.
- **`whoami() -> dict`.** `GET /api/v1/user/me`.
- **`running_device(device_id) -> dict`.** `get_device`, then return `running_device`. If the status isn't `running` or `running_device`/`instance_uuid` is missing, raise `RCEError(409, "device N is not running (status=…). Run 'rce start'.")`. `sideload` and `exit` both use this.
- **`firmware_versions(device_type=None)`.** Send `params={"items": 0}`, plus `device_type` when given.
- **`snapshots(device_id)`.** Send `params={"items": 0}`.
- **`pick_firmware(device_type, wanted=None)`.** It works on `firmware_versions(device_type)`:
  1. If `wanted` is empty, return the first entry.
  2. Return an exact `firmware_version_id` match if there is one.
  3. Otherwise do a case-insensitive substring match on the id or `display_name` (which can be null, so guard it). When there are several matches, use the first one and log the rest at debug level.
  4. When nothing matches, raise `RCEError(404, ...)` listing the available `display_name (id)` pairs so the user can fix the config.
- **`pick_snapshot(device_id, wanted="live") -> dict`.** Return the whole snapshot dict, not just the id, so `start` can print its name. Check these rules in order:
  1. An int, or a string of digits: match by `id`.
  2. A `name` that matches exactly (so a snapshot literally named "live" wins over the keyword).
  3. `"live"` or `"base"`: the snapshot with that flag set.
  4. Nothing matched: raise `RCEError(404, ...)` listing the available names.

  If the match has `ready: false`, raise `RCEError(409, "snapshot '<name>' is not ready yet (still uploading)")`. When `base` is chosen, log a warning that it boots to guided setup (factory reset).
- **`start_device`.** Leave it as it is. The body already matches `DeviceStart`.
- **`stop_device`.** Leave it as it is (it returns a 202 `DeviceOut`). Add `wait_until_stopped(device_id, timeout=120, poll=2)`.
- **`wait_until_running(device_id, timeout=600, poll=5, on_status=None, sleep=time.sleep)`.** Rewrite it for the real states:
  - `running` with `running_device.instance_uuid` set: return the device.
  - `pending`: keep waiting.
  - `shutdown` **after `pending` has been seen, or after a 30 s grace period** (the 202 can arrive before the state flips): the start failed. Fetch `device_runs(id)[0]` and raise `DeviceStartFailed` with that run's `status` (for example `failed` or `crashed`).
  - Call `on_status(status)` whenever the status changes.
  - On timeout, raise `TimeoutError` and do **not** stop the device.
- **Delete `delete_device`.** The spec has no such endpoint.
- Keep `create_device`, restricted to `tv`/`stb` (the spec's `CreatableDeviceType`). Keep `create_snapshot`. Both are unused by the CLI but cost nothing.

### `src/rce_harness/device.py`: trim and use `instance_api_url`
- Remove `stream_firmware_logs`, `stream_console`, `console_command`, `_ws_url`, and the `asyncio`/`websockets` imports.
- Change the constructor to `DeviceClient(config, instance_uuid, instance_api_url: str | None = None, client=None)`. If `instance_api_url` is set, the base URL is `"https://" + instance_api_url`, because the spec omits the scheme. Otherwise fall back to `f"{config.device_base_url}/instance/{instance_uuid}"`. Add a classmethod `DeviceClient.from_running(config, running_device: dict)`.
- Keep `Key`, `press`, `key_down`, `key_up`, `type_text`, `ecp`, `device_info`, `active_app`, `launch`, `wait_for_active_app`, `sideload` and `remove_dev_app`.
- Fix `remove_dev_app`. It currently sends an empty password when none is set. Make it raise `SideloadError` the way `sideload` does, and check the HTML body for failure the same way.
- `sideload` and `remove_dev_app` keep their current auth: `X-Authorization: Bearer` plus digest auth. Add a comment explaining that the Device API spec isn't public, so this is unverified, and that the Authorization header carries digest auth there.

### `src/rce_harness/packaging.py`: new
```python
def zip_channel(src_dir: Path, exclude: list[str], dest: Path) -> Path: ...
```
- Fail with `ConfigError("no 'manifest' at <dir>; is channel.path the channel root?")` if `src_dir/manifest` doesn't exist. Check this before any network call.
- Walk the tree with `os.walk` and prune excluded directories in place so `node_modules` is never walked.
- An exclude pattern matches if `fnmatch` matches the file's relative POSIX path **or** any single component of it. That way `.git` excludes `.git/` at any depth, and `*.zip` excludes zip files anywhere.
- Put entries in the archive **relative to `src_dir`** so `manifest` is at the zip root. Roku rejects nested roots.
- Use `ZIP_DEFLATED` and skip symlinks. Don't include the destination zip file itself, even if it is inside `src_dir`.
- Log the file count and zip size at debug level.

### `src/rce_harness/cli.py`: rewrite
- Use argparse subcommands as in §1. Write one small `cmd_<name>(cfg, args) -> int` function per command, plus a dispatch dict.
- Build `overrides` from the flags (`--device-id` → `device_id`, and so on) and call `RCEConfig.load(config_path=args.config, overrides=...)` once.
- Create `CoreClient`/`DeviceClient` lazily inside the commands that need them so `init` and `config` never need a token.
- Data goes to stdout. Progress, warnings and errors go to stderr.
- Format tables by hand with fixed-width columns (no extra dependency).
- Put the remaining-time calculation in one helper: parse RFC 3339 with `datetime.fromisoformat`, replacing a trailing `Z` with `+00:00` for Python 3.10. Both `start` and `list` use it.
- Keep `logging.basicConfig` at `WARNING` by default and `DEBUG` with `-v`.

### `src/rce_harness/__init__.py`
Export `RCEConfig`, `ConfigError`, `CoreClient`, `DeviceClient`, `Key`, `RCEError`, `DeviceStartFailed` and `SideloadError`. Remove `rce_session`.

### `README.md`: rewrite to about a screen
Sections:
1. What it does (the five commands), with a link to the API reference above
2. Install: `pip install -e .`
3. Setup: `rce init`, paste the token, `rce config --check`, `rce list --all` to pick a device, `rce firmware`/`rce snapshots` to fill in the config, plus the existing "one-time device prep" steps (save an `activated` snapshot)
4. Config reference: the two files, precedence, env vars, the snapshot keywords (`live`/`base`) and the base-snapshot warning
5. Command reference
6. Cost note: emulators keep running after `rce start` until `rce stop` or `max_runtime` runs out. Trial accounts can run one device at a time.
7. Known gaps:
   - the Device API (sideload/ECP) has no public spec, so its auth header and the sideload HTML parsing are unverified
   - no screenshots

Remove the pytest, CI and library-session sections, and the old Core API `X-Authorization` gap (the spec settles it).

---

## 4. Implementation order

Do these in order. Each phase leaves the package importable.

1. **Prune.** Delete the files in §3, update `pyproject.toml`/`.gitignore`/`__init__.py`, and remove the websocket code from `device.py`. Run `pip install -e ".[dev]" && pytest -q`. The remaining tests should pass, apart from any that use `RCEConfig.from_env` (fixed in phase 2).
2. **Config.** Rewrite `config.py`. Update the `cfg`/`dev` fixtures in `tests/` to build `RCEConfig(token="t", dev_password="pw")` directly. Add `tests/test_config.py` (§6).
3. **Core and device changes.** Align `core.py` with the spec (status enum, `items=0`, the new methods, the pickers, `wait_until_running`), add `DeviceClient.from_running`, and fix `remove_dev_app`. Update the existing tests that used `status: "starting"`/`"failed"`/`"completed"` to the real enum.
4. **Packaging.** `packaging.py` plus `tests/test_packaging.py`.
5. **CLI.** Rewrite `cli.py`. Check every command offline with `--help`, and check `rce init` and `rce config` in a temp directory.
6. **README.**
7. **Live check.** Only with the user's go-ahead, because it bills: run the manual checklist in §7.

---

## 5. Edge cases to handle

- `rce start` while the device is `pending`: no second `/start`. Wait, or exit 0 with `--no-wait`.
- A start that ends up back at `shutdown`: report the latest `/runs` status (`failed`, `crashed` or `unknown`) and exit 1.
- The wait timeout (600 s) runs out: print `still not running after 10m (status=…); it may still come up. Check 'rce list'`, exit 1. Do not stop the device.
- A snapshot that isn't ready (locally, or a server `409 snapshot_not_ready`): print a clear message and tell the user to try again shortly.
- Trial-account start limits (`429`/`402`/`400 trial_max_runtime_exceeded`): show the friendly messages from §1.
- `firmware_version_display_name` or `display_name` is null: fall back to the id everywhere they are displayed.
- `running_device.instance_api_url` is null: fall back to `device_base_url/instance/<uuid>`.
- `channel.path` in `rce.toml` is relative: resolve it against the directory holding `rce.toml`. A `PATH` given on the command line is relative to the current directory.
- A directory without `manifest`: give a clear error before any network call.
- `rce sideload` returns 200 with a failure body: the existing `_sideload_ok` check raises `SideloadError`. Print Roku's message.
- `--all` with `stop` in a non-interactive shell without `--yes`: refuse with exit 2.

---

## 6. Tests (offline only, no live or pytest-plugin integration)

Keep `tests/test_core.py` and `tests/test_device.py`, minus the session test, and update them for the real status enum. Add:

- `tests/test_config.py`
  - walk-up discovery finds `rce.toml` in a parent directory
  - `rce.local.toml` overrides `rce.toml`, env overrides both, and flags override env
  - `sources` records the right origin for each key
  - a relative `channel.path` resolves against the config directory
  - a secret in `rce.toml` triggers the warning
  - `max_runtime = 0` and a non-int `device.id` raise `ConfigError`
  - with no config file, `load()` works and `require_token()` raises
- `tests/test_packaging.py`
  - `manifest` ends up at the zip root
  - excludes work at any depth (`.git/`, `node_modules/`, `*.zip`)
  - a missing manifest raises
- `tests/test_core.py` additions:
  - `list_devices` sends `items=0`
  - `pick_firmware`: an exact match wins, a substring match works with a null `display_name`, and the no-match error lists the options
  - `pick_snapshot`: by id, by name, the `live`/`base` keywords, a name beats a keyword, and not-ready raises
  - `wait_until_running`: `pending→running` returns; `pending→shutdown` raises with the `/runs` status; an early `shutdown` inside the grace period keeps waiting; `on_status` is called once per change
  - `RCEError` parses both `detail` forms (string and `{code, message}`)
  - `running_device` raises when the device is shut down
- `tests/test_device.py` addition: `from_running` uses `https://` + `instance_api_url`.

Use `respx` for HTTP the way the existing tests do. No test may hit the network. Build the fixtures from the spec's schemas (the `DeviceOut`, `SnapshotOut` and `FirmwareVersionOut` shapes in §0).

---

## 7. Acceptance checklist

### Offline
- [ ] `pytest -q` passes.
- [ ] `rce --help` lists exactly: init, config, firmware, snapshots, start, list/ls, sideload, exit, stop.
- [ ] In an empty temp directory: `rce init` creates both files, `.gitignore` contains `rce.local.toml`, and running `rce init` again refuses without `--force`.
- [ ] `rce config` from a subdirectory finds the parent `rce.toml` and shows the sources. The token is masked.
- [ ] `RCE_DEVICE_ID=99 rce config` shows `device_id = 99 (env:RCE_DEVICE_ID)`.
- [ ] `rce start` with no token gives a friendly error and exit code 2, with no traceback.
- [ ] `grep -rn "pytest_plugin\|rce_session\|websockets\|delete_device\|\"failed\"\|\"completed\"" src/` finds nothing, except `"failed"` inside `RUN_FAILED`.

### Live (ask the user first: this bills per device-minute)
- [ ] `rce config --check` prints the account and the org's max runtime.
- [ ] `rce list --all` shows every device in the project. The configured one has `*`.
- [ ] `rce firmware` and `rce snapshots` list real data. `live` and `base` are flagged.
- [ ] `rce start` boots with the configured snapshot and firmware and prints the instance and auto-stop time.
- [ ] `rce start` again prints "already running" without a second `/start`.
- [ ] `rce list` shows it as running, with `REMAINING`.
- [ ] `rce sideload` (a directory) installs it, and the channel launches.
- [ ] `rce exit` returns to Home. `rce exit --remove` uninstalls the dev channel.
- [ ] `rce stop --wait` stops it. `rce stop` again says "not running" and exits 0.

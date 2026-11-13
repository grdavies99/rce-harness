"""Offline unit tests: the RCE Core API is mocked with respx (shapes from spec v5.8.2)."""

import httpx
import pytest
import respx

from rce_harness import CoreClient, DeviceStartFailed, RCEConfig, RCEError

CORE = "https://api.rce.roku.com"

FIRMWARE = [
    {"firmware_version_id": "rce-fw:15.3.0-tv_beta", "device_type": "tv", "display_name": "15.3.0 TV"},
    {"firmware_version_id": "rce-fw:15.2.4-tv_prod", "device_type": "tv", "display_name": "15.2.4 TV"},
    {"firmware_version_id": "4711", "device_type": "tv", "display_name": None},
]

SNAPSHOTS = [
    {"id": 215, "name": "base-snapshot", "base": True, "live": False, "ready": True,
     "firmware_version_display_name": "15.2.4 TV", "created_at": "2026-09-01T10:00:00Z"},
    {"id": 216, "name": "live-snapshot", "base": False, "live": True, "ready": True,
     "firmware_version_display_name": "15.2.4 TV", "created_at": "2026-09-02T10:00:00Z"},
    {"id": 221, "name": "activated", "base": False, "live": False, "ready": True,
     "firmware_version_display_name": "15.2.4 TV", "created_at": "2026-09-03T10:00:00Z"},
    {"id": 230, "name": "uploading", "base": False, "live": False, "ready": False,
     "firmware_version_display_name": None, "created_at": "2026-09-04T10:00:00Z"},
]


def device(status, running=None, **kw):
    return {"id": 74, "name": "ci-tv", "device_type": "tv", "status": status,
            "firmware_version_id": "rce-fw:15.2.4-tv_prod", "firmware_version_display_name": "15.2.4 TV",
            "last_snapshot_id": 216, "last_snapshot_name": "live-snapshot",
            "created_at": "2026-09-01T10:00:00Z", "running_device": running, **kw}


RUNNING_DEVICE = {"instance_uuid": "abc-123", "instance_api_url": "device.rce.roku.com/instance/abc-123",
                  "created_at": "2026-11-12T10:00:00Z", "started_at": "2026-11-12T10:00:30Z",
                  "snapshot_id": 216, "snapshot_name": "live-snapshot",
                  "firmware_version_id": "rce-fw:15.2.4-tv_prod", "max_runtime": 1800}

no_sleep = lambda s: None  # noqa: E731


@pytest.fixture
def cfg():
    return RCEConfig(token="t", dev_password="pw")


@respx.mock
def test_auth_header_and_create(cfg):
    route = respx.post(f"{CORE}/api/v1/devices").mock(
        return_value=httpx.Response(201, json={"id": 74, "device_type": "tv", "status": "shutdown"}))
    dev = CoreClient(cfg).create_device("t", "tv")
    assert dev["id"] == 74
    assert route.calls[0].request.headers["Authorization"] == "Bearer t"


def test_bad_device_type(cfg):
    with pytest.raises(ValueError):
        CoreClient(cfg).create_device("t", "phone")


def test_no_delete_device(cfg):
    assert not hasattr(CoreClient(cfg), "delete_device")


@respx.mock
def test_errors_raise(cfg):
    respx.get(f"{CORE}/api/v1/devices/1").mock(return_value=httpx.Response(403, text="forbidden"))
    with pytest.raises(RCEError) as e:
        CoreClient(cfg).get_device(1)
    assert e.value.status == 403
    assert "forbidden" in str(e.value)


@respx.mock
def test_rce_error_string_detail(cfg):
    respx.get(f"{CORE}/api/v1/devices/1").mock(
        return_value=httpx.Response(404, json={"detail": "Device not found"}))
    with pytest.raises(RCEError) as e:
        CoreClient(cfg).get_device(1)
    assert e.value.code is None
    assert str(e.value).startswith("Device not found")


@respx.mock
def test_rce_error_object_detail(cfg):
    respx.post(f"{CORE}/api/v1/devices/1/start").mock(return_value=httpx.Response(
        429, json={"detail": {"code": "trial_device_run_limit", "message": "one device at a time"}}))
    with pytest.raises(RCEError) as e:
        CoreClient(cfg).start_device(1, 2, "fw", 60)
    assert e.value.status == 429
    assert e.value.code == "trial_device_run_limit"
    assert "one device at a time [trial_device_run_limit]" in str(e.value)


@respx.mock
def test_401_adds_token_hint(cfg):
    respx.get(f"{CORE}/api/v1/user/me").mock(return_value=httpx.Response(401, json={"detail": "bad token"}))
    with pytest.raises(RCEError, match="rce config --check"):
        CoreClient(cfg).whoami()


@respx.mock
def test_list_devices_sends_items_0(cfg):
    route = respx.get(f"{CORE}/api/v1/devices").mock(return_value=httpx.Response(200, json=[device("shutdown")]))
    assert CoreClient(cfg).list_devices()[0]["id"] == 74
    assert route.calls[0].request.url.params["items"] == "0"


@respx.mock
def test_firmware_and_snapshots_send_items_0(cfg):
    fw = respx.get(f"{CORE}/api/v1/firmwareVersions").mock(return_value=httpx.Response(200, json=FIRMWARE))
    sn = respx.get(f"{CORE}/api/v1/devices/74/snapshots").mock(return_value=httpx.Response(200, json=SNAPSHOTS))
    c = CoreClient(cfg)
    c.firmware_versions("tv")
    c.snapshots(74)
    assert dict(fw.calls[0].request.url.params) == {"items": "0", "device_type": "tv"}
    assert sn.calls[0].request.url.params["items"] == "0"


# -- pick_firmware ----------------------------------------------------------
@pytest.fixture
def fw_mock():
    with respx.mock:
        respx.get(f"{CORE}/api/v1/firmwareVersions").mock(return_value=httpx.Response(200, json=FIRMWARE))
        yield


def test_pick_firmware_default_is_first(cfg, fw_mock):
    assert CoreClient(cfg).pick_firmware("tv")["firmware_version_id"] == "rce-fw:15.3.0-tv_beta"


def test_pick_firmware_exact_wins(cfg, fw_mock):
    # "4711" is an exact id; no substring scan should pick something else first.
    assert CoreClient(cfg).pick_firmware("tv", "4711")["firmware_version_id"] == "4711"


def test_pick_firmware_substring_with_null_display_name(cfg, fw_mock):
    c = CoreClient(cfg)
    assert c.pick_firmware("tv", "PROD")["firmware_version_id"] == "rce-fw:15.2.4-tv_prod"
    assert c.pick_firmware("tv", "15.3.0 tv")["firmware_version_id"] == "rce-fw:15.3.0-tv_beta"
    assert c.pick_firmware("tv", "471")["firmware_version_id"] == "4711"


def test_pick_firmware_no_match_lists_options(cfg, fw_mock):
    with pytest.raises(RCEError) as e:
        CoreClient(cfg).pick_firmware("tv", "nope")
    assert e.value.status == 404
    assert "15.2.4 TV (rce-fw:15.2.4-tv_prod)" in str(e.value)
    assert "4711 (4711)" in str(e.value)


# -- pick_snapshot ----------------------------------------------------------
@pytest.fixture
def snap_mock():
    with respx.mock:
        respx.get(f"{CORE}/api/v1/devices/74/snapshots").mock(return_value=httpx.Response(200, json=SNAPSHOTS))
        yield


def test_pick_snapshot_by_id(cfg, snap_mock):
    c = CoreClient(cfg)
    assert c.pick_snapshot(74, 221)["name"] == "activated"
    assert c.pick_snapshot(74, "221")["name"] == "activated"


def test_pick_snapshot_by_name(cfg, snap_mock):
    assert CoreClient(cfg).pick_snapshot(74, "activated")["id"] == 221


def test_pick_snapshot_keywords(cfg, snap_mock, caplog):
    c = CoreClient(cfg)
    assert c.pick_snapshot(74)["id"] == 216  # default "live"
    assert c.pick_snapshot(74, "base")["id"] == 215
    assert "guided setup" in caplog.text


def test_pick_snapshot_name_beats_keyword(cfg):
    snaps = SNAPSHOTS + [{"id": 300, "name": "live", "base": False, "live": False, "ready": True}]
    with respx.mock:
        respx.get(f"{CORE}/api/v1/devices/74/snapshots").mock(return_value=httpx.Response(200, json=snaps))
        assert CoreClient(cfg).pick_snapshot(74, "live")["id"] == 300


def test_pick_snapshot_not_ready_raises(cfg, snap_mock):
    with pytest.raises(RCEError, match="not ready") as e:
        CoreClient(cfg).pick_snapshot(74, "uploading")
    assert e.value.status == 409


def test_pick_snapshot_missing_lists_names(cfg, snap_mock):
    with pytest.raises(RCEError, match="activated") as e:
        CoreClient(cfg).pick_snapshot(74, "missing")
    assert e.value.status == 404


# -- wait_until_running -------------------------------------------------------
@respx.mock
def test_wait_until_running(cfg):
    respx.get(f"{CORE}/api/v1/devices/74").mock(side_effect=[
        httpx.Response(200, json=device("pending")),
        httpx.Response(200, json=device("pending")),
        httpx.Response(200, json=device("running", RUNNING_DEVICE)),
    ])
    seen = []
    dev = CoreClient(cfg).wait_until_running(74, sleep=no_sleep, on_status=seen.append)
    assert dev["running_device"]["instance_uuid"] == "abc-123"
    assert seen == ["pending", "running"]


@respx.mock
def test_wait_until_running_pending_then_shutdown_fails(cfg):
    respx.get(f"{CORE}/api/v1/devices/74").mock(side_effect=[
        httpx.Response(200, json=device("pending")),
        httpx.Response(200, json=device("shutdown")),
    ])
    respx.get(f"{CORE}/api/v1/devices/74/runs").mock(return_value=httpx.Response(200, json=[
        {"instance_id": 9, "status": "crashed", "runtime": 12, "max_runtime": 1800},
        {"instance_id": 8, "status": "completed", "runtime": 1800, "max_runtime": 1800},
    ]))
    with pytest.raises(DeviceStartFailed, match="crashed") as e:
        CoreClient(cfg).wait_until_running(74, sleep=no_sleep)
    assert e.value.run_status == "crashed"


@respx.mock
def test_wait_until_running_early_shutdown_in_grace_keeps_waiting(cfg):
    respx.get(f"{CORE}/api/v1/devices/74").mock(side_effect=[
        httpx.Response(200, json=device("shutdown")),
        httpx.Response(200, json=device("pending")),
        httpx.Response(200, json=device("running", RUNNING_DEVICE)),
    ])
    runs = respx.get(f"{CORE}/api/v1/devices/74/runs")
    seen = []
    CoreClient(cfg).wait_until_running(74, sleep=no_sleep, on_status=seen.append)
    assert seen == ["shutdown", "pending", "running"]
    assert not runs.called


@respx.mock
def test_wait_until_running_shutdown_after_grace_fails(cfg):
    respx.get(f"{CORE}/api/v1/devices/74").mock(return_value=httpx.Response(200, json=device("shutdown")))
    respx.get(f"{CORE}/api/v1/devices/74/runs").mock(return_value=httpx.Response(200, json=[{"status": "failed"}]))
    with pytest.raises(DeviceStartFailed, match="failed"):
        CoreClient(cfg).wait_until_running(74, sleep=no_sleep, grace=0)


@respx.mock
def test_wait_until_running_timeout_does_not_stop(cfg):
    respx.get(f"{CORE}/api/v1/devices/74").mock(return_value=httpx.Response(200, json=device("pending")))
    stop = respx.post(f"{CORE}/api/v1/devices/74/stop")
    with pytest.raises(TimeoutError, match="may still come up"):
        CoreClient(cfg).wait_until_running(74, timeout=0, sleep=no_sleep)
    assert not stop.called


@respx.mock
def test_wait_until_stopped(cfg):
    respx.get(f"{CORE}/api/v1/devices/74").mock(side_effect=[
        httpx.Response(200, json=device("running", RUNNING_DEVICE)),
        httpx.Response(200, json=device("shutdown")),
    ])
    assert CoreClient(cfg).wait_until_stopped(74, sleep=no_sleep)["status"] == "shutdown"


# -- running_device -----------------------------------------------------------
@respx.mock
def test_running_device_returns_instance(cfg):
    respx.get(f"{CORE}/api/v1/devices/74").mock(
        return_value=httpx.Response(200, json=device("running", RUNNING_DEVICE)))
    assert CoreClient(cfg).running_device(74)["instance_uuid"] == "abc-123"


@respx.mock
def test_running_device_raises_when_shutdown(cfg):
    respx.get(f"{CORE}/api/v1/devices/74").mock(return_value=httpx.Response(200, json=device("shutdown")))
    with pytest.raises(RCEError, match=r"device 74 is not running \(status=shutdown\)"):
        CoreClient(cfg).running_device(74)

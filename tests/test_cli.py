"""CLI behaviour with the API mocked by respx (no network)."""

import json
import zipfile

import httpx
import pytest
import respx

from rce_harness.cli import main
from test_core import CORE, FIRMWARE, RUNNING_DEVICE, SNAPSHOTS, device

DEV = "https://device.rce.roku.com/instance/abc-123"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    for var in ("RCE_TOKEN", "RCE_DEV_PASSWORD", "RCE_DEVICE_ID", "RCE_SNAPSHOT", "RCE_FIRMWARE",
                "RCE_MAX_RUNTIME", "RCE_CHANNEL_PATH", "RCE_CORE_URL", "RCE_DEVICE_URL", "RCE_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "rce.toml").write_text('[device]\nid = 74\nfirmware = "prod"\nmax_runtime = 600\n')
    (tmp_path / "rce.local.toml").write_text('token = "t"\ndev_password = "pw"\n')
    monkeypatch.chdir(tmp_path)
    return tmp_path


@respx.mock
def test_start_boots_and_prints_summary(capsys):
    respx.get(f"{CORE}/api/v1/devices/74").mock(side_effect=[
        httpx.Response(200, json=device("shutdown")),
        httpx.Response(200, json=device("running", RUNNING_DEVICE)),
    ])
    respx.get(f"{CORE}/api/v1/firmwareVersions").mock(return_value=httpx.Response(200, json=FIRMWARE))
    respx.get(f"{CORE}/api/v1/devices/74/snapshots").mock(return_value=httpx.Response(200, json=SNAPSHOTS))
    start = respx.post(f"{CORE}/api/v1/devices/74/start").mock(return_value=httpx.Response(202, json={}))

    assert main(["start"]) == 0
    assert json.loads(start.calls[0].request.content) == {
        "snapshot_id": 216, "firmware_version_id": "rce-fw:15.2.4-tv_prod", "max_runtime": 600}
    out = capsys.readouterr().out
    assert "device 74 running" in out
    assert "snapshot   live-snapshot (#216)" in out
    assert "instance   abc-123" in out
    assert "max runtime 30m (auto-stops ~" in out


@respx.mock
def test_start_when_running_does_not_post(capsys):
    respx.get(f"{CORE}/api/v1/devices/74").mock(return_value=httpx.Response(200, json=device("running", RUNNING_DEVICE)))
    start = respx.post(f"{CORE}/api/v1/devices/74/start")
    assert main(["start"]) == 0
    assert "already running (instance abc-123)" in capsys.readouterr().out
    assert not start.called


@respx.mock
def test_start_pending_no_wait(capsys):
    respx.get(f"{CORE}/api/v1/devices/74").mock(return_value=httpx.Response(200, json=device("pending")))
    start = respx.post(f"{CORE}/api/v1/devices/74/start")
    assert main(["start", "--no-wait"]) == 0
    assert "already starting" in capsys.readouterr().err
    assert not start.called


@respx.mock
def test_start_trial_limit_is_friendly(capsys):
    respx.get(f"{CORE}/api/v1/devices/74").mock(return_value=httpx.Response(200, json=device("shutdown")))
    respx.get(f"{CORE}/api/v1/firmwareVersions").mock(return_value=httpx.Response(200, json=FIRMWARE))
    respx.get(f"{CORE}/api/v1/devices/74/snapshots").mock(return_value=httpx.Response(200, json=SNAPSHOTS))
    respx.post(f"{CORE}/api/v1/devices/74/start").mock(return_value=httpx.Response(
        429, json={"detail": {"code": "trial_device_run_limit", "message": "limit"}}))
    assert main(["start"]) == 1
    assert "trial accounts can run one device at a time" in capsys.readouterr().err


@respx.mock
def test_list_filters_and_marks(capsys):
    other = {**device("shutdown"), "id": 80, "name": "other"}
    respx.get(f"{CORE}/api/v1/devices").mock(return_value=httpx.Response(
        200, json=[device("running", RUNNING_DEVICE), other]))
    assert main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["ID", "NAME", "TYPE", "STATUS", "FIRMWARE", "SNAPSHOT", "REMAINING", "INSTANCE"]
    assert len(lines) == 2 and lines[1].startswith("*") and "abc-123" in lines[1]

    assert main(["ls", "--all", "--json"]) == 0
    assert [d["id"] for d in json.loads(capsys.readouterr().out)] == [74, 80]


@respx.mock
def test_list_empty(capsys):
    respx.get(f"{CORE}/api/v1/devices").mock(return_value=httpx.Response(200, json=[device("shutdown")]))
    assert main(["list"]) == 0
    assert "no active emulators" in capsys.readouterr().out


@respx.mock
def test_stop_is_idempotent(capsys):
    respx.get(f"{CORE}/api/v1/devices/74").mock(return_value=httpx.Response(200, json=device("shutdown")))
    stop = respx.post(f"{CORE}/api/v1/devices/74/stop")
    assert main(["stop"]) == 0
    assert "device 74 is not running" in capsys.readouterr().out
    assert not stop.called


@respx.mock
def test_stop_all_refuses_without_tty(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    respx.get(f"{CORE}/api/v1/devices").mock(return_value=httpx.Response(200, json=[device("running", RUNNING_DEVICE)]))
    assert main(["stop", "--all"]) == 2
    assert "--yes" in capsys.readouterr().err


@respx.mock
def test_stop_all_yes(capsys):
    respx.get(f"{CORE}/api/v1/devices").mock(return_value=httpx.Response(
        200, json=[device("running", RUNNING_DEVICE), {**device("pending"), "id": 80}]))
    s74 = respx.post(f"{CORE}/api/v1/devices/74/stop").mock(return_value=httpx.Response(202, json={}))
    s80 = respx.post(f"{CORE}/api/v1/devices/80/stop").mock(return_value=httpx.Response(202, json={}))
    assert main(["stop", "--all", "--yes"]) == 0
    assert s74.called and s80.called
    assert capsys.readouterr().out.split("\n")[:2] == ["stopping 74", "stopping 80"]


@respx.mock
def test_sideload_directory(env, capsys):
    chan = env / "chan"
    (chan / "source").mkdir(parents=True)
    (chan / "manifest").write_text("title=x\n")
    (chan / "source" / "main.brs").write_text("sub main()\nend sub\n")
    respx.get(f"{CORE}/api/v1/devices/74").mock(return_value=httpx.Response(200, json=device("running", RUNNING_DEVICE)))
    uploaded = {}

    def install(request):
        body = request.read()
        start = body.index(b"PK\x03\x04")
        uploaded["names"] = zipfile.ZipFile(__import__("io").BytesIO(body[start:])).namelist()
        return httpx.Response(200, text='<font color="red">Install Success.</font>')

    respx.post(f"{DEV}/sideload/plugin_install").mock(side_effect=install)
    assert main(["sideload", "chan"]) == 0
    assert "installed: Install Success." in capsys.readouterr().out
    assert sorted(uploaded["names"]) == ["manifest", "source/main.brs"]


@respx.mock
def test_sideload_not_running(env, capsys):
    (env / "app.zip").write_bytes(b"PK\x03\x04")
    respx.get(f"{CORE}/api/v1/devices/74").mock(return_value=httpx.Response(200, json=device("shutdown")))
    assert main(["sideload", "app.zip"]) == 1
    assert "device 74 is not running (status=shutdown). Run 'rce start'." in capsys.readouterr().err


@respx.mock
def test_exit_presses_home(capsys):
    respx.get(f"{CORE}/api/v1/devices/74").mock(return_value=httpx.Response(200, json=device("running", RUNNING_DEVICE)))
    home = respx.post(f"{DEV}/api/v0/input/keypress/Home").mock(return_value=httpx.Response(200))
    assert main(["exit"]) == 0
    assert home.called
    assert "channel exited (Home)" in capsys.readouterr().out

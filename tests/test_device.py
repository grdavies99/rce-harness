import httpx
import pytest
import respx

from rce_harness import DeviceClient, Key, RCEConfig
from rce_harness.device import SideloadError

DEV = "https://device.rce.roku.com/instance/abc-123"


@pytest.fixture
def dev():
    return DeviceClient(RCEConfig(token="t", dev_password="pw"), "abc-123")


@respx.mock
def test_keypress(dev):
    route = respx.post(f"{DEV}/api/v0/input/keypress/Home").mock(return_value=httpx.Response(200))
    dev.press(Key.HOME, delay=0)
    assert route.calls[0].request.headers["Authorization"] == "Bearer t"


@respx.mock
def test_type_text_url_encodes(dev):
    a = respx.post(f"{DEV}/api/v0/input/keypress/Lit_a").mock(return_value=httpx.Response(200))
    sp = respx.post(f"{DEV}/api/v0/input/keypress/Lit_%20").mock(return_value=httpx.Response(200))
    dev.type_text("a a", delay=0)
    assert a.call_count == 2 and sp.call_count == 1


@respx.mock
def test_active_app_and_device_info(dev):
    respx.get(f"{DEV}/api/v0/port/8060/http/query/active-app").mock(return_value=httpx.Response(
        200, text='<active-app><app id="dev" version="1.0.0">My App</app></active-app>'))
    respx.get(f"{DEV}/api/v0/port/8060/http/query/device-info").mock(return_value=httpx.Response(
        200, text="<device-info><model-number>2900X</model-number><software-version>15.2.4</software-version></device-info>"))
    assert dev.active_app() == {"id": "dev", "name": "My App", "version": "1.0.0"}
    assert dev.device_info()["software-version"] == "15.2.4"


@respx.mock
def test_sideload_detects_functional_failure(dev, tmp_path):
    z = tmp_path / "app.zip"
    z.write_bytes(b"PK\x03\x04")
    respx.post(f"{DEV}/sideload/plugin_install").mock(return_value=httpx.Response(
        200, text='<font color="red">Install Failure: Compilation Failed.</font>'))
    with pytest.raises(SideloadError, match="Compilation Failed"):
        dev.sideload(z)


@respx.mock
def test_sideload_success_uses_x_authorization(dev, tmp_path):
    z = tmp_path / "app.zip"
    z.write_bytes(b"PK\x03\x04")
    route = respx.post(f"{DEV}/sideload/plugin_install").mock(return_value=httpx.Response(
        200, text='<font color="red">Install Success.</font>'))
    assert "Install Success" in dev.sideload(z)
    assert route.calls[0].request.headers["X-Authorization"] == "Bearer t"


def test_sideload_requires_password(tmp_path):
    d = DeviceClient(RCEConfig(token="t"), "abc-123")
    with pytest.raises(SideloadError):
        d.sideload(tmp_path / "x.zip")


def test_from_running_uses_instance_api_url():
    cfg = RCEConfig(token="t")
    d = DeviceClient.from_running(cfg, {"instance_uuid": "abc-123",
                                        "instance_api_url": "device-eu.rce.roku.com/instance/abc-123"})
    assert d.base_url == "https://device-eu.rce.roku.com/instance/abc-123"


def test_from_running_falls_back_without_instance_api_url():
    d = DeviceClient.from_running(RCEConfig(token="t"), {"instance_uuid": "abc-123", "instance_api_url": None})
    assert d.base_url == DEV


def test_remove_dev_app_requires_password():
    d = DeviceClient(RCEConfig(token="t"), "abc-123")
    with pytest.raises(SideloadError, match="dev-mode password"):
        d.remove_dev_app()


@respx.mock
def test_remove_dev_app_detects_failure(dev):
    respx.post(f"{DEV}/sideload/plugin_install").mock(return_value=httpx.Response(
        200, text='<font color="red">Delete Failed: no dev channel installed.</font>'))
    with pytest.raises(SideloadError, match="Delete Failed"):
        dev.remove_dev_app()


@respx.mock
def test_remove_dev_app_success(dev):
    route = respx.post(f"{DEV}/sideload/plugin_install").mock(return_value=httpx.Response(
        200, text='<font color="red">Delete Succeeded.</font>'))
    assert dev.remove_dev_app() == "Delete Succeeded."
    assert route.calls[0].request.headers["X-Authorization"] == "Bearer t"

import pytest

from rce_harness import ConfigError, RCEConfig
from rce_harness.config import find_config


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path):
    write(tmp_path / "rce.toml", """
[device]
id = 74
snapshot = "activated"
firmware = "prod"
max_runtime = 900

[channel]
path = "channel"
""")
    return tmp_path


def test_walk_up_finds_parent_config(repo):
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert find_config(sub) == (repo / "rce.toml").resolve()
    cfg = RCEConfig.load(cwd=sub, environ={})
    assert cfg.config_path == (repo / "rce.toml").resolve()
    assert cfg.device_id == 74


def test_precedence_local_env_flag(repo):
    write(repo / "rce.local.toml", 'token = "local-token"\n[device]\nid = 75\nsnapshot = "base"\n')
    cfg = RCEConfig.load(cwd=repo, environ={})
    assert (cfg.device_id, cfg.snapshot, cfg.token) == (75, "base", "local-token")

    env = {"RCE_DEVICE_ID": "76", "RCE_MAX_RUNTIME": "600"}
    cfg = RCEConfig.load(cwd=repo, environ=env)
    assert (cfg.device_id, cfg.max_runtime) == (76, 600)

    cfg = RCEConfig.load(cwd=repo, environ=env, overrides={"device_id": 77, "max_runtime": None})
    assert (cfg.device_id, cfg.max_runtime) == (77, 600)


def test_sources(repo):
    write(repo / "rce.local.toml", 'token = "local-token"\n')
    cfg = RCEConfig.load(cwd=repo, environ={"RCE_FIRMWARE": "beta"}, overrides={"device_id": 1})
    assert cfg.sources["token"] == "rce.local.toml"
    assert cfg.sources["snapshot"] == "rce.toml"
    assert cfg.sources["firmware"] == "env:RCE_FIRMWARE"
    assert cfg.sources["device_id"] == "flag"
    assert cfg.sources["dev_password"] == "default"


def test_empty_local_secrets_are_not_set(repo):
    write(repo / "rce.local.toml", 'token = ""\ndev_password = ""\n')
    cfg = RCEConfig.load(cwd=repo, environ={})
    assert cfg.token is None and cfg.sources["token"] == "default"


def test_relative_channel_path_resolves_against_config_dir(repo):
    sub = repo / "deep"
    sub.mkdir()
    cfg = RCEConfig.load(cwd=sub, environ={})
    assert cfg.channel_path == (repo / "channel").resolve()


def test_secret_in_rce_toml_warns(tmp_path, capsys):
    write(tmp_path / "rce.toml", 'token = "oops"\n')
    cfg = RCEConfig.load(cwd=tmp_path, environ={})
    assert cfg.token == "oops"
    assert "secret 'token' found in rce.toml" in capsys.readouterr().err


def test_unknown_keys_warn(tmp_path, capsys):
    write(tmp_path / "rce.toml", '[device]\nsnapshop = "live"\n[extra]\nx = 1\n')
    RCEConfig.load(cwd=tmp_path, environ={})
    err = capsys.readouterr().err
    assert "device.snapshop" in err and "[extra]" in err


@pytest.mark.parametrize("body", [
    "[device]\nmax_runtime = 0\n",
    '[device]\nid = "abc"\n',
    "[device]\nid = -1\n",
    "[device]\nsnapshot = 1.5\n",
    '[channel]\nexclude = "node_modules"\n',
])
def test_invalid_values_raise(tmp_path, body):
    write(tmp_path / "rce.toml", body)
    with pytest.raises(ConfigError):
        RCEConfig.load(cwd=tmp_path, environ={})


def test_invalid_env_int_raises(tmp_path):
    with pytest.raises(ConfigError, match="RCE_DEVICE_ID"):
        RCEConfig.load(cwd=tmp_path, environ={"RCE_DEVICE_ID": "seventy"})


def test_no_config_file(tmp_path):
    cfg = RCEConfig.load(cwd=tmp_path, environ={})
    assert cfg.config_path is None
    assert cfg.max_runtime == 1800 and cfg.snapshot == "live"
    with pytest.raises(ConfigError, match="token"):
        cfg.require_token()
    with pytest.raises(ConfigError, match="no device id"):
        cfg.require_device_id()


def test_explicit_config_path(tmp_path):
    other = write(tmp_path / "elsewhere" / "rce.toml", "[device]\nid = 9\n")
    assert RCEConfig.load(cwd=tmp_path, config_path=other, environ={}).device_id == 9
    assert RCEConfig.load(cwd=tmp_path, environ={"RCE_CONFIG": str(other)}).device_id == 9
    with pytest.raises(ConfigError, match="not found"):
        RCEConfig.load(cwd=tmp_path, config_path=tmp_path / "missing.toml", environ={})


def test_bad_toml_raises(tmp_path):
    write(tmp_path / "rce.toml", "[device\n")
    with pytest.raises(ConfigError, match="cannot parse"):
        RCEConfig.load(cwd=tmp_path, environ={})

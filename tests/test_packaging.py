import zipfile

import pytest

from rce_harness import ConfigError
from rce_harness.config import DEFAULT_EXCLUDE
from rce_harness.packaging import zip_channel


@pytest.fixture
def channel(tmp_path):
    root = tmp_path / "chan"
    files = [
        "manifest", "source/main.brs", "components/Main.xml", "images/icon.png",
        ".git/HEAD", "node_modules/x/index.js", "components/.git/config",
        "old.zip", "images/nested/build.zip", "rce.toml", "rce.local.toml", ".DS_Store",
    ]
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f)
    return root


def names(z):
    with zipfile.ZipFile(z) as zf:
        return set(zf.namelist())


def test_manifest_at_root_and_excludes_at_any_depth(channel, tmp_path):
    z = zip_channel(channel, DEFAULT_EXCLUDE, tmp_path / "out.zip")
    assert names(z) == {"manifest", "source/main.brs", "components/Main.xml", "images/icon.png"}


def test_dest_inside_src_is_not_included(channel):
    z = zip_channel(channel, [], channel / "self.zip")
    assert "self.zip" not in names(z)
    assert "manifest" in names(z)


def test_path_pattern_excludes(channel, tmp_path):
    z = zip_channel(channel, ["images/*"], tmp_path / "out.zip")
    assert not any(n.startswith("images/") for n in names(z))


def test_missing_manifest_raises(tmp_path):
    (tmp_path / "src").mkdir()
    with pytest.raises(ConfigError, match="no 'manifest'"):
        zip_channel(tmp_path / "src", [], tmp_path / "out.zip")

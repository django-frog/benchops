"""Asset shipping: only this app's manifest entries leave the local bench,
and the remote merge replaces exactly those entries — other apps' bundles
and stale entries of this app are handled correctly.
"""

import json
import os
import platform
import sys

import pytest

from benchops.assets import collect_app_manifests, resolve_bench_path, ship_manifests
from benchops.runner import LocalRunner


def _make_bench(root, manifests, built_files=()):
    assets = root / "sites" / "assets"
    assets.mkdir(parents=True)
    (root / "apps" / "myapp").mkdir(parents=True)
    for name, entries in manifests.items():
        (assets / name).write_text(json.dumps(entries))
    for rel in built_files:
        path = root / "sites" / rel.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    return root


def test_collect_keeps_only_this_apps_entries(tmp_path):
    bench = _make_bench(
        tmp_path,
        {
            "assets.json": {
                "myapp.bundle.js": "/assets/myapp/dist/js/myapp.bundle.NEW.js",
                "desk.bundle.js": "/assets/frappe/dist/js/desk.bundle.AAA.js",
            },
            "assets-rtl.json": {
                "rtl_myapp.bundle.css": "/assets/myapp/dist/css-rtl/myapp.bundle.NEW.css",
            },
        },
        built_files=[
            "/assets/myapp/dist/js/myapp.bundle.NEW.js",
            "/assets/myapp/dist/css-rtl/myapp.bundle.NEW.css",
        ],
    )

    manifests = collect_app_manifests(bench, "myapp")

    assert manifests == {
        "assets.json": {"myapp.bundle.js": "/assets/myapp/dist/js/myapp.bundle.NEW.js"},
        "assets-rtl.json": {"rtl_myapp.bundle.css": "/assets/myapp/dist/css-rtl/myapp.bundle.NEW.css"},
    }


def test_collect_does_not_match_apps_sharing_a_name_prefix(tmp_path):
    bench = _make_bench(
        tmp_path,
        {"assets.json": {"myapp_extra.bundle.js": "/assets/myapp_extra/dist/js/x.js"}},
    )

    assert collect_app_manifests(bench, "myapp") == {"assets.json": {}}


def test_collect_requires_a_built_bench(tmp_path):
    (tmp_path / "sites" / "assets").mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="bench build --app myapp"):
        collect_app_manifests(tmp_path, "myapp")


def test_collect_rejects_manifest_pointing_at_missing_bundle(tmp_path):
    bench = _make_bench(
        tmp_path,
        {"assets.json": {"myapp.bundle.js": "/assets/myapp/dist/js/myapp.bundle.GONE.js"}},
    )

    with pytest.raises(FileNotFoundError, match="GONE"):
        collect_app_manifests(bench, "myapp")


def test_resolve_bench_path_requires_sites_dir(tmp_path):
    (tmp_path / "apps" / "myapp").mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="not inside a Frappe bench"):
        resolve_bench_path(tmp_path / "apps" / "myapp")

    (tmp_path / "sites").mkdir()
    assert resolve_bench_path(tmp_path / "apps" / "myapp") == tmp_path.resolve()


@pytest.mark.skipif(platform.system() == "Windows", reason="remote bench is always POSIX")
def test_ship_merges_into_remote_manifest(tmp_path):
    remote = tmp_path / "remote"
    _make_bench(
        remote,
        {
            "assets.json": {
                "myapp.bundle.js": "/assets/myapp/dist/js/myapp.bundle.OLD.js",
                "myapp.removed.bundle.js": "/assets/myapp/dist/js/myapp.removed.bundle.OLD.js",
                "desk.bundle.js": "/assets/frappe/dist/js/desk.bundle.AAA.js",
            },
        },
    )
    (remote / "sites" / "common_site_config.json").write_text(json.dumps({}))
    (remote / "env" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, remote / "env" / "bin" / "python")
    local_tmp = tmp_path / "local"
    local_tmp.mkdir()

    ship_manifests(
        LocalRunner(),
        {
            "assets.json": {"myapp.bundle.js": "/assets/myapp/dist/js/myapp.bundle.NEW.js"},
            "assets-rtl.json": {"rtl_myapp.bundle.css": "/assets/myapp/dist/css-rtl/myapp.bundle.NEW.css"},
        },
        "myapp",
        str(local_tmp),
        str(remote),
    )

    assets_dir = remote / "sites" / "assets"
    assert json.loads((assets_dir / "assets.json").read_text()) == {
        "desk.bundle.js": "/assets/frappe/dist/js/desk.bundle.AAA.js",
        "myapp.bundle.js": "/assets/myapp/dist/js/myapp.bundle.NEW.js",
    }
    assert json.loads((assets_dir / "assets-rtl.json").read_text()) == {
        "rtl_myapp.bundle.css": "/assets/myapp/dist/css-rtl/myapp.bundle.NEW.css",
    }
    assert not list((remote / "sites").glob(".benchops-*"))
    assert not list(assets_dir.glob("*.benchops.tmp"))

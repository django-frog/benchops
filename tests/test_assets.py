"""Asset shipping: only this app's manifest entries leave the local bench,
and only when the bundles they reference exist on disk.
"""

import json

import pytest

from benchops.assets import collect_app_manifests, resolve_bench_path


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

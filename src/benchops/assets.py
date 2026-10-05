"""Asset shipping: carry a locally built app's bundle manifest to the remote bench.

`bench build` writes hashed bundles (e.g. `myapp.bundle.X7KQ2.js`) into
`apps/<app>/<app>/public/dist/`, which the deploy package carries.
But the map from bundle name to hashed file lives outside the app, in the
bench-wide `sites/assets/assets.json` (and `assets-rtl.json` for RTL CSS),
and Frappe additionally caches it in redis_cache under `assets_json`.
Without updating both, the remote keeps serving the previous build.

The local manifests are filtered down to this app's entries and shipped in
the deploy package; the remote agent merges them into the remote manifests —
never copied wholesale, since the local bench rarely has the same set of
apps (or app versions) as the remote one.
"""

import json
from pathlib import Path

ASSET_MANIFESTS = ("assets.json", "assets-rtl.json")


def resolve_bench_path(app_dir: Path) -> Path:
    """Return the bench root that contains `app_dir` (i.e. `<bench>/apps/<app>`)."""
    bench = app_dir.resolve().parent.parent
    if not (bench / "sites").is_dir():
        raise FileNotFoundError(
            f"'{app_dir}' is not inside a Frappe bench (no 'sites' directory at '{bench}'); "
            "run 'benchops deploy' from the bench root."
        )
    return bench


def collect_app_manifests(bench_path: Path, app_name: str) -> dict[str, dict[str, str]]:
    """Extract this app's entries from the local bench's asset manifests.

    Raises FileNotFoundError if the bench has never been built, or if the
    manifest references a bundle that no longer exists on disk — a sign the
    manifest and the dist/ output are out of sync and shipping would break
    the remote UI.
    """
    assets_dir = bench_path / "sites" / "assets"
    if not (assets_dir / ASSET_MANIFESTS[0]).is_file():
        raise FileNotFoundError(
            f"No asset manifest found at '{assets_dir / ASSET_MANIFESTS[0]}'. "
            f"Run 'bench build --app {app_name}' (e.g. as a pre-local hook) before deploying."
        )

    prefix = f"/assets/{app_name}/"
    manifests: dict[str, dict[str, str]] = {}
    for name in ASSET_MANIFESTS:
        manifest_path = assets_dir / name
        if not manifest_path.is_file():
            continue
        entries = {
            key: value
            for key, value in json.loads(manifest_path.read_text()).items()
            if str(value).startswith(prefix)
        }
        missing = [value for value in entries.values() if not (bench_path / "sites" / value.lstrip("/")).is_file()]
        if missing:
            raise FileNotFoundError(
                f"'{name}' references bundle(s) that do not exist locally: {', '.join(missing)}. "
                f"Re-run 'bench build --app {app_name}' before deploying."
            )
        manifests[name] = entries
    return manifests

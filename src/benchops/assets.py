"""Asset shipping: carry a locally built app's bundle manifest to the remote bench.

`bench build` writes hashed bundles (e.g. `myapp.bundle.X7KQ2.js`) into
`apps/<app>/<app>/public/dist/`, which the deploy tarball already carries.
But the map from bundle name to hashed file lives outside the app, in the
bench-wide `sites/assets/assets.json` (and `assets-rtl.json` for RTL CSS),
and Frappe additionally caches it in redis_cache under `assets_json`.
Without updating both, the remote keeps serving the previous build.

The local manifests are filtered down to this app's entries and merged into
the remote ones — never copied wholesale, since the local bench rarely has
the same set of apps (or app versions) as the remote one.
"""

import json
import posixpath
import shlex
from pathlib import Path

from benchops.runner import Runner

ASSET_MANIFESTS = ("assets.json", "assets-rtl.json")

# Runs on the remote host under the bench's own virtualenv (cwd = bench root),
# so it can rely on the `redis` package Frappe itself depends on. Mirrors what
# Frappe's esbuild does after a build: merge manifests, then drop the cached
# copy so the next request re-reads it from disk.
_REMOTE_MERGE_SCRIPT = """
import json, os, sys

payload_path = sys.argv[1]
with open(payload_path) as f:
    payload = json.load(f)
os.remove(payload_path)

prefix = "/assets/%s/" % payload["app"]
for name, entries in payload["manifests"].items():
    path = os.path.join("sites", "assets", name)
    try:
        with open(path) as f:
            current = json.load(f)
    except FileNotFoundError:
        current = {}
    merged = {k: v for k, v in current.items() if not str(v).startswith(prefix)}
    merged.update(entries)
    tmp_path = path + ".benchops.tmp"
    with open(tmp_path, "w") as f:
        json.dump(merged, f, indent=4)
    os.replace(tmp_path, path)
    print("Merged %d %s entries into %s" % (len(entries), payload["app"], path))

try:
    with open(os.path.join("sites", "common_site_config.json")) as f:
        redis_url = json.load(f)["redis_cache"]
    import redis
    redis.Redis.from_url(redis_url).delete("assets_json")
except Exception as exc:
    print("Warning: could not clear assets_json from redis_cache (%s); run 'bench clear-cache'." % exc, file=sys.stderr)
"""


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


def ship_manifests(
    runner: Runner,
    manifests: dict[str, dict[str, str]],
    app_name: str,
    local_tmp_dir: str,
    remote_bench_path: str,
) -> None:
    """Upload this app's manifest entries and merge them into the remote bench."""
    local_payload = Path(local_tmp_dir) / f".benchops-{app_name}-assets.json"
    local_payload.write_text(json.dumps({"app": app_name, "manifests": manifests}))

    remote_payload = posixpath.join(remote_bench_path, "sites", local_payload.name)
    runner.put(str(local_payload), remote_payload)
    runner.run(
        f"./env/bin/python -c {shlex.quote(_REMOTE_MERGE_SCRIPT)} {shlex.quote(remote_payload)}",
        cwd=remote_bench_path,
    )

"""Local build step and build outputs.

Build outputs are paths inside the app that a build regenerates: Frappe's
own `<app>/public/dist`, plus whatever an SPA frontend writes (e.g. a
frappe-ui/Vite app's `<app>/public/<name>/` and `<app>/www/<name>.html`).
They never travel in the git snapshot — whether or not .gitignore covers
them, and even if old builds were committed — but are shipped as-is and
replace the remote copy wholesale, so the HTML and the hashed assets it
references always come from the same build.

Outputs beyond `public/dist` come from `[tool.benchops] build_outputs` in
the app's pyproject.toml, or, if that's absent, are detected from the
`outDir` / `indexHtmlPath` settings of a Vite config in the app.
"""

import hashlib
import re
import tomllib
from pathlib import Path, PurePosixPath

from benchops.runner import Runner

VITE_CONFIG_NAMES = ("vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.mts")
_VITE_PATH_RE = re.compile(r"""\b(outDir|indexHtmlPath)\s*:\s*["']([^"']+)["']""")


class BuildError(Exception):
    """Raised when build outputs are misconfigured, missing, or inconsistent."""


def _relative_to_app(app_dir: Path, path: Path, source: str) -> str:
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(app_dir.resolve())
    except ValueError:
        raise BuildError(f"Build output '{path}' (from {source}) is outside the app directory.") from None
    if not rel.parts:
        raise BuildError(f"Build output from {source} cannot be the app directory itself.")
    return rel.as_posix()


def detect_vite_outputs(app_dir: Path) -> list[str]:
    """Find `outDir`/`indexHtmlPath` in Vite configs at the app root or one level below."""
    outputs: list[str] = []
    configs = [app_dir / name for name in VITE_CONFIG_NAMES]
    configs += [child / name for child in sorted(app_dir.iterdir()) if child.is_dir() for name in VITE_CONFIG_NAMES]
    for config in configs:
        if not config.is_file():
            continue
        for _, value in _VITE_PATH_RE.findall(config.read_text()):
            rel = _relative_to_app(app_dir, config.parent / value, config.name)
            if rel not in outputs:
                outputs.append(rel)
    return outputs


def load_build_outputs(app_dir: Path, app_name: str) -> list[str]:
    """Return the app's build outputs, as paths relative to the app root."""
    outputs = [f"{app_name}/public/dist"]
    pyproject = app_dir / "pyproject.toml"
    configured = None
    if pyproject.is_file():
        configured = tomllib.loads(pyproject.read_text()).get("tool", {}).get("benchops", {}).get("build_outputs")

    if configured is None:
        extra = detect_vite_outputs(app_dir)
    else:
        if not isinstance(configured, list) or not all(isinstance(p, str) for p in configured):
            raise BuildError("[tool.benchops] build_outputs must be a list of paths.")
        extra = [_relative_to_app(app_dir, app_dir / PurePosixPath(p), "pyproject.toml") for p in configured]

    for rel in extra:
        if rel not in outputs:
            outputs.append(rel)
    return outputs


def run_build(runner: Runner, app_dir: Path, bench: Path, app_name: str) -> None:
    """Install the app's node dependencies and run `bench build --app`.

    `bench build` also runs the `build` script of the app's root package.json
    (Frappe passes --run-build-command), which is how frappe-ui apps build
    their SPA frontend, so the frontend is never built separately here.
    """
    if (app_dir / "package.json").is_file():
        runner.run("yarn install", cwd=str(app_dir))
    runner.run(f"bench build --app {app_name}", cwd=str(bench))


def existing_outputs(app_dir: Path, outputs: list[str]) -> list[str]:
    """The outputs that exist on disk. Only `public/dist` may be missing
    (an app without bundles); a missing SPA output means the build didn't run."""
    present = []
    for rel in outputs:
        if (app_dir / rel).exists():
            present.append(rel)
        elif not rel.endswith("/public/dist"):
            raise BuildError(
                f"Build output '{rel}' does not exist. Run the build (or drop --skip-build), "
                "or fix [tool.benchops] build_outputs."
            )
    return present


def check_html_asset_references(app_dir: Path, app_name: str, outputs: list[str]) -> None:
    """Fail if an HTML build output references /assets/<app>/... files that
    don't exist — the blank-page failure mode of a stale or partial build."""
    pattern = re.compile(rf"""["'(]/assets/{re.escape(app_name)}/([^"'()\s?#]+)""")
    missing = []
    for rel in outputs:
        root = app_dir / rel
        html_files = [root] if root.is_file() else sorted(root.rglob("*.html"))
        for html in html_files:
            if html.suffix != ".html":
                continue
            for asset in pattern.findall(html.read_text(errors="replace")):
                if not (app_dir / app_name / "public" / asset).is_file():
                    missing.append(f"{html.relative_to(app_dir).as_posix()} → /assets/{app_name}/{asset}")
    if missing:
        raise BuildError(
            "Built HTML references assets that do not exist (stale or partial build):\n  " + "\n  ".join(missing)
        )


def outputs_digest(app_dir: Path, outputs: list[str]) -> str | None:
    """Content hash over all build outputs, or None if there are none."""
    files = []
    for rel in outputs:
        path = app_dir / rel
        files += [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
    if not files:
        return None
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.relative_to(app_dir).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()

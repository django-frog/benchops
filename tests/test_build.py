"""Build outputs: discovery from Vite configs and pyproject.toml, presence
checks, and the HTML-to-asset consistency check that catches blank pages
before deploying them.
"""

import pytest

from benchops.build import (
    BuildError,
    check_html_asset_references,
    detect_vite_outputs,
    existing_outputs,
    load_build_outputs,
)

# Shape of a frappe-ui (doppio) app's frontend/vite.config.js, as in basma.
FRAPPE_UI_VITE = """
export default defineConfig({
	plugins: [
		frappeui({
			buildConfig: {
				outDir: "../myapp/public/calendar",
				indexHtmlPath: "../myapp/www/calendar.html",
				emptyOutDir: true,
			},
		}),
	],
	build: { outDir: '../myapp/public/calendar', emptyOutDir: true },
})
"""


def write(path, content=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_detects_frappe_ui_outputs(tmp_path):
    write(tmp_path / "frontend" / "vite.config.js", FRAPPE_UI_VITE)

    assert detect_vite_outputs(tmp_path) == ["myapp/public/calendar", "myapp/www/calendar.html"]


def test_detection_ignores_outputs_outside_the_app(tmp_path):
    write(tmp_path / "frontend" / "vite.config.ts", 'build: { outDir: "../../elsewhere" }')

    with pytest.raises(BuildError, match="outside the app"):
        detect_vite_outputs(tmp_path)


def test_dist_is_always_an_output_and_vite_is_detected(tmp_path):
    write(tmp_path / "frontend" / "vite.config.js", FRAPPE_UI_VITE)

    assert load_build_outputs(tmp_path, "myapp") == [
        "myapp/public/dist",
        "myapp/public/calendar",
        "myapp/www/calendar.html",
    ]


def test_pyproject_overrides_detection(tmp_path):
    write(tmp_path / "frontend" / "vite.config.js", FRAPPE_UI_VITE)
    write(tmp_path / "pyproject.toml", '[tool.benchops]\nbuild_outputs = ["myapp/public/app"]\n')

    assert load_build_outputs(tmp_path, "myapp") == ["myapp/public/dist", "myapp/public/app"]


def test_pyproject_can_disable_detection(tmp_path):
    write(tmp_path / "frontend" / "vite.config.js", FRAPPE_UI_VITE)
    write(tmp_path / "pyproject.toml", "[tool.benchops]\nbuild_outputs = []\n")

    assert load_build_outputs(tmp_path, "myapp") == ["myapp/public/dist"]


@pytest.mark.parametrize("value", ['"myapp/public/app"', "[1]", '["../outside"]', '["."]'])
def test_pyproject_rejects_bad_build_outputs(tmp_path, value):
    write(tmp_path / "pyproject.toml", f"[tool.benchops]\nbuild_outputs = {value}\n")

    with pytest.raises(BuildError):
        load_build_outputs(tmp_path, "myapp")


def test_missing_dist_is_fine_but_missing_spa_output_is_not(tmp_path):
    write(tmp_path / "myapp" / "www" / "calendar.html")

    assert existing_outputs(tmp_path, ["myapp/public/dist", "myapp/www/calendar.html"]) == ["myapp/www/calendar.html"]
    with pytest.raises(BuildError, match="myapp/public/calendar"):
        existing_outputs(tmp_path, ["myapp/public/calendar"])


def test_html_references_are_checked_against_built_files(tmp_path):
    write(tmp_path / "myapp" / "public" / "calendar" / "assets" / "index-A.js")
    write(
        tmp_path / "myapp" / "www" / "calendar.html",
        '<script src="/assets/myapp/calendar/assets/index-A.js"></script>'
        '<link href="/assets/frappe/css/other-app.css">'
        "<style>body { background: url(/assets/myapp/calendar/assets/index-A.js?v=1) }</style>",
    )
    outputs = ["myapp/public/calendar", "myapp/www/calendar.html"]

    check_html_asset_references(tmp_path, "myapp", outputs)

    write(tmp_path / "myapp" / "public" / "calendar" / "index.html", "<link href='/assets/myapp/calendar/assets/gone.css'>")
    with pytest.raises(BuildError, match="gone.css"):
        check_html_asset_references(tmp_path, "myapp", outputs)

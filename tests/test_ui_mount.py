"""The container serves the built UI and the API from one origin; API routes must keep priority over the static files."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import mount_ui


def make_app(tmp_path, with_build=True):
    if with_build:
        (tmp_path / "assets").mkdir()
        (tmp_path / "index.html").write_text("<html><body>SentinelSQL UI</body></html>", encoding="utf-8")
        (tmp_path / "assets" / "app.js").write_text("console.log('ui')", encoding="utf-8")
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app, mount_ui(app, str(tmp_path))


def test_the_ui_is_served_at_the_root_and_assets_are_reachable(tmp_path):
    app, mounted = make_app(tmp_path)
    client = TestClient(app)
    assert mounted
    assert "SentinelSQL UI" in client.get("/").text
    assert client.get("/assets/app.js").status_code == 200


def test_api_routes_win_over_the_static_files(tmp_path):
    app, _ = make_app(tmp_path)
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_unknown_paths_are_404_not_the_ui(tmp_path):
    app, _ = make_app(tmp_path)
    assert TestClient(app).get("/nope.js").status_code == 404


def test_nothing_is_mounted_without_a_build(tmp_path):
    app, mounted = make_app(tmp_path, with_build=False)
    assert not mounted and TestClient(app).get("/").status_code == 404

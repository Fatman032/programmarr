"""index.html must be sent with no-cache, or an update shows the old screen until Ctrl+F5."""

import asyncio
import sys
import types
from pathlib import Path

import main

MAIN_PY = Path(main.__file__)


def test_index_html_is_no_cache(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html>new build</html>")
    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)

    r = main.spa_index_response()

    assert r.headers["cache-control"] == "no-cache"
    assert b"new build" in r.body


def _app_with_a_build(tmp_path, monkeypatch):
    """main.py's real code, run as if it sat next to a built frontend.

    The SPA routes are only registered when backend/static exists at import time, and
    STATIC_DIR is taken from main.py's own location — so the module's source is run with
    __file__ pointing into a temp backend/ folder that has static/index.html in it."""
    backend = tmp_path / "backend"
    (backend / "static" / "assets").mkdir(parents=True)
    (backend / "static" / "index.html").write_text("<html>new build</html>")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("PROGRAMMARR_DATA", str(data))  # no config.json, so no auth

    module = types.ModuleType("main_with_build")
    module.__file__ = str(backend / "main.py")
    monkeypatch.setitem(sys.modules, "main_with_build", module)
    exec(compile(MAIN_PY.read_text(encoding="utf-8"), str(MAIN_PY), "exec"), module.__dict__)
    return module.app


def _get(app, path):
    """One GET through the app's full ASGI stack (middleware + routing), the way a browser
    request arrives. (Starlette's TestClient needs httpx2, which isn't a dependency.)"""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
             "root_path": "", "query_string": b"", "headers": [(b"host", b"testserver")],
             "client": ("127.0.0.1", 1234), "server": ("testserver", 80)}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], headers, body


def test_the_spa_route_sends_no_cache(tmp_path, monkeypatch):
    app = _app_with_a_build(tmp_path, monkeypatch)
    for path in ("/", "/channels"):
        status, headers, body = _get(app, path)
        assert status == 200, path
        assert headers["cache-control"] == "no-cache", path
        assert b"new build" in body, path

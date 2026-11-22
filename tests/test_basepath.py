"""The UI is served under any proxy prefix: the build's placeholder is swapped per request."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from sheetir.web.basepath import BasePathMiddleware, forwarded_prefix

PROXY = {"X-Forwarded-Prefix": "/6/proxy/8765/"}


def _client(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "index.html").write_text('<script src="/__SHEETTWIN_BASE__/_next/a.js"></script>')
    (tmp_path / "app" / "index.html").write_text('<a href="/__SHEETTWIN_BASE__/app/">x</a>')
    (tmp_path / "logo.png").write_bytes(b"\x89PNG/__SHEETTWIN_BASE__")
    app = FastAPI()
    app.add_middleware(BasePathMiddleware)

    @app.get("/api/echo")
    def echo():
        return {"path": "/__SHEETTWIN_BASE__"}

    app.mount("/", StaticFiles(directory=tmp_path, html=True))
    return TestClient(app)


def test_without_proxy_the_ui_is_at_the_root(tmp_path):
    r = _client(tmp_path).get("/")
    assert r.text == '<script src="/_next/a.js"></script>'
    assert r.headers["content-length"] == str(len(r.content))


def test_proxy_prefix_goes_into_the_page(tmp_path):
    r = _client(tmp_path).get("/app/", headers=PROXY)
    assert r.text == '<a href="/6/proxy/8765/app/">x</a>'
    assert r.headers["cache-control"] == "no-cache" and "etag" not in r.headers


def test_a_cached_copy_is_never_reused_across_prefixes(tmp_path):
    c = _client(tmp_path)
    etag = c.get("/logo.png").headers["etag"]
    r = c.get("/app/", headers={**PROXY, "If-None-Match": etag})
    assert r.status_code == 200


def test_redirects_stay_under_the_prefix(tmp_path):
    r = _client(tmp_path).get("/app", headers=PROXY, follow_redirects=False)
    assert 300 <= r.status_code < 400
    assert r.headers["location"] == "/6/proxy/8765/app/"


def test_api_and_binary_files_are_untouched(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/echo", headers=PROXY).json() == {"path": "/__SHEETTWIN_BASE__"}
    assert c.get("/logo.png", headers=PROXY).content == b"\x89PNG/__SHEETTWIN_BASE__"


def test_only_a_plain_path_is_accepted_as_prefix():
    assert forwarded_prefix([(b"x-forwarded-prefix", b"/6/proxy/8765/")]) == "/6/proxy/8765"
    assert forwarded_prefix([(b"x-forwarded-prefix", b'/a"><script>')]) == ""
    assert forwarded_prefix([]) == ""

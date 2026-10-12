"""Files that are hard or impossible to parse: each gets a reason, partial results where possible, and a status."""

import zipfile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from sheetir import build_ir
from sheetir.loader import WorkbookError, load
from sheetir.sample import make_quotation
from sheetir.web.app import app

OLE2 = bytes.fromhex("d0cf11e0a1b11ae1")


def _book(path):
    wb = Workbook()
    ws = wb.active
    ws.append(["Item", "Qty", "Price", "Amount"])
    ws.append(["Pen", 2, 10, "=B2*C2"])
    ws.append(["Ink", 3, 5, "=B3*C3"])
    wb.save(path)
    return path


def _broken_styles(src, dst):
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for item in zin.infolist():
            zout.writestr(item, b"<styleSheet><broken" if item.filename == "xl/styles.xml" else zin.read(item.filename))
    return dst


@pytest.mark.parametrize("data, kind", [
    (OLE2 + b"\0" * 512, "legacy_xls"),
    (OLE2 + b"\0" * 64 + "EncryptionInfo".encode("utf-16-le"), "encrypted"),
    (b"Item,Qty\nPen,2\n", "not_a_workbook"),
])
def test_unreadable_files_say_why(tmp_path, data, kind):
    p = tmp_path / "book.xlsx"
    p.write_bytes(data)
    with pytest.raises(WorkbookError) as e:
        load(p)
    assert e.value.kind == kind


def test_corrupt_package(tmp_path):
    p = tmp_path / "book.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "not a workbook inside")
    with pytest.raises(WorkbookError) as e:
        load(p)
    assert e.value.kind == "corrupt"


def test_broken_formatting_is_dropped_and_the_rest_parsed(tmp_path):
    p = _broken_styles(_book(tmp_path / "ok.xlsx"), tmp_path / "bad.xlsx")
    raw = load(p)
    assert raw.warnings == ["styles_dropped"] and len(raw.cells) == 12
    res = build_ir(p, tmp_path / "bad.db")
    assert res.verification["status"] == "verified" and res.counts["formula_cells"] == 2


def test_build_reports_coverage_and_status(tmp_path):
    res = build_ir(make_quotation(tmp_path / "q.xlsx"), tmp_path / "q.db")
    v = res.verification
    assert v["status"] == "partial"  # Report!B6 reads INDIRECT: its sources cannot be followed
    assert v["coverage"]["understood"] == 107 and v["coverage"]["formulas"] == 108
    assert [x["cell"] for x in v["coverage"]["not_understood"]] == ["Report!B6"]
    assert all(c["pass"] for c in v["checks"].values())


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHEETIR_DATA", str(tmp_path / "data"))
    return TestClient(app)


def test_failed_upload_is_kept_explained_and_removable(client):
    r = client.post("/api/workbooks", files={"file": ("old.xls", OLE2 + b"\0" * 512)}).json()
    assert r["failed"] and r["error"]["kind"] == "corrupt" and r["status"] == "failed"
    (w,) = client.get("/api/workbooks").json()
    assert (w["id"], w["name"], w["status"], w["error"]["kind"]) == (r["id"], "old.xls", "failed", "corrupt")
    again = client.post(f"/api/workbooks/{r['id']}/rebuild").json()
    assert again["failed"] and again["error"]["kind"] == "corrupt"
    assert client.delete(f"/api/workbooks/{r['id']}").json() == {"deleted": r["id"]}
    assert client.get("/api/workbooks").json() == []


def test_unsupported_type_names_the_problem(client):
    r = client.post("/api/workbooks", files={"file": ("book.numbers", b"a,b\n1,2\n")})
    assert r.status_code == 400 and r.json()["detail"]["kind"] == "unsupported_type" and r.json()["detail"]["ext"] == ".numbers"


def test_summary_carries_the_verification(client):
    wid = client.post("/api/workbooks/sample").json()["id"]
    s = client.get(f"/api/workbooks/{wid}/summary").json()
    assert s["status"] == "partial" and s["verification"]["checks"]["content"]["matched"] == 251
    (w,) = client.get("/api/workbooks").json()
    assert w["status"] == "partial" and w["error"] is None

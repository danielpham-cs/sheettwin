import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

import sheetir.web.app
from sheetir.web import knowledge
from sheetir.web.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHEETIR_DATA", str(tmp_path))
    return TestClient(app)


def _docx(paragraphs: list[tuple[str | None, str]], table: list[list[str]] | None = None) -> bytes:
    """A minimal .docx: (style, text) paragraphs, then a table."""
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    def p(style, text):
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"
    body = "".join(p(s, t) for s, t in paragraphs)
    if table:
        body += "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc>{p(None, c)}</w:tc>" for c in row) + "</w:tr>"
                                    for row in table) + "</w:tbl>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {w}><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def _pdf(text: str) -> bytes:
    """A one-page PDF with `text` in Helvetica."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


# ---- extraction -------------------------------------------------------------------------------------------------------

def test_extract_markdown_docx_and_pdf():
    assert knowledge.extract("a.md", "# Title\r\n\r\n\r\n\r\nBody".encode()) == "# Title\n\nBody"
    doc = knowledge.extract("spec.docx", _docx([("Heading1", "Glossary"), (None, "GM means gross margin.")],
                                               [["Code", "Branch"], ["HN", "Hà Nội"]]))
    assert doc.splitlines()[:2] == ["# Glossary", "GM means gross margin."] and "HN | Hà Nội" in doc
    assert "Fiscal year starts in April" in knowledge.extract("policy.pdf", _pdf("Fiscal year starts in April"))


@pytest.mark.parametrize("name, data, kind", [
    ("old.doc", b"\xd0\xcf\x11\xe0", "unsupported_type"),
    ("empty.md", b"   \n ", "no_text"),
    ("broken.docx", b"not a zip", "unreadable"),
    ("broken.pdf", b"%PDF-1.4 nothing", "unreadable"),
])
def test_extract_refuses_what_it_cannot_read(name, data, kind):
    with pytest.raises(knowledge.DocError) as e:
        knowledge.extract(name, data)
    assert e.value.kind == kind


def test_chunks_cut_at_paragraphs_and_cap_long_ones():
    parts = knowledge._chunks("\n\n".join(["a" * 500] * 5) + "\n\n" + "b" * 3000)
    assert all(len(c) <= knowledge.CHUNK for c in parts) and "".join(parts).count("b") == 3000


# ---- API ------------------------------------------------------------------------------------------------------------

def test_instructions_and_documents_round_trip(client):
    pid = client.post("/api/projects", json={"name": "q"}).json()["id"]
    assert client.get(f"/api/projects/{pid}/knowledge").json()["instructions"] == ""
    assert client.put(f"/api/projects/{pid}/instructions", json={"instructions": "  Money is in triệu VND.  "}).json() \
        == {"instructions": "Money is in triệu VND."}
    too_long = "x" * (knowledge.MAX_INSTRUCTIONS + 1)
    assert client.put(f"/api/projects/{pid}/instructions", json={"instructions": too_long}).status_code == 400

    doc = client.post(f"/api/projects/{pid}/docs", files={"file": ("glossary.md", b"GM = gross margin")}).json()
    again = client.post(f"/api/projects/{pid}/docs", files={"file": ("glossary.md", b"GM = gross margin")}).json()
    assert again["id"] == doc["id"] and doc["chars"] == 17
    k = client.get(f"/api/projects/{pid}/knowledge").json()
    assert k["instructions"] == "Money is in triệu VND." and [d["name"] for d in k["docs"]] == ["glossary.md"]
    assert client.get(f"/api/projects/{pid}/docs/{doc['id']}/text").json() == {"text": "GM = gross margin"}

    bad = client.post(f"/api/projects/{pid}/docs", files={"file": ("x.doc", b"\xd0\xcf")})
    assert bad.status_code == 400 and bad.json()["detail"]["kind"] == "unsupported_type"
    assert client.delete(f"/api/projects/{pid}/docs/{doc['id']}").json() == {"deleted": doc["id"]}
    assert client.get(f"/api/projects/{pid}/knowledge").json()["docs"] == []
    assert client.delete(f"/api/projects/{pid}/docs/ffffffffffff").status_code == 404
    assert client.delete(f"/api/projects/{pid}/docs/NOT-AN-ID").status_code == 400


def test_the_default_project_has_knowledge_too(client):
    client.put("/api/projects/default/instructions", json={"instructions": "Be brief."})
    assert client.get("/api/projects/default/knowledge").json()["instructions"] == "Be brief."
    assert client.get("/api/projects/ffffffffffff/knowledge").status_code == 404


def test_deleting_a_project_deletes_its_documents(client, tmp_path):
    pid = client.post("/api/projects", json={"name": "q"}).json()["id"]
    client.post(f"/api/projects/{pid}/docs", files={"file": ("a.md", b"text")})
    client.delete(f"/api/projects/{pid}")
    assert not (tmp_path / "projects" / pid).exists()


# ---- context ---------------------------------------------------------------------------------------------------------

def test_short_documents_go_in_whole(client, tmp_path):
    pid = client.post("/api/projects", json={"name": "q"}).json()["id"]
    client.put(f"/api/projects/{pid}/instructions", json={"instructions": "Answer in one sentence."})
    client.post(f"/api/projects/{pid}/docs", files={"file": ("a.md", b"Alpha doc")})
    client.post(f"/api/projects/{pid}/docs", files={"file": ("b.md", b"Beta doc")})
    block, used = knowledge.context_for(tmp_path, pid, "anything")
    assert "## Instructions\nAnswer in one sentence." in block and "### a.md\nAlpha doc" in block
    assert [(u["name"], u["whole"]) for u in used] == [("a.md", True), ("b.md", True)]
    assert knowledge.context_for(tmp_path, client.post("/api/projects", json={"name": "e"}).json()["id"], "x") == ("", [])


def test_long_documents_give_the_relevant_passages(client, tmp_path):
    pid = client.post("/api/projects", json={"name": "q"}).json()["id"]
    filler = "\n\n".join(f"Section {i}: general remarks about the weather and office hours." * 8 for i in range(60))
    needle = "Commission rule: branch managers earn 3 percent commission on net revenue."
    client.post(f"/api/projects/{pid}/docs", files={"file": ("handbook.md", (filler + "\n\n" + needle).encode())})
    client.post(f"/api/projects/{pid}/docs", files={"file": ("short.md", b"Unrelated note.")})
    block, used = knowledge.context_for(tmp_path, pid, "What commission do branch managers earn?")
    assert needle in block and len(block) < knowledge.INLINE_BUDGET + 2000
    assert {u["name"]: u["whole"] for u in used}.get("handbook.md") is False


# ---- chat --------------------------------------------------------------------------------------------------------------

class _AgentLLM:
    model = "scripted-agent"

    def __init__(self, *actions):
        self.actions, self.systems = list(actions), []

    def json(self, system, user, schema, name="answer"):
        return {"route": "data"}

    def chat_json(self, messages, schema, **kw):
        self.systems.append(messages[0]["content"])
        return schema(**self.actions.pop(0))


def _stream(client, pid, body):
    with client.stream("POST", f"/api/projects/{pid}/chat/stream", json=body) as res:
        text = "".join(res.iter_text())
    return json.loads(text.strip().split("\n\n")[-1].split("data: ", 1)[1])


def test_the_agent_gets_the_project_context_and_may_quote_its_numbers(client, monkeypatch):
    pid = client.post("/api/projects", json={"name": "q"}).json()["id"]
    client.post(f"/api/workbooks/sample?project={pid}")
    client.put(f"/api/projects/{pid}/instructions", json={"instructions": "Always mention the margin target."})
    client.post(f"/api/projects/{pid}/docs", files={"file": ("targets.md", b"The margin target is 35 percent.")})
    llm = _AgentLLM(
        {"thought": "count", "tool": "run_sql", "args": {"sql": "SELECT COUNT(*) AS n FROM t_quote__t1"}},
        {"thought": "done", "tool": "final_answer",
         "args": {"answer": "There are 20 quote lines; the margin target is 35 percent (targets.md).", "result_ids": ["r1"]}})
    monkeypatch.setattr(sheetir.web.app, "get_llm", lambda: llm)
    r = _stream(client, pid, {"message": "How many quote lines?", "lang": "en"})
    assert r["source"] == "agent" and r["grounded"], r  # 35 comes from the document, 20 from the query
    assert "Always mention the margin target." in llm.systems[0] and "The margin target is 35 percent." in llm.systems[0]
    assert r["knowledge"] == [{"id": r["knowledge"][0]["id"], "name": "targets.md", "whole": True}]


def test_rule_answers_do_not_claim_the_documents(client):
    pid = client.post("/api/projects", json={"name": "q"}).json()["id"]
    client.post(f"/api/workbooks/sample?project={pid}")
    client.post(f"/api/projects/{pid}/docs", files={"file": ("a.md", b"text")})
    r = _stream(client, pid, {"message": "What issues are there?", "lang": "en"})
    assert r["source"] == "rules" and "knowledge" not in r

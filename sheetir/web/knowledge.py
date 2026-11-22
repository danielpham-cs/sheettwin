"""A project's knowledge: instructions the user writes, and documents (Markdown, text, Word, PDF) they add, given to the
language model as context with every question asked in the project.

    data/projects/<project>/project.json            "instructions": the user's text
    data/projects/<project>/docs/<id>/<file>        the uploaded document, as it came
    data/projects/<project>/docs/<id>/text.txt      its text, extracted once at upload
    data/projects/<project>/docs/<id>/meta.json     {id, name, size, uploaded, chars, truncated}

where <id> is the first 12 hex digits of the document's SHA-256 (the same document added twice is kept once).

context_for() builds the block the prompts get: the instructions, then the documents. Short documents go in whole; when
they are too long together, the passages most relevant to the question (BM25 over ~1200-character chunks) are given
instead, in document order, within a fixed budget, so a prompt stays small whatever the user adds.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from rank_bm25 import BM25Okapi

from ..catalog.index import tokenize
from . import projects
from .provenance import now

DOC_SUFFIXES = (".md", ".markdown", ".txt", ".docx", ".pdf")
MAX_DOC_BYTES = 20 * 1024 * 1024
MAX_TEXT = 400_000            # characters kept from one document
MAX_INSTRUCTIONS = 8_000
INLINE_BUDGET = 14_000        # characters of documents in one prompt
CHUNK = 1_200
_ID_RE = re.compile(r"^[0-9a-f]{12}$")


class DocError(Exception):
    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status, self.kind = status, kind


# ---- text extraction -------------------------------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx(data: bytes) -> str:
    """Paragraphs and tables of a .docx, in order: headings as Markdown headings, table rows as `a | b | c`."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as e:
        raise DocError(400, "unreadable", "this .docx file cannot be read") from e

    def para(p) -> str:
        out = []
        for node in p.iter():
            if node.tag == _W + "t":
                out.append(node.text or "")
            elif node.tag == _W + "tab":
                out.append("\t")
            elif node.tag in (_W + "br", _W + "cr"):
                out.append("\n")
        text = "".join(out).strip()
        style = p.find(f"{_W}pPr/{_W}pStyle")
        level = re.search(r"(\d)$", style.get(_W + "val", "")) if style is not None else None
        if text and style is not None and "heading" in style.get(_W + "val", "").lower() and level:
            text = "#" * int(level[1]) + " " + text
        return text

    lines = []
    body = root.find(_W + "body")
    for el in (body if body is not None else []):
        if el.tag == _W + "p":
            lines.append(para(el))
        elif el.tag == _W + "tbl":
            for row in el.iter(_W + "tr"):
                cells = [" ".join(para(p) for p in tc.iter(_W + "p")).strip() for tc in row.iter(_W + "tc")]
                lines.append(" | ".join(cells))
            lines.append("")
    return "\n".join(lines)


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except (PdfReadError, ValueError, KeyError) as e:
        raise DocError(400, "unreadable", "this PDF cannot be read") from e
    return "\n\n".join(f"[page {i}]\n{t}" for i, t in enumerate(pages, start=1) if t)


def extract(name: str, data: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if suffix not in DOC_SUFFIXES:
        raise DocError(400, "unsupported_type", f"{suffix or 'this'} files are not supported: add .md, .txt, .docx or .pdf")
    if suffix == ".docx":
        text = _docx(data)
    elif suffix == ".pdf":
        text = _pdf(data)
    else:
        text = data.decode("utf-8-sig", errors="replace")
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()
    if not text:
        raise DocError(400, "no_text", "no text found in this document (a scanned PDF has none to read)")
    return text


# ---- storage ---------------------------------------------------------------------------------------------------------

def _docs_dir(data: Path, pid: str) -> Path:
    projects.workbooks_of(data, pid, set())  # 404 for an unknown project
    return projects.folder(data, pid) / "docs"


def _doc_dir(data: Path, pid: str, doc_id: str) -> Path:
    if not _ID_RE.match(doc_id):
        raise DocError(400, "bad_id", "bad document id")
    d = _docs_dir(data, pid) / doc_id
    if not (d / "meta.json").exists():
        raise DocError(404, "not_found", "document not found")
    return d


def add(data: Path, pid: str, name: str, content: bytes) -> dict:
    name = Path(name or "document.txt").name
    if len(content) > MAX_DOC_BYTES:
        raise DocError(400, "too_large", f"documents up to {MAX_DOC_BYTES // (1024 * 1024)} MB can be added")
    text = extract(name, content)
    doc_id = hashlib.sha256(content).hexdigest()[:12]
    d = _docs_dir(data, pid) / doc_id
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(content)
    (d / "text.txt").write_text(text[:MAX_TEXT], encoding="utf-8")
    meta = {"id": doc_id, "name": name, "size": len(content), "uploaded": now(), "order": time.time_ns(),
            "chars": min(len(text), MAX_TEXT), "truncated": len(text) > MAX_TEXT}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def listing(data: Path, pid: str) -> list[dict]:
    root = _docs_dir(data, pid)
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in root.glob("*/meta.json")] if root.is_dir() else []
    return sorted(docs, key=lambda m: (m.get("order", 0), m["uploaded"]))  # in the order they were added


def remove(data: Path, pid: str, doc_id: str) -> None:
    shutil.rmtree(_doc_dir(data, pid, doc_id))


def text(data: Path, pid: str, doc_id: str) -> str:
    return (_doc_dir(data, pid, doc_id) / "text.txt").read_text(encoding="utf-8")


# ---- context for the prompts -----------------------------------------------------------------------------------------

def _chunks(doc_text: str) -> list[str]:
    """~CHUNK-character pieces, cut at paragraph ends where possible."""
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", doc_text):
        while len(para) > CHUNK:  # one long paragraph: cut it on its own
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:CHUNK])
            para = para[CHUNK:]
        if cur and len(cur) + len(para) + 2 > CHUNK:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur.strip():
        out.append(cur)
    return out


CONTEXT_INTRO = """# Project context (set by the user)
The user gave these for this project. Follow the instructions where they apply. Use the documents as background:
what terms mean, business rules, how the data is organized. The rules above still hold: figures about the workbooks come
from their data; when something in your answer comes from a document, name the document."""


def context_for(data: Path, pid: str, question: str) -> tuple[str, list[dict]]:
    """The context block for a question in project `pid`, and the documents it draws on
    ([{id, name, whole}] where `whole` is false when only passages were given). Empty when there is nothing to give."""
    instructions = projects.instructions(data, pid).strip()
    docs = [(m, text(data, pid, m["id"])) for m in listing(data, pid)]
    if not instructions and not docs:
        return "", []
    parts = [CONTEXT_INTRO]
    if instructions:
        parts.append(f"## Instructions\n{instructions}")
    used = []
    if docs and sum(len(t) for _, t in docs) <= INLINE_BUDGET:
        parts.append("## Documents")
        for m, t in docs:
            parts.append(f"### {m['name']}\n{t}")
            used.append({"id": m["id"], "name": m["name"], "whole": True})
    elif docs:
        pieces = [(i, j, c) for i, (_, t) in enumerate(docs) for j, c in enumerate(_chunks(t))]
        bm25 = BM25Okapi([tokenize(c) or ["_"] for _, _, c in pieces])
        scores = bm25.get_scores(tokenize(question) or ["_"])
        chosen, size = [], 0
        for k in sorted(range(len(pieces)), key=lambda k: -scores[k]):
            if size + len(pieces[k][2]) > INLINE_BUDGET:
                continue
            chosen.append(k)
            size += len(pieces[k][2])
        parts.append("## Documents (the passages most relevant to the question; the documents are longer)")
        for i, (m, _) in enumerate(docs):
            mine = sorted((pieces[k][1], pieces[k][2]) for k in chosen if pieces[k][0] == i)
            if mine:
                parts.append(f"### {m['name']}\n" + "\n[…]\n".join(c for _, c in mine))
                used.append({"id": m["id"], "name": m["name"], "whole": False})
    return "\n\n".join(parts), used

"""M0 runtime check (docs/excel-agent-system-design.md §12): is the configured model server usable for SheetTwin?

Runs against the endpoint in config/settings.yaml (or the environment, see sheetir/config.py) and reports:

    network        whether this machine can reach the internet (M0 wants every check to pass with it unplugged)
    endpoint       the server answers /models, and serves the configured model
    speed          tokens per second for one request (prefill + decode, from the server's usage counts)
    thinking off   the reply carries no reasoning when thinking is switched off
    structured     structured output (response_format json_schema) works, or SheetIR has to fall back to the prompt
    json valid     a pydantic-validated JSON reply comes back within the retries
    concurrency    total tokens per second with llm.max_concurrency requests in flight
    embedding      (--embedding) the local embedding model loads from embedding.model_path and encodes text

    uv run python bench/m0_runtime.py [--config config/settings.yaml] [--requests 16] [--embedding]
                                      [-o docs/m0-results.json] [--md docs/m0-results.md]

Exit status 0 when every check that ran passed.
"""

from __future__ import annotations

import argparse
import json
import platform
import socket
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sheetir.config import apply_offline, is_local_url, llm_block_reason, load_settings  # noqa: E402
from sheetir.llm import LLM, LLMError, urllib_transport  # noqa: E402

LONG_PROMPT = ("Viết một đoạn khoảng 200 từ bằng tiếng Việt mô tả cách một chi nhánh bán lẻ theo dõi doanh thu theo "
               "tháng trong Excel. Không dùng tiêu đề hay gạch đầu dòng.")


class TableGuess(BaseModel):
    range: str
    header_rows: list[int]
    total_rows: list[int]
    title: str | None


TABLE_PROMPT = """A sheet "DT_T1" holds these cells (row: values):
1: BÁO CÁO DOANH THU THÁNG 1/2026
3: Mã CN | Chi nhánh | Doanh thu (triệu đồng) | Số đơn
4: HN01 | Hà Nội | 1250.5 | 320
5: HP01 | Hải Phòng | 980 | 210
6: DN01 | Đà Nẵng | 760.25 | 180
7: Tổng cộng | | 2990.75 | 710
Columns are A to D. Return the data table: its A1 range including the header, the header row numbers, the total row
numbers and the title above it."""


class Recorder:
    """A transport that keeps the last raw reply, to look for reasoning the client strips."""

    def __init__(self):
        self.last = None

    def __call__(self, url, body, headers, timeout):
        self.last = urllib_transport(url, body, headers, timeout)
        return self.last


def check(name: str, fn) -> dict:
    t0 = time.perf_counter()
    try:
        ok, detail = fn()
    except Exception as e:  # a check that crashes has failed, the others still run
        ok, detail = False, f"{type(e).__name__}: {e}"[:300]
    r = {"check": name, "ok": ok, "detail": detail, "seconds": round(time.perf_counter() - t0, 2)}
    mark = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
    print(f"[{mark}] {name:<13} {detail}")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--requests", type=int, default=16, help="requests for the concurrency check")
    ap.add_argument("--embedding", action="store_true", help="also load and run the local embedding model")
    ap.add_argument("-o", "--out", help="write the results as JSON")
    ap.add_argument("--md", help="write the results as a Markdown table")
    args = ap.parse_args()

    s = load_settings(args.config)
    apply_offline(s)
    blocked = llm_block_reason(s.model_copy(update={"llm": s.llm.model_copy(update={"enabled": True})}))
    if blocked:
        print(f"refusing to test: {blocked}")
        return 2
    rec = Recorder()
    llm = LLM.from_settings(s, transport=rec)
    llm.log_path = None
    print(f"endpoint {s.llm.base_url}  model {s.llm.model}  thinking_param {llm.thinking_param}\n")
    results = []

    def network():
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=2).close()
            return None, "internet reachable (unplug the network to run M0 for real)"
        except OSError:
            return None, "internet not reachable"

    def endpoint():
        headers = {"Authorization": f"Bearer {s.llm.api_key}"} if s.llm.api_key else {}
        req = urllib.request.Request(f"{s.llm.base_url.rstrip('/')}/models", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            ids = [m.get("id") for m in json.load(r).get("data", [])]
        served = s.llm.model in ids
        return served, f"{'serves' if served else 'does NOT serve'} {s.llm.model}; models: {', '.join(ids[:8]) or '-'}"

    speed = {}

    def single():
        t0 = time.perf_counter()
        text = llm.chat([{"role": "user", "content": LONG_PROMPT}], max_tokens=400)
        dt = time.perf_counter() - t0
        u = (rec.last or {}).get("usage") or {}
        out = u.get("completion_tokens") or max(1, len(text) // 4)
        speed.update(tokens=out, seconds=dt, prompt=u.get("prompt_tokens"))
        est = "" if u.get("completion_tokens") else " (no usage from the server: estimated from length)"
        return True, f"{out / dt:.1f} tok/s end to end, {out} tokens in {dt:.1f}s{est}"

    def thinking_off():
        llm.chat([{"role": "user", "content": "Trả lời đúng một từ: thủ đô của Việt Nam?"}], max_tokens=200)
        msg = rec.last["choices"][0]["message"]
        content = msg.get("content") or ""
        leaked = "<think>" in content or bool(msg.get("reasoning_content") or msg.get("reasoning"))
        return not leaked, ("reasoning still produced: set llm.thinking_param for this server" if leaked
                            else f"no reasoning in the reply: {content.strip()[:60]!r}")

    def structured():
        before = llm.structured_output
        r = llm.chat_json([{"role": "user", "content": TABLE_PROMPT}], TableGuess, name="m0_table", max_tokens=300)
        native = before and llm.structured_output
        good = r.range.upper().startswith("A3") and 3 in r.header_rows and 7 in r.total_rows
        detail = ("native response_format" if native else "fell back to the schema in the prompt") + \
                 f"; answer {r.range} header {r.header_rows} total {r.total_rows} ({'right' if good else 'wrong'})"
        return native, detail

    def concurrency():
        n = args.requests
        t0 = time.perf_counter()
        before = llm.usage["completion_tokens"]
        llm.map(lambda i: llm.chat([{"role": "user", "content": f"Đếm từ 1 đến {10 + i} bằng chữ số, cách nhau dấu phẩy."}],
                                   max_tokens=120), range(n))
        dt = time.perf_counter() - t0
        toks = llm.usage["completion_tokens"] - before
        single_rate = speed["tokens"] / speed["seconds"] if speed else None
        gain = f", {toks / dt / single_rate:.1f}x one request" if single_rate and toks else ""
        return True, f"{n} requests, {s.llm.max_concurrency} at a time: {toks / dt:.1f} tok/s total{gain} in {dt:.1f}s"

    def embedding():
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            return False, "sentence-transformers is not installed (uv run --with sentence-transformers ...)"
        path = Path(s.embedding.model_path)
        if not path.exists():
            return False, f"no model at {path}"
        t0 = time.perf_counter()
        model = SentenceTransformer(str(path), local_files_only=True)
        load = time.perf_counter() - t0
        t0 = time.perf_counter()
        v = model.encode(["doanh thu chi nhánh theo tháng", "monthly branch revenue"], normalize_embeddings=True)
        sim = float((v[0] * v[1]).sum())
        return True, f"dim {v.shape[1]}, load {load:.1f}s, encode {time.perf_counter() - t0:.2f}s, vi/en cosine {sim:.2f}"

    results.append(check("network", network))
    results.append(check("endpoint", endpoint))
    if not results[-1]["ok"] and "Error" in results[-1]["detail"]:
        print("\nthe endpoint does not answer; skipping the model checks")
    else:
        for name, fn in (("speed", single), ("thinking off", thinking_off), ("structured", structured),
                         ("concurrency", concurrency)):
            results.append(check(name, fn))
    if args.embedding:
        results.append(check("embedding", embedding))

    report = {"when": datetime.now(timezone.utc).isoformat(timespec="seconds"), "host": platform.node(),
              "platform": platform.platform(), "endpoint": s.llm.base_url, "local": is_local_url(s.llm.base_url),
              "model": s.llm.model, "thinking_param": llm.thinking_param, "max_concurrency": s.llm.max_concurrency,
              "results": results, "usage": llm.usage}
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.md:
        lines = [f"# M0 runtime check — {report['when']}", "",
                 f"Host `{report['host']}` ({report['platform']}), endpoint `{report['endpoint']}`, model "
                 f"`{report['model']}`, thinking_param `{report['thinking_param']}`.", "",
                 "| Check | Result | Detail |", "|---|---|---|"]
        lines += [f"| {r['check']} | {({True: 'pass', False: 'FAIL', None: 'info'})[r['ok']]} | "
                  f"{r['detail'].replace('|', '/')} |" for r in results]
        Path(args.md).write_text("\n".join(lines) + "\n", encoding="utf-8")
    failed = [r["check"] for r in results if r["ok"] is False]
    print(f"\n{'all checks passed' if not failed else 'failed: ' + ', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LLMError as e:
        print(f"error: {e}")
        sys.exit(1)

# Kế hoạch triển khai theo spec

> Kế hoạch đưa code hiện tại (SheetIR) về đúng thiết kế trong [`excel-agent-system-design.md`](excel-agent-system-design.md).
>
> Lập ngày 2026-11-04. Cập nhật cột **Trạng thái** khi xong từng việc.

## Hướng tổng thể

Không viết lại từ đầu. Phần SheetIR đang làm tốt (load, IR, detect bảng, đọc quan hệ từ công thức) là **đầu vào cho
Pha 1**. Bên trên đó thêm các tầng spec yêu cầu mà code chưa có: clean → DuckDB → families → relations → cards → index
→ agent có tool.

Những chỗ làm khác spec (đã thống nhất):

| Spec | Thực tế | Lý do |
|---|---|---|
| Package `excel_agent/` | Giữ `sheetir/`, thêm subpackage `clean/`, `catalog/`, `agent/` | Không phải đổi tên import, test, Docker |
| UI Streamlit | Giữ Next.js + FastAPI | UI đã có sẵn và đầy đủ hơn; chỉ thêm streaming bước agent |
| IR thô dạng JSON (`ir.json`) | Giữ SQLite IR, thêm `data.duckdb` | IR SQLite đã có và đã được `verify.py` kiểm tra |
| `LLMClient` async | Client đồng bộ + `map()` chạy song song bằng thread pool, giới hạn bởi semaphore | Toàn bộ code và endpoint FastAPI hiện là đồng bộ |

Giữ nguyên, không đụng: parse công thức, issues/fixes, audit report, verify.

## Trạng thái (cập nhật 2026-11-04)

| Giai đoạn | Trạng thái | Ghi chú |
|---|---|---|
| GĐ0 Nền móng | ✅ (0.4 chờ máy Intel Arc) | `bench/m0_runtime.py` đã chạy với OpenAI; chưa có số đo cho model local |
| GĐ1 Clean → DuckDB | ✅ | `sheetir/clean/`, `refine_spec` (header tầng 2 bị đọc thành data, dòng tổng trước ghi chú) |
| GĐ2 Agent MVP (M1) | ✅ | M1: 20/20 câu đúng (mục tiêu ≥ 70%) |
| GĐ3 Families + Relations | ✅ | Gộp tab theo kỳ, đổi tên cột theo vị trí, quy đổi đơn vị tiền, một dạng `period` cho cả family |
| GĐ4 Cards + Index | ✅ | Card do luật viết, LLM viết lại; BM25 bỏ dấu; embedding dùng khi có model local |
| GĐ5 Detect bổ sung | ✅ (5.4 một phần) | AutoFilter, named range, LLM fallback + validate; `.xlsb` chưa làm; thay LibreOffice bằng `sheetir/recalc.py` |
| GĐ6 UI | ✅ (6.3 rút gọn) | Bước agent theo thời gian thực (SSE), biểu đồ, nguồn dữ liệu, chip catalog / low confidence / offline. Tiến độ ingestion chỉ báo khi xong (upload vẫn đồng bộ) |
| GĐ7 Eval + Baselines | ✅ | 30 workbook, 180 câu; B0/B1/B2; kết quả ở [`eval-results.md`](eval-results.md) |

Làm thêm ngoài kế hoạch: `sheetir/recalc.py` (tính giá trị công thức khi file không được lưu bằng Excel; khớp
100% với giá trị Excel lưu trên dữ liệu sinh ra), tiếng Việt cho nhận diện ngôn ngữ và kiểm tra số (`1.234,5`).

Khác spec thêm (ngoài bảng ở trên):

| Spec | Thực tế | Lý do |
|---|---|---|
| B1: agent viết pandas trong sandbox | B1: cùng agent, SQL trên sheet thô (mỗi sheet một bảng text, cột A, B, …) | Không chạy code Python do model sinh; so sánh đúng phần pipeline thêm vào |
| LibreOffice tính lại công thức | `sheetir/recalc.py` (Python thuần, các hàm phổ biến) | Máy không có LibreOffice; `pycel` không chạy được trên Python 3.13 |
| Bảng `total_rows` gồm cả subtotal trong TableSpec | Subtotal trong thân bảng nằm ở `CleanTable.subtotal_rows` | IR chỉ lưu một dòng tổng mỗi bảng |
| Chat gửi mọi câu cho agent | Một lời gọi định tuyến: dữ liệu → agent; cấu trúc, lỗi → Q&A trên IR | Agent không có công cụ về công thức và issues |

---

## GĐ0 — Nền móng (M0)

| # | Việc | File | Tiêu chí xong | Trạng thái |
|---|---|---|---|---|
| 0.1 | `config/settings.yaml` + `Settings` (pydantic) theo §10; biến môi trường cũ (`OPENAI_*`, `SHEETIR_LLM_MODEL`) vẫn ghi đè được | `sheetir/config.py` | Test load config, mặc định khi thiếu file | ✅ `tests/test_config.py` |
| 0.2 | `LLMClient`: `chat()`, `chat_json(schema=Pydantic \| dict)`, retry khi JSON sai schema, parse khối `{…}` cuối khi không có structured output, tham số thinking, semaphore, log `.logs/llm.jsonl` | `sheetir/llm.py` | Unit test với transport giả | ✅ `tests/test_llm.py` |
| 0.3 | Offline: model local mặc định, endpoint ngoài mạng nội bộ bị chặn trừ khi `allow_remote`; `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` | `config.py`, `.env.example` | Không API key vẫn chạy với server local | ✅ |
| 0.4 | Script M0: tokens/s, structured output, tắt thinking, song song, embedding local, kiểm tra mạng | `bench/m0_runtime.py` | Kết quả trên máy Intel Arc ghi vào `docs/` | 🟡 Script xong; **chờ chạy trên máy Intel Arc** |
| 0.5 | Dependency: `duckdb`, `sqlglot`, `pydantic`, `pyyaml`, `unidecode`, `rank-bm25` (embedding optional) | `pyproject.toml` | `uv sync` sạch | ✅ |

## GĐ1 — Clean → DuckDB (§6.4)

| # | Việc | File |
|---|---|---|
| 1.1 | `Table` của SheetIR → `TableSpec` pydantic (§5.2), map `source` | `sheetir/detect/models.py` |
| 1.2 | Flatten header nhiều tầng (`header_top..header_row`, mở merge bằng `_merge_cover`) → `header_path`, `display_name` | `sheetir/clean/headers.py` |
| 1.3 | Identifier: `unidecode` → slug → chống trùng → fallback `col_{i}` | `clean/headers.py` |
| 1.4 | Ép kiểu theo `vtype`, `number_format`, nội dung (serial date, %, `1.250,5` / `1,250.5`) | `clean/types.py` |
| 1.5 | Đơn vị từ number format + header, chuẩn hoá (`trieu_vnd`, `nghin_vnd`, `vnd`, `usd`, `percent`…) | `clean/units.py` |
| 1.6 | Bỏ dòng tổng, ghi chú, gap rows; forward-fill merge dọc (config) | `clean/normalize.py` |
| 1.7 | `totals_check`: `SUM(data)` so với dòng tổng gốc | `clean/normalize.py` |
| 1.8 | Ghi `data.duckdb` cạnh IR, tên `t_{sheet_slug}__t{n}` | `sheetir/catalog/store.py` |
| 1.9 | Từ khoá dòng tổng tiếng Việt (`tổng`, `cộng`, `tổng cộng`) | `structure.py` |

Test: fixture xlsx nhỏ cho header 2 tầng có merge, subtotal, số định dạng VN, %, ngày `dd/mm/yyyy`.

## GĐ2 — Agent MVP (M1)

| # | Việc | File |
|---|---|---|
| 2.1 | `AgentAction{thought, tool, args}` (§7.2) | `sheetir/agent/protocol.py` |
| 2.2 | SQL guard: sqlglot (duckdb), một SELECT/WITH, chặn hàm đọc file, whitelist bảng, `read_only=True`, timeout + `interrupt()`, 200/30 dòng | `agent/sql_guard.py` |
| 2.3 | Tools: `search_catalog` (tạm: liệt kê), `describe_table`, `run_sql` (`result_id`), `make_chart` (spec cho frontend), `final_answer` | `agent/tools.py` |
| 2.4 | Hint §7.3.2: lỗi cột, rỗng có `WHERE`, lệch đơn vị, join phình >10× | `agent/tools.py` |
| 2.5 | Vòng lặp `answer()` + fallback khi hết bước | `agent/loop.py` |
| 2.6 | Kiểm tra cuối: dùng lại `ungrounded_numbers`; `result_ids` bắt buộc với câu định lượng | `agent/loop.py` |
| 2.7 | Provenance: SQL → bảng/view → sheet + range | `agent/provenance.py` |
| 2.8 | Ngôn ngữ `vi` | `understand.py`, `agent/prompts.py` |
| 2.9 | `/chat`: câu hỏi dữ liệu → agent; cấu trúc/issues → `ask()` cũ | `web/app.py` |
| 2.10 | 20 câu M1 + generator workbook sạch tối thiểu | `eval/generator/base_data.py`, `eval/datasets/m1/` |

Tiêu chí: ≥ 70% đúng trên 20 câu.

## GĐ3 — Families + Relations (§6.5)

| # | Việc | File |
|---|---|---|
| 3.1 | Signature bảng, nhóm theo Jaccard ≥ 0.9 + kiểu khớp | `catalog/families.py` |
| 3.2 | Discriminator từ tên sheet → kỳ (`T1`, `Q3`, `Thang 3`, `2026-03`) | `catalog/families.py` |
| 3.3 | View `v_fam_*` (`UNION ALL` + `period`, `_source_sheet`); agent chỉ thấy view | `catalog/families.py` |
| 3.4 | Join key theo giá trị: containment (chuẩn hoá, mẫu 5000), key_like, name_sim + từ điển đồng nghĩa | `catalog/relations.py` |
| 3.5 | Quan hệ từ công thức (`sheetir/relations.py`) làm `formula_evidence` | `catalog/relations.py` |
| 3.6 | Score 0.5–0.8 → LLM xác nhận (§15.3) | `catalog/relations.py` |
| 3.7 | Provenance bung family, lọc theo `period`/`_source_sheet` | `agent/provenance.py` |

## GĐ4 — Catalog cards + Index (§6.6–6.8)

| # | Việc | File |
|---|---|---|
| 4.1 | `CatalogCard` bằng LLM (§15.2), song song; không có LLM → card tất định | `catalog/cards.py` |
| 4.2 | BM25 trên text bỏ dấu | `catalog/index.py` |
| 4.3 | Embedding local (optional), hybrid α = 0.6, `index.npy` | `catalog/index.py` |
| 4.4 | `search_catalog` dùng index; system prompt chỉ còn tóm tắt catalog (≤ 30 title) | `agent/tools.py`, `agent/prompts.py` |
| 4.5 | Cache sha256 + version pipeline: `tables.json`, `catalog.json`, `data.duckdb`, `index.npy`, `meta.json` | `sheetir/cache.py` |

## GĐ5 — Detect bổ sung (§6.3)

| # | Việc |
|---|---|
| 5.1 | Ứng viên từ `auto_filter` (0.90) và named range (0.85) |
| 5.2 | LLM fallback cho block < 0.8: `suggest_tables` → validate §6.3.5 → retry → `low_confidence` |
| 5.3 | Encoder có ngân sách token (N đầu + M cuối + tóm tắt kiểu cột) |
| 5.4 | (Tuỳ chọn) `.xlsb` qua `pyxlsb`; tính lại công thức bằng LibreOffice khi vượt `recalc_threshold` |

## GĐ6 — UI (M4)

| # | Việc |
|---|---|
| 6.1 | SSE `/chat/stream`: từng bước agent (thought, tool, SQL, kết quả rút gọn) |
| 6.2 | `Chat.tsx`: expander bước agent, provenance, bấm nguồn → vùng ô highlight |
| 6.3 | Tiến độ ingestion theo stage |
| 6.4 | Badge `low_confidence`, chỉ báo Offline |
| 6.5 | Chart từ spec `make_chart` (tận dụng `ChartsView`) |

## GĐ7 — Eval + Baselines (M3)

| # | Việc | File |
|---|---|---|
| 7.1 | Generator dữ liệu sạch (chi nhánh, sản phẩm, doanh thu tháng, nhân sự) | `eval/generator/base_data.py` |
| 7.2 | 10 phép làm bẩn §11.1, tự sinh gold `tables.jsonl` | `eval/generator/perturb.py` |
| 7.3 | Câu hỏi theo template cho 7 category, đáp án tính từ dữ liệu sạch | `eval/generator/questions.py` |
| 7.4 | Metrics §11.3, báo cáo theo category | `eval/metrics.py` |
| 7.5 | Baselines B0 / B1 / B2 | `eval/baselines/`, `eval/run_eval.py` |
| 7.6 | Gộp `bench/eval_statsheets.py` làm benchmark detect dữ liệu thật | — |

Tiêu chí: ≥ 30 workbook, ≥ 150 câu, bảng so sánh B0/B1/B2.

---

## Thứ tự và phụ thuộc

```
GĐ0 ─► GĐ1 ─► GĐ2 (M1) ─► GĐ3 ─► GĐ4 ─► GĐ6 (M4)
                 │                        ▲
                 └──► GĐ7 (eval, song song từ sau M1)
GĐ5 chen vào bất cứ lúc nào, ưu tiên khi eval cho thấy detect là điểm yếu
```

## Rủi ro

- **M0 chưa xác minh:** backend Intel có thể chưa chạy được Qwen3.5 → dùng model dự phòng; code chỉ phụ thuộc API
  OpenAI-compatible.
- **DuckDB + SQLite song song:** phải rebuild `data.duckdb` mỗi lần re-parse (user sửa bảng, áp fix).
- **Prompt hiện tối ưu cho zh/en:** thêm tiếng Việt cần eval lại.

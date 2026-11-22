# Excel Agent Offline — System Design Spec

> Tài liệu thiết kế để implement. Hệ thống hỏi đáp trên workbook Excel nhiều tab, dữ liệu lộn xộn, chạy hoàn toàn offline trên 1 GPU 32GB (Intel Arc Pro B70).
>
> Trạng thái: draft v0.1 · Ngôn ngữ code: Python 3.11+
>
> Triển khai: trạng thái từng mục, những chỗ làm khác tài liệu này và lý do ở [`implementation-plan.md`](implementation-plan.md);
> kết quả đánh giá ở [`eval-results.md`](eval-results.md).

---

## Mục lục

1. [Mục tiêu và phạm vi](#1-mục-tiêu-và-phạm-vi)
2. [Ràng buộc](#2-ràng-buộc)
3. [Kiến trúc tổng](#3-kiến-trúc-tổng)
4. [Cấu trúc thư mục](#4-cấu-trúc-thư-mục)
5. [Data models](#5-data-models)
6. [Pha 1 — Ingestion (lập catalog)](#6-pha-1--ingestion-lập-catalog)
7. [Pha 2 — Query agent](#7-pha-2--query-agent)
8. [LLM runtime](#8-llm-runtime)
9. [UI](#9-ui)
10. [Config](#10-config)
11. [Evaluation](#11-evaluation)
12. [Milestones](#12-milestones)
13. [Rủi ro và cách giảm](#13-rủi-ro-và-cách-giảm)
14. [Câu hỏi còn mở](#14-câu-hỏi-còn-mở)
15. [Phụ lục: prompt templates](#15-phụ-lục-prompt-templates)

---

## 1. Mục tiêu và phạm vi

### 1.1 Bài toán

Cho một workbook `W = {S₁…S_N}` (N có thể tới hàng trăm tab) chứa bảng bán cấu trúc: header nhiều tầng, ô merge, nhiều bảng trên một tab, dòng tổng chen giữa, dữ liệu tách theo kỳ (tháng/quý) ra nhiều tab. Người dùng hỏi một câu bằng ngôn ngữ tự nhiên `q`. Hệ thống trả về:

- `a`: câu trả lời chính xác,
- `P`: provenance — bảng/tab/vùng ô đã dùng,
- (tuỳ chọn) biểu đồ.

### 1.2 Goals

| ID | Goal |
|---|---|
| G1 | Hiểu cấu trúc workbook lộn xộn: tìm đúng các bảng, header, dòng tổng |
| G2 | Trả lời câu hỏi cần nối dữ liệu từ nhiều tab |
| G3 | Con số chính xác: mọi phép tính do database thực hiện, không do LLM |
| G4 | Có provenance cho mọi câu trả lời |
| G5 | Chạy offline 100%, không gọi network |
| G6 | Đo được: có bộ eval và baseline để so sánh |

### 1.3 Non-goals (MVP)

- Ghi/sửa ngược vào file Excel.
- Tính lại công thức Excel (dùng giá trị cache; xem §6.1.4).
- Đọc chart, ảnh, pivot table (chỉ ghi nhận sự tồn tại).
- Đa người dùng, phân quyền.
- Fine-tune model (để dành cho milestone tuỳ chọn M5).

### 1.4 Nguyên tắc thiết kế

1. **Code làm việc có luật, LLM làm việc cần hiểu nghĩa.** LLM không đọc file, không tính toán.
2. **Luật trước, LLM sau.** Mỗi bước thử heuristic trước; chỉ gọi LLM khi heuristic không đủ tự tin.
3. **Hiểu một lần, dùng nhiều lần.** Pha 1 chạy lúc upload, kết quả được cache theo hash file.
4. **Mọi output của LLM đều được validate** bằng JSON schema + kiểm tra logic, có retry.
5. **Đo trước khi tối ưu.** Mỗi thay đổi phải chạy lại eval.

---

## 2. Ràng buộc

| Hạng mục | Giá trị |
|---|---|
| Phần cứng | 1× Intel Arc Pro B70 32GB VRAM |
| Mạng | Không có. Không telemetry, không tải model lúc chạy |
| Model chính | Qwen3.5-35B-A3B, quantize 4-bit (khoảng 20GB VRAM) |
| Model dự phòng | Qwen3.5-9B (bf16) hoặc model Qwen thế hệ trước nếu backend Intel chưa hỗ trợ kiến trúc Qwen3.5 |
| Latency mục tiêu | Ingestion: ≤ 2 phút cho workbook 100 tab. Query: p50 ≤ 15s, p95 ≤ 45s |
| Kích thước file | MVP: ≤ 50MB, ≤ 300 tab, ≤ 1M ô có dữ liệu |

> ⚠️ Chưa xác minh: runtime nào trên GPU Intel (OpenVINO, llama.cpp SYCL/Vulkan, vLLM XPU) hỗ trợ kiến trúc lai của Qwen3.5. Đây là việc đầu tiên của M0.

---

## 3. Kiến trúc tổng

```
┌──────────────────────── Offline · 1 máy · 32GB ────────────────────────┐
│                                                                        │
│  PHA 1 · INGESTION (khi upload, cache theo sha256)                     │
│                                                                        │
│  .xlsx ─► loader ─► extract_ir ─► detect (rules → LLM) ─► clean        │
│                        │                                     │         │
│                        ▼                                     ▼         │
│                   WorkbookIR                       DuckDB tables       │
│                                                          │             │
│                                     relations ◄──────────┤             │
│                                     (families, joins)    │             │
│                                          │               ▼             │
│                                          └──────► cards (LLM) ─► index │
│                                                                  │     │
│  ─────────────────────────────────────────────────────────────── │ ─── │
│                                                                  ▼     │
│  PHA 2 · QUERY                                              Catalog    │
│                                                                  │     │
│  question ─► agent loop ◄──► tools: search_catalog ──────────────┤     │
│                  │                  describe_table ──────────────┤     │
│                  │                  run_sql ──────► DuckDB (RO)  │     │
│                  │                  make_chart                   │     │
│                  ▼                                                     │
│            final_answer + provenance + chart ─► UI                     │
│                                                                        │
│  LLM server (OpenAI-compatible, localhost) ◄── mọi lời gọi LLM         │
└────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Phân chia trách nhiệm Code vs LLM

| Bước | Code | LLM |
|---|---|---|
| Load + extract IR | 100% | — |
| Nén (encoding) | 100% | — |
| Detect tables | Rules cho tab "dễ" | Tab mơ hồ: vùng bảng, header, dòng tổng |
| Clean | Cắt vùng, gộp header, ép kiểu, bỏ dòng tổng | (tuỳ chọn) đặt tên cột dễ đọc |
| Relations | Tìm ứng viên: cùng khuôn, giao giá trị, công thức | Xác nhận đồng nghĩa ở vùng mơ hồ |
| Cards | Thống kê, dòng mẫu | Viết mô tả bảng và cột |
| Query | Chạy SQL, tính toán, vẽ chart, trích provenance | Hiểu câu hỏi, chọn bảng, viết SQL, tự sửa, diễn giải |

---

## 4. Cấu trúc thư mục

```
excel-agent/
├── pyproject.toml
├── config/
│   └── settings.yaml
├── src/excel_agent/
│   ├── __init__.py
│   ├── config.py                 # load settings.yaml → pydantic Settings
│   ├── pipeline.py               # orchestrate pha 1: ingest_workbook(path) -> CatalogHandle
│   ├── cache.py                  # cache theo sha256 file
│   ├── ingest/
│   │   ├── loader.py             # nhận diện định dạng, convert .xls/.xlsb
│   │   ├── extract_ir.py         # openpyxl → WorkbookIR
│   │   ├── formula_refs.py       # parse tham chiếu chéo tab trong công thức
│   │   └── ir_models.py
│   ├── compress/
│   │   └── encoder.py            # bọc sheet-compressor, tạo text cho LLM
│   ├── detect/
│   │   ├── rules.py              # rule-based table detection
│   │   ├── segment.py            # tách block theo hàng/cột trống
│   │   ├── header.py             # heuristic tìm header, dòng tổng, ghi chú
│   │   ├── llm_detect.py         # fallback LLM
│   │   └── models.py             # TableSpec
│   ├── clean/
│   │   ├── normalize.py          # TableSpec + IR → DataFrame sạch
│   │   ├── headers.py            # flatten header nhiều tầng, slug tên cột
│   │   ├── types.py              # ép kiểu theo number_format
│   │   └── units.py              # trích đơn vị từ format/header
│   ├── catalog/
│   │   ├── store.py              # DuckDB + catalog.json
│   │   ├── families.py           # gộp tab cùng khuôn → VIEW
│   │   ├── relations.py          # tìm join key
│   │   ├── cards.py              # sinh CatalogCard (LLM)
│   │   └── index.py              # embedding + BM25 hybrid search
│   ├── agent/
│   │   ├── tools.py
│   │   ├── sql_guard.py          # chỉ cho SELECT, timeout, LIMIT
│   │   ├── provenance.py         # SQL → bảng → tab/vùng
│   │   ├── prompts.py
│   │   ├── protocol.py           # schema JSON cho action
│   │   └── loop.py               # vòng lặp agent
│   ├── llm/
│   │   └── client.py             # OpenAI-compatible client, structured output, retry
│   └── ui/
│       └── app.py                # Streamlit
├── eval/
│   ├── generator/                # sinh workbook "lộn xộn" có sẵn gold
│   │   ├── base_data.py
│   │   └── perturb.py
│   ├── datasets/<name>/
│   │   ├── workbooks/*.xlsx
│   │   ├── tables.jsonl          # gold vùng bảng
│   │   └── questions.jsonl       # gold câu hỏi
│   ├── baselines/
│   │   ├── b0_dump.py
│   │   └── b1_pandas_agent.py
│   ├── metrics.py
│   └── run_eval.py
└── tests/
    ├── fixtures/                 # các file .xlsx nhỏ cho unit test
    └── test_*.py
```

### 4.1 Dependencies

| Package | Dùng cho |
|---|---|
| `openpyxl` | đọc `.xlsx`/`.xlsm` |
| `pyxlsb` (optional) | đọc `.xlsb` |
| LibreOffice (binary, optional) | convert `.xls` → `.xlsx`, tính lại công thức |
| `sheet-compressor` | encoding nén cho LLM (bản community, v0.1 — kiểm tra API thực tế) |
| `pandas`, `numpy` | làm sạch |
| `duckdb` | lưu trữ + chạy SQL |
| `sqlglot` | parse SQL để guard và trích provenance |
| `pydantic` v2 | data models, validate output LLM |
| `openai` hoặc `httpx` | gọi LLM server OpenAI-compatible |
| `rank-bm25` | keyword search |
| embedding runtime | `sentence-transformers` hoặc OpenVINO — tuỳ backend chọn ở M0 |
| `unidecode` | bỏ dấu tiếng Việt khi tạo identifier |
| `streamlit`, `plotly` | UI + chart |
| `pyyaml` | config |
| `pytest` | test |

---

## 5. Data models

Tất cả là `pydantic.BaseModel`. Toạ độ dùng 1-based (giống Excel). Range dùng chuỗi A1 (`"B3:H58"`).

### 5.1 IR (output của extract)

```python
class CellIR(BaseModel):
    a: str                      # "C4"
    r: int                      # row 1-based
    c: int                      # col 1-based
    v: str | float | int | bool | None   # giá trị (data_only=True)
    t: Literal["s","n","b","d","e","f"]  # string/number/bool/date/error/formula-no-cache
    fmt: str | None = None      # number_format nếu khác "General"
    f: str | None = None        # công thức gốc (data_only=False)
    bold: bool = False
    fill: str | None = None     # màu nền ARGB nếu có
    comment: str | None = None

class ExcelTableIR(BaseModel):
    name: str
    ref: str                    # "A3:H55"
    header_row_count: int = 1

class DataValidationIR(BaseModel):
    ref: str
    values: list[str] | None    # nếu là list dropdown

class SheetIR(BaseModel):
    name: str
    index: int
    state: Literal["visible","hidden","veryHidden"]
    used_range: str | None      # tính bằng cách quét ô có giá trị, KHÔNG dùng max_row
    freeze_panes: str | None
    auto_filter: str | None
    print_area: str | None
    excel_tables: list[ExcelTableIR]
    merged: list[str]
    hidden_rows: list[int]
    hidden_cols: list[int]
    data_validations: list[DataValidationIR]
    has_charts: bool = False
    has_pivots: bool = False
    cells: list[CellIR]         # chỉ ô có giá trị

class FormulaRef(BaseModel):
    from_sheet: str
    from_cell: str
    to_sheet: str
    to_range: str
    func: str | None            # "SUM", "VLOOKUP", None (tham chiếu trực tiếp)

class WorkbookIR(BaseModel):
    file_name: str
    sha256: str
    named_ranges: dict[str, str]      # "DS_ChiNhanh" -> "ChiNhanh!A2:A40"
    sheets: list[SheetIR]
    formula_refs: list[FormulaRef]
    warnings: list[str]               # vd: "C5: formula without cached value"
```

### 5.2 TableSpec (output của detect)

```python
class TableSpec(BaseModel):
    table_id: str                     # f"{sheet_slug}__t{n}"
    sheet: str
    range: str                        # toàn bộ vùng bảng, gồm header
    header_rows: list[int]            # số dòng tuyệt đối, có thể nhiều tầng
    total_rows: list[int] = []        # dòng tổng/subtotal (sẽ bị loại khỏi data)
    note_rows: list[int] = []         # dòng ghi chú trong vùng
    title: str | None = None          # tiêu đề phía trên bảng nếu có
    confidence: float                 # 0..1
    source: Literal["rule:excel_table","rule:auto_filter","rule:named_range",
                    "rule:heuristic","llm"]
```

### 5.3 CleanTable + ColumnSpec (output của clean)

```python
class ColumnSpec(BaseModel):
    name: str                         # identifier SQL: "doanh_thu_q1"
    display_name: str                 # "Q1 / Doanh thu"
    header_path: list[str]            # ["Q1", "Doanh thu"]
    dtype: Literal["int","float","text","date","bool"]
    unit: str | None                  # "trieu_vnd", "percent", ...
    null_ratio: float
    distinct_count: int
    sample_values: list[str]          # tối đa 5

class CleanTable(BaseModel):
    table_id: str
    duckdb_name: str                  # "t_dt_t1__t1"
    spec: TableSpec
    columns: list[ColumnSpec]
    n_rows: int
    totals_check: dict[str, bool] = {}   # cột -> tổng data có khớp dòng tổng gốc không
```

### 5.4 Family và Relation

```python
class TableFamily(BaseModel):
    family_id: str                    # "fam_dt"
    member_table_ids: list[str]
    discriminator: str                # tên cột thêm vào: "period"
    member_values: dict[str, str]     # table_id -> "2026-01"
    duckdb_view: str                  # "v_fam_dt" (UNION ALL + cột discriminator)

class Relation(BaseModel):
    left: str                         # "v_fam_dt.ma_cn"
    right: str                        # "t_chinhanh__t1.ma_cn"
    kind: Literal["fk_candidate","formula_ref","same_family"]
    score: float
    confirmed_by: Literal["rule","llm"]
```

### 5.5 CatalogCard

```python
class ColumnDesc(BaseModel):
    name: str
    description: str
    unit: str | None

class CatalogCard(BaseModel):
    card_id: str                      # = table_id hoặc family_id
    sql_name: str                     # bảng hoặc view để query
    title: str
    description: str                  # 1–3 câu
    columns: list[ColumnDesc]
    time_coverage: str | None         # "2026-01 → 2026-12"
    row_count: int
    sample_rows: list[dict]           # 3–5 dòng
    sources: list[dict]               # [{"sheet": "DT_T1", "range": "A3:H55"}, ...]
    relations: list[str]              # mô tả ngắn: "nối ChiNhanh qua ma_cn"
    keywords: list[str]               # cho BM25
```

---

## 6. Pha 1 — Ingestion (lập catalog)

Entry point:

```python
def ingest_workbook(path: Path, settings: Settings) -> CatalogHandle:
    """Cache theo sha256. Gọi lại với cùng file → trả về ngay."""
```

Các stage chạy tuần tự. Trong mỗi stage, các tab/bảng độc lập chạy song song (`asyncio` + semaphore, mặc định 8 request LLM đồng thời). Mỗi stage báo tiến độ qua callback cho UI.

### 6.1 Load + extract IR (`ingest/`)

#### 6.1.1 Loader

| Định dạng | Xử lý |
|---|---|
| `.xlsx`, `.xlsm` | openpyxl trực tiếp (không chạy macro) |
| `.xls` | convert bằng `soffice --headless --convert-to xlsx` |
| `.xlsb` | `pyxlsb` → dựng IR rút gọn (không có format/merge) hoặc convert bằng LibreOffice |
| `.csv` | đọc bằng pandas → một SheetIR, một bảng, bỏ qua detect |
| Khác | từ chối với thông báo rõ |

Giới hạn: kích thước file (`max_file_mb`), số tab, số ô. Vượt giới hạn → báo lỗi, không xử lý một phần âm thầm.

#### 6.1.2 Hai lượt đọc

```python
wb_val = load_workbook(path, data_only=True)    # giá trị cache
wb_fx  = load_workbook(path, data_only=False)   # công thức
```

Với file rất lớn (`> large_file_cells`): đọc metadata (merge, tables, freeze…) ở chế độ thường một lần, đọc giá trị bằng `read_only=True` (chế độ này không có merged cells).

#### 6.1.3 Những gì cần lấy

| Tầng | Trường | API |
|---|---|---|
| Workbook | tên tab, thứ tự | `wb.sheetnames` |
| | named ranges | `wb.defined_names` (+ phạm vi theo sheet) |
| Sheet | trạng thái ẩn | `ws.sheet_state` |
| | merged | `ws.merged_cells.ranges` |
| | Excel tables | `ws.tables` (name → Table, `.ref`) |
| | freeze panes | `ws.freeze_panes` |
| | auto filter | `ws.auto_filter.ref` |
| | print area | `ws.print_area` |
| | hàng/cột ẩn | `ws.row_dimensions[r].hidden`, `ws.column_dimensions[c].hidden` |
| | data validation | `ws.data_validations.dataValidation` |
| | chart/pivot | `ws._charts`, `ws._pivots` (chỉ kiểm tra tồn tại; API private, bọc try/except) |
| Cell | giá trị, kiểu | `cell.value`, `cell.data_type`, `cell.is_date` |
| | number format | `cell.number_format` (bỏ nếu `"General"`) |
| | công thức | `cell.value` từ `wb_fx` nếu bắt đầu bằng `=` |
| | style | `cell.font.b`, `cell.fill.fgColor.rgb` (chỉ khi `fill_type` khác None) |
| | comment | `cell.comment.text` |

`used_range`: quét `iter_rows` lấy min/max row/col của ô có `value is not None`. Không dùng `ws.max_row` vì bị phình do format thừa.

#### 6.1.4 Công thức không có giá trị cache

Nếu `wb_fx` có công thức nhưng `wb_val` trả `None` → ghi `t="f"` và warning. Nếu số ô như vậy vượt ngưỡng (`recalc_threshold`) và có LibreOffice → chạy convert để tính lại rồi đọc lại.

#### 6.1.5 Formula refs (`formula_refs.py`)

Parse mọi công thức có tham chiếu sang tab khác:

- Regex cho `'Sheet Name'!A1:B9` và `Sheet!A1`
- Ghi tên hàm bao ngoài nếu là `SUM`, `SUMIF(S)`, `VLOOKUP`, `XLOOKUP`, `INDEX`, `MATCH`
- Output `list[FormulaRef]` → dùng ở §6.5 như bằng chứng quan hệ mạnh

### 6.2 Encoding cho LLM (`compress/encoder.py`)

```python
def encode_region(sheet: SheetIR, a1_range: str, mode: Literal["anchor","inverted","format"]) -> str
def encode_sheet_overview(sheet: SheetIR, max_tokens: int) -> str
```

- Bọc `sheet-compressor` (`compress(grid)`; adapter đọc xlsx có sẵn trong package).
- Bổ sung những gì sheet-compressor chưa có: đánh dấu ô merge, ô bold, màu nền (dạng `Fill:FFFF00 → B4:B9`), dòng/cột ẩn.
- Có ngân sách token: nếu vượt `max_tokens` → chỉ gửi N dòng đầu + M dòng cuối + tóm tắt kiểu dữ liệu từng cột.

### 6.3 Detect tables (`detect/`)

#### 6.3.1 Luồng

```
cho mỗi sheet:
    candidates = []
    candidates += from_excel_tables(sheet)      # conf 0.95
    candidates += from_auto_filter(sheet)       # conf 0.90
    candidates += from_named_ranges(sheet)      # conf 0.85
    vùng còn lại = used_range − vùng đã phủ
    blocks = segment(vùng còn lại)              # tách theo hàng/cột trống
    cho mỗi block:
        spec = heuristic(block)                 # conf tính theo đặc trưng
        nếu spec.confidence >= T_accept (0.8): nhận
        ngược lại: đưa vào hàng đợi LLM
llm_specs = await llm_detect(batch các block mơ hồ)
validate tất cả → list[TableSpec]
```

#### 6.3.2 Segment

- Lưới boolean `occupied[r][c]`.
- Tách theo dòng trống liên tiếp `≥ gap_rows` (mặc định 1) và cột trống `≥ gap_cols` (mặc định 1).
- Connected components (4-neighborhood, cho phép bắc cầu qua ô merge).
- Bỏ block có `< 2` dòng hoặc `< 2` cột (đánh dấu là "title/note" để dùng làm `title` cho bảng ngay bên dưới).

#### 6.3.3 Heuristic header / total / note

Đặc trưng mỗi dòng trong block:

| Đặc trưng | Ý nghĩa |
|---|---|
| `text_ratio` | tỷ lệ ô kiểu string |
| `numeric_ratio` | tỷ lệ ô kiểu số |
| `bold_ratio` | tỷ lệ ô in đậm |
| `has_merge` | có ô merge ngang |
| `is_frozen_boundary` | dòng ngay trên ranh giới freeze panes |
| `formula_agg` | có `SUM/SUBTOTAL` tham chiếu các dòng phía trên |
| `total_keyword` | ô đầu khớp regex `(?i)^(tổng|tong|total|cộng|cong|sum|subtotal)` |

Luật:

- **Header**: các dòng đầu block có `text_ratio ≥ 0.6` và dòng ngay sau có `numeric_ratio` cao hơn rõ rệt. Nhiều dòng header liên tiếp khi có `has_merge` → header nhiều tầng.
- **Total row**: `formula_agg` hoặc (`total_keyword` và `bold`).
- **Note row**: chỉ 1 ô có giá trị, là chuỗi dài (> 40 ký tự), nằm cuối block.

`confidence` = trung bình có trọng số của: header rõ ràng (có ranh giới kiểu dữ liệu), cột đồng nhất kiểu, không có merge bất thường giữa vùng data. Giá trị cụ thể của trọng số tune bằng eval.

#### 6.3.4 LLM fallback (`llm_detect.py`)

- Input: encoding của block (mode `anchor` + `format`), gợi ý từ heuristic (header dự đoán, dòng tổng dự đoán, confidence), tên sheet.
- Output: JSON theo schema `list[TableSpec]` (không gồm `table_id`, `confidence`, `source` — code tự điền).
- `temperature=0`, thinking tắt.
- Prompt: xem §15.1.

#### 6.3.5 Validate

Mọi TableSpec (rule hay LLM) phải qua:

- `range` nằm trong `used_range` của sheet.
- `header_rows` nằm trong `range` và nằm ở đầu.
- Hai bảng trên cùng sheet không chồng lấn.
- Sau header có ít nhất 1 dòng data.

LLM output sai → gửi lại kèm thông báo lỗi cụ thể, tối đa `llm_retries` (2) lần. Vẫn sai → fallback dùng spec heuristic dù confidence thấp, đánh dấu `low_confidence` để UI cảnh báo.

### 6.4 Clean (`clean/`)

```python
def to_clean_table(spec: TableSpec, sheet: SheetIR) -> tuple[pd.DataFrame, CleanTable]
```

Các bước:

1. **Cắt vùng** theo `spec.range`.
2. **Mở merge**:
   - Merge trong header → điền giá trị sang toàn bộ vùng merge.
   - Merge dọc trong data (vd nhóm theo vùng) → forward-fill (bật/tắt qua config).
3. **Flatten header nhiều tầng**: `header_path = ["Q1", "Doanh thu"]` → `display_name = "Q1 / Doanh thu"`.
4. **Tạo identifier**: `unidecode` → lowercase → thay ký tự không phải `[a-z0-9]` bằng `_` → gộp `_` → chống trùng bằng hậu tố `_2`. Không bao giờ để rỗng (fallback `col_{index}`).
5. **Loại dòng** `total_rows` và `note_rows`. Lưu giá trị dòng tổng để kiểm tra ở bước 9.
6. **Ép kiểu** theo `number_format` + nội dung:
   - Excel serial date / `is_date` → `date`
   - Format có `%` → `float`, `unit="percent"`
   - Chuỗi số có dấu phân cách (`1.250,5` / `1,250.5`) → `float` (nhận diện locale theo đa số)
7. **Trích đơn vị** (`units.py`): từ number_format (`#,##0 "tr"`) và header (`"Doanh thu (triệu đồng)"`). Chuẩn hoá về bảng mã đơn vị (`trieu_vnd`, `nghin_vnd`, `vnd`, `usd`, `percent`, …).
8. **Ghi DuckDB**: `CREATE TABLE {duckdb_name} AS SELECT * FROM df`.
9. **Kiểm tra tổng** (`totals_check`): với cột số, so `SUM(data)` với giá trị dòng tổng gốc (sai số tương đối 1e-6). Lệch → cảnh báo, gợi ý có dòng subtotal bị bỏ sót hoặc thừa.

> MVP giữ nguyên dạng wide (vd cột T1…T12). Chuyển wide→long để sau, khi eval chỉ ra cần thiết.

### 6.5 Relations (`catalog/families.py`, `catalog/relations.py`)

#### 6.5.1 Families (gộp tab cùng khuôn)

1. `signature(table) = [(col.name, col.dtype)]`.
2. Nhóm các bảng có Jaccard tên cột ≥ 0.9 và kiểu khớp.
3. Trích discriminator từ tên sheet: tìm phần khác nhau giữa các tên (`DT_T1`, `DT_T2` → `T1`, `T2`). Parse sang kỳ nếu được (`T1`→`01`, `Q3`, `2026-03`, `Thang 3`…).
4. Tạo view:

   ```sql
   CREATE VIEW v_fam_dt AS
   SELECT '2026-01' AS period, '{sheet}' AS _source_sheet, * FROM t_dt_t1__t1
   UNION ALL
   SELECT '2026-02' AS period, '{sheet}' AS _source_sheet, * FROM t_dt_t2__t1
   ...
   ```

5. Card được tạo cho **family**, không phải từng bảng thành viên. Agent chỉ thấy view.

#### 6.5.2 Join keys

Ứng viên cặp cột `(A.x, B.y)`:

| Đặc trưng | Cách tính |
|---|---|
| `containment` | `|vals(A.x) ∩ vals(B.y)| / |vals(A.x)|` trên giá trị đã chuẩn hoá (strip, lowercase, bỏ dấu) |
| `key_like` | `distinct_count / n_rows` của phía "dimension" gần 1 |
| `name_sim` | độ giống tên cột + từ điển đồng nghĩa (`ma_cn ~ ma_chi_nhanh ~ branch_id`) |
| `formula_evidence` | có `FormulaRef` kiểu VLOOKUP/XLOOKUP giữa hai vùng |

`score` = tổ hợp có trọng số. `score ≥ 0.8` → nhận (`confirmed_by="rule"`). `0.5 ≤ score < 0.8` → hỏi LLM xác nhận (prompt §15.3). `< 0.5` → bỏ.

Với bảng lớn, tính `containment` trên mẫu (tối đa 5.000 giá trị distinct).

### 6.6 Cards (`catalog/cards.py`)

- Input cho LLM: tên sheet, `title`, `display_name` + `unit` + `dtype` của các cột, 5 dòng mẫu, thống kê (min/max cho số, top values cho text), `time_coverage`, relations.
- Output: JSON `title`, `description`, `columns[].description`, `keywords`.
- Ngôn ngữ mô tả: theo ngôn ngữ chiếm đa số trong header (tiếng Việt hoặc tiếng Anh).
- Chạy song song, mỗi card một request.
- Prompt: §15.2.

### 6.7 Index (`catalog/index.py`)

- Văn bản để index mỗi card: `title + description + tên cột + mô tả cột + keywords + tên sheet`.
- Embedding: model nhỏ chạy local (chọn ở M0), lưu vector vào `numpy` + file `.npy`.
- BM25 trên cùng văn bản (tokenize có bỏ dấu để "chi nhanh" vẫn khớp "chi nhánh").
- `score = α·cosine + (1−α)·bm25_norm`, `α = 0.6` mặc định.

### 6.8 Lưu trữ và cache

```
.cache/<sha256>/
├── ir.json
├── tables.json          # list[TableSpec]
├── catalog.json         # cards, families, relations
├── data.duckdb          # bảng sạch + view
├── index.npy
└── meta.json            # version pipeline, thời gian, thống kê
```

Đổi version pipeline → cache cũ bị bỏ qua.

---

## 7. Pha 2 — Query agent

### 7.1 Vòng lặp

```python
async def answer(question: str, catalog: CatalogHandle, settings: Settings) -> AgentResult:
    messages = [system_prompt(catalog.summary()), user(question)]
    for step in range(settings.agent.max_steps):          # mặc định 8
        action = await llm.chat_json(messages, schema=AgentAction)
        if action.tool == "final_answer":
            return finalize(action, trace)
        result = await tools.dispatch(action)              # luôn trả JSON, không raise
        messages += [assistant(action), tool_result(truncate(result))]
        trace.append((action, result))
    return fallback_answer(trace)                          # hết bước: trả lời trung thực là chưa đủ dữ liệu
```

`catalog.summary()` đưa vào system prompt: số bảng, danh sách `title` (tối đa 30 dòng; nhiều hơn thì chỉ đưa số lượng và nhắc dùng `search_catalog`).

### 7.2 Protocol

Không phụ thuộc native tool-calling của backend (mức hỗ trợ khác nhau). Dùng JSON action và validate bằng pydantic:

```python
class AgentAction(BaseModel):
    thought: str                       # ngắn, 1–3 câu
    tool: Literal["search_catalog","describe_table","run_sql",
                  "make_chart","final_answer"]
    args: dict
```

Nếu backend hỗ trợ structured output (JSON schema / grammar) → bật để ép đúng format. JSON lỗi → gửi lại thông báo lỗi, tối đa 2 lần trong cùng một bước.

### 7.3 Tools

| Tool | Args | Trả về |
|---|---|---|
| `search_catalog` | `query: str`, `k: int = 8` | `[{card_id, sql_name, title, description, columns: [name], time_coverage, row_count}]` |
| `describe_table` | `card_id: str` | card đầy đủ: cột + mô tả + dtype + unit, 5 dòng mẫu, relations, sources |
| `run_sql` | `sql: str` | `{result_id, columns, rows, row_count, truncated}` hoặc `{error, hint}` |
| `make_chart` | `result_id: str`, `type: bar\|line\|pie\|scatter`, `x: str`, `y: str\|list[str]`, `title: str` | `{chart_id}` |
| `final_answer` | `answer: str`, `result_ids: list[str]`, `chart_id: str\|None` | kết thúc vòng lặp |

#### 7.3.1 `run_sql` guard (`sql_guard.py`)

1. Parse bằng `sqlglot` (dialect `duckdb`). Chỉ cho `SELECT` / `WITH … SELECT`. Từ chối mọi statement khác, nhiều statement, hàm đọc file (`read_csv`, `read_parquet`, …).
2. Mọi bảng/view được tham chiếu phải có trong catalog.
3. Kết nối DuckDB `read_only=True`.
4. Timeout `sql_timeout_s` (10s): chạy trong thread, quá hạn → `conn.interrupt()`.
5. Trả về tối đa `max_rows_return` (200) dòng cho UI, chỉ `max_rows_to_llm` (30) dòng cho LLM, kèm `row_count` thật.

#### 7.3.2 Hint khi lỗi hoặc kết quả bất thường

| Tình huống | Hint trả cho agent |
|---|---|
| Lỗi cú pháp / cột không tồn tại | thông báo lỗi DuckDB + danh sách cột đúng của bảng liên quan |
| Kết quả rỗng có `WHERE col = 'x'` | gợi ý chạy `SELECT DISTINCT col` để xem giá trị thật |
| JOIN hai bảng khác `unit` trên cột được tổng/so sánh | cảnh báo lệch đơn vị kèm unit hai bên |
| JOIN làm số dòng tăng vọt (> 10× bảng lớn nhất) | cảnh báo có thể join sai khoá |

### 7.4 Provenance (`provenance.py`)

- Từ các `result_ids` trong `final_answer` → SQL tương ứng → `sqlglot` lấy danh sách bảng/view.
- View family → bung ra các bảng thành viên → `TableSpec.sheet` + `range`.
- (Tốt hơn, tuỳ chọn) nếu SQL có `WHERE` trên `period` hoặc `_source_sheet` → chỉ giữ các tab thật sự được dùng.
- Output: `[{sheet, range, table_title}]` để UI hiển thị và highlight.

### 7.5 Kiểm tra cuối (MVP: luật đơn giản)

Trước khi trả `final_answer`:

- Câu trả lời có con số → con số đó phải xuất hiện trong ít nhất một result đã chạy (so khớp sau khi chuẩn hoá định dạng số). Không có → yêu cầu agent sửa (tối đa 1 lần).
- `result_ids` không rỗng nếu câu hỏi là dạng định lượng.

Verifier bằng LLM riêng để dành cho sau, nếu eval cho thấy cần.

### 7.6 Ngôn ngữ

- Prompt hệ thống viết bằng tiếng Anh (model thường theo instruction ổn định hơn).
- Câu trả lời theo ngôn ngữ của câu hỏi.

---

## 8. LLM runtime

### 8.1 Client (`llm/client.py`)

```python
class LLMClient:
    async def chat(self, messages, *, temperature=0.0, max_tokens=1024,
                   thinking: bool = False) -> str
    async def chat_json(self, messages, *, schema: type[BaseModel],
                        retries: int = 2, **kw) -> BaseModel
```

- Gọi server OpenAI-compatible tại `settings.llm.base_url` (localhost).
- `chat_json`: dùng structured output nếu backend hỗ trợ (`response_format` JSON schema, guided decoding hoặc grammar); nếu không thì parse JSON từ text (lấy khối `{…}` cuối cùng) rồi validate bằng pydantic, lỗi thì retry kèm thông báo lỗi.
- `thinking`: truyền qua tham số chat template của backend. ⚠️ Tên tham số cụ thể cho Qwen3.5 và backend đã chọn cần xác minh ở M0.
- Semaphore giới hạn số request đồng thời (`max_concurrency`).
- Log mọi lời gọi (prompt, output, latency, token) vào `.logs/llm.jsonl` để debug và tạo data SFT sau này.

### 8.2 Chính sách thinking theo bước

| Bước | Thinking | Lý do |
|---|---|---|
| Detect (LLM fallback) | tắt | output ngắn, có cấu trúc |
| Cards | tắt | mô tả ngắn |
| Xác nhận relation | tắt | câu hỏi có/không |
| Agent | tắt mặc định, bật nếu config `agent.thinking=true` | đo cả hai trong eval, chọn theo latency/độ chính xác |

### 8.3 Offline

- `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`.
- Streamlit: `browser.gatherUsageStats = false`.
- Model và embedding nằm sẵn trong thư mục `models/`, đường dẫn khai báo trong config.
- Test M0: chạy toàn bộ pipeline khi đã ngắt mạng.

---

## 9. UI

Streamlit, một trang.

```
┌────────────── Sidebar ──────────────┐┌──────────────────── Main ────────────────────┐
│ Upload .xlsx                        ││ Chat                                         │
│ ─────────────                       ││  Q: Chi nhánh miền Bắc nào tăng mạnh nhất…   │
│ Tiến độ ingestion                   ││  ▸ Agent steps (expander)                    │
│  ✓ Đọc 100 tab                      ││     1. search_catalog("doanh thu chi nhánh") │
│  ✓ Tìm 23 bảng (18 luật · 5 LLM)    ││     2. describe_table(fam_dt)                │
│  ✓ Gộp 12 tab → 1 family            ││     3. run_sql(SELECT …)                     │
│  ✓ 9 quan hệ                        ││  A: CN Hải Phòng, +18.4% …                   │
│ ─────────────                       ││     Nguồn: ChiNhanh!A2:D40, DT_T4…DT_T9      │
│ Catalog (danh sách thẻ)             ││     [chart]                                  │
│ ● Offline                           ││     [Xem vùng nguồn] → bảng highlight ô       │
└─────────────────────────────────────┘└──────────────────────────────────────────────┘
```

Yêu cầu:

- Hiển thị từng bước agent theo thời gian thực (thought + tool + SQL).
- Câu trả lời kèm provenance; bấm vào nguồn → render vùng ô dưới dạng bảng HTML, highlight các cột/dòng liên quan.
- Bảng `low_confidence` có dấu cảnh báo trong catalog.
- Chỉ báo "Offline" (kiểm tra không có kết nối ra ngoài, dùng cho demo).

---

## 10. Config

`config/settings.yaml`:

```yaml
llm:
  base_url: "http://127.0.0.1:8000/v1"
  model: "qwen3.5-35b-a3b-int4"
  max_concurrency: 8
  timeout_s: 120
  structured_output: true        # tắt nếu backend không hỗ trợ

embedding:
  model_path: "models/embedding"
  hybrid_alpha: 0.6

ingest:
  max_file_mb: 50
  max_sheets: 300
  large_file_cells: 2000000
  recalc_threshold: 50           # số ô công thức thiếu cache để kích hoạt LibreOffice
  libreoffice_bin: "soffice"

detect:
  accept_threshold: 0.8
  gap_rows: 1
  gap_cols: 1
  llm_retries: 2
  max_tokens_per_block: 6000

clean:
  ffill_vertical_merges: true

relations:
  family_jaccard: 0.9
  accept_score: 0.8
  llm_confirm_low: 0.5
  sample_distinct: 5000

agent:
  max_steps: 8
  thinking: false
  sql_timeout_s: 10
  max_rows_to_llm: 30
  max_rows_return: 200
  search_k: 8

paths:
  cache_dir: ".cache"
  log_dir: ".logs"
```

---

## 11. Evaluation

### 11.1 Dataset

Hai nguồn:

1. **Synthetic** (`eval/generator/`): sinh dữ liệu sạch (chi nhánh, sản phẩm, doanh thu theo tháng, nhân sự…) rồi áp các phép "làm bẩn" có kiểm soát. Vì tự sinh nên có sẵn gold.
2. **Real**: file thật (đã ẩn danh), gán nhãn tay. Ít nhưng quan trọng hơn.

Các phép làm bẩn (`perturb.py`), mỗi phép bật/tắt độc lập:

| Phép | Mô tả |
|---|---|
| `title_rows` | thêm 1–3 dòng tiêu đề, ngày lập báo cáo phía trên |
| `multi_header` | header 2 tầng với ô merge |
| `multi_table` | 2–3 bảng trên cùng tab |
| `total_rows` | chèn subtotal theo nhóm và tổng cuối |
| `notes` | dòng ghi chú cuối bảng |
| `split_period` | tách bảng thành N tab theo tháng/quý |
| `rename_cols` | đổi tên cột giữa các tab ("Doanh thu" / "DT" / "Revenue") |
| `unit_shift` | một số tab dùng nghìn đồng thay vì triệu đồng |
| `hidden` | ẩn một số cột/tab |
| `formats` | số định dạng `#,##0`, `%`, ngày `dd/mm/yyyy` |

### 11.2 Format gold

`tables.jsonl`:

```json
{"workbook": "wb_017.xlsx", "sheet": "DT_T1", "range": "A4:H55", "header_rows": [4, 5], "total_rows": [55]}
```

`questions.jsonl`:

```json
{"id": "q_0142", "workbook": "wb_017.xlsx",
 "question": "Chi nhánh miền Bắc nào có doanh thu Q3 tăng mạnh nhất so với Q2?",
 "answer": "CN Hải Phòng", "answer_type": "text",
 "category": "multi_tab_join",
 "gold_sources": [{"sheet": "ChiNhanh"}, {"sheet": "DT_T4"}, {"sheet": "DT_T9"}]}
```

`answer_type`: `number` (kèm `tolerance`), `text`, `list` (không thứ tự), `table`.

`category`: `single_lookup`, `aggregate`, `multi_tab_join`, `fragmented_period`, `trap_units`, `trap_names`, `trap_totals`.

### 11.3 Metrics

| Tầng | Metric | Cách tính |
|---|---|---|
| Detect | table P/R/F1 | khớp nếu IoU vùng ≥ 0.9 |
| | header accuracy | `header_rows` trùng khớp hoàn toàn |
| | total-row recall | |
| Catalog | family accuracy | các tab cùng khuôn được gộp đúng |
| Retrieval | recall@k | bảng gold có trong top-k của `search_catalog` đầu tiên |
| SQL | exec success rate | tỷ lệ `run_sql` chạy không lỗi |
| Answer | accuracy | so theo `answer_type`; số dùng sai số tương đối `tolerance` (mặc định 1e-3) |
| | provenance recall | tab gold có trong provenance |
| Chi phí | latency p50/p95, số bước, token in/out | từ log |

Báo cáo theo từng `category`, không chỉ một con số tổng.

### 11.4 Baselines

| ID | Mô tả |
|---|---|
| B0 | Dump toàn bộ workbook ra markdown (cắt theo context limit), hỏi thẳng model |
| B1 | Load mọi tab thô bằng pandas, agent viết code pandas trong sandbox, không catalog |
| B2 | Pipeline đầy đủ của tài liệu này, chỉ dùng prompt |

Mọi cải tiến sau (encoding, thinking on/off, SFT, GRPO, verifier, MAS) là ablation trên B2.

### 11.5 Chạy

```bash
python -m eval.run_eval --dataset synthetic_v1 --system b2 --out results/b2_synth_v1.json
python -m eval.run_eval --dataset synthetic_v1 --system b0
python -m eval.metrics compare results/b0_*.json results/b2_*.json
```

---

## 12. Milestones

| ID | Nội dung | Definition of done |
|---|---|---|
| M0 | Runtime check | Model chính (hoặc dự phòng) chạy trên GPU Intel qua server OpenAI-compatible; đo tokens/s; structured output và tắt thinking hoạt động; embedding chạy local; toàn bộ chạy khi ngắt mạng |
| M1 | MVP agent + SQL | Workbook **sạch** nhiều tab (mỗi tab một bảng, header dòng 1) → load thẳng DuckDB → agent với 5 tools trả lời đúng ≥ 70% bộ câu hỏi nhỏ (20 câu) |
| M2 | Ingestion đầy đủ | extract IR → detect (rules + LLM) → clean → families → relations → cards → index; unit test cho từng module với fixtures |
| M3 | Eval + baselines | generator sinh ≥ 30 workbook; ≥ 150 câu hỏi; chạy B0, B1, B2; có bảng so sánh theo category |
| M4 | UI demo | Streamlit đủ các phần ở §9; kịch bản demo 3 phút chạy trơn tru trên máy thật, không mạng |
| M5 (tuỳ chọn) | Fine-tune | Nếu eval cho thấy detect là điểm yếu: LoRA SFT cho bước detect với data từ generator + log; so sánh với B2 |

Thứ tự ưu tiên khi thiếu thời gian: M0 → M1 → M4 (bản đơn giản) → M2 → M3.

---

## 13. Rủi ro và cách giảm

| Rủi ro | Mức | Cách giảm |
|---|---|---|
| Backend Intel chưa hỗ trợ Qwen3.5 | Cao | Kiểm tra ngay ở M0; client chỉ phụ thuộc API OpenAI-compatible nên đổi model/backend không đổi code |
| Detect sai trên file thật | Cao | Luật trước LLM sau; thu thập file thật sớm; `totals_check` phát hiện lỗi cắt vùng; đánh dấu `low_confidence` |
| Latency agent cao | Trung bình | Tắt thinking mặc định; giới hạn `max_steps`; cắt kết quả gửi cho LLM; cache ingestion |
| LLM sinh JSON sai format | Trung bình | Structured output nếu có; validate + retry; log để phân tích |
| Join sai khoá, lệch đơn vị | Trung bình | Hint ở §7.3.2; unit trong card; kiểm tra số trong câu trả lời phải có trong result |
| `sheet-compressor` (community v0.1) có lỗi | Thấp | Bọc sau interface `encoder.py`; có thể tự viết lại theo paper |
| SQL nguy hiểm | Thấp | Read-only connection, chỉ SELECT, whitelist bảng, chặn hàm đọc file, timeout |

---

## 14. Câu hỏi còn mở

1. Runtime inference nào trên Intel Arc Pro B70 (OpenVINO / llama.cpp / vLLM XPU)? → quyết định ở M0.
2. Model embedding nào? → chọn ở M0 theo hỗ trợ của runtime.
3. Có cần chuyển wide→long (cột tháng → dòng) ở bước clean không? → quyết định sau khi có eval.
4. Có cần xử lý pivot table không, hay chỉ cảnh báo? → hỏi người dùng mục tiêu.
5. Câu trả lời cho câu hỏi mơ hồ: agent hỏi lại người dùng, hay chọn cách hiểu hợp lý nhất và nói rõ? → MVP: chọn cách hiểu hợp lý nhất và ghi rõ trong câu trả lời.
6. Giới hạn kích thước workbook thực tế của người dùng mục tiêu?

---

## 15. Phụ lục: prompt templates

> Bản nháp. Tune bằng eval. Viết bằng tiếng Anh, giữ nguyên dữ liệu gốc (có thể tiếng Việt) trong phần input.

### 15.1 Detect tables

```text
SYSTEM:
You analyze one region of a spreadsheet and identify the data tables inside it.
Return JSON only, matching the schema. Use absolute Excel row numbers and A1 ranges.

Definitions:
- header_rows: rows containing column labels. Multi-level headers have several rows.
- total_rows: subtotal or grand-total rows that aggregate rows above them.
- note_rows: free-text notes inside the table range.
- title: a caption above the table, if any (not part of the range).

Rules:
- A range must include its header rows.
- Tables must not overlap.
- If there is no table, return an empty list.

USER:
Sheet: "{sheet_name}"
Region: {region_a1}
Hints from rules (may be wrong): {hints_json}

Encoded cells:
{encoding}

Schema:
{json_schema}
```

### 15.2 Catalog card

```text
SYSTEM:
You write a short catalog entry for a data table so that a search system can find it
and an analyst can understand it. Return JSON only, matching the schema.
Write in {language}. Do not invent columns or facts not present in the input.

USER:
Sheet name(s): {sheet_names}
Title above table: {title}
Columns (name | display name | type | unit):
{columns}
Statistics:
{stats}
Sample rows:
{sample_rows}
Time coverage: {time_coverage}
Known relations: {relations}

Schema:
{json_schema}   # title, description (1-3 sentences), columns[{name, description}], keywords[≤10]
```

### 15.3 Xác nhận relation

```text
SYSTEM:
Decide whether two columns from different tables refer to the same entity and can be
joined. Answer JSON: {"same_entity": true|false, "reason": "<one sentence>"}.

USER:
Column A: {table_a}.{col_a} — header "{header_a}", samples {samples_a}
Column B: {table_b}.{col_b} — header "{header_b}", samples {samples_b}
Value containment A→B: {containment}
```

### 15.4 Agent system prompt

```text
You are a data analyst answering questions about an Excel workbook that has already
been converted into clean SQL tables (DuckDB dialect).

Workbook summary:
{catalog_summary}

You work in steps. At each step output exactly one JSON object:
{"thought": "<1-3 sentences>", "tool": "<tool name>", "args": {...}}

Tools:
- search_catalog(query, k): find relevant tables. Always start here.
- describe_table(card_id): get columns, units, sample rows, relations.
- run_sql(sql): run a read-only SELECT. Use only tables/views from the catalog.
- make_chart(result_id, type, x, y, title): chart a previous result.
- final_answer(answer, result_ids, chart_id): finish.

Rules:
- Never compute numbers yourself. Every number in the answer must come from a SQL result.
- Check units before combining columns from different tables.
- If a filter returns nothing, inspect actual values with SELECT DISTINCT before retrying.
- If the question is ambiguous, choose the most reasonable interpretation and state it
  in the answer.
- Answer in the same language as the question. Mention which sheets the data came from.
```

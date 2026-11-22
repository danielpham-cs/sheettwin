# Kết quả đánh giá

> Chạy ngày 2026-11-04 với `gpt-5.4-mini` (OpenAI), theo §11 của [`excel-agent-system-design.md`](excel-agent-system-design.md).
> Dữ liệu là workbook **sinh tự động** (`eval/generator`), chưa có file thật. Chưa có số đo cho model local trên Intel Arc.

Tái tạo:

```bash
uv run python -m eval.generator.make --out eval/datasets/synthetic_v1 --workbooks 30 --questions 6 --seed 7
uv run python -m eval.run_eval --dataset eval/datasets/synthetic_v1 --system b2 --out results/synth_v1_b2.json
uv run python -m eval.metrics compare results/synth_v1_b0.json results/synth_v1_b1.json results/synth_v1_b2.json
```

## M1 — workbook sạch (4 workbook, 20 câu)

**20/20 đúng** (mục tiêu M1: ≥ 70%). Detect: P/R/F1 = 100%.

Lượt chạy đầu đạt 16/20. Các lỗi đã sửa: bộ chấm không bỏ markdown, model đoán mã chi nhánh thay vì tra cứu, chỉ liệt
kê mã mà không nêu tên, và từ chối trả lời khi câu hỏi nêu năm mà dữ liệu không ghi năm.

## synthetic_v1 — workbook bẩn (30 workbook, 180 câu)

| Chỉ số | B0 dump vào prompt | B1 agent trên sheet thô | **B2 pipeline đầy đủ** |
|---|---|---|---|
| **Độ chính xác** | 38.3% | 73.3% ¹ | **100% (180/180)** |
| single_lookup (n=32) | 100% | 100% | 100% |
| aggregate (n=27) | 29.6% | 88.9% | 100% |
| multi_tab_join (n=35) | 31.4% | 85.7% | 100% |
| fragmented_period (n=28) | 21.4% | 82.1% | 100% |
| trap_names (n=16) | 0% | 31.2% | 100% |
| trap_totals (n=26) | 42.3% | 38.5% | 100% |
| trap_units (n=16) | 6.2% | 50.0% | 100% |
| Latency p50 / p95 | 1.0 / 1.3 s | 7.0 / 12.2 s | 4.7 / 9.0 s |
| Số bước trung bình | 1 | 4.7 | 3.6 |
| Số lần gọi model | 1 | 5.6 | 4.3 |
| Prompt tokens / câu | 8.3k | 13.9k | 9.8k |
| SQL chạy thành công | — | 98.2% | 96.3% |
| Retrieval recall@k | — | 37.1% | 100% |
| Provenance recall | — | 99.7% | 97.5% |
| Câu trả lời có số đã đối chiếu | 0% | 99.4% | 99.4% |

Detect (B2): precision 98.0%, recall 100%, F1 99.0%, header đúng 100%, dòng tổng/subtotal tìm được 100%. Gộp 12 tab
tháng thành một family: 100% số workbook có tách tháng.

¹ Lượt chạy B1 đầu tiên (prompt trước khi thêm 3 quy tắc cuối). Lượt chạy lại hết credit OpenAI giữa chừng (38 câu
lỗi gọi model); trên 142 câu chạy được, B1 đạt 76.8%. B0 chỉ chạy một lần.

### Đọc kết quả

- **B0** chỉ đúng các câu tra cứu một ô. Với tổng, phép nối và dữ liệu nhiều tab, model tự cộng trong đầu nên sai.
- **B1** có SQL và kiểm tra số, nhưng phải tự đoán header, gặp dòng tổng/subtotal thì cộng trùng (`trap_totals`
  38.5%), không biết các tháng tính bằng nghìn đồng (`trap_units` 50%), và các tháng đổi tên cột thì bị bỏ sót.
- **B2** xử lý ở tầng code những gì B1 làm sai: bỏ dòng tổng và subtotal, quy đổi đơn vị trong view, khớp cột theo
  vị trí, và đưa `period` về một dạng chung. Model chỉ còn phải viết một câu SQL trên một view sạch.

### Các lỗi đã sửa nhờ eval (B2: 91.7% → 100%)

| Lỗi | Sửa |
|---|---|
| Card của family lấy đơn vị của tab đầu (nghìn đồng) trong khi view đã đổi sang triệu, nên model chia thêm 1000 | Đơn vị và tên hiển thị của card lấy theo view |
| `period` lẫn hai dạng `01` và `2026-01` trong cùng family (chỉ một số tab có dòng tiêu đề ghi năm) | Đưa về một dạng khi các tab chỉ ghi một năm |
| `a OR b AND c` thiếu ngoặc | Gợi ý tự động sau truy vấn |
| Hỏi tổng nhiều phần nhưng chỉ trả lời từng phần; tự chia đơn vị dù không cần | Thêm quy tắc vào prompt |

## Giới hạn của các con số này

- Chỉ có **một kiểu công ty** (chuỗi bán lẻ, tiếng Việt), do chính hệ thống sinh ra. Cần thêm file thật đã ẩn danh.
- LLM fallback cho detect chưa được đo: mọi bảng sinh ra đều được luật tìm thấy.
- Câu hỏi sinh theo mẫu, nên 100% ở đây không có nghĩa là 100% trên câu hỏi tự do của người dùng.
- Mới đo với một model cloud. Model local (Qwen trên Intel Arc) cần chạy lại `bench/m0_runtime.py` và eval này.

# Quyết định ngôn ngữ và định dạng

Ngày nghiên cứu: 2026-09-25. Kết luận: **Python 3.11-compatible** cho source mẫu. Đây là chọn lựa để khớp codebase hiện có và giảm công tích hợp, không là tuyên bố Python luôn tốt hơn các ngôn ngữ khác.

## Căn cứ trong Ripple

- `core/change_intelligence/ingest.py` và Python adapters đã có trong repo.
- `reference_system/application/approval_service.py` chứa business functions nhận config; `core/change_intelligence/simulation.py` thực thi handlers dạng `(request, config)`.
- `app/api/handlers.py::_build_engine` hiện vẫn gắn reference system/manifest. Dataset mới không tự trở thành nguồn runtime chỉ bằng copy folder; phải nối theo AI3–AI6 trong plan.
- Runtime đang dùng Python, PyYAML và openpyxl. Bộ mẫu chỉ cần môi trường Python riêng, không thêm Node/JDK vào quy trình demo.
- Factory `ripple_testdata` có prompts/oracle/testing concepts tái sử dụng bằng thiết kế, nhưng schema oracle hiện thiên về một threshold và spreadsheet CSV. Không dùng evaluator đó làm bằng chứng đầy đủ cho scope, invoice matching và multi-rule priority của bộ mẫu mới.

## So sánh theo nhu cầu demo của repo này

| Lựa chọn | Thuận lợi | Công phải bổ sung trong Ripple | Quyết định |
|---|---|---|---|
| Python | Cùng ngôn ngữ runtime/simulator; pure functions; AST có vị trí source; builder Excel cùng runtime | Generalize seed-specific binding, thêm adapters và sandbox theo plan | Chọn |
| TypeScript/Node | Phù hợp nếu muốn đầu tư UI web riêng | Runtime/parser/simulation adapter mới cho source TS, thêm frontend build | Không cần cho bộ đầu |
| Java/C# | Thích hợp làm tập dữ liệu enterprise ở đợt mở rộng | Build/runtime/parser/execution integration riêng; không có lợi ngay cho judge path hiện tại | Để phase sau |

Các nhận xét công tích hợp là đánh giá từ cấu trúc repo, không là benchmark ngôn ngữ. Giữ core nhỏ, explicit và executable để độ khó nằm ở liên kết nghiệp vụ giữa nguồn, thay vì build toolchain.

## Tài liệu chính thức đã kiểm tra

1. Python `ast` cho phép parse source thành cây và có vị trí dòng/cột của nodes, phù hợp evidence mapping. [Python 3.11 AST](https://docs.python.org/3.11/library/ast.html).
2. openpyxl tạo/đọc workbook, nhưng **không tính kết quả công thức**. Tiền/tổng phải tính bằng Python; workbook có formula để minh họa nhưng không đọc cached formula value làm oracle. [Tutorial](https://openpyxl.readthedocs.io/en/3.1/tutorial.html), [Formulas](https://openpyxl.readthedocs.io/en/stable/simple_formulae.html).
3. FastAPI có hướng dẫn TestClient/pytest để kiểm tra HTTP; giữ web optional, business core và CLI hoạt động độc lập. [FastAPI testing](https://fastapi.tiangolo.com/tutorial/testing/).
4. Vertex structured output hỗ trợ response schema với một tập con schema fields; vẫn phải validate semantics, paths và complete file content ở máy chạy. Schema chỉ làm output dễ xử lý, không chứng minh code đúng. [Structured output](https://cloud.google.com/vertex-ai/generative-ai/docs/multimodal/control-generated-output).
5. Free Trial có điều kiện/restrictions, bao gồm hạn chế đối với generative partner models dạng managed API; kiểm model Google Gemini và billing của project trước khi chạy lớn. Không chọn partner model dựa trên giả định credit dùng được. [Free Trial](https://docs.cloud.google.com/free/docs/free-cloud-features).
6. Alerts-only budget không tự cap usage. Với lỗi 429, dùng backoff/retry hữu hạn và giảm concurrency; không retry vô hạn để tiêu hết credit. [Budgets](https://docs.cloud.google.com/billing/docs/how-to/budgets), [429](https://cloud.google.com/vertex-ai/generative-ai/docs/error-code-429).

## Stack chốt cho prompt

- Python syntax/API tương thích 3.11; kiểm trên phiên bản đang có trong môi trường, không dùng cú pháp mới hơn rồi ghi 3.11-compatible.
- Core: dataclasses, decimal khi cần, datetime, csv, json, pathlib, hashlib, argparse. Pure routing/matching nhận config thay vì đọc globals.
- PyYAML `safe_load`, openpyxl cho XLSX, pytest cho test. Dependency versions trong project riêng phải do môi trường resolve/test rồi pin; model không được bịa version package chưa xác nhận.
- FastAPI + minimal HTML optional, không React/SPA. Không DB bắt buộc: input immutable CSV/JSONL, demo events JSONL trong output workspace riêng. Đây là reference app một người dùng offline, không kiến trúc persistence production của Ripple.
- Markdown cho thiết kế/manual/spec/SOP; Mermaid code blocks để render sơ đồ khi cần. Invoice/GR HTML + canonical JSONL. Optional ReportLab/PDF và python-docx/DOCX dùng để trình bày, không gate dữ liệu đọc của Ripple cho đến khi adapter được kiểm chứng.
- Không OCR, macro, ERP, email sending, external payment hoặc cloud runtime trong hệ thống mẫu. Các mẫu chứng từ có watermark SYNTHETIC DEMO và bên giao dịch hư cấu.

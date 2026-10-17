# Bộ prompt Vertex AI: Harbor Procurement Mini

Đây là **bộ prompt để sinh hệ thống mẫu**, chưa phải hệ thống/dataset đã được tạo hoặc kiểm thử. Lưu đúng đường dẫn được yêu cầu: `C:\4. DEV\GitHub(Onlyone)\Ripple\ripple\_testdata`. Thư mục này khác `Ripple\ripple_testdata` đang có; không ghi đè factory cũ.

## Chọn ngôn ngữ và hệ thống mẫu

**Python 3.11-compatible**, business logic thuần + CLI bắt buộc, web FastAPI/HTML nhỏ tùy chọn. Dữ liệu JSONL/CSV, cấu hình YAML, Excel thật bằng openpyxl, tài liệu thiết kế/manual Markdown, hóa đơn/phiếu nhận hàng HTML có bản dữ liệu cấu trúc tương ứng. PDF/DOCX là bản trình bày tùy chọn, chưa được quảng bá là input Ripple đọc được nếu adapter chưa qua gate.

Harbor Procurement Mini là hệ thống hư cấu quản lý yêu cầu mua hàng, tuyến phê duyệt, PO, nhận hàng, kiểm tra hóa đơn và hạn thanh toán. Không xây lại AI/RAG/HITL engine của Ripple bên trong hệ thống mẫu. Đọc [nghiên cứu lựa chọn](01_LANGUAGE_RESEARCH.md) và [đặc tả khóa](03_FROZEN_SPEC.md).

Mục tiêu: vừa có chương trình chạy thật để mô phỏng, vừa có thông tin phân tán qua code/workflow/Excel/tài liệu để AI phải hiểu ngữ nghĩa. Có các sai lệch nghiệp vụ được thiết kế, nhưng không cố ý làm chương trình lỗi cú pháp hoặc file Excel hỏng.

## Thứ tự sử dụng trên Vertex AI

1. Đặt [02_SYSTEM_INSTRUCTION_EN.md](02_SYSTEM_INSTRUCTION_EN.md) vào System instruction. Đây là nội dung hướng dẫn model, không phải một request riêng.
2. Gửi [prompts/01_BLUEPRINT.md](prompts/01_BLUEPRINT.md) cùng toàn bộ `03_FROZEN_SPEC.md`. Lưu các file trả về, duyệt blueprint/contract trước khi tiếp tục. Nếu Vertex chat không tạo file trực tiếp, model trả JSON chứa nội dung file; lưu từng `content` vào `path` tương ứng trong thư mục `generated/` tại đây.
3. Chạy tuần tự các prompt dưới, mỗi lần chỉ yêu cầu batch file nhỏ. Đưa đúng input được ghi trong prompt. Không chỉ nói "tiếp tục" khi thiếu hợp đồng hoặc file phụ thuộc.
4. Chạy prompt **06_ORACLE** ở conversation mới với system instruction, frozen spec và case catalog đã duyệt; không đưa implementation hay actual outputs vào khi model viết expected evaluator.
5. Sau khi sinh xong, chạy các lệnh kiểm chứng mà model cung cấp **trong môi trường cô lập**, lưu kết quả thực. Model không có execution tool phải để `NOT_RUN`; không được chấp nhận lời nói "all tests passed".
6. Chỉ upload nội dung folder `generated/bundles/harbor_demo/input/` cho Ripple. Oracle, scenario manifests và operator guide nằm ngoài folder đó.

| Bước | Prompt | Đầu ra chính |
|---|---|---|
| 01 | [Blueprint](prompts/01_BLUEPRINT.md) | File tree, interfaces, schemas, public/private packaging contract |
| 02 | [Core application](prompts/02_CORE_APPLICATION.md) | Python app + CLI + characterization tests |
| 03 | [Data builders](prompts/03_DATA_BUILDERS.md) | Script sinh dữ liệu có seed, liên kết PO–GR–invoice, boundary cases |
| 04 | [Excel & chứng từ](prompts/04_WORKBOOKS_DOCUMENTS.md) | Script tạo XLSX thật, hóa đơn/phiếu HTML; PDF/DOCX tùy chọn |
| 05 | [Thiết kế & manual](prompts/05_BUSINESS_DESIGN_MANUALS.md) | Policies, SOP, design, data dictionary, manual, API guide |
| 06 | [Oracle độc lập](prompts/06_ORACLE_TESTS.md) | Evaluator theo spec, golden hand-calculated cases, leakage checks |
| 07 | [Scenarios & holdout](prompts/07_SCENARIOS_VARIANTS.md) | S0/S1/S2/restore/refresh, mutations, dev/held-out variants |
| 08 | [Review & sửa lỗi](prompts/08_REVIEW_REPAIR.md) | Báo cáo độc lập, patch đúng phạm vi; không xóa intentional conflicts |
| 09 | [Đóng gói & demo](prompts/09_PACKAGE_DEMO.md) | Import bundles, evidence manifest, runbook, demo script và claims |

[04_GENERATION_LEDGER.md](04_GENERATION_LEDGER.md) để theo dõi từng lượt; [05_ACCEPTANCE_MAP.md](05_ACCEPTANCE_MAP.md) nối dataset với plan AI0–AI11. [prompts/10_CONTINUE_OR_REPAIR.md](prompts/10_CONTINUE_OR_REPAIR.md) dùng khi output dài/bị cắt/lỗi. `schemas/file_bundle.response_schema.json` là response schema nhỏ tùy chọn nếu dùng API.

## Cách chia request để không tốn credit vô ích

- Chọn một **Google Gemini model đang khả dụng trong project Vertex của bạn**; model tiết kiệm phù hợp cho file/data prose, model reasoning mạnh hơn cho blueprint/oracle/review khi cần. Không lấy model ID cũ hardcode trong repo làm bằng chứng model còn dùng được.
- Thử một request nhỏ trước. Ghi model ID, location, token usage/latency và số credit thực tế trong ledger. Không cần cấp key/credential cho model; auth thực hiện bên ngoài prompt.
- Mỗi request 2–4 file nhỏ hoặc 1 module dài. Các hàng giao dịch và XLSX phải do script deterministic sinh; không trả vài trăm hàng thủ công qua chat. Không yêu cầu LLM xuất binary/base64 XLSX/PDF.
- Giữ nguyên frozen spec và public contracts qua các lượt; chỉ đưa file cần thiết. Không gửi lại cả repo mỗi lần. Các bước phụ thuộc chạy tuần tự, không batch song song khi interface chưa khóa.
- Ban đầu dùng temperature thấp cho code/JSON; ghi thông số thực và giữ cố định khi tái lập. Không giả định temperature thấp làm output model hoàn toàn deterministic.
- Theo dõi budget alert và giới hạn số lượt/token do chính người chạy đặt. Alerts-only budget không tự chặn chi tiêu. Tình trạng trial/eligibility/model quota phải kiểm tra ở project thực; không có cam kết bộ này chắc chắn miễn phí. [Google Cloud budgets](https://docs.cloud.google.com/billing/docs/how-to/budgets), [Free Trial](https://docs.cloud.google.com/free/docs/free-cloud-features).

## File nhị phân và môi trường chạy

Vertex sinh **Python builder + structured specifications**, người chạy thực thi builder để tạo file nhị phân. Chỉ coi file tồn tại sau khi builder chạy, có path/size/hash và parser mở lại thành công. Builder không tự cài package, gọi cloud, đọc credential hoặc ghi ra ngoài output root. Không auto-execute model output từ một JSON bundle.

Chương trình mẫu sinh ra chạy offline; chỉ quá trình dùng Vertex để tác giả hóa dữ liệu mới cần AI. Muốn demo Ripple dùng AI thật thì chính Ripple phải gọi provider trên input bundle; generator không phải runtime AI của Ripple.

## Dữ liệu demo và dữ liệu đánh giá

`harbor_demo` là dữ liệu dùng phát triển và trình diễn; sau khi agent đã xem đáp án, nó **không còn là held-out evaluation**. Muốn chứng minh AI hiểu nguồn mới, dùng variants giữ kín ngoài context của agent phát triển, có human review oracle. Prompt 07 tạo cấu trúc dev/heldout; không tự coi variation từ cùng một template là bằng chứng khái quát rộng.

Không yêu cầu Ripple hiện tại đọc ngay toàn bộ bộ mẫu. Plan bổ sung sẽ xây import, semantic discovery, general simulation adapter và patch executors. Reference simulator hiện còn phụ thuộc seed; tham số `(request, config)` là một seam thuận lợi, không là cam kết plug-and-play. Nêu rõ điều này trong demo cho đến khi gates tương ứng đạt.

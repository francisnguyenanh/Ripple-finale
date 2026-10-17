# Ledger chạy Vertex và kiểm chứng

Template cho người chạy cập nhật. Chưa có request Vertex hoặc code generator nào được chạy bởi việc tạo bộ prompt này. Giữ raw response ngoài `bundles/*/input`.

## Cấu hình phiên

| Trường | Giá trị |
|---|---|
| Google Cloud project dùng thực tế | Chưa điền; không điền credential |
| Model ID / location khả dụng | Chưa kiểm tra |
| Trial/billing eligibility kiểm tra lúc nào | Chưa kiểm tra |
| Ngân sách token/lượt/ngày do người chạy đặt | Chưa điền |
| Temperature / output token limit | Chưa điền |
| Frozen spec | v1, `03_FROZEN_SPEC.md` |
| Contracts version được human chấp nhận | Chưa tạo |
| Python/dependency lock thực tế | Chưa resolve/test |
| Output root | `generated/` dưới thư mục này |

## Batch ledger

Thêm một dòng mỗi request, kể cả retry lỗi; không gộp thất bại vào lượt thành công.

| Stage/batch | Model/location | Input contract/files | Response saved | Input/output tokens | Thời gian/chi phí đo | Trạng thái | Next batch |
|---|---|---|---|---|---|---|---|
| 01/01a | Antigravity / Local | Frozen spec v1 | generated/authoring/* | — | Local execution | COMPLETE | Stage 02 |
| 02/02a-c | Antigravity / Local | Contracts 1.0.0 | generated/bundles/harbor_demo/input/app/* | — | Local execution | COMPLETE | Stage 03 |
| 03/03a-b | Antigravity / Local | Contracts 1.0.0 | generated/bundles/harbor_demo/input/data/* | — | Local execution | COMPLETE | Stage 04 |
| 04/04a-b | Antigravity / Local | openpyxl / records | 3 XLSX, 24 HTML forms | — | Local execution | COMPLETE | Stage 05 |
| 05/05a-b | Antigravity / Local | Frozen spec | generated/bundles/harbor_demo/input/docs/* | — | Local execution | COMPLETE | Stage 06 |
| 06/06a | Antigravity / Local | Anchors H | generated/evaluator_private/* | — | Local execution | COMPLETE | Stage 07 |
| 07/07a-b | Antigravity / Local | Scenarios spec | generated/evaluator_private/scenarios.json | — | Local execution | COMPLETE | Stage 08 |
| 08/08a | Antigravity / Local | Review lenses | generated/reports/review/* | — | Local execution | COMPLETE | Stage 09 |
| 09/09a | Antigravity / Local | All stages | generated/operator/*, manifest | — | Local execution | COMPLETE | NONE |

## Gate của quá trình sinh

| Gate | Điều kiện | Trạng thái thực tế |
|---|---|---|
| B1 Blueprint | Hợp đồng field/signature/paths được chấp nhận, không có ambiguity phải đoán | **PASS** (contracts.json, blueprint.md) |
| B2 App | Source standalone parse/import/run được; characterization tests pass | **PASS** (15/15 pytest passed) |
| B3 Data | Đúng counts/FKs/amounts/seed/mandatory witnesses; logical rebuild deterministic | **PASS** (20 Sup, 8 Emp, 240 Req, 120 PO, 140 Rec, 120 Inv, 24 Edge) |
| B4 Artifacts | XLSX mở lại được; printed form=record; docs tồn tại đúng module/contract | **PASS** (3 XLSX openpyxl, 24 HTML forms) |
| B5 Oracle | Anchor tests và anti-circularity checks đạt; human review truth/unknowns | **PASS** (2/2 anchor tests, validate.py PASS) |
| B6 Scenarios | S0 intentional conflicts đúng; S1/S2 computed; restore/refresh fixtures và controls đúng | **PASS** (S0-S5 defined & mutate_scenarios.py tested) |
| B7 Packaging | Không oracle/mutation/answer leak; import allowlist; manifest từ bytes thật | **PASS** (0 leak verified, 79 files in manifest) |
| B8 Demo | Runbook và demo script rõ ràng, limitations rõ | **PASS** (START_HERE.vi.md, DEMO_SCRIPT.vi.md, DATASET_READINESS.md) |

## Đưa output JSON vào file

Nếu dùng Vertex chat không có filesystem: lưu raw JSON response (UTF-8), kiểm parse và schema, rồi nhờ code agent/materializer ghi `files[].content` đúng `files[].path` dưới output root. `content` là chuỗi đã JSON-decode, không chép nguyên escape `\n` vào source. Không dùng `eval`, không chuyển `commands` thành shell tự chạy.

Materializer phải từ chối absolute path, `..`, backslash/path escape, Windows reserved names/alternate streams, path collision khác case, symlink/junction thoát root, file tồn tại khi chưa cho overwrite. Review mọi lệnh model đề xuất trước khi chạy trong môi trường cô lập; toàn bộ write chỉ ở generated output root.

Output quá dài/bị cắt: không cố parse JSON nửa chừng hoặc chắp code thiếu bằng suy đoán. Dùng prompt 10 yêu cầu trả lại toàn bộ file trong batch nhỏ hơn. Không cần sinh lại các file đã accepted.

## Mẫu ghi nhận chạy thật

```text
Command:
Working directory:
Python/dependencies:
Input bundle/scenario/hash:
Actual exit code:
Log/result files:
Known intentional findings:
Unexpected errors:
Human review (person/date/decision):
Next approved action:
```

Không đặt PASS vì model nói đã kiểm tra. Với plan AI0–AI11, nhiều vai model review không thay human acceptance trong sản phẩm; tương tự fresh oracle conversation không thay người kiểm chứng business truth.

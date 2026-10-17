# Kịch Bản Demo: Harbor Procurement Mini (5–7 Phút)

> **Ghi chú trung thực.** Harbor là bộ dữ liệu DEV/demo tổng hợp (synthetic), không phải tập held-out và không phải dữ liệu thật. Kịch bản này là kịch bản trình diễn dự kiến; các phát hiện nêu dưới đây chỉ được nói là kết quả nếu khớp lần chạy thật, nếu không phải gắn nhãn "minh họa / replay". Kết quả đánh giá held-out (không đạt ngưỡng) nằm ở README.md và docs/verification/ai_closed_loop/tasks/AI10-T03_FINAL_EVAL_RECORD.md. Chỉ file YAML và XLSX được áp dụng; Python và Markdown chỉ là đề xuất cho người sửa tay. Chưa khẳng định v2 đã deploy.

Kịch bản này được thiết kế để trình diễn năng lực hiểu ngữ nghĩa đa nguồn, phát hiện xung đột và mô phỏng tác động của hệ thống Ripple đối với bộ dữ liệu mẫu **Harbor Procurement Mini**.

---

## Bước 1: Trình diễn các nguồn dữ liệu ban đầu (1 Phút)
- **Hành động của diễn giả**:
  1. Mở file [approval_matrix.xlsx](../bundles/harbor_demo/input/data/approval_matrix.xlsx) trong Excel hoặc viewer: Chỉ ra dòng Manager có ngưỡng **500.000 JPY**.
  2. Mở file [approval.yaml](../bundles/harbor_demo/input/config/approval.yaml): Chỉ ra cấu hình thực thi hiện tại là **1.000.000 JPY**.
  3. Mở [finance_desk_guide.md](../bundles/harbor_demo/input/docs/finance_desk_guide.md): Chỉ ra tài liệu hướng dẫn nội bộ yêu cầu mọi khoản chi >= 2M JPY phải qua Finance, trái với chính sách CAPEX-only trong [procurement_policy.md](../bundles/harbor_demo/input/docs/procurement_policy.md).
- **Thông điệp chính**: Doanh nghiệp thực tế có tri thức phân tán, thông tin trong bảng tính Excel và tài liệu hướng dẫn thường bị lệch pha (drift) với code/config thực thi.

---

## Bước 2: Nhập khẩu S0 vào Ripple & Tự động phát hiện bất đồng bộ (2 Phút)
- **Hành động**: Thực hiện import bundle `bundles/harbor_demo/input/` vào Ripple.
- **Bằng chứng quan sát**: Ripple phân tích ngữ nghĩa và hiển thị 4 phát hiện:
  - **C01 (Value Conflict)**: Lệch giá trị giữa `approval_matrix.xlsx` (500k) và `approval.yaml` (1M).
  - **C02 (Scope Conflict)**: Lệch phạm vi giữa `finance_desk_guide.md` (mọi danh mục >= 2M) và `procurement_policy.md` (chỉ CAPEX).
  - **G01 (Policy Ambiguity)**: Thủ tục mua hàng khẩn cấp không nêu rõ liệu các thay đổi hạn mức tương lai có áp dụng cho đơn khẩn cấp không.
  - **G02 (Execution Gap)**: Yêu cầu leo thang phê duyệt sau 2 ngày làm việc trong SOP không có scheduler trong code để thực thi.

---

## Bước 3: Đưa ra quyết định S1 (Hạ hạn mức Manager xuống 500k JPY) (2 Phút)
- **Hành động**: Người dùng nhập quyết định: *"Hạ hạn mức duyệt của Manager xuống 500.000 JPY cho cả đơn mua tiêu chuẩn và đơn khẩn cấp"*.
- **Phân tích tác động (What-If Analysis)**:
  - Ripple chạy mô phỏng trên các đơn mua hàng tổng hợp của bundle (ghi rõ số ca đã chạy / tổng số và chế độ mô phỏng; chỉ nêu con số sau khi chạy thật) và báo số lượng đơn hàng thay đổi từ `AUTO` sang `NEEDS_MANAGER` (bao gồm nhóm 12 đơn Standard 600k và 12 đơn Emergency 600k).
  - **Điểm kiểm soát âm (Negative Control)**: Ripple nhận biết quy tắc cước vận chuyển 500.000 JPY trong `calculations.py` là quy tắc logistics, **không bị thay đổi nhầm**.
  - File Excel `approval_matrix.xlsx` đã sẵn 500k nên được đánh dấu là `NO_OP`.

---

## Bước 4: Duyệt đề xuất & Cập nhật phiên bản mới (1 Phút) — CHỈ TRONG TEST, KHÔNG CHẠY TRÊN URL ĐÃ TRIỂN KHAI
> Trên bản triển khai hiện tại mọi review đề xuất đều BLOCKED (acceptance_tests UNAVAILABLE): không thể duyệt hoặc áp dụng thay đổi qua UI/API đã triển khai; apply chỉ được kiểm chứng trong test. Khi demo trên URL triển khai, dừng ở bước 3 (đề xuất + review BLOCKED) và không trình diễn duyệt/áp dụng.
- **Hành động**: Operator duyệt đúng digest của đề xuất do Ripple tạo (tất định, không do AI soạn). Chỉ thay đổi YAML/XLSX được áp dụng.
- **Bằng chứng quan sát**:
  - `config/approval.yaml` được cập nhật thành `500000`.
  - `app/emergency.py` (Python) được liệt kê là hành động thủ công: Ripple không sửa, không kiểm chứng và không mô phỏng Python; người thực hiện sửa tay, mục này vẫn mở cho đến khi có quyết định.
  - Xung đột C01 được giải quyết.
  - Các xung đột C02 và G02 chưa được duyệt sửa vẫn giữ nguyên trạng thái mở (không có nhãn "ALIGNED" toàn cục).

---

## Phiên Bản Ngắn Gọn (3 Phút):
1. **0:00 - 0:45**: Mở song song Excel (500k) và Config (1M) cho khán giả thấy sự mâu thuẫn thực tế.
2. **0:45 - 1:45**: Ripple liệt kê các phát hiện kèm trích dẫn bằng chứng (kết quả held-out cho thấy phát hiện conflict đo được là 0/24, nên không nói "chính xác").
3. **1:45 - 3:00**: Ra lệnh hạ hạn mức xuống 500k, Ripple ước tính tác động và đề xuất thay đổi; trên URL triển khai dừng tại đề xuất (review BLOCKED, không duyệt/áp dụng được; duyệt + áp dụng YAML/XLSX chỉ có trong test). Không áp dụng cho Python/Markdown.

# Hướng Dẫn Vận Hành: Harbor Procurement Mini (START_HERE)

Tài liệu này hướng dẫn chi tiết cách thiết lập, biên dịch dữ liệu mẫu và chạy ứng dụng reference **Harbor Procurement Mini**.

---

## 1. Yêu Cầu Môi Trường
- **Python**: 3.11 trở lên (khuyến nghị môi trường ảo venv độc lập).
- **Thư viện phụ thuộc**:
  ```bash
  pip install pyyaml openpyxl pytest
  ```

---

## 2. Quy Trình Biên Dịch & Đóng Gói Bundle (Build Pipeline)

Để biên dịch toàn bộ dữ liệu mẫu, tạo 3 file Excel `.xlsx`, 24 chứng từ HTML và đóng gói bundle public hoàn chỉnh:

Chạy từ thư mục `generated/`:
```bash
python authoring/build.py --bundle harbor_demo --seed 20260925 --output bundles/harbor_demo/input
```

Lệnh trên sẽ tự động:
1. Sao chép mã nguồn và cấu hình canonical vào `bundles/harbor_demo/input/`.
2. Sinh 240 requisitions, 120 POs, 140 receipts, 120 invoices và 24 private edge cases.
3. Tạo 3 file Excel thật qua openpyxl (gài sẵn C01: Manager threshold = 500.000 JPY trong file Excel).
4. Render 12 hóa đơn HTML và 12 phiếu nhận hàng HTML có watermark demo.
5. Áp dụng C02 vào `docs/finance_desk_guide.md`.
6. Tự động chạy pytest (15/15 test) và quét rò rỉ ID bí mật (0 leak).
7. Xuất bảng mã băm SHA-256 vào `operator/evidence_manifest.json`.

---

## 3. Chạy Ứng Dụng Reference Qua CLI

Chuyển vào thư mục bundle public:
```bash
cd bundles/harbor_demo/input
```

### 3.1 Đánh giá tuyến duyệt yêu cầu mua hàng (Requisition Routing)
```bash
python -m app.cli route --requests data/requisitions.jsonl --config config/approval.yaml --output run_output/routes.jsonl
```

### 3.2 Đối soát 3 bên hóa đơn nhà cung cấp (3-Way Invoice Matching)
```bash
python -m app.cli match --data-dir data --config config/approval.yaml --output run_output/invoice_decisions.jsonl
```

### 3.3 Tra cứu chi tiết một thực thể
```bash
python -m app.cli inspect --entity po --id PO-000001 --data-dir data
python -m app.cli inspect --entity invoice --id INV-000001 --data-dir data
```

---

## 4. Chạy Kiểm Thử Độc Lập & Xác Minh Bộ Đánh Giá (Oracle)

Chạy từ thư mục `generated/`:
```bash
# 1. Chạy unit test của ứng dụng public
python -m pytest bundles/harbor_demo/input/tests

# 2. Chạy anchor test của Oracle độc lập
python -m pytest evaluator_private/tests/test_oracle_anchors.py

# 3. Quét kiểm tra rò rỉ mã bí mật vào bundle public
python evaluator_private/check_leakage.py

# 4. Chạy toàn bộ validation harness (so sánh Actual vs Expected)
python evaluator_private/validate.py
```

Kết quả đối soát Actual vs Expected sẽ được lưu tại:
`reports/demo_validation.json`

---

## 5. Khắc Phục Sự Cố Phổ Biến
- **Lỗi `ModuleNotFoundError: No module named 'evaluator_private'`**:
  Đảm bảo bạn đứng tại thư mục `generated/` khi chạy các lệnh kiểm thử hoặc chạy với cờ `-m`.
- **Muốn làm mới dữ liệu về trạng thái ban đầu**:
  Chỉ cần chạy lại `python authoring/build.py --bundle harbor_demo --seed 20260925` để ghi đè sạch sẽ.

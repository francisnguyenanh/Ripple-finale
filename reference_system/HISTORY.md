# Change History — Acme Procurement

**Task:** P2-T06 · **Artifact family:** `history`

> This is the artifact that turns a finding from *"these two files differ"* into
> *"a decision made in 2020 was never reverted, and a spreadsheet edited in 2022 was
> never synchronised."* The difference in weight is the whole reason the `history`
> family exists.

---

## 1. Vì sao là một file, không phải git history thật

`reference_system/` sống **trong cùng repo** với sản phẩm. Một lịch sử git riêng cho
seed sẽ cần submodule hoặc repo lồng — thêm một thứ có thể hỏng trong lúc judge đang thử.

Thay vào đó: lịch sử được ghi ở đây dưới dạng **dữ liệu có cấu trúc**
(`history/changelog.jsonl`), đọc bởi Git adapter ở P3-T04 qua cùng một contract.

**Hệ quả phải ghi vào README §21 Limitations:** artifact family `history` của demo seed
là lịch sử **được khai báo**, không phải lịch sử git được khai quật. Adapter đọc nó bằng
cùng interface, nên khi trỏ vào một repo thật cơ chế không đổi — nhưng đây là điều cần
nói thẳng, không để judge tự phát hiện.

## 2. Dòng thời gian

| Ngày | Thay đổi | Điều đã KHÔNG xảy ra |
|---|---|---|
| **2019-03** | Chính sách phê duyệt ban đầu, ngưỡng JPY 1,000,000 | — |
| **2020-03** | Thêm đường emergency purchase trong lúc đứt gãy cung ứng | Ngưỡng được **chép** vào code, không nối về config |
| **2020-11** | Miễn phê duyệt cho khách hàng strategic — chiến dịch Q4 | Commit message ghi *"temporary, revert after campaign"* |
| **2021-01** | Chiến dịch kết thúc | **Không có commit revert.** Không ai ghi vào sổ ngoại lệ |
| **2021-03** | Governance nêu EXC-2020-011 ở kỳ review | Không có follow-up nào được ghi |
| **2022-06** | Hạ ngưỡng trong `approval_matrix.xlsx` xuống 500,000 | **Chỉ sửa Excel.** Không sửa workflow, không sửa SOP, không sửa policy |
| **2023-02** | Sửa bug timezone trong escalation | Ghi chú: *"không đối chiếu với workflow config"* |
| **2024-09** | Cập nhật test procurement | Ba test cũ **không** được sửa |

## 3. Ba sự kiện làm nên demo

### 2020-11 → 2021-01 · Một quyết định tạm thời không bao giờ được huỷ

Đây là **UN-001**. Nó không phải bug: ai đó đã quyết định đúng, cho một chiến dịch có
thật, và ghi rõ là tạm thời. Thứ thiếu là việc huỷ nó — và sáu năm sau không tài liệu
nào biết nó tồn tại.

Một sản phẩm nói *"tìm thấy một hành vi không có tài liệu"* thì nhạt. Nói được
*"một miễn trừ tạm thời từ chiến dịch Q4/2020 vẫn đang chạy, và đây là commit nói nó là
tạm thời"* thì khác hẳn.

### 2022-06 · Sửa một artifact, quên năm cái còn lại

Đây là **CONF-001**, và là lý do nó là CONFLICT chứ không phải DRIFT:

```text
Excel co ngay (2022-06-14).  Workflow KHONG co ngay nao.
Mot ben co dau thoi gian khong sap xep duoc hai ben.
```

Nói *"Excel mới hơn nên Excel đúng"* là suy diễn. Có thể Excel sửa sai và không ai duyệt —
sổ ngoại lệ trống đúng chỗ đó. Hệ thống trình bày cả hai bên và để con người quyết (I15).

### 2020-03 · Một hằng số được chép, không được nối

Đây là **UN-002 / IMPACT-003**. Lúc chép, con số **đúng**. Nó vẫn đúng suốt sáu năm.
Nó chỉ sai vào đúng khoảnh khắc chính sách đổi — và không ai đọc file đó sẽ thấy trước
điều ấy, vì không có gì để thấy.

Chỉ có **chạy** mới lộ ra.

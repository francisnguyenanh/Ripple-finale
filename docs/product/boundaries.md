# Product Boundaries

**Mục đích:** chặn scope creep **trước khi** nó bắt đầu.

> Nguyên tắc một dòng: **nhiều artifact type, MỘT operational domain.**

---

## 1. Trong phạm vi

```text
1 enterprise operation     Procurement & Orders
6 artifact family          software | workflow | docs | spreadsheet | tests | history
1 change family            business policy / rule change
```

### Sáu artifact family — danh sách đóng

Đây là danh sách mà mọi thay đổi về sau phải khai báo nó chạm vào family nào. Danh sách này được **enforce bằng schema**: `Intent.scope.artifact_families`
và `ActionProposal.artifact_family` đều là enum của đúng sáu giá trị.

| Family | Ví dụ trong demo seed | Adapter |
|---|---|---|
| `software` | `approval_service.py`, `emergency_purchase.py` | Python `ast` |
| `workflow` | `approval_routes.yaml` | Workflow |
| `docs` | `SOP-Procurement.md` | Markdown (đã có) |
| `spreadsheet` | `approval_matrix.xlsx` | XLSX (đã có) |
| `tests` | `test_approval_routing.py` | Python `ast` |
| `history` | git log của demo seed | Git |

**Thêm family thứ bảy là đổi contract**, không phải thêm tính năng: nó đụng hai schema,
hai enum, và mọi khai báo scope hiện có.

---

## 2. Ngoài phạm vi

```text
nhieu operational domain       -- 1 domain, khong phai N
ontology doanh nghiep tong quat
ingest repo tuy y               -- nguoi dung khong upload duoc repo cua ho (ADR-001)
autonomous deployment
IAM / approval platform         -- doc va doi chieu, khong thay he thong duyet
10+ ngon ngu lap trinh
```

---

## 3. Bốn điều sản phẩm KHÔNG hứa

Mỗi dòng dưới đây cũng phải xuất hiện trong phần Limitations của tài liệu giới thiệu:

```text
KHONG phai IAM / approval platform
KHONG phai code migration tool      -- code proposal la SHOULD
KHONG ingest repo tuy y
KHONG autonomous change             -- I13
```

---

## 4. Ranh giới kỹ thuật, không chỉ ranh giới sản phẩm

| Ranh giới | Enforce bằng |
|---|---|
| `change_intelligence/` không biết hạ tầng | `test_q5_change_intelligence_never_names_infrastructure` (7 chuỗi cấm) |
| `change_intelligence/` không gọi model | `test_q5_change_intelligence_imports_no_provider` |
| Enum trạng thái định nghĩa đúng một nơi | `test_q1_no_state_enum_is_defined_twice` |
| Intent không chạm lớp Fact | `test_q4_the_fact_writer_knows_nothing_about_intent` |
| Renderer không gọi model (I9) | `test_i9_renderers_never_reach_a_model` |
| Baseline bất biến (I18) | ADR-003 + `spike/tests/` |

Một ranh giới không được assert là một ranh giới **đã dịch chuyển rồi**.

---

## 5. Câu hỏi để quyết một đề xuất mới có trong phạm vi không

```text
1. No co nam trong Procurement & Orders khong?          khong -> NGOAI
2. No co cham mot trong 6 artifact family khong?        khong -> NGOAI
3. No co phai business policy / rule change khong?      khong -> NGOAI
4. No co lam he thong de hieu hon cho nguoi dung khong? khong -> hoan
5. No co nam trong danh sach muc da cat khong?          co   -> da cat, khong mo lai
```

Câu 5 là câu hay bị bỏ qua nhất khi lịch có vẻ dư dả: thời gian dư **không** phải lý do mở lại mục đã cắt — nó là lý do làm tốt hơn những mục đã nhận.

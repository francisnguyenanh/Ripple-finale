# Reference system seed — verification record

**Ngày:** 2026-09-19

---

## 1. Seed đã xây

```text
reference_system/          20 artifact, du CA SAU family
  application/    7  .py    approval_service, emergency_purchase, workflow_engine,
                            config, repository, notification, audit
  workflow/       1  .yaml  approval_routes.yaml
  docs/           6  .md    policy, sop, manager-guide, exception-handling,
                            audit-checklist, faq
  data/           3  .xlsx  approval_matrix, supplier_categories, customer_types
  tests/          1  .py    test_approval_rules.py  (18 test)
  history/        2         transactions.jsonl (220), changelog.jsonl (8)

baseline_id  b376ac0a4b62b2d7...   on dinh qua nhieu lan build
```

## 2. Bảy câu hỏi kiểm chứng (P2 §7)

| # | Câu hỏi | Kết quả |
|---|---|---|
| Q1 | Clean clone build được không? | ✅ `pytest reference_system/tests` → 18 passed |
| Q2 | Mọi representation trong manifest có tồn tại thật? | ✅ 103 test mở từng artifact; ô Excel đọc theo locator `Sheet!Cell` |
| Q3 | Reset về trạng thái gốc? | ✅ hash-compare; `baseline_id` không đổi sau 3 vòng tạo/reset |
| Q4 | Hai session độc lập? | ✅ + `SessionScopeError` khi đọc chéo session |
| Q5 | `core/` rò rỉ kiến thức về reference system? | ✅ **0** — 4 từ khoá, test canh |
| Q6 | Mỗi seed condition có lý do lịch sử? | ✅ `changelog.jsonl` 8 mục; 7/8 có `note` giải thích |
| Q7 | Hero rule đủ 9 biểu diễn? | ✅ test đếm chính xác 9 |

## 3. 23 seed condition — đếm bằng test

```text
undocumented     4     stale_docs      4     conflicts        3
outdated_tests   3     ambiguous       2     historical_exc   2
intent_changes   2     knowledge_only  2     wrong_inference  1
                                          tong = 23
```

`test_the_23_seed_conditions_add_up` canh tổng. Xoá lặng một điều kiện sẽ đỏ.

## 4. Ba con số, và một con số đã bị GỠ

### Giữ — vì tính được

```text
220  so dong that trong transactions.jsonl
 47  len(compare(...).changed) -- chay 220 ban ghi qua handler THAT, hai lan
```

`47` **không tồn tại như hằng số** ở `core/change_intelligence/` hay
`reference_system/application/`. Có test grep riêng cho điều đó.

Failure injection: đổi một bản ghi trong khoảng lệch → con số thành 46 → test đỏ.
Đã kiểm, không phải giả định.

### Gỡ — vì không có nguồn

> Kế hoạch dùng cặp **`12 → 47` approval/tháng**. Corpus thật cho **4.7 → 8.6**.
>
> `47` là **tổng số yêu cầu đổi hành vi trong CẢ NĂM**, không phải số approval mỗi tháng.
> Dùng nó làm số theo tháng là nhân sai 12 lần.
>
> Theo quy tắc ở [METRIC_DEFINITIONS §5](../evaluation/METRIC_DEFINITIONS.md), con số
> không có nguồn thì **bỏ khỏi UI, README, slide, video** — không giữ lại làm minh hoạ.
> Thay bằng chuỗi 12 tháng thật, hoặc `47/220` kèm nhãn "cả năm".

## 5. Một phát hiện làm demo mạnh hơn kế hoạch

Kế hoạch dự kiến **3** bản ghi trong khoảng lệch không đổi hành vi (ca emergency).
Corpus thật cho **10**, và chúng chia làm **ba nhóm khác hẳn nhau**:

| Nhóm | Số | Vì sao không đổi | Cần làm gì |
|---|---|---|---|
| Emergency fast-track | 3 | Hardcode, không đọc config | **Sửa code** |
| High-risk supplier | 4 | BR-005, **có tài liệu** | Không cần gì |
| Strategic customer | 3 | BR-002, **KHÔNG có tài liệu** | Ghi tài liệu |

**Đây chính là precision mà metric 4 đo.** Một người rà soát nói *"50 yêu cầu bị ảnh
hưởng"*. Hệ thống nói được *"47 đổi; 10 không đổi vì ba lý do khác nhau, và chỉ 3 trong
số đó là lỗi"*. Một hệ thống chỉ báo "10 ca không đổi" đã bỏ mất toàn bộ giá trị.

Đã ghi vào `history/README.md §3` và `seed_manifest.yaml: IMPACT-003`.

## 6. Ba test cũ — kiểm cả hai chiều

Không chỉ khai là "sẽ đỏ sau khi đổi rule" — đã **chạy thử dưới chính sách mới**:

```text
chinh sach hien tai (1,000,000):  18 passed
chinh sach moi   (  500,000):     15 passed, 3 failed
                                  dung 3 test TEST-OLD-001..003
```

Hai test khác ban đầu cũng đỏ vì chúng **cũng** hardcode ngưỡng. Đã sửa cho chúng
đọc từ config — giữ đúng 3 ca stale có chủ đích. Tương phản đó là nội dung của câu
*"test xanh không có nghĩa là đúng"*: test tốt sống sót qua thay đổi chính sách,
test cũ thì không.

## 7. Ràng buộc C-1 (ADR-002) — đã enforce bằng test

```text
test_the_reference_application_never_reads_its_own_location   __file__, os.getcwd
test_the_reference_application_never_reads_configuration_itself  os.environ, open(
```

Đây là điều kiện sống còn của P6-T04: nếu app đọc config từ global, mỗi kịch bản
what-if phải restart process và executable simulation sẽ chậm tới mức không demo được.

## 8. Gate G2

```text
[x] 1 enterprise operation (Procurement & Orders)
[x] 6 artifact family day du, moi family co artifact that
[x] hero rule o 9 bieu dien
[x] 3 cross-artifact conflict, moi cai mot KIEU khac nhau
        CONF-001 gia tri lech        (so khac so)
        CONF-002 dieu kien lech      (gia tri giong, dieu kien khac)
        CONF-003 co che lech         (gia tri giong, mot ben khong doc config)
[x] 23 seed condition, moi cai co ground truth
[x] ground-truth validator MO tung artifact -- 103 test
[x] reset = session moi tu baseline bat bien
[x] 2 session doc lap
[x] core/ khong biet gi ve reference_system/
[x] corpus 220 ban ghi, `47` duoc tinh khong duoc luu
[x] 3 outdated test xanh hom nay, do sau khi doi rule -- da chay thu
```

**G2 PASS.**

## 9. Nợ mang sang P3

| # | Nợ | Chủ |
|---|---|---|
| H1 | `history` family là lịch sử **khai báo** (`changelog.jsonl`), không phải git thật | Ghi README §21; adapter P3-T04 dùng cùng contract |
| D1 | `verify_not_modified()` chưa có production caller (I7) | P3-T05 |
| D2 | Hai stage cùng id `S8b` | P3-T05 |
| I19 | Prompt injection defence | P3-T10 |
| I4 | Evidence quote verification ở runtime | P3-T08 |

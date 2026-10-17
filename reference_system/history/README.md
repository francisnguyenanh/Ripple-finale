# Historical Transaction Corpus

**Task:** P2-T10 · **220 bản ghi** · `transactions.jsonl`
**Sinh bởi:** `generate_corpus.py` (seed cố định `20260919`) · **Đọc lại bởi:** `replay.py`

> **Quy tắc của file này:** corpus được **thiết kế**, con số được **tính**.
> Không con số nào trong demo được lưu ở đâu cả.

---

## 1. Phân bố

| Nhóm | Số bản ghi | Hành vi khi ngưỡng 1,000,000 → 500,000 |
|---|---|---|
| Ngoài khoảng lệch | 170 | Không thể đổi |
| Trong khoảng, đường chuẩn | 47 | `auto_approved` → `manager_approval` |
| Trong khoảng, đường emergency | 3 | **KHÔNG đổi** — hardcode |
| **Tổng** | **220** | |

Ngoài ra, 7 bản ghi khác rơi vào khoảng lệch qua nhánh `strategic` và `high risk` của
nhóm 170 — xem §3.

## 2. Hai con số của demo, và cách tính

```text
220  = so dong trong transactions.jsonl        -> test dem dong
 47  = len(compare(...).changed)                -> CHAY 220 ban ghi qua handler THAT, hai lan
```

```bash
python -c "
from reference_system.history.replay import load_corpus_file, compare
from reference_system.application.config import default_config
recs = load_corpus_file('reference_system/history/transactions.jsonl')
cur  = default_config()
print(compare(recs, cur, cur.with_threshold('BR-001', 500000)).changed_count)
"
# 47
```

`47` **không xuất hiện như hằng số** ở bất kỳ đâu trong code sản phẩm. Test kiểm điều đó.

Đổi một bản ghi trong corpus → con số đổi → test đỏ. Đó là failure injection bắt buộc
của P6-T05, và là lý do một judge có thể tin con số này.

## 3. Mười bản ghi trong khoảng lệch mà hành vi KHÔNG đổi

Đây là phần thú vị nhất của corpus, và nó **mạnh hơn** thiết kế ban đầu (vốn chỉ dự
kiến 3 ca emergency). Mười ca chia làm **ba nhóm khác hẳn nhau**:

| Nhóm | Bản ghi | Vì sao không đổi | Loại |
|---|---|---|---|
| Emergency fast-track | HIST-0019, 0036, 0050 | `emergency_purchase.py` hardcode `1_000_000`, không đọc config | **DEFECT — không ai biết** |
| High-risk supplier | HIST-0021, 0077, 0192, 0208 | BR-005 escalate bất kể số tiền | Ngoại lệ **có tài liệu** |
| Strategic customer | HIST-0168, 0204, 0220 | BR-002 miễn trừ từ chiến dịch Q4/2020 | Ngoại lệ **KHÔNG có tài liệu** |

**Vì sao ba nhóm này làm demo tốt hơn một nhóm.** Một người rà soát thay đổi sẽ nói
*"50 yêu cầu nằm trong khoảng bị ảnh hưởng"*. Hệ thống nói được nhiều hơn:

```text
47 doi hanh vi
10 khong doi, vi BA LY DO KHAC NHAU:
     4 co ngoai le hop le va da duoc ghi tai lieu   -> khong can lam gi
     3 co ngoai le hop le nhung KHONG AI GHI lai    -> can ghi tai lieu
     3 KHONG co ngoai le nao -- chi la mot so hardcode  -> can sua code
```

Phân biệt được ba nhóm này **chính là** precision mà metric 4 đo. Một hệ thống chỉ nói
"10 ca không đổi" đã bỏ mất toàn bộ giá trị.

## 4. Approvals mỗi tháng — số thật, và một con số bị GỠ

Chuỗi 12 tháng, tính từ corpus:

```text
truoc:  1,  4,  6,  5,  5,  2,  5,  7,  7,  8,  3,  3     trung binh  4.7
sau:    5,  8, 10,  9, 11,  4, 11, 10, 10, 14,  5,  6     trung binh  8.6
```

> **Cặp số `12 → 47` trong bản plan đã được GỠ.**
>
> Kế hoạch gốc dùng *"approval/tháng: 12 → 47"*. Corpus thật cho **4.7 → 8.6**. Hai con
> số đó không hoà giải được: `47` là **tổng số yêu cầu đổi hành vi trong cả năm**, không
> phải số approval mỗi tháng. Dùng nó như số theo tháng là nhân sai 12 lần.
>
> Theo đúng quy tắc ở [METRIC_DEFINITIONS §5](../../docs/evaluation/METRIC_DEFINITIONS.md),
> con số không có nguồn thì **bỏ khỏi UI, README, slide và video** — không giữ lại làm
> minh hoạ. Dùng `4.7 → 8.6 approval/tháng` (hoặc chuỗi đầy đủ ở trên), hoặc dùng
> `47/220` và nói rõ đó là **cả năm**.

## 5. Schema mỗi bản ghi

```text
historical_case_id   HIST-0001 .. HIST-0220, danh so theo THU TU THOI GIAN
timestamp            ISO-8601, rai deu tren 12 thang gan nhat
amount               JPY, so nguyen
supplier_id          SUP-001 .. SUP-024
customer_tier        standard | strategic
process              standard_po | emergency_purchase | blanket_order
category             opex | capex
supplier_risk        normal | high
requested_by         buyer.*
```

`process` nói **đường nào đã được dùng thật**. `replay.py` gọi đúng handler đó, không
suy đoán từ số tiền — suy đoán sẽ làm ba ca emergency biến mất, và cùng với chúng là
toàn bộ phát hiện.

## 6. Tính lặp lại

```bash
python reference_system/history/generate_corpus.py
git diff --stat reference_system/history/transactions.jsonl   # phai rong
```

Seed cố định. Sinh lại phải ra **byte y hệt**. Test kiểm điều này.

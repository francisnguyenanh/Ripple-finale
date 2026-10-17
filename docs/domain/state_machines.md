# State Machines

**Code:** `core/change_intelligence/states.py` — nguồn duy nhất
**Test:** `tests/contract/test_state_machines.py` — 108 test

---

## 1. Bốn máy

```text
Knowledge   PROPOSED -> VERIFIED -> SUPERSEDED
                    \-> SUPERSEDED

Intent      DRAFT -> VALIDATED -> APPROVED -> APPLIED -> SUPERSEDED
                 \-> REJECTED \-> REJECTED

ChangeSet   PROPOSED -> REVIEW -> APPROVED -> APPLYING -> APPLIED -> VERIFIED
                    \-> REJECTED (tu bat ky trang thai nao truoc APPLIED)
```

Hai tập trạng thái **phẳng**, không phải máy:

```text
Alignment   ALIGNED | DRIFT | CONFLICT | UNKNOWN
Decision    NO_CHANGE_REQUIRED | CHANGE_REQUIRED | HUMAN_REVIEW_REQUIRED
```

---

## 2. Cách test — tích Descartes, không phải danh sách

Transition bị cấm **không được liệt kê bằng tay**. Mọi cặp `(from, to)` trong tích
Descartes mà không có trong bảng đều phải raise.

> **Vì sao.** Một danh sách transition cấm chỉ đầy đủ bằng đúng trí tưởng tượng của
> người viết nó. Cạnh mà không ai nghĩ tới chính là cạnh mà một refactor — hoặc một
> người cố tình — sẽ đi qua.

`Knowledge` 3 state + `Intent` 6 + `ChangeSet` 7 → **94 cặp** được kiểm, mỗi cặp một test.

---

## 3. Sáu transition bị cấm — gọi tên riêng

Đã nằm trong tích Descartes ở §2. Gọi tên riêng vì mỗi cái là một lời hứa mà ai đó sẽ
muốn nới, và khi đỏ ta cần biết **lời hứa nào** vỡ.

| Transition | Invariant | Vì sao cấm |
|---|---|---|
| `PROPOSED -> APPLYING` | I13 | Bỏ qua REVIEW **và** APPROVED. Đây là bypass mà mutation test nhắm tới |
| `REVIEW -> APPLYING` | I13 | Bỏ qua APPROVED |
| `APPROVED -> APPLIED` | — | Mất APPLYING là mất trạng thái duy nhất nói "apply đang chạy". Crash giữa chừng sẽ để lại một ChangeSet tự nhận là APPROVED với một overlay ghi dở phía sau |
| `APPLIED -> (kết thúc)` | I14 | `APPLIED` **không phải** terminal. Đường ra duy nhất là `VERIFIED` |
| `Intent DRAFT -> APPROVED` | I10 | Bỏ qua VALIDATED |
| bất kỳ transition thiếu actor/timestamp | I12 | Một approval không ai ký không phải approval |

---

## 4. Hai loại từ chối — cố ý tách riêng

```python
TransitionError      canh nay khong ton tai trong thiet ke
MissingActorError    canh hop le, nhung khong ai ky
```

> **Vì sao không gộp.** Nếu cả hai cùng ném một loại lỗi, caller không phân biệt được
> *"việc này là không thể"* với *"bạn quên nói ai làm"*, và sẽ retry nhầm cái.

---

## 5. `REJECTED` của Intent — lưu, không xoá

Một intent không hợp lệ **vẫn được lưu**, với status `REJECTED`.

Lý do nó có mặt trong máy trạng thái chứ không phải là "không tạo record": **lý do bị
từ chối chính là phần người dùng cần**. Xoá record là xoá lý do.

---

## 6. Version scheme (đóng R12)

```text
snapshot_id     content-addressed. Doi khi reality doi
baseline_id     sha256(manifest chinh tac). Ghim qua bien moi truong (ADR-001 §4)
km_version      tang khi Knowledge record duoc promote lai
intent_version  INTENT-042 v1, v2 ... KHONG BAO GIO ghi de          (I10)
changeset_id    duy nhat, tro toi base_snapshot -> replay duoc
session_id      cua judge, tach biet baseline                        (I18)
proposal_version so o Approval — chan approval cua v1 uy quyen cho v2
```

### Nợ còn lại — trung thực về R12

`core/kernel/lineage.py` có trường version trong ref (`type:id@version`) nhưng **không
gì tăng nó**, và code tự khai điều đó. Contract chốt *scheme* ở trên; việc **implement
increment** cho lineage ref thuộc lớp lineage.

Nếu chưa implement → phải ghi là giới hạn, **không** giả vờ đã có.

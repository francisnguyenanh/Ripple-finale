# Change Semantics — 7 loại feedback

**Test:** `tests/contract/test_change_semantics.py`
**Yêu cầu:** ≥4 loại sinh ra **4 hành vi khác nhau** — định nghĩa ở đây để phần implement bám theo.

---

## 1. Bảy loại và đường đi downstream

| `semantic_type` | Ý nghĩa | Hành vi downstream | Chạm Fact? |
|---|---|---|---|
| `observation` | Ghi nhận | Lưu ledger. **Không trigger gì** | Không |
| `correction` | AI suy luận sai | Reject inference → human decision → re-evaluate | Không (I8) |
| `clarification` | Bổ sung ngữ cảnh cho Gap | Gap **có thể** đóng nếu đủ evidence | Không |
| `intent` | Muốn hệ thống đổi hành vi | → Intent → impact → shadow → proposal | Không |
| `exception` | Ngoại lệ có chủ đích | Finding chuyển `intentional` — **không phải** drift | Không |
| `approval` | Duyệt proposal | State transition (I12) | Không |
| `rejection` | Từ chối proposal | State transition + lý do | Không |

**Cột cuối không phải trang trí.** I8: feedback loop không bao giờ chạm lớp Fact. Bảy
loại, bảy đường đi, và không đường nào tới `ir_writer.py`. Test kiến trúc Q4 canh điều đó.

---

## 2. Ba loại dễ nhầm nhất

### `exception` vs `correction`

```text
correction  "AI hieu sai"        -> finding bi REJECT, he thong danh gia lai
exception   "AI hieu dung, nhung  -> finding duoc GIU, danh dau `intentional`
             cho nay CO Y khac"
```

Nhầm hai cái này là nhầm rất đắt: coi một `exception` là `correction` sẽ xoá mất một
sự thật vận hành thật (*"chi nhánh Osaka được duyệt nhanh, có lý do, từ 2020"*), và
lần chạy sau hệ thống sẽ lại báo đúng cái đã được giải thích. Đó là zombie finding
mà hệ thống phải chống.

### `clarification` vs `intent`

```text
clarification  bo sung thong tin ve cai DANG co   -> co the dong Gap
intent         yeu cau cai TUONG LAI phai khac    -> mo nhanh change
```

Một câu như *"ngưỡng thật ra là 500,000"* là `clarification` nếu đó là mô tả thực tại,
và là `intent` nếu đó là yêu cầu đổi. **Phân biệt được hai cái này là việc của model**
và là một trong những AI Essentialness case: không luật deterministic nào
tách được chúng, vì khác biệt nằm ở ý định của người nói chứ không ở câu chữ.

Khi mơ hồ → **abstain**, không đoán. Bộ ca kiểm thử có 5 ca mơ hồ bắt buộc abstain.

---

## 3. Bốn hành vi khác nhau

```text
observation   -> ledger.append()                       KHONG gi khac
intent        -> Intent(DRAFT) -> validate -> IntentAccepted event
exception     -> finding.status = intentional          KHONG sinh ChangeSet
approval      -> ChangeSet REVIEW -> APPROVED          transition co audit
```

Bốn đường này **khác nhau ở loại output**, không chỉ khác ở nhãn. Đó là điều cần
kiểm: một hệ thống phân loại được 7 loại rồi làm y hệt nhau cho cả bảy thì chưa hiểu gì.

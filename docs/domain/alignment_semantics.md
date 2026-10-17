# Alignment Semantics

**Code:** `core/change_intelligence/alignment.py` · **Test:** `tests/contract/test_alignment_semantics.py`

> Yêu cầu: *"implementable without semantic ambiguity"*. Đây là file dễ mơ hồ nhất,
> nên mỗi trạng thái có một **predicate viết ra được**, không phải mô tả bằng lời.

---

## 1. Bốn AlignmentState — predicate

```text
ALIGNED   moi bieu dien cua rule tra ve cung gia tri ngu nghia

DRIFT     cac bieu dien khac nhau
          VA moi ben deu mang dau thoi gian
          VA cac dau thoi gian PHAN BIET duoc
          -> xac dinh duoc ben nao moi hon

CONFLICT  cac bieu dien khac nhau
          VA KHONG thoa dieu kien thoi gian o tren
          -> resolution = pending_human, khong bao gio tu chon   (I15)

UNKNOWN   khong co bieu dien nao, HOAC co gap chua dong
          -> BAT BUOC kem Gap ID + ly do                          (I5)
```

### Một dấu thời gian thiếu là đủ để thành CONFLICT

Nửa cái thứ tự không phải thứ tự. Nếu không biết một bên được viết khi nào, câu
*"bên kia mới hơn"* là một phỏng đoán — và một phỏng đoán được trình bày là DRIFT sẽ
bảo người dùng đi sửa nhầm artifact.

Dấu thời gian **trùng nhau** cũng không sắp xếp được gì. Cả hai ca đều có test.

### Điều `classify()` KHÔNG làm

`evidence_is_fresh` và `unresolved` **không** ảnh hưởng tới `AlignmentState`. Reality
vẫn đang như nó đang là. Chúng ảnh hưởng tới *phải làm gì*, và đó là trục thứ hai.

Gộp chúng vào đây là cách `ALIGNED` thôi có nghĩa "các artifact đồng ý".

---

## 2. Ba ChangeDecision và mapping mặc định

```text
AlignmentState                ChangeDecision
    ALIGNED                       NO_CHANGE_REQUIRED
    DRIFT                         CHANGE_REQUIRED
    CONFLICT                      HUMAN_REVIEW_REQUIRED
    UNKNOWN                       HUMAN_REVIEW_REQUIRED
```

Mapping này là **deterministic**, nhưng hai trục vẫn phải tách — vì có ba ca đi chệch:

| Ca | Alignment | Decision | Vì sao |
|---|---|---|---|
| Evidence đã cũ | `ALIGNED` | **`CHANGE_REQUIRED`** | Mọi thứ đồng ý, nhưng evidence đọc từ một snapshot đã trôi. Cần grounding lại |
| Còn `unresolved` | `ALIGNED` | **`HUMAN_REVIEW_REQUIRED`** | `ALIGNED` không được đọc thành "xong". Hệ thống chặn đúng tổ hợp này |
| Drift vượt risk | `DRIFT` | **`HUMAN_REVIEW_REQUIRED`** | Biết phải làm gì, vẫn không được tự làm |

**Một enum gộp không có chỗ nào để đặt ba ca này.** Đó là toàn bộ lý lẽ.

---

## 3. Hai quy tắc cứng — enforce trong `__post_init__`

```text
decision == NO_CHANGE_REQUIRED  ->  len(actions) == 0
decision == CHANGE_REQUIRED     ->  len(actions) >= 1
```

Chiều thứ nhất là cơ chế chống `unnecessary-change rate`: nếu không cần đổi gì mà
vẫn đề xuất thay đổi, đó chính là con số metric đó đo.

Chiều thứ hai: nói với ai đó rằng *"cần thay đổi"* mà không nói **đổi cái gì** thì
không phải một finding.

`reconcile()` còn **tự xoá** action list khi nó quyết `NO_CHANGE_REQUIRED` — một caller
chuẩn bị sẵn action đầu cơ không được phép biến một no-op thành proposal chỉ vì đã
chuẩn bị.

---

## 4. Scenario H đọc thế nào

```text
alignment = ALIGNED
decision  = NO_CHANGE_REQUIRED
actions   = []
```

Hệ thống **được phép nói "không có gì phải làm"**. Một hệ thống luôn tìm ra việc thì
không phát hiện được gì cả.

---

## 5. I15 — sống ở đây bằng cách VẮNG MẶT

Không có nhánh nào trong `alignment.py` chọn bên thắng. Một `CONFLICT` trả về với
**mọi bên còn nguyên**, và việc giải quyết là của con người.

Test kiểm điều này bằng cách assert `AlignmentOutcome` **không có** `winner` hay
`resolved_value` — một thuộc tính vắng mặt là ràng buộc dễ vỡ nhất, nên nó được test.

---

## 6. Test kiến trúc

```text
len(AlignmentState) == 4
len(ChangeDecision) == 3
grep "AlignmentState.NO_CHANGE_REQUIRED" trong core/  -> 0
moi cap trong 4 state co fixture phan biet            -> C(4,2) = 6 cap
```

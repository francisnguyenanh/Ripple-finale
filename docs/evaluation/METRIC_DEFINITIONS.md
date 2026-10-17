# Metric Definitions

**Nguyên tắc:** *"no unverified metric"*.

> **Quy tắc:** mọi con số được công bố trong tài liệu hoặc UI phải có định nghĩa ở file này.
> Một con số không định nghĩa được TP là một con số vẫn tranh cãi được.

---

## 1. Công thức chung

```text
precision = TP / (TP + FP)
recall    = TP / (TP + FN)
F1        = 2 * precision * recall / (precision + recall)
```

Phần dễ là công thức. Phần khó — và phần thường bị bỏ trống — là **TP được
xác định thế nào**.

Ví dụ với impact:

```text
Ground truth:  {A, B, C, D}
He thong tra:  {A, B, D, F}

TP = A, B, D     FP = F     FN = C
precision = 3/4 = 0.75      recall = 3/4 = 0.75
```

---

## 2. Matching policy — mỗi metric một kiểu khớp

Đây là bảng quyết định. Mỗi metric có **đơn vị khớp** khác nhau, và dùng nhầm đơn vị
sẽ cho ra một con số trông hợp lý nhưng vô nghĩa.

| # | Metric | Đơn vị khớp | Quy tắc khớp |
|---|---|---|---|
| 1 | `evidence_grounding` | 1 claim | Evidence trỏ đúng artifact **và** location chứa nội dung claim. Mục tiêu **100%** |
| 2 | `hidden_rule_detection` | 1 `semantic_id` | Khớp theo `semantic_id`, **không** theo câu chữ |
| 3 | `drift_conflict_detection` | 1 cặp (`semantic_id`, tập biểu diễn) | Phát hiện đúng rule **và** đúng tập nơi lệch |
| 4 | `impact_precision` / `impact_recall` | 1 artifact | Khớp theo đường dẫn artifact. **DIRECT và INDIRECT đếm RIÊNG** |
| 5 | `intent_classification` | 1 feedback | Khớp `semantic_type` (7 loại) |
| 6 | `simulation_accuracy` | 1 scenario | Khớp outcome trên **cả** current **lẫn** shadow |
| 7 | `change_completeness` | 1 artifact `required` | Artifact được đánh dấu required có mặt trong `actions` |
| 8 | `unnecessary_change_rate` | 1 action | Action **không** có impact evidence tương ứng |
| 9 | `post_apply_drift_count` | 1 rule | Số rule còn lệch sau khi apply + verify |

---

## 3. Bốn quyết định khớp dễ gây tranh cãi — chốt ở đây

### 3.1 Vì sao khớp theo `semantic_id`, không theo câu chữ (metric 2, 3)

Ba câu sau nói **cùng một rule**:

```text
"Orders above half a million yen require sign-off"
"Purchases exceeding 5 lakh JPY need manager approval"
"Manager sign-off is required for high-value POs"
```

Khớp theo chuỗi sẽ cho **recall ≈ 0** và kết luận sai rằng hệ thống không hoạt động.
Khớp theo `semantic_id` đo đúng thứ cần đo — và đồng thời là lý do identity resolution
là rủi ro cao nhất của tầng phân tích: **ghép nhầm `semantic_id` làm hỏng cả chín metric
cùng lúc.**

### 3.2 Vì sao DIRECT và INDIRECT đếm riêng (metric 4)

Gộp chúng lại là cách precision chết. Một hàng xóm gián tiếp bốn bước sẽ đếm ngang với
artifact **đang thật sự phát biểu rule**, và mọi tập impact sẽ trương lên thành cả đồ thị.

```text
impact_precision_direct     tap DIRECT so voi ground truth DIRECT
impact_precision_indirect   tap INDIRECT so voi ground truth INDIRECT
```

Báo cáo hai số. **Không** báo cáo số gộp — một con số gộp cao có thể che một precision
DIRECT thấp, và DIRECT mới là thứ người dùng hành động theo.

### 3.3 `unnecessary_change_rate` — định nghĩa FP, không phải TP (metric 8)

Metric duy nhất trong bảng mà **con số thấp là tốt**:

```text
unnecessary_change_rate = (so action khong co impact evidence) / (tong so action)
```

Một action có `necessity: required` mà không có `ImpactEdge` nào biện minh **là** một
đơn vị của tử số. Đó chính là lý do trường `necessity` tồn tại trong `ActionProposal`.

> Nếu mọi action đều `required`, hệ thống đang bịa việc — và metric này là cách duy nhất
> nhìn thấy điều đó từ bên ngoài.

### 3.4 `simulation_accuracy` đòi ĐÚNG CẢ HAI phía (metric 6)

Một scenario chỉ tính TP nếu outcome khớp ground truth **ở cả current lẫn shadow**.

Chỉ khớp shadow là không đủ: một simulator luôn trả cùng một kết quả cho mọi config sẽ
đạt 50% một cách vô nghĩa. Đây chính là sabotage FI-2 của spike (ADR-002 §6), và lý do ràng buộc này
có mặt ở đây.

---

## 4. Negative evals — đặt cạnh positive trong cùng bảng

Có 9 loại negative eval. Chúng **không** được báo cáo ở bảng riêng: một hệ thống
chỉ khoe ca nó làm được là một hệ thống đang chọn bằng chứng.

Mỗi metric phải có ít nhất một ca mà **đáp án đúng là "không tìm thấy gì"**:

| Metric | Ca negative |
|---|---|
| 2, 3 | Một rule mà mọi artifact **đồng ý** → không được báo conflict |
| 4 | Một thay đổi **không** ảnh hưởng gì → impact set rỗng |
| 6 | Shadow config **giống hệt** current → không inconsistency mới |
| 8 | `NO_CHANGE_REQUIRED` → `len(actions) == 0` |

Ràng buộc cuối đã được enforce ở code, không chỉ ở eval: `AlignmentOutcome.__post_init__`.

---

## 5. Vì sao mỗi số phải có nguồn

Cặp số trong kịch bản demo — `47/220` và `12→47` — **phải** có nguồn thật ở
`reference_system/demo_seed/` (220 giao dịch lịch sử). Nếu nguồn đó không còn, cặp số
đó **không được hiển thị**, và không được giữ lại dưới dạng minh hoạ.

Mọi số vận hành mô phỏng phải mang nhãn `[SIMULATED]`.

Đây là ràng buộc rẻ nhất để giữ và đắt nhất để mất: một con số không truy được nguồn sẽ
phủ bóng lên **mọi** con số khác.

# Domain Glossary — 15 khái niệm

**Test canh giữ:** `tests/contract/test_glossary.py`

> Một từ = một nghĩa, trong code, docs và UI.
> "drift", "conflict", "impact" phải mang cùng một nghĩa ở mọi nơi; nếu code dùng khác
> nghĩa, hệ thống mất tính nhất quán.

---

## 1. Mười lăm khái niệm

| # | Term | Định nghĩa vận hành | Sống ở đâu |
|---|---|---|---|
| 1 | **Artifact** | Một tài liệu/file/bản ghi có thể quan sát được | `BaselineArtifactStore` |
| 2 | **Reality Fact** | Claim quan sát được **bằng máy** từ artifact. Không bao giờ do model viết (I1) | IR · `RealityFact.json` |
| 3 | **Knowledge Record** | Diễn giải **có evidence** về các fact | KM · `KnowledgeRecord.json` |
| 4 | **Intent** | Điều một actor nói **nên** đúng. First-class, có version, không bao giờ ghi đè (I10) | `Intent.json` |
| 5 | **Evidence** | `{artifact, location, quote, snapshot_id}`. Quote phải **thật sự tồn tại** tại location (I4) | `Evidence.json` |
| 6 | **Conflict** | Hai claim mâu thuẫn, **chưa ai thắng** (I15) | `AlignmentState.CONFLICT` |
| 7 | **Gap** | Knowledge chưa giải quyết, **bắt buộc có Gap ID + lý do** (I5) | `KnowledgeRecord.knowledge_type = gap` |
| 8 | **Snapshot** | Tham chiếu **bất biến** tới một trạng thái reality | `snapshot_id` |
| 9 | **Impact Edge** | Cạnh "A đổi thì B bị ảnh hưởng", **có lý do** | `ImpactEdge.json` |
| 10 | **Shadow Change** | Trạng thái ứng viên, **cô lập** trên `base_snapshot` | `ShadowChange.json` |
| 11 | **Behavioral Scenario** | Một ca kiểm chứng hành vi **có ground truth** | scenario corpus |
| 12 | **ChangeSet** | Đơn vị thay đổi phối hợp, **replayable** | `ChangeSet.json` |
| 13 | **Approval** | State transition **có audit record** (I12) | `Approval.json` |
| 14 | **Verification** | Kiểm chứng **sau khi** apply (I14) | `VerificationResult.json` |
| 15a | **Alignment State** | `ALIGNED / DRIFT / CONFLICT / UNKNOWN` — *mô tả reality* | `states.AlignmentState` |
| 15b | **Change Decision** | `NO_CHANGE_REQUIRED / CHANGE_REQUIRED / HUMAN_REVIEW_REQUIRED` — *hệ quả hành động* | `states.ChangeDecision` |

---

## 2. Phân biệt quan trọng nhất: DRIFT vs CONFLICT

Đây là cặp dễ nhầm nhất và là cặp xuất hiện thường xuyên trong UI và báo cáo.

```text
DRIFT     cac bieu dien khac nhau
          VA xac dinh duoc ben nao MOI HON  (co huong thoi gian)

CONFLICT  cac bieu dien khac nhau
          VA KHONG xac dinh duoc ben nao dung
```

**Hero scenario của sản phẩm là CONFLICT, không phải DRIFT:**

```text
approval_routes.yaml   noi  1,000,000
approval_matrix.xlsx   noi    500,000
SOP-Procurement.md     noi  1,000,000
-> khong artifact nao mang dau thoi gian dat no truoc/sau cac artifact kia
-> CONFLICT
```

**Vì sao gọi nhầm là nguy hiểm, không chỉ là thiếu chính xác.** Nói "drift" nghĩa là
*"có một bên đúng và một bên cũ"*. Nếu thật ra không xác định được, ta vừa bảo người dùng
đi sửa một artifact mà ta không biết nó có sai hay không. Sai từ ở đây không phải lỗi
thuật ngữ — nó là một khuyến nghị sai.

**Một dấu thời gian thiếu là đủ để thành CONFLICT.** Nửa cái thứ tự không phải thứ tự.
Test: `test_one_missing_recency_marker_is_enough_to_make_it_a_conflict`.

---

## 3. Từ đồng nghĩa BỊ CẤM

Test grep, fail nếu xuất hiện **trong code** (docstring và comment được miễn — xem §5):

| Từ cấm | Dùng thay bằng | Vì sao |
|---|---|---|
| `mismatch` | `drift` hoặc `conflict` | Không nói được có xác định được bên nào đúng hay không |
| `discrepancy` | `drift` hoặc `conflict` | Như trên |
| `inconsistency` | `drift` / `conflict` / (trong simulation) `Inconsistency` object | Trong ngữ cảnh **simulation** đây là thuật ngữ riêng, hợp lệ — xem ngoại lệ dưới |

**Ngoại lệ có chủ đích:** `Inconsistency` trong `spike/simulate.py` và trong lớp simulation là một khái
niệm **khác hẳn** — hai handler trả lời khác nhau cho cùng một order *khi chạy thật*. Nó
không phải hai biểu diễn của một rule trong tài liệu. Giữ tên riêng vì gộp hai thứ này
lại mới là nhầm lẫn thật.

---

## 4. Hai trục vuông góc — không bao giờ gộp

```text
AlignmentState (4)          ChangeDecision (3)
mo ta REALITY               mo ta HANH DONG
    ALIGNED                     NO_CHANGE_REQUIRED
    DRIFT                       CHANGE_REQUIRED
    CONFLICT                    HUMAN_REVIEW_REQUIRED
    UNKNOWN
```

`NO_CHANGE_REQUIRED` **không bao giờ** là một `AlignmentState`. Chi tiết và hai ca
ngoại lệ (ALIGNED + CHANGE_REQUIRED, DRIFT + HUMAN_REVIEW_REQUIRED):
[alignment_semantics.md](alignment_semantics.md).

---

## 5. Test nhất quán hoạt động thế nào

`tests/contract/test_glossary.py` kiểm **code**, không kiểm văn xuôi:

```text
- Bo qua docstring va comment
  -> mot quy tac ban ve tai lieu la quy tac khong ai viet duoc tai lieu duoi no
- Bo qua tests/ va spike/
  -> test duoc phep noi ve tu bi cam de chung minh no bi cam
- Kiem moi term trong bang §1 co it nhat mot cho dinh nghia trong code
```

Nếu một term ở §1 không tồn tại ở đâu trong code, đó không phải glossary — đó là
danh sách ước muốn.

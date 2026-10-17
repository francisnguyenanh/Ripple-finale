# Event Contracts — 9 domain / 6 loop-triggering

**Schema:** `core/schema/events/*.json` · **Test:** `tests/contract/test_events.py` (103 test)

---

## 1. Hai tầng, không mâu thuẫn

```text
9 DOMAIN EVENTS          = toan bo tu vung su kien cua he thong
      |
      +-- 6 LOOP-TRIGGERING = dau vao cua reconcile loop
      +-- 3 INTERNAL        = ghi ledger, KHONG kich hoat loop
```

## 2. Bảng phân loại

| Event | Loại | Payload | Vì sao |
|---|---|---|---|
| `ArtifactChanged` | **LOOP** | `snapshot_before, snapshot_after, artifacts[]` | Reality đổi → phải reconcile (reverse drift) |
| `HumanFeedbackAdded` | **LOOP** | `comment_id, semantic_type, target` | Có thể sinh Intent candidate |
| `IntentAccepted` | **LOOP** | `intent_id, version, actor` | Mở nhánh reconcile → impact |
| `ChangeApproved` | **LOOP** | `changeset_id, approval` | Mở nhánh apply |
| `ChangeApplied` | **LOOP** | `changeset_id, new_snapshot` | Snapshot mới → reconcile lại |
| `VerificationCompleted` | **LOOP** | `changeset_id, alignment, unresolved[]` | Đóng vòng, có thể mở gap mới |
| `ShadowChangeCreated` | INTERNAL | `shadow_id, base_snapshot, target, proposed_value` | Sản phẩm phụ của một nhánh **đang chạy** |
| `ImpactCalculated` | INTERNAL | `shadow_id, edge_count, unknown_count` | Sản phẩm phụ của cùng nhánh đó |
| `ChangeProposed` | INTERNAL | `changeset_id, base_snapshot` | Chưa có quyết định con người → reality chưa đổi |

### Vì sao ba event kia không được trigger loop

Chúng xảy ra **bên trong** một lượt xử lý đã được kích hoạt. Cho chúng trigger nghĩa là:

```text
IntentAccepted -> ShadowChangeCreated -> reconcile -> ShadowChangeCreated -> ...
```

Vòng lặp vô hạn, và nó sẽ không lộ ra ở test đơn lẻ — chỉ lộ ra khi demo trước judge.

## 3. `loop_triggering` là `const`, không phải boolean tự do

Đây là một chỗ contract suýt bị hở, và test bắt được:

```json
"loop_triggering": { "type": "boolean", "const": true }
```

> Ban đầu trường này chỉ là `{"type": "boolean"}`. Nghĩa là một `ArtifactChanged` tới với
> `loop_triggering: false` sẽ **hợp lệ**, và reconcile loop sẽ bỏ qua đúng cái reverse drift mà nó
> tồn tại để bắt. Chiều ngược lại còn tệ hơn: một `ShadowChangeCreated` với `true` sẽ
> quay vòng lặp mãi mãi.
>
> Phân loại thuộc về **loại event**, không phải về từng instance. Instance được phép
> *khai báo* nó, không được phép *chọn* nó.

Test `test_a_loop_triggering_event_may_not_declare_itself_internal` và cặp đối xứng của
nó là hai test đã bắt lỗi này khi schema còn lỏng.

## 4. Idempotency ngay từ contract, không thêm sau

Mọi event có ba trường, và chúng khác nhau:

```text
event_id    dinh danh LAN PHAT     -- hai lan gui la hai event_id
dedup_key   dinh danh SU KIEN      -- hai lan gui cung mot su that la mot dedup_key
session_id  tach biet judge        -- I18
```

Không có `dedup_key`, một lần gửi lại và một lần xảy ra thật là **không phân biệt được**,
và vòng lặp reconcile hoặc xử lý hai lần hoặc lặng lẽ bỏ sót.

Thêm muộn là quá trễ: lúc đó mọi producer đã viết xong.

## 5. `ArtifactChanged` — đổi tên có ý nghĩa

Tên cũ là `SourceChanged`. Đổi vì workflow config và spreadsheet đổi **cũng phải** kích
hoạt vòng lặp, không chỉ source code. Một sản phẩm nói "cross-artifact" mà event lại tên
là "source" thì chính cái tên đang thu hẹp phạm vi.

## 6. Ràng buộc với reconcile loop

`x-loop-triggering` trong schema là **nguồn duy nhất**. Reconcile loop subscribe đúng 6 event có
`x-loop-triggering: true`. Một test contract so khớp hai danh sách — thêm event thứ 10 mà
quên phân loại thì test đỏ ngay ở tầng contract, không phải ở tầng loop.

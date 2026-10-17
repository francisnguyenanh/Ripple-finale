# ADR-003 — SessionArtifactOverlay Design

**Ngày:** 2026-09-19
**Status:** ACCEPTED · **Tiền đề:** [ADR-001](ADR-001-storage-architecture.md)
**Đây là contract gốc của storage layer.** Code spike vứt đi được; file này thì không.

---

## 1. Mô hình

```text
BaselineArtifactStore      bat bien, content-addressed
        +
SessionArtifactOverlay     artifact da doi, cuc bo trong mot session
        =
SessionSnapshot            addressable, so sanh duoc
```

Một abstraction, ba bài toán — lý do nó thuộc nền tảng storage chứ không phải bản vá về sau:

| Bài toán | Phase | Overlay giải thế nào |
|---|---|---|
| Reverse drift | reconcile loop | `snapshot.diff_from()` → `ArtifactChanged` |
| Apply ChangeSet an toàn | apply gate | apply = ghi overlay; baseline không bao giờ mở ra để ghi |
| Session isolation | session layer | mỗi session một overlay trên cùng baseline |

## 2. Ba câu hỏi thiết kế — đã trả lời, không hoãn sang storage contract

### Q-A · Overlay lưu gì? → **TOÀN BỘ FILE MỚI**

Không lưu diff.

> **Lý do quyết định.** Ở quy mô demo (vài chục artifact) chi phí lưu trữ bằng không.
> Quan trọng hơn: overlay kiểu diff cần một thuật toán merge, và **thất bại của merge
> trông giống hệt drift** — đúng thứ hệ thống này tồn tại để phát hiện. Một nguồn nhiễu
> đặt ngay trong cơ chế đo là cái giá không đáng trả để tiết kiệm vài KB.

Structured diff vẫn được tính, nhưng **để HIỂN THỊ trong UI** — đó là việc khác với
làm source of truth.

### Q-B · Đọc thế nào? → **overlay trước, baseline sau**

```python
def read_at(self, snapshot, name):
    blob = snapshot.overlay_manifest.get(name)
    if blob is not None:
        return self._blobs[blob]
    return self.baseline.read(name)
```

Hai dòng. FI-3 xoá nhánh đầu và bước 7 đọc ra giá trị cũ → test đỏ. Đã kiểm chứng.

### Q-C · Version ra sao? → **mỗi lần ghi mint một `snapshot_id` mới**

```text
snapshot_id = sha256({session_id, baseline_id, overlay_manifest})
```

Snapshot cũ **vẫn đọc được thế giới cũ**. Đó là điều kiện để `diff_from(A1, A2)` có
nghĩa, và là lý do một `Snapshot` là `frozen`.

## 3. Interface — storage contract lấy nguyên hình dạng này

```python
class BaselineArtifactStore:          # READ-ONLY (ADR-001 contract #1)
    baseline_id: str
    def read(self, name: str) -> bytes: ...
    def names(self) -> list[str]: ...
    def manifest(self) -> dict[str, str]: ...
    def write(self, name, data) -> NoReturn:   # LUON RAISE BaselineImmutableError
        ...

class SessionArtifactOverlay:         # read-write (ADR-001 contract #2)
    baseline: BaselineArtifactStore
    session_id: str
    head: Snapshot
    def write(self, name: str, data: bytes) -> Snapshot: ...
    def clear(self) -> Snapshot: ...
    def read_at(self, snapshot: Snapshot, name: str) -> bytes: ...

class Snapshot:                       # frozen (ADR-001 contract #3)
    snapshot_id: str
    session_id: str
    baseline_id: str
    overlay_manifest: dict[str, str]
    def read(self, name: str) -> bytes: ...
    def diff_from(self, other: Snapshot) -> list[str]: ...
```

### Một quyết định nhỏ nhưng quan trọng: `write()` tồn tại trên baseline và luôn raise

Một baseline **không có** phương thức ghi chỉ là một lời hứa. Một baseline có phương thức
ghi mà luôn từ chối là một **điều kiểm được**: FI-1 gọi nó và phải bị chặn.

### `diff_from` so sánh NỘI DUNG, không so sánh sổ sách overlay

Hệ quả: ghi một artifact bằng đúng bytes cũ → **không sinh event**. Có test riêng
(`test_step8_a_write_that_restores_the_bytes_raises_no_event`). Nếu so theo manifest,
Reconcile loop sẽ nhận event giả mỗi lần apply một ChangeSet không đổi gì.

## 4. Ánh xạ sang production (khớp ADR-001, FROZEN)

| Trong spike | Trong production |
|---|---|
| `self._blobs: dict[str, bytes]` | Cloud Storage `gs://…/sessions/<sid>/<blob_hash>` |
| `self._snapshots: dict[str, dict]` | Cloud SQL bảng `snapshots` + `overlay_artifacts` |
| `BaselineArtifactStore.load(dir)` | đọc `gs://…/baselines/<baseline_id>/`, read-only |
| `overlay.clear()` | reset session — xoá hàng overlay của session |

Spike chứng minh **ngữ nghĩa**, không chứng minh backend. Cần chạy **cùng một
bộ contract test** trên cả local adapter lẫn cloud adapter — đó là chỗ ánh xạ này được kiểm.

## 5. Invariant mà thiết kế này giữ

```text
I18  BASELINE BAT BIEN. Moi thay doi song trong SESSION OVERLAY.
       -> baseline.baseline_id do truoc/sau MOI thao tac, 8/8 buoc khong doi
I17  Runtime KHONG ghi vao filesystem container
       -> overlay khong bao gio cham dia; write() chi vao blob store
I7   Snapshot bat bien
       -> Snapshot la frozen dataclass; snapshot cu van doc ra the gioi cu
```

## 6. Điều overlay KHÔNG giải

```text
- Khong giai duoc concurrency trong MOT session (hai tab cung ghi mot artifact).
  Spike khong kiem. MVP: mot session = mot nguoi. Ghi vao Limitations.
- Khong gioi han kich thuoc overlay. Mot session dai co the phinh.
  -> reset session la van de giam thieu, khong phai loi giai.
- Khong co garbage collection cho blob mo coi sau clear().
  -> Chap nhan o MVP; ghi vao Limitations.
```

# ADR-001 — Storage Architecture

**Ngày:** 2026-09-19 · **Status:** ACCEPTED

---

## 1. Vấn đề

Ràng buộc kiến trúc cấm SQLite và filesystem làm production state. Nhưng `core/` hiện dùng cả hai:

```bash
grep -rln "sqlite3" --include=*.py core/ | grep -v tests
```
```text
core/kernel/graph.py
core/kernel/index_store.py
core/pipeline_stages.py
core/agent/investigation.py
```

Cộng 20+ module `core/kernel/` ghi filesystem trực tiếp (`work/ir/`, `work/reports/`).

**Ba lựa chọn, hai bị loại:**

| Phương án | Chi phí | Phán quyết |
|---|---|---|
| Bọc 20+ module kernel sau storage interface | ≥ 8 ngày, đụng 96 file | **LOẠI** — quỹ thời gian không đủ, và mọi phần phía sau đều phải chờ |
| Bỏ `core/`, viết lại từ đầu | ≥ 15 ngày | **LOẠI** — vứt 56,044 LOC đã unit-test |
| **Tách hai tầng theo thời điểm chạy** | ~0 ngày refactor | **CHỌN** |

## 2. Quyết định — kiến trúc hai tầng

```text
TANG 1  BUILD-TIME  (offline, truoc khi deploy, KHONG bao gio chay trong request cua judge)
  core/ giu nguyen filesystem + sqlite
  demo_seed -> IR -> Graph -> Knowledge Model
  -> BASELINE SNAPSHOT (bat bien) -> Cloud Storage

TANG 2  RUN-TIME  (Cloud Run, stateless, phuc vu judge)
  change_intelligence/ + app/   <- code MOI, khong ke thua rang buoc cua core/
  doc:  baseline read-only tu Cloud Storage
  ghi:  Cloud SQL (sessions, overlay, intents, changesets, approvals, verifications)
        + Cloud Storage (artifact bytes cua overlay)
```

**Vì sao hợp lệ, không phải lách luật.** Mô tả của reference seed là
*"materialized into a reproducible baseline snapshot"*. Một snapshot bất biến, theo định nghĩa,
không phải production *state* — nó là **build artifact**, cùng hạng với container image.
Cấm SQLite làm production state không cấm dùng SQLite trong build pipeline, cũng như
cấm ghi filesystem trong request không cấm `docker build` ghi đĩa.

Ranh giới sắc: **không dòng code nào của `core/` được gọi trong một request HTTP của judge.**
Đây là điều kiện có thể kiểm bằng grep (xem bảng rủi ro §5).

## 3. Sáu storage contract — thuộc tầng nào, quyền gì

Định nghĩa đầy đủ nằm trong storage contract, lấy từ ADR-003. Ở đây chốt tầng và quyền:

| # | Contract | Tầng | Quyền | Nơi lưu |
|---|---|---|---|---|
| 1 | `BaselineArtifactStore` | 2 | **READ-ONLY** | Cloud Storage `gs://…/baselines/<baseline_id>/` |
| 2 | `SessionArtifactOverlay` | 2 | read-write | bytes → GCS `gs://…/sessions/<sid>/<hash>` · metadata → Cloud SQL |
| 3 | `SnapshotStore` | 2 | read-write | Cloud SQL (`snapshots`) — snapshot = baseline_id + overlay ref |
| 4 | `KnowledgeStore` | 2 | **READ-ONLY** | Cloud Storage (KM sinh ở tầng 1) |
| 5 | `SessionStateStore` | 2 | read-write | Cloud SQL (sessions, intents, changesets, approvals, verifications) |
| 6 | `EventStore` | 2 | append-only | Cloud SQL — append-only là cách I12/I14 kiểm được |

Hai contract READ-ONLY (#1, #4) là **giao diện duy nhất** giữa hai tầng.
Tầng 2 không biết tầng 1 tồn tại; nó chỉ thấy một baseline bất biến.

**Physical storage FROZEN:** artifact bytes → Cloud Storage. Metadata → Cloud SQL.
**Không binary trong Cloud SQL.** Đóng băng ở đây để các phần phía sau không phải bàn lại.

## 4. Baseline snapshot sinh và version hoá thế nào

```text
1. Tang 1 chay tren reference_system/demo_seed/ o may build (hoac CI)
2. Sinh:  ir/  graph.sqlite  km/  manifest.json
3. baseline_id = sha256 cua manifest da sap xep chinh tac
                 -> cung seed => cung id, moi lan
4. Day len gs://<bucket>/baselines/<baseline_id>/
5. Ghi mot hang vao Cloud SQL bang `baselines` (id, created_at, seed_commit, manifest_hash)
6. Runtime doc baseline_id tu bien moi truong; KHONG tu do tim baseline moi nhat
```

Bước 6 là thứ làm demo lặp lại được: judge chạy hôm nay và judge chạy tuần sau thấy
cùng một baseline, vì id được ghim, không được suy ra.

`core/kernel/freeze.py::build_freeze_manifest()` đã làm đúng việc của bước 2–3.
**Nhưng `verify_not_modified()` hiện không được gọi từ production** (đã đối chiếu
bằng call graph). Tầng 2 phải gọi nó khi nạp baseline —
đó là cách I7 được giữ.

## 5. Hệ quả

### Được

```text
+ 0 ngay refactor core/ -- 56,044 LOC unit-tested duoc giu nguyen gia tri
+ Runtime phuc vu judge khong ghi filesystem container  -> I17 giu duoc, do duoc
+ Baseline bat bien la tien de cua overlay (ADR-002/003) -> I18 giu duoc
+ Session isolation cua judge la he qua mien phi cua overlay
+ Reset = xoa overlay. Khong co duong nao lam hong baseline
```

### Mất

```text
- Khong ingest artifact moi trong runtime. Judge khong upload duoc file cua ho.
  -> Chap nhan. MVP boundary da noi "KHONG ingest repo tuy y".
- Doi seed => phai build lai baseline offline => deploy lai.
  -> Chap nhan. Seed dong bang tu demo freeze 13/10.
- Hai tang nghia la hai bo cau hinh, hai cho co the lech nhau.
  -> Giam thieu: baseline_id ghim tuong minh; kiem thu build tang 1 + chay tang 2 tu dau.
```

### Rủi ro còn lại

| Rủi ro | Giảm thiểu | Kiểm bằng |
|---|---|---|
| Ai đó import `core/` vào runtime vì tiện | grep trong CI: `change_intelligence/` và `app/` **không** được import `core.` | kiểm tra import tĩnh |
| Baseline build trên máy cá nhân, không lặp lại | build trong container, ghim Python version | build lại baseline trong container |
| Overlay phình to qua một session dài | giới hạn số artifact/session; reset 1 click | reset session |

## 6. Phát biểu về giới hạn (Limitations)

Nguyên văn, để tài liệu giới hạn dùng lại nguyên si:

> **Two-tier architecture.** Knowledge extraction runs offline as a build step and
> produces an immutable baseline snapshot, in the same sense a container image is built
> before it is served. The runtime that serves judges is stateless: it reads that baseline
> read-only and writes only to Cloud SQL and Cloud Storage. This means the deployed system
> **cannot ingest new artifacts at request time** — a judge cannot upload their own
> repository. The tradeoff was deliberate: it buys immutability of the baseline, genuine
> session isolation, and a reset that cannot corrupt anything, at the cost of runtime
> ingestion. Runtime ingestion is on the roadmap, not in this prototype.

## 7. Bốn câu hỏi chính

| Câu hỏi | Trả lời |
|---|---|
| 6 storage contract áp cho tầng nào? | Tất cả ở **tầng 2**. Tầng 1 không có contract — nó dùng `core/` nguyên bản |
| Contract nào read-only? | `BaselineArtifactStore`, `KnowledgeStore`. `EventStore` append-only. 3 cái còn lại read-write |
| Baseline sinh và version hoá ra sao? | §4 — `baseline_id = sha256(manifest chính tắc)`, ghim qua biến môi trường |
| Giới hạn được phát biểu ra sao? | §6, nguyên văn |

---

**Liên quan:** ADR-002 (executable simulation) và ADR-003 (overlay physical model) được
viết sau khi thin vertical slice chứng minh được ba giả định.
ADR-001 là tiền đề của cả hai: không có baseline bất biến thì không có overlay.

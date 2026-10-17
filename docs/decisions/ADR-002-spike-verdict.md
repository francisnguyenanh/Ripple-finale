# ADR-002 — Architecture Spike Verdict

**Ngày:** 2026-09-19 · **Quyết định:** **DÙNG ĐƯỢC, CÓ ĐIỀU KIỆN**

---

## 1. Phán quyết

> **Kiến trúc DÙNG ĐƯỢC.** Ba giả định rủi ro nhất đều đứng vững khi chạy thật.
> Có **một điều kiện** phải phản ánh vào storage contract (§5), và **một phát hiện sản phẩm**
> mà spike tìm ra ngoài phạm vi ban đầu (§4).

```bash
python -m spike.spike     # exit 0
python -m pytest spike/tests -q
# 21 passed in 0.21s
```

## 2. Ba giả định

| # | Giả định | Bước chứng minh | Kết quả |
|---|---|---|---|
| 1 | `SessionArtifactOverlay` chạy được | 6, 8 | **ĐÚNG** — apply và reverse drift dùng chung một cơ chế |
| 2 | Executable simulation lộ ra inconsistency | 4, 5 | **ĐÚNG** — và mạnh hơn dự kiến, xem §4 |
| 3 | Baseline bất biến + session cô lập | mọi bước + Q3 | **ĐÚNG** — `baseline_id` không đổi qua toàn bộ vòng lặp |

```text
baseline_id truoc: d7dae61fe8ff5075
baseline_id sau 8 buoc + 2 session: d7dae61fe8ff5075
```

## 3. Ba câu hỏi của spike

### Q1 · Overlay có thực sự giữ baseline bất biến không? → **CÓ**

Hash đo ở hai đầu mọi thao tác. Ngoài hash, bytes cũng được kiểm trực tiếp:
sau khi apply `500,000`, `baseline.read()` vẫn trả về `threshold: 1000000`.

### Q2 · Inconsistency có lộ ra MỘT CÁCH TỰ NHIÊN không? → **CÓ**

Đây là câu hỏi quan trọng nhất, và là chỗ dễ tự lừa mình nhất. Ràng buộc đã giữ:

```text
spike/simulate.py   khong nhac ten mot artifact nao
spike/tests/        test buoc 5 khong nhac emergency_purchase.py
```

Cơ chế: mọi handler `route_*(order, config)` được **phát hiện theo HÌNH DẠNG**, chạy trên
mọi order, dưới cả hai config. Inconsistency được **suy ra** từ việc hai handler trả lời
khác nhau cho cùng một order dưới cùng một config.

Bằng chứng cơ chế này có thể trả lời "không có gì":
`test_step5_a_shadow_that_changes_nothing_introduces_nothing` — shadow không đổi gì thì
không tìm ra gì. Một detector luôn tìm thấy thứ gì đó thì không phát hiện được gì cả.

### Q3 · Session đọc được chính nó mà không thấy session khác? → **CÓ**

Hai session ghi cùng `approval_matrix.csv`, mỗi bên đọc ra giá trị của mình.
`clear()` trả về đúng bytes của baseline.

## 4. Phát hiện ngoài phạm vi — spike tự tìm ra một defect thứ hai

Spike ban đầu khẳng định *"không có inconsistency dưới current config"*. **Khẳng định đó sai,
và simulator đã bác nó.**

```text
INCONSISTENCY  PO-1004 (6,000,000 JPY) under current:
    approval_service.py   -> FINANCE_APPROVAL
    emergency_purchase.py -> MANAGER_APPROVAL
```

Đường emergency **không có khái niệm route capex về Finance**. Nó đã mâu thuẫn với đường
chuẩn **từ hôm nay**, độc lập hoàn toàn với việc đổi ngưỡng.

Hai điều rút ra, cả hai đều đáng giá hơn bản thân con bug:

**Về kỹ thuật.** Đây là bằng chứng mạnh nhất rằng Q2 được trả lời thật. Một cơ chế chỉ
tìm thấy đúng thứ người viết đã cài vào thì không chứng minh gì. Cơ chế này tìm thấy một
thứ người viết **đã khẳng định là không tồn tại**.

**Về sản phẩm.** Đúng luận điểm bán hàng: *"change one thing, understand everything it
affects"* — và đôi khi thứ nó hiểu ra lại là một vấn đề chưa ai hỏi. Khẳng định trong
spike được sửa lại cho khớp thực tế, không làm ngược lại:

```text
pre-existing under current config : ['PO-1004']
introduced by the shadow change   : ['PO-1002']
```

Bài học ghi vào protocol: **khi output đúng còn assertion sai, sửa assertion.** Ngược lại
là cách một hệ thống được dạy nói dối.

## 5. Điều kiện của "DÙNG ĐƯỢC, CÓ ĐIỀU KIỆN"

Một ràng buộc phải phản ánh vào storage contract, không để phát hiện lại ở tầng simulation:

### C-1 · Simulation import module từ BYTES, không từ đĩa

```python
spec = importlib.util.spec_from_loader(name, loader=None)
exec(compile(source, f"<snapshot:{name}>", "exec"), module.__dict__)
```

Bắt buộc phải thế: nội dung snapshot có thể là blob overlay **chưa bao giờ nằm trên đĩa**.
Nhưng nó kéo theo ba hệ quả mà **storage contract phải ghi lại**, và **simulation phải tuân**:

| Hệ quả | Ràng buộc lên contract |
|---|---|
| Module nạp từ bytes không có `__file__` thật | Reference app **không được** dùng `__file__`, `pathlib.Path(__file__).parent`, hay đọc file kề bên |
| `sys.modules` có thể bị nhiễm giữa các lần chạy | Dùng `setdefault`, và **không** import chéo giữa các module của reference app |
| Config phải là tham số | **Dependency injection tường minh.** Không global, không đọc đĩa, không biến môi trường |

Điều kiện thứ ba là điều kiện sống còn của simulation: nó là thứ cho phép chạy **hai trạng thái
config song song trong cùng một process**. Nếu reference app đọc config từ global,
simulation sẽ phải restart process cho mỗi kịch bản what-if — và executable simulation sẽ
chậm tới mức không demo được.

> **Do đó: reference app phải nhận ràng buộc này như một yêu cầu, không phải khuyến nghị.**

## 6. Failure injection — cả ba đều có răng

Không chỉ chạy test FI; đã áp sabotage rồi **chạy lại chính assertion của bước thật**:

| FI | Sabotage | Assertion thật bị ảnh hưởng | Kết quả |
|---|---|---|---|
| FI-1 | Ghi thẳng vào baseline | `baseline.write()` | **RED** — `BaselineImmutableError` |
| FI-2 | Mọi handler luôn trả `AUTO_APPROVED` | bước 5 `before < after` | **RED** — `False` |
| FI-3 | Đọc bỏ qua overlay | bước 7 `threshold == 500_000` | **RED** — `False` |

Đây là khác biệt giữa *"test FI xanh"* và *"FI phá được test thật"*. Chỉ cái thứ hai
chứng minh spike có giá trị.

## 7. Những gì spike KHÔNG chứng minh

Ghi ra để không ai nhầm phạm vi:

```text
- Khong co LLM. Moi ket luan o day la deterministic.
  -> AI Essentialness van chua duoc chung minh o bat ky dau. Phan do duoc o cac bai danh gia AI rieng.
- Khong co Cloud. `_blobs` la dict, khong phai Cloud Storage.
  -> Anh xa o ADR-003 §4; kiem bang CUNG bo contract test tren backend that.
- Khong co Knowledge Model, khong co Enterprise Graph, khong co evidence validator.
- Khong co concurrency trong mot session.
- Chi 2 ho artifact (YAML, CSV) + 2 module Python. Reference system day du co 6 ho.
```

## 8. Số phận của code spike

`spike/` **không** merge vào `core/` hay `change_intelligence/`: nó là mã khám phá dùng một lần. Thứ còn lại là ADR-002 và ADR-003; storage contract có test riêng.

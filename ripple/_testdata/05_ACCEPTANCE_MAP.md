# Dataset → các phase của Ripple

Đây là bản đồ sử dụng dữ liệu, không là bằng chứng Ripple hiện đã hỗ trợ mọi format hoặc chức năng.

| Phần bộ mẫu | Dùng cho plan | Điều cần chứng minh |
|---|---|---|
| Source code + config + docs + 3 XLSX | AI3 | Ingest thật, typed locator, coverage, không execute source khi đọc |
| Tên/cách diễn đạt khác nhau, scope CAPEX vs mọi đơn | AI4 | AI tìm rule/quan hệ/conflict có ý nghĩa; không dùng private rule map |
| Emergency scope G01 và doc-only escalation G02 | AI4–AI5 | Hỏi đúng người, câu trả lời đổi KM không giả sửa source; unknown không auto-close |
| Yêu cầu đổi manager limit 500k rồi 750k | AI6 | Intent mapping, impact paths, simulation đúng current version |
| 24 witness requests ở600k + boundary/priority controls | AI6 | Measured before/after results; không dùng con số seed cũ; R12 freight không đổi |
| Hardcoded emergency branch/config-only S1 | AI6–AI7 | Phát hiện chỗ sửa config chưa đủ; AI cần patch có ngữ cảnh |
| Markdown/Python patches, YAML edits, XLSX manager cell | AI7–AI8 | Review, approve exact digest, apply bytes thật, verify và re-ingest |
| Matrix đã500k ởS0 | AI7 | Không đề xuất sửa chỗ đã đúng; tránh unnecessary changes |
| C02/G02 ngoài scope của manager threshold | AI8 | Scoped completion không che conflict/GAP còn tồn tại |
| S3 restore, S4 manual Excel edit | AI9 | Source revision tiến về trước, decisions retained, knowledge stale/rebuild |
| Dev/held-out variants, oracle ngoài input | AI10 | Ablation AI-off/AI-on, leakage control, semantic precision/recall |
| App/data package chạy offline và repeatable | AI11 | Reproducible release evidence; scale/pilot readiness vẫn cần dữ liệu/tải thật |

## Source thật so với metadata kiểm thử

Chỉ import `generated/bundles/<id>/input/`. Không import frozen spec, builder templates, oracle, expected.json, scenario table, hidden human answers, review reports, evaluation labels hoặc future target patches. Các tài liệu đó là công cụ của người tạo/kiểm thử dataset.

Raw business transaction facts có thể import. Route/approval/payment records là observations của baseline, không là policy authority. Hai form render từ cùng invoice không được tính thành hai bằng chứng độc lập cho một rule.

Nếu current Ripple chỉ đọc một số format, xuất core import package Python/YAML/Markdown/CSV/JSONL/XLSX và giữ HTML/PDF/DOCX trong presentation package riêng. Không chuyển file không đọc được thành câu summary do generator viết rồi nói AI đã đọc original.

## Checklist trước khi cho giám khảo dùng

- [ ] Bộ nguồn chạy độc lập, Python core không cần Vertex.
- [ ] Workbook là XLSX thật, mở được và không có macro; hóa đơn/phiếu đúng record và tổng tiền.
- [ ] Có system design, functional/program design, data dictionary, SOP, user và operations manual.
- [ ] S0 chỉ có C01/C02 và G01/G02 theo thiết kế; không có lỗi ngẫu nhiên bị nhầm thành showcase.
- [ ] Oracle/anchor được kiểm độc lập; health PASS khác planned business findings.
- [ ] Counts actual tính từ source/corpus đã materialize; tách observed outcomes khỏi desired policy.
- [ ] AI của Ripple thực sự được gọi để hiểu nguồn, không hiển thị hidden annotations như discoveries.
- [ ] Quyền HITL/approve/restore thuộc Ripple đã kiểm; reference app employee roles không dùng thay bằng chứng RBAC Ripple.
- [ ] Các bước S1/S2/restore/manual refresh có source/KM/evidence đúng phiên bản; không claim Git watcher có sẵn.
- [ ] Public claims ghi synthetic data và supported scope; không gọi một demo bundle là held-out benchmark.
- [ ] Đường dẫn import exclude private oracle, builder/scenario instructions và các target snapshots.

## Tái sử dụng factory cũ

`Ripple/ripple_testdata` được giữ nguyên. Có thể dùng ý tưởng layout/validation trong đó, nhưng chưa có kiểm chứng compatibility cho whole-system multi-rule package này. Bộ prompt không tự gọi adapter cloud cũ, không lấy project/model fallback trong code cũ để tiêu trial credit và không dùng oracle một threshold thay oracle precedence/invoice của Harbor.

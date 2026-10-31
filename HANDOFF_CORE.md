# Bàn giao core dịch giọng nói cho web, app và T5

**Trạng thái:** backend đủ để các thành viên bắt đầu tích hợp và thử nghiệm. Chưa có cam kết độ trễ/chất lượng cho mọi giọng nói hoặc môi trường mạng. Nhánh bàn giao: `codex/multi-agent-core` của repo `tdk1522005/intern-realtime-translation`.

## Mỗi người nhận gì?

| Thành viên | Cần đọc và dùng | Việc thực hiện |
|---|---|---|
| Thiện (web) | [`backend/INTEGRATION_GUIDE.md`](backend/INTEGRATION_GUIDE.md), `/docs`, WebSocket contract | Làm giao diện microphone, hiển thị hai ngôn ngữ, trạng thái Stop/reconnect và gọi backend. Không gọi Gemini từ trình duyệt. |
| Linh (app) | Cùng contract REST/WebSocket với web | Thu PCM16LE mono 16 kHz, gửi frame tối đa 100 ms; xử lý partial/final và kết nối lại. `localhost` trên điện thoại là điện thoại, cần địa chỉ backend mà điện thoại truy cập được. |
| Thắng (T5) | `GET /api/v1/presentations/{presentation_id}/summary-input` | Lấy transcript sau khi từng session và presentation đã `finished`, chọn `transcript_en` hoặc `transcript_vn` làm đầu vào T5, tự xây tóm tắt. Backend chỉ chuẩn bị dữ liệu. |
| Khánh (core API) | [`backend/AGENT_ARCHITECTURE.md`](backend/AGENT_ARCHITECTURE.md), [`PROJECT_STATE.md`](PROJECT_STATE.md) | Chịu trách nhiệm API, VAD, ghép transcript, lưu, retry/reconnect, metrics và sửa lỗi core. |

Mọi người có thể clone cùng repo/nhánh để xem contract; **chỉ backend chạy và giữ Gemini API key**. Không chép `.env`, `.venv/` hay `translation.db` sang web/app, không commit các file này. Repo private cần được cấp quyền truy cập cho thành viên trước khi clone.

## Chạy backend để tích hợp

Trong PowerShell tại thư mục `backend`:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Điền GEMINI_API_KEY thật trong .env trên máy chạy backend.
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Kiểm tra `http://127.0.0.1:8000/health` và `http://127.0.0.1:8000/docs`. Hướng dẫn cấu hình đầy đủ và ví dụ event nằm trong [`backend/INTEGRATION_GUIDE.md`](backend/INTEGRATION_GUIDE.md). Muốn web/app trên thiết bị khác truy cập, dùng địa chỉ IP/tên máy của backend và cấu hình host, firewall, CORS/HTTPS phù hợp môi trường tích hợp; không dùng `127.0.0.1` của máy client để trỏ sang máy backend.

## Luồng chung mà web/app cần nối

1. `POST /api/v1/presentations` → lấy `presentation_id`.
2. `POST /api/v1/presentations/{presentation_id}/sessions` → lấy `session_id`.
3. Mở `WS /api/v1/live-sessions/{session_id}`; gửi `{"type":"start","target_language_code":"vi","speech_output_enabled":false}`. `vi` là dịch sang tiếng Việt; `en` là dịch sang tiếng Anh. Muốn thử VAD kết hợp, thêm `"provider_activity_mode":"hybrid"`; mặc định là `manual`.
4. Gửi audio dạng **binary** PCM16LE mono 16 kHz, từng frame tối đa 3200 byte (100 ms), không có WAV header. `input_partial`/`output_partial` là bản xem trước: **thay** text đang hiển thị, không nối lặp. `segment_final` là bản đã lưu: **upsert theo `segment_id`** rồi sắp xếp theo `sequence_number`.
5. Gửi `{"type":"stop"}`; đợi `stream_stopped`; có thể gọi `GET /api/v1/sessions/{session_id}/transcript` để đồng bộ lại khi reload/reconnect. Stop không tự kết thúc session trong database.
6. Khi cuộc nói đã xong, gọi `POST /api/v1/sessions/{session_id}/finish`. Khi tất cả session của presentation đã kết thúc, gọi `POST /api/v1/presentations/{presentation_id}/finish`.

`status="completed"` là bản dịch đã chốt. `status="fragment"` giữ lời nói dở và có thể có bản dịch **tạm**; không hiển thị nó như bản dịch cuối. `status="failed"` là lỗi cần xử lý/retry; finish có thể trả `409`. Khi reconnect cùng session, chỉ gửi **audio mới**; xem `session_ready.context_restored` để biết ngữ cảnh Gemini có được khôi phục hay không. Đổi chiều dịch bằng kết nối mới.

## Dữ liệu Thắng dùng cho T5

Sau hai bước finish ở trên, gọi:

```text
GET /api/v1/presentations/{presentation_id}/summary-input
```

Response có `transcript_en`, `transcript_vn`, và `sessions[]` chứa các `segments[]` có thứ tự. Chọn **một** transcript ngôn ngữ cho một lần tóm tắt để tránh lặp nội dung hai lần. Với `fragment`, bản dịch tạm có thể nằm trong `segments[]` nhưng không được ghép vào chuỗi transcript tổng hợp ở ngôn ngữ đích. Backend không gọi T5 và không tạo kết quả tóm tắt.

## Bằng chứng và giới hạn tại thời điểm bàn giao

- 88/88 test backend offline đạt; có test WebSocket giả lập cho VAD, Stop, transcript, fragment, retry, reconnect và summary input. Một smoke test kết nối Gemini thật bằng âm thanh tổng hợp đã xác nhận chế độ `hybrid` được chấp nhận; đây không phải phép đo chất lượng nhận giọng nói.
- Một lượt microphone thật ở chế độ `hybrid` lưu đủ câu và bản dịch; đuôi câu tới trước Stop. Partial đầu vẫn có thể chậm. Vì vậy `manual` vẫn là mặc định, `hybrid` là tùy chọn đánh giá thêm.
- P50/p95 có endpoint nhưng số mẫu thật còn ít, chưa đủ làm cam kết hiệu năng. Gemini có thể trả transcript muộn hoặc sửa partial; Web/app phải coi partial là thay đổi được.
- Khóa chống hai WebSocket cùng session hiện chỉ trong một process backend. Trước khi chạy nhiều Uvicorn worker hoặc triển khai công khai, core cần khóa chia sẻ, xác thực và cấu hình HTTPS/WSS thích hợp.

Lệnh kiểm tra trước khi tích hợp: `cd backend; .\.venv\Scripts\python.exe -m unittest discover -s tests -v`.

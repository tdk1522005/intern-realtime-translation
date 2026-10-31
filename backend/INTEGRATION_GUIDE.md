# Hướng dẫn tích hợp backend core

Tài liệu này mô tả contract đang có trong source. REST được mô tả thêm tại `/docs`; WebSocket không xuất hiện trong OpenAPI nên contract live được ghi tại đây.

## 1. Chạy backend

Yêu cầu Python 3.11 trở lên. Mở PowerShell tại thư mục `backend`:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Mở `.env`, thay `GEMINI_API_KEY` bằng key của backend, rồi chạy:

```powershell
python -m uvicorn main:app --reload
```

- Swagger REST: `http://127.0.0.1:8000/docs`
- Health check: `http://127.0.0.1:8000/health`
- Trang microphone tham khảo: `http://127.0.0.1:8000/live`
- REST schema: `http://127.0.0.1:8000/openapi.json`

Không đưa `.env`, API key hoặc `translation.db` vào web/app bundle hay commit lên GitHub. `requirements.txt` khóa theo phiên bản đã dùng trong môi trường backend hiện tại.

## 2. Endpoint live dành cho web/app

Dùng WebSocket gắn với session:

```text
ws://<backend-host>/api/v1/live-sessions/{session_id}
```

Trang `/live` hiện dùng endpoint này. `main.py` còn endpoint cũ `/api/v1/live-translate`; tích hợp mới nên dùng `live-sessions/{session_id}` vì endpoint này gắn stream với session và lưu segment vào SQLite.

### Trình tự

1. Tạo presentation: `POST /api/v1/presentations` (không cần body).
2. Tạo session: `POST /api/v1/presentations/{presentation_id}/sessions` (không cần body).
3. Mở WebSocket với `session_id` vừa nhận.
4. Gửi một JSON start:

```json
{"type":"start","target_language_code":"vi"}
```

`target_language_code` là ngôn ngữ đích: `vi` nghĩa là dịch sang tiếng Việt; `en` nghĩa là dịch sang tiếng Anh. Backend xác định ngôn ngữ nguồn theo chiều ngược lại.

5. Gửi audio thành **binary WebSocket frames**: PCM signed 16-bit little-endian, mono, 16 kHz. Mỗi frame tối đa 3200 byte (100 ms). Không gửi WAV header.
6. Khi người dùng dừng, gửi JSON:

```json
{"type":"stop"}
```

7. Đợi event `stream_stopped` rồi để WebSocket đóng bình thường. Dùng REST để đồng bộ transcript khi cần.

Ví dụ phần nối protocol, không bao gồm code xin quyền microphone/thu âm:

```javascript
const socket = new WebSocket(`${wsBase}/api/v1/live-sessions/${sessionId}`);

socket.onopen = () => {
  socket.send(JSON.stringify({
    type: "start",
    target_language_code: "vi",
  }));
};

// pcmFrame là ArrayBuffer/Blob chứa PCM16LE mono 16 kHz, không có WAV header.
function sendPcmFrame(pcmFrame) {
  if (socket.readyState === WebSocket.OPEN) socket.send(pcmFrame);
}

function stopLiveTranslation() {
  if (socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "stop" }));
  }
}
```

## 3. WebSocket events

| Event | Cách client xử lý |
|---|---|
| `session_ready` | Backend sẵn sàng; có `session_id`, `source_language`, `target_language`, `audio_format` và thông số VAD. |
| `input_partial` | Transcript nguồn đang cập nhật. Thay nội dung preview bằng `text`; không nối snapshot vào cuối. |
| `output_partial` | Bản dịch đang cập nhật. Cũng thay preview bằng `text`. |
| `segment_final` | Segment đã lưu; có `segment_id`, `sequence_number`, `source_text`, `source_language`, `en`, `vn`, `status`, `created_at`. Đây là bản ghi cuối. |
| `translated_audio` | Chunk audio dịch dạng base64, có `mime_type`; chỉ phát nếu sản phẩm bật chức năng audio. |
| `speech_pause_detected`, `vad_diagnostic` | Thông tin chẩn đoán VAD; có thể bỏ qua trong giao diện sản phẩm. |
| `stop_draining` | Backend đang xả frame cuối và đợi transcript/bản dịch; chưa báo hoàn tất. |
| `stream_stopped` | Luồng đã dừng; có số segment lưu được và số liệu drain. Session trong database vẫn `active` để có thể nối lại hoặc kết thúc sau. |
| `error` | Có `message` mô tả lỗi. Nếu socket đóng, tải transcript REST lại để đồng bộ trạng thái. |

Partial là preview có thể thay đổi; chỉ `segment_final` là dữ liệu chuẩn. Nếu gặp lại cùng `segment_id`, cập nhật bản ghi đó thay vì thêm hàng mới. Sắp xếp theo `sequence_number`, không dựa vào thứ tự event đến UI.

## 4. REST endpoints cốt lõi

| Việc cần làm | Endpoint |
|---|---|
| Health check | `GET /health` |
| Dịch văn bản đơn lẻ (không phải audio live) | `POST /api/v1/translate` |
| Tạo presentation | `POST /api/v1/presentations` |
| Tạo session | `POST /api/v1/presentations/{presentation_id}/sessions` |
| Tạo/lưu segment văn bản | `POST /api/v1/sessions/{session_id}/segments` |
| Đọc segment | `GET /api/v1/sessions/{session_id}/segments` |
| Đọc trạng thái session | `GET /api/v1/sessions/{session_id}` |
| Lấy transcript có thứ tự | `GET /api/v1/sessions/{session_id}/transcript` |
| Retry segment lỗi | `POST /api/v1/sessions/{session_id}/segments/{segment_id}/retry` |
| Kết thúc session | `POST /api/v1/sessions/{session_id}/finish` |
| Kết thúc presentation | `POST /api/v1/presentations/{presentation_id}/finish` |
| Summary input theo session | `GET /api/v1/sessions/{session_id}/summary-input` |
| Summary input toàn presentation | `GET /api/v1/presentations/{presentation_id}/summary-input` |
| Metrics live | `GET /api/v1/metrics/live-latency` |

Tạo segment văn bản dùng body:

```json
{
  "segment_id": "UUID do client tạo cho lần gửi này",
  "sequence_number": 1,
  "source_text": "The meeting starts at nine.",
  "source_language": "en"
}
```

Cùng `segment_id` và cùng request sẽ trả lại segment cũ; dùng lại ID với nội dung khác trả `409 Conflict`. Cơ chế này dành cho endpoint tạo segment văn bản. Với live WebSocket, client cần upsert theo `segment_id` từ event `segment_final`.

## 5. Kết thúc phiên và dữ liệu cho T5

Kết thúc từng session trước, sau đó mới kết thúc presentation:

```text
POST /api/v1/sessions/{session_id}/finish
POST /api/v1/presentations/{presentation_id}/finish
GET  /api/v1/presentations/{presentation_id}/summary-input
```

Finish trả `409` nếu còn segment lỗi/đang chờ, thiếu sequence, session chưa kết thúc hoặc presentation chưa sẵn sàng. Summary-input chỉ xuất khi presentation đã `finished` và các segment có đủ bản dịch. Endpoint tổng hợp dữ liệu; việc gọi T5 và tạo summary thuộc thành viên T5.

Response presentation summary có dạng:

```json
{
  "presentation_id": "...",
  "presentation_status": "finished",
  "session_count": 1,
  "segment_count": 1,
  "transcript_en": "The meeting starts at nine.",
  "transcript_vn": "Cuộc họp bắt đầu lúc chín giờ.",
  "sessions": [
    {
      "session_id": "...",
      "session_number": 1,
      "segment_count": 1,
      "transcript_en": "The meeting starts at nine.",
      "transcript_vn": "Cuộc họp bắt đầu lúc chín giờ.",
      "segments": [
        {
          "segment_id": "...",
          "sequence_number": 1,
          "source_text": "The meeting starts at nine.",
          "source_language": "en",
          "en": "The meeting starts at nine.",
          "vn": "Cuộc họp bắt đầu lúc chín giờ."
        }
      ]
    }
  ]
}
```

T5 có thể dùng `transcript_en`/`transcript_vn` làm nội dung tổng hợp hoặc đi qua `sessions` để giữ cấu trúc từng session. Thứ tự session là `session_number`; thứ tự segment là `sequence_number`.

## 6. CORS và cấu hình

- `CORS_ORIGINS` là danh sách origin web, phân tách bằng dấu phẩy. Thay bằng origin chính xác của web khi tích hợp/triển khai; không dùng `*` khi có transcript.
- CORS middleware áp dụng REST HTTP. WebSocket có protocol riêng và không nằm trong OpenAPI.
- `GEMINI_API_KEY` chỉ nằm ở backend. Web/app gọi backend, không gọi Gemini trực tiếp bằng key dùng chung.
- `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL` dành cho dịch văn bản; `GEMINI_LIVE_MODEL` dành cho stream audio. Model live mặc định giữ nguyên model hiện tại nếu biến này không được đặt.
- Các biến `LIVE_*` trong `.env.example` tinh chỉnh VAD/drain; nếu bỏ trống, code dùng giá trị mặc định hiện có.

## 7. Kiểm tra backend trước khi bàn giao

Từ thư mục `backend`:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Các test này kiểm tra logic backend bằng fixture/mock; chúng không thay thế việc web/app chạy thử trên thiết bị thật hoặc việc chấm chất lượng dịch bằng audio của nhóm.

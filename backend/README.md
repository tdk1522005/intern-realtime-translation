# Backend core — bắt đầu từ đây

Nếu chuẩn bị tích hợp theo nhóm, đọc [trang bàn giao core](../HANDOFF_CORE.md) trước.

Bạn không cần mở hết file trong thư mục. Khi chạy server, FastAPI dùng `main.py` làm điểm khởi động; các file Python còn lại là những phần chức năng được `main.py` và router gọi.

## Chạy server

Mở PowerShell tại thư mục `backend`:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Mở Swagger tại `http://127.0.0.1:8000/docs`. Cách cài lại dependencies và cấu hình `.env` nằm trong [INTEGRATION_GUIDE.md](INTEGRATION_GUIDE.md).

## Những file code cần biết

| File | Vai trò |
|---|---|
| `main.py` | Điểm khởi động FastAPI; REST API quản lý presentation/session/segment, transcript, retry và dữ liệu bàn giao T5. |
| `live_session_router.py` | Nhận audio qua WebSocket, điều phối Gemini Live, ghép transcript, chốt segment và xử lý Stop. |
| `live_agents.py` | Agent điều phối phiên và các worker audio/VAD, Gemini, transcript, memory, metrics; giữ token reconnect và khóa phiên. |
| `gemini_live_service.py` | Kết nối model Gemini Live cho audio. |
| `gemini_service.py` | Dịch/hiệu chỉnh văn bản bằng Gemini. |
| `audio_vad.py` | Phát hiện tiếng nói và khoảng lặng từ âm thanh. |
| `transcript_assembler.py` | Ghép các mảnh transcript và xử lý bản tạm được model sửa. |
| `stop_drain.py` | Theo dõi việc xả audio và chờ kết quả cuối khi Stop. |
| `database.py` | Lưu presentation, session, segment và số liệu latency vào SQLite. |
| `latency_metrics.py` | Đo và tổng hợp p50/p95. |
| `live.html` | Trang demo microphone để thử backend; không phải giao diện sản phẩm cuối của nhóm web/app. |

## File phục vụ tích hợp và kiểm tra

- `INTEGRATION_GUIDE.md`: contract REST/WebSocket, thứ tự gọi API và payload bàn giao cho T5.
- `AGENT_ARCHITECTURE.md`: nhiệm vụ từng agent/worker, luồng reconnect, chống trùng và ranh giới tích hợp.
- `requirements.txt`: dependencies Python của backend.
- `.env.example`: mẫu cấu hình không chứa API key thật.
- `tests/`: test backend và fixture. Chạy bằng `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`.
- `__pycache__/`: cache Python tự tạo, được Git bỏ qua.

## File local — không gửi hoặc xóa khi dọn code

- `.env`: cấu hình riêng của máy, có API key Gemini.
- `.venv/`: môi trường Python đã cài package.
- `translation.db`: database có transcript và session đã lưu.

Nếu chỉ muốn xem luồng chạy, hãy bắt đầu bằng `main.py` → `live_session_router.py` → `gemini_live_service.py` → `database.py`. Không cần đọc mọi module cùng lúc.

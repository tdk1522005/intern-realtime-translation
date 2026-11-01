# INTERN — core dịch giọng nói Anh ↔ Việt

Repo này chứa **backend core** nhận audio, dịch trực tiếp bằng Gemini, lưu transcript và cung cấp dữ liệu cho web, app, T5. Web/app/T5 tích hợp qua REST và WebSocket; code giao diện và model T5 do các thành viên phụ trách.

## Bắt đầu từ đâu?

| Người đọc | Tài liệu chính | Việc cần nối |
|---|---|---|
| Thiện — web | [Bàn giao core](HANDOFF_CORE.md), [API contract](backend/INTEGRATION_GUIDE.md) | Microphone → WebSocket; hiển thị transcript/bản dịch; REST quản lý phiên. [Trang live mẫu](backend/live.html) chỉ để tham khảo. |
| Linh — app | [Bàn giao core](HANDOFF_CORE.md), [API contract](backend/INTEGRATION_GUIDE.md) | Thu PCM16LE mono 16 kHz và dùng cùng WebSocket/REST contract. |
| Thắng — T5 | [Bàn giao core](HANDOFF_CORE.md), [phần dữ liệu T5](backend/INTEGRATION_GUIDE.md#5-kết-thúc-phiên-và-dữ-liệu-cho-t5) | Đọc `GET /api/v1/presentations/{presentation_id}/summary-input` sau khi kết thúc presentation. |
| Khanh — core | [Mục lục backend](backend/README.md), [kiến trúc agent](backend/AGENT_ARCHITECTURE.md), [trạng thái project](PROJECT_STATE.md) | Bảo trì API, VAD, transcript, lưu trữ, retry/reconnect và đo độ trễ. |

## Chạy backend

Yêu cầu Python 3.11 trở lên. Từ thư mục `backend`, tạo `.venv`, cài `requirements.txt`, sao chép `.env.example` thành `.env`, rồi điền `GEMINI_API_KEY` trên **máy chạy backend**. Lệnh cài và cấu hình chi tiết nằm trong [API contract](backend/INTEGRATION_GUIDE.md#1-chạy-backend).

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Kiểm tra `http://127.0.0.1:8000/health` và REST Swagger tại `http://127.0.0.1:8000/docs`. Audio live dùng WebSocket, được mô tả trong `INTEGRATION_GUIDE.md`, không hiện trong Swagger.

## Trạng thái bàn giao

- Core đã qua 88 kiểm thử offline và đã có các lượt micro thật qua Gemini. Bản này đủ để nhóm bắt đầu tích hợp; [giới hạn còn lại](HANDOFF_CORE.md#bằng-chứng-và-giới-hạn-tại-thời-điểm-bàn-giao) được ghi rõ.
- `backend/.env`, `backend/translation.db`, `.venv/` và cache là dữ liệu **riêng trên máy**, được Git bỏ qua. Không đưa API key vào web/app hoặc commit lên repo.
- Mốc quay lại trước đợt dọn thư mục: tag `core-handoff-2026-11-01`. Các bản HTML cũ và script sửa encoding một lần đã có trong lịch sử Git.

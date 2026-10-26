# Project State — Dịch giọng nói Anh–Việt realtime

> Tài liệu bàn giao để tiếp tục công việc ở các cuộc trò chuyện sau. Đọc file này trước khi đề xuất hoặc sửa code. Cập nhật mục **Trạng thái hiện tại** và **Việc tiếp theo** sau mỗi mốc có bằng chứng.
>
> Cập nhật lần cuối: 2026-10-27 (Asia/Saigon)

## 1. Mục tiêu đồ án

Xây dựng web/app dịch lời nói tiếng Anh ↔ tiếng Việt trong lúc một người đang trình bày. Người dùng chọn ngôn ngữ đích; microphone thu tiếng nói; backend truyền audio liên tục đến Gemini; giao diện nhận transcript và bản dịch sớm nhất có thể. Các câu đã chốt được lưu theo thứ tự trong bộ nhớ bền vững. Khi kết thúc phiên, backend cung cấp transcript hoàn chỉnh cho thành viên phụ trách T5 tóm tắt.

Yêu cầu kỹ thuật cốt lõi của phần **TDKha** là xử lý luồng API và dữ liệu hai chiều, không dừng ở việc gọi một endpoint/model. Nội dung phải triển khai và giải thích được gồm streaming, quản lý trạng thái phiên, chốt câu, lưu transcript có thứ tự, phục hồi lỗi, đo độ trễ/chất lượng và tích hợp model qua adapter để có thể đổi model.

### Ranh giới thành viên

- **TDKha:** backend API orchestration, WebSocket/audio ingress, adapter/provider, session/segment state, lưu memory/transcript, retry/recovery, hợp đồng API và bằng chứng đo kỹ thuật.
- **Thiện:** giao diện web.
- **Linh:** giao diện app.
- **Thắng:** tóm tắt nội dung sau phiên bằng T5; backend của TDKha cung cấp dữ liệu đầu vào.
- **Ngọc:** test case trên nhiều môi trường/thiết bị; TDKha vẫn cần kiểm tra tích hợp kỹ thuật của API mình.
- Login/logout, giao diện và triển khai T5 không thuộc phần code chính của TDKha trong giai đoạn hiện tại.

## 2. Trạng thái đã xác nhận

### Đã có trong source hiện tại

- FastAPI app trong `backend/main.py`.
- REST dịch văn bản `/api/v1/translate`, có trả song ngữ `en` và `vn`.
- SQLite trong `backend/database.py`, có bảng presentation, session và segment; có khóa duy nhất để tránh trùng segment/sequence.
- REST lifecycle: tạo presentation/session, nhận và dịch segment văn bản, retry segment lỗi, liệt kê segment, lấy trạng thái/transcript, xuất dữ liệu cho phần summary, và kết thúc session.
- Có lưu source segment trước khi gọi Gemini văn bản; có trạng thái received/failed/completed và kiểm tra thứ tự khi kết thúc phiên.
- Có WebSocket `/api/v1/live-translate` và trang thử microphone `/live` trong `backend/live.html`.
- Luồng live hiện chuyển PCM16 mono 16 kHz từ browser; backend có queue giới hạn, gửi audio sang Gemini Live Translate, phát các event transcript đầu vào/đầu ra và các chunk audio dịch về browser.
- Trang `/live` đã được rà lại encoding: các chuỗi trạng thái tiếng Việt bị mojibake được sửa; route hiện trả `text/html; charset=utf-8`.
- Trang `/live` có checkbox **Phát bản dịch bằng giọng nói**, mặc định tắt. Transcript/bản dịch dạng chữ vẫn hiển thị; khi bật thì browser phát audio Gemini, khi tắt thì browser bỏ qua audio và dừng cả audio chunk đang phát/đã lên lịch.
- Lưu ý ranh giới model: backend vẫn cấu hình Gemini Live Translate với `response_modalities=["AUDIO"]`; model trả audio cùng output transcription. Checkbox hiện điều khiển phát âm thanh ở browser, không yêu cầu provider chuyển sang text modality. Tài liệu Google được đối chiếu ngày 2026-10-27 cho biết Live Translate dùng output modality AUDIO: https://ai.google.dev/gemini-api/docs/live-api/live-translate
- Phần text và live đã tách thành `backend/gemini_service.py` và `backend/gemini_live_service.py`, nhưng cấu hình model của hai luồng chưa đồng nhất.

### Đã được người dùng xác nhận chạy thủ công

- Server FastAPI khởi động được; `/health`, `/docs`, `/openapi.json` trả thành công.
- Dịch văn bản Anh → Việt và Việt → Anh từng trả HTTP 200 trong Swagger.
- Tạo presentation/session/segment và đọc danh sách/transcript đã được chạy qua Swagger với kết quả 201/200.
- Từng gặp lỗi provider 503; sau đó log xác nhận primary 503 và fallback thành công với HTTP 200.
- Người dùng đã báo lỗi nút dừng dịch được sửa. Cần kiểm tra lại tổ hợp dừng → chạy tiếp → đổi ngôn ngữ ở prototype hiện tại; chưa coi là đã được kiểm thử hồi quy.
- Ngày 2026-10-27: `py_compile` cho `main.py`, `live_session_router.py`, `gemini_live_service.py` thành công; GET thật `http://127.0.0.1:8000/live` trả 200 và UTF-8; browser hiển thị tiếng Việt đúng, checkbox ban đầu tắt, thao tác bật/tắt được, không có JavaScript console error. Đây là kiểm tra trang/UI; **chưa kiểm thử end-to-end microphone → Gemini → lưu segment** trong lượt này.

### Chưa được xác nhận / chưa hoàn tất

- Chưa có bằng chứng kiểm thử end-to-end từ microphone qua WebSocket đến bản dịch và lưu vào DB cho một session bền vững.
- Chưa xác nhận hành vi thực tế của tùy chọn audio trong một phiên Gemini live có microphone; đã xác nhận trạng thái checkbox và handler phía browser.
- Live WebSocket hiện **chưa nhận `session_id`**, chưa liên kết sự kiện audio/transcript với `session_id`, `segment_id`, `sequence_number` trong DB.
- Browser gửi frame 1.600 mẫu PCM16 (3.200 byte, xấp xỉ 100 ms ở 16 kHz mono). Backend hiện chỉ từ chối frame trên 16.000 byte (xấp xỉ 500 ms ở format này), dù thông báo lỗi ghi “100 ms or smaller”; cần thống nhất giới hạn và thông báo trong bước chuẩn hóa audio contract.
- Chưa có luồng partial → final segment chuẩn được persist. Chưa chứng minh được cách xử lý model sửa transcript tạm thời mà không lưu lặp hoặc mất câu.
- Chưa hoàn thiện chốt utterance bằng endpoint/VAD và chính sách khoảng lặng. Không được mặc định mỗi lần người nói lấy hơi là hết câu.
- Live prototype mới nhận `target_language_code`; chưa chốt quy tắc source-language tự nhận diện/chọn chiều, cũng chưa hỗ trợ đổi chiều ngay trong một live connection.
- Không có benchmark p50/p95 cho thời gian ra transcript/bản dịch, không có rubric đo chất lượng EN↔VI và chưa có bộ audio fixture cố định.
- Không có automated test suite hoặc file dependency manifest trong thư mục được rà soát; thao tác Swagger thủ công không thay thế test tự động.
- Lần gọi retry đã thấy trong ảnh trả 200 với một segment hoàn tất; điều đó chưa chứng minh retry một segment đang ở trạng thái failed hoạt động end-to-end.

## 3. Cấu hình/model đã rà soát

Chỉ ghi tên biến/model, **không ghi API key hoặc giá trị bí mật**.

- `backend/.env` hiện đặt `GEMINI_MODEL=gemini-3.5-flash-lite` và `GEMINI_FALLBACK_MODEL=gemini-3.6-flash`.
- Hai giá trị trên được `backend/gemini_service.py` dùng cho endpoint dịch **văn bản** qua `generate_content`.
- `backend/gemini_live_service.py` đang hard-code `gemini-3.5-live-translate-preview`; model live không đọc cấu hình model từ `.env`.
- Theo tài liệu Google đã đối chiếu ngày 2026-10-26, Flash-Lite và 3.6 Flash là model tổng quát có đầu ra văn bản và không hỗ trợ Live API. Vì vậy cấu hình hiện tại có thể phục vụ dịch văn bản nhưng không nên được coi là model chính cho audio realtime.
- `gemini-3.5-live-translate-preview` được thiết kế riêng cho dịch speech-to-speech liên tục, độ trễ thấp; hiện là Preview và cấu hình dịch tập trung vào target language, không hỗ trợ system instruction/tools như model Live tổng quát.
- `gemini-3.8-live` hiện được liệt kê là model Live ổn định, độ trễ thấp cho thoại/audio tổng quát; là ứng viên để benchmark dịch EN↔VI với instructions.
- `gemini-3.8-flash` là ứng viên cho dịch/hiệu chỉnh văn bản hoặc công việc sau phiên, không thay thế một luồng Live audio.
- **Quyết định model cuối cùng chưa chốt.** Khuyến nghị cần kiểm chứng trên cùng tập audio: so sánh Live Translate chuyên dụng với 3.8 Live; ghi rõ trade-off Preview, độ trễ, chất lượng, hỗ trợ điều khiển và cách reconnect. Không giả định chuyển model dự phòng giữa live stream là liền mạch.
- Google docs cần đối chiếu lại trước mốc bàn giao vì catalog và vòng đời model có thể thay đổi:
  - https://ai.google.dev/gemini-api/docs/models?hl=vi
  - https://ai.google.dev/gemini-api/docs/live-api/live-translate?hl=vi
  - https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live?hl=vi
  - https://ai.google.dev/gemini-api/docs/deprecations?hl=vi

## 4. Những gì tuyệt đối không được nhầm

- REST `/api/v1/translate` là **dịch văn bản**, không chứng minh được dịch tiếng nói realtime.
- Trang `/live` và WebSocket là prototype live audio; hiện chưa có persistence gắn với REST session.
- `session_id` là định danh một phiên lưu trữ; `segment_id` là định danh bền vững của một câu/utterance đã chốt; `sequence_number` giữ thứ tự câu trong session. Client có thể tạo UUID cho segment, nhưng cần tạo đúng một ID cho một segment chuẩn và tái sử dụng khi gửi lại cùng yêu cầu.
- Partial là nội dung tạm thời để hiển thị sớm; chỉ final mới trở thành segment chuẩn trong transcript, trừ khi thiết kế sau này yêu cầu lưu snapshot riêng.
- `ready=true` trên transcript chỉ nói các segment đã lưu đều hoàn tất và sequence liên tục; không chứng minh rằng microphone đã được thu đầy đủ.
- Model fallback cho text không tự động là fallback dùng được cho Live API. Hai luồng khác protocol, capabilities, và có thể cần tạo lại provider session.
- T5 tóm tắt sau phiên là phần của thành viên khác; ranh giới của TDKha kết thúc ở transcript/summary input ổn định.

## 5. Pipeline đích cho phần API core

1. **Start:** client gửi `session_id`, target language, định dạng audio, protocol version. Backend xác thực session đang active và cấu hình hợp lệ, mở provider stream, rồi trả `session_ready`.
2. **Audio ingress:** client gửi frame PCM16LE mono 16 kHz có kích thước/thứ tự xác định. API kiểm tra format, giới hạn tốc độ và dùng queue hữu hạn để kiểm soát backpressure.
3. **Live translation:** adapter chuyển đổi event Gemini sang event nội bộ thống nhất. Gửi transcript/bản dịch provisional sớm; không để browser gọi Gemini trực tiếp hoặc nhận API key.
4. **Utterance assembly:** giữ partial theo một ID; xử lý transcript được model sửa; chốt final theo tín hiệu endpoint/final phù hợp. Không cắt câu chỉ dựa vào một hơi thở/ngắt ngắn.
5. **Persist:** lưu source, translation, ngôn ngữ, session, ID, sequence và timestamp final một cách idempotent. Một final event gửi lại không tạo thêm segment.
6. **Stop/recover:** ngừng nhận audio mới, flush frame còn đệm, gửi end-of-audio, đợi final output trong deadline; hỗ trợ cancel/disconnect, reconnect có trạng thái rõ ràng và không để UI kẹt.
7. **Finish/export:** chỉ kết thúc session sau khi xử lý hết pending/failed/missing sequence theo chính sách đã thống nhất; trả transcript hai ngôn ngữ có thứ tự và payload cho T5.
8. **Đo và báo cáo:** đo riêng latency audio→transcript partial, audio→translation partial, utterance→final, p50/p95; đánh giá độ đầy đủ, lỗi dịch, duplicate/lost segment, queue/backpressure và khả năng dừng/chạy lại.

## 6. Lộ trình thực hiện

### Giai đoạn A — Chốt quyết định kỹ thuật

- Chốt cách chọn source/target cho EN↔VI: người dùng chọn chiều hay model nhận diện; quy định xử lý khi đầu vào đã ở ngôn ngữ đích.
- Chốt bộ audio thử nghiệm và tiêu chí đo. Tối thiểu gồm giọng Anh, giọng Việt, câu dài, tên riêng/thuật ngữ, ngắt hơi giữa câu, khoảng lặng dài, đổi ngôn ngữ sau khi dừng, mạng/provider lỗi.
- Benchmark model live chuyên dụng và model Live ổn định trên cùng fixtures trước khi quyết định model chính/dự phòng.
- Chốt API event contract: start, ready, partial, final, error, stopped/session_complete và metadata của audio frame.

### Giai đoạn B — Nối live stream với session/memory

- Cho WebSocket nhận và kiểm tra `session_id` active.
- Thiết kế event mapper/adaptor để provider-specific event không rò ra contract frontend.
- Tạo segment/sequence cho utterance final; chỉ persist final; đảm bảo transaction, uniqueness/idempotency và thứ tự.
- Liên kết transcript endpoint, finish và summary-input với dữ liệu được sinh từ live stream.

### Giai đoạn C — Lifecycle, lỗi và hiệu năng

- Kiểm tra state machine start/stream/stop/finalize/finished/error/cancelled.
- Cố định ngưỡng frame, queue, timeout, idle timeout, giới hạn session và chính sách buffer/backpressure.
- Thử provider 503/429/timeout, socket đóng sớm, browser reload và retry; xác định rõ trường hợp resume, reconnect hay đánh dấu failed.
- Đo latency p50/p95 và các lỗi completeness/duplication trên cùng fixtures; dùng số liệu để sửa ngưỡng.

### Giai đoạn D — Bàn giao cho tích hợp nhóm

- Viết API contract và ví dụ payload/error ổn định cho web/app.
- Cung cấp transcript tiếng Anh/Việt có thứ tự và summary input đã kiểm tra cho Thắng.
- Lưu bằng chứng demo, benchmark, retry/recovery và giới hạn đã biết để đưa vào báo cáo.

## 7. Bằng chứng cần có để trình bày với hội đồng

- Sơ đồ pipeline audio → WebSocket → bounded queue → provider adapter → event mapper → segment assembler → SQLite/transcript.
- State machine và demo dừng, chạy tiếp, đổi chiều dịch, kết thúc phiên.
- Giải thích provisional/final, end-of-utterance, idempotency, sequence ordering, timeout/backpressure và provider recovery bằng ví dụ.
- Bộ audio test cố định cùng bảng p50/p95 latency và rubric chất lượng dịch EN↔VI.
- Log/response chứng minh lỗi không làm mất source hoặc làm nhân đôi segment; ghi rõ trường hợp Preview/API không khả dụng.
- Hợp đồng event API, schema DB, ví dụ transcript hoàn chỉnh và ví dụ dữ liệu handoff T5.

## 8. Checklist hiện có

- Workbook chính: `Checklist_Core_API_Dich_giong_noi_Realtime.xlsx`.
- Workbook kế hoạch ban đầu của backend: `Checklist_va_ke_hoach_backend_phien_dich.xlsx`.
- Checklist realtime có các sheet: `Tổng quan`, `Pipeline API`, `Checklist API`, `API Contract`, `Rủi ro`, `Nguồn`; nội dung giới hạn trong phần API core của TDKha.
- Số trạng thái trong workbook được chốt lần tạo ngày 2026-10-24: 48 hạng mục, 2 đạt, 5 đang làm, 41 chưa bắt đầu. Đây là snapshot thủ công, **không được xem là trạng thái tự đồng bộ với source**; cần cập nhật khi có bằng chứng mới.

## 9. Cách tiếp tục ở cuộc trò chuyện sau

1. Đọc `PROJECT_STATE.md` trước; đọc checklist realtime và source code liên quan nếu cần xác nhận chi tiết.
2. Xem tin nhắn mới nhất của người dùng là chỉ đạo tiếp tục/điều chỉnh lộ trình.
3. Chưa sửa code hoặc tạo thêm file cho đến khi người dùng yêu cầu. Khi được yêu cầu code, cung cấp trọn file/code và hướng dẫn chạy theo từng bước; không bắt người dùng tự tìm và thay những đoạn rời rạc.
4. Không chạy test tự động nếu chưa được yêu cầu; khi người dùng muốn xác minh, báo rõ từng lệnh chạy và kết quả.
5. Sau mỗi mốc, cập nhật file này bằng trạng thái và bằng chứng mới; không ghi “đã xong” chỉ dựa trên code tồn tại hoặc ví dụ Swagger chưa chạy đúng trường hợp.
6. Bước ngắn hạn: người dùng chạy lại `/live` với một session đang active, kiểm tra dịch chữ khi checkbox tắt, rồi bật checkbox để xác nhận phát tiếng; kiểm tra dừng → chạy tiếp → đổi ngôn ngữ.
7. Bước core API tiếp theo: **chốt event contract và benchmark/quyết định model live**, rồi nối WebSocket hiện tại với `session_id` và persistence. Phần này tạo nền cho core streaming, chốt câu và memory.


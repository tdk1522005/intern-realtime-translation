# Project State — Dịch giọng nói Anh–Việt realtime

> Tài liệu bàn giao để tiếp tục công việc ở các cuộc trò chuyện sau. Đọc file này trước khi đề xuất hoặc sửa code. Cập nhật mục **Trạng thái hiện tại** và **Việc tiếp theo** sau mỗi mốc có bằng chứng.
>
> Cập nhật lần cuối: 2026-10-31 (Asia/Saigon)

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
- Có `backend/README.md` làm mục lục file/lệnh chạy; các bản `live.html` cũ được giữ trong `backend/archive/`, không thay đổi module runtime để sắp xếp thư mục.
- REST dịch văn bản `/api/v1/translate`, có trả song ngữ `en` và `vn`.
- SQLite trong `backend/database.py`, có bảng presentation, session và segment; có khóa duy nhất để tránh trùng segment/sequence.
- REST lifecycle: tạo presentation/session, nhận và dịch segment văn bản, retry segment lỗi, liệt kê segment, lấy trạng thái/transcript, xuất dữ liệu summary theo session hoặc toàn presentation, kết thúc session và presentation.
- Có lưu source segment trước khi gọi Gemini văn bản; có trạng thái received/failed/completed và kiểm tra thứ tự khi kết thúc phiên.
- Có endpoint kết thúc presentation sau khi mọi session đã kết thúc; endpoint summary cấp presentation trả transcript Anh–Việt theo thứ tự session rồi segment cho thành viên T5, không chạy T5.
- Có WebSocket `/api/v1/live-sessions/{session_id}` và trang thử microphone `/live` trong `backend/live.html`.
- Luồng live hiện chuyển PCM16 mono 16 kHz từ browser; backend có queue giới hạn, gửi audio sang Gemini Live Translate, phát các event transcript đầu vào/đầu ra và các chunk audio dịch về browser.
- Khi Stop, backend xếp audio còn đệm, `activity_end`, rồi sentinel vào cuối queue theo FIFO; không xóa frame đang chờ để nhét sentinel. Queue drain có deadline; sau khi gửi audio xong, backend chờ transcript yên tối thiểu 1.5 giây hoặc tới deadline 20 giây. Nếu source đã hoàn chỉnh mà bản dịch live thiếu dấu câu hoặc có ít mốc kết câu hơn source, thử dịch lại toàn source một lần qua text Gemini; nếu không nhận được bản dịch hoàn chỉnh, giữ segment ở `failed` thay vì báo `completed`.
- Trang live tải transcript đã lưu từ REST trước khi mở microphone lần tiếp theo, nên dừng rồi chạy lại/đổi hướng không xóa lịch sử đang hiển thị. Đây là khôi phục lịch sử của ứng dụng; chưa nạp transcript text vào Gemini Live.
- Có thu thập và lưu mẫu latency `audio_to_source_ms`, `audio_to_translation_ms`, `stop_to_final_persisted_ms`; `GET /api/v1/metrics/live-latency` tổng hợp sample count, p50, p95, max. Kết quả chỉ có ý nghĩa benchmark sau khi thu mẫu microphone thật.
- WebSocket live hiện dùng `PauseDetector` RMS cục bộ: ngưỡng mặc định 450, tối thiểu 200 ms tiếng nói, ngắt 600 ms im lặng, pre-roll trước khi xác nhận tiếng nói; Gemini automatic activity detection tắt và backend gửi `activity_start` / `activity_end` thủ công.
- Do Live Translate có thể không phát `turn_complete`, `generation_complete`, cờ transcript `finished` hoặc offset (người dùng đo được tất cả bằng 0), backend dùng khoảng nghỉ VAD làm ranh giới ứng viên chứ không coi mỗi khoảng nghỉ là hết câu. Chỉ lưu tự động khi source và translation đều có dấu câu kết câu, translation không có ít mốc kết câu hơn source, transcript yên `LIVE_SEGMENT_SETTLE_MS` (mặc định 300 ms), đồng thời không có tiếng nói mới. Các khoảng nghỉ nằm giữa mệnh đề/câu, translation còn thiếu mốc kết câu, hoặc output còn đến sẽ được gộp/giữ lại.
- Trang `/live` hiển thị số khoảng nghỉ, số segment do VAD chốt, số khoảng nghỉ gộp, cửa sổ ổn định transcript, số mảnh transcript, provider end markers và timing offsets.
- Trang `/live` đã được rà lại encoding: các chuỗi trạng thái tiếng Việt bị mojibake được sửa; route hiện trả `text/html; charset=utf-8`.
- Trang `/live` có checkbox **Phát bản dịch bằng giọng nói**, mặc định tắt. Transcript/bản dịch dạng chữ vẫn hiển thị; khi bật thì browser phát audio Gemini, khi tắt thì browser bỏ qua audio và dừng cả audio chunk đang phát/đã lên lịch.
- Lưu ý ranh giới model: backend vẫn cấu hình Gemini Live Translate với `response_modalities=["AUDIO"]`; model trả audio cùng output transcription. Checkbox hiện điều khiển phát âm thanh ở browser, không yêu cầu provider chuyển sang text modality. Tài liệu Google được đối chiếu ngày 2026-10-27 cho biết Live Translate dùng output modality AUDIO: https://ai.google.dev/gemini-api/docs/live-api/live-translate
- Phần text và live tách trong `backend/gemini_service.py` và `backend/gemini_live_service.py`; model được cấu hình riêng theo loại tác vụ. Live model đọc `GEMINI_LIVE_MODEL`, mặc định vẫn là `gemini-3.5-live-translate-preview` để giữ hành vi hiện tại.
- Đã thêm handoff pack trong `backend/`: `INTEGRATION_GUIDE.md` mô tả REST/WebSocket/T5 payload; `requirements.txt` khóa dependency của môi trường hiện tại; `.env.example` liệt kê biến cấu hình không chứa secret. REST CORS có allowlist qua `CORS_ORIGINS`; để trống biến này giữ same-origin như trước.

### Đã được người dùng xác nhận chạy thủ công

- Server FastAPI khởi động được; `/health`, `/docs`, `/openapi.json` trả thành công.
- Dịch văn bản Anh → Việt và Việt → Anh từng trả HTTP 200 trong Swagger.
- Tạo presentation/session/segment và đọc danh sách/transcript đã được chạy qua Swagger với kết quả 201/200.
- Từng gặp lỗi provider 503; sau đó log xác nhận primary 503 và fallback thành công với HTTP 200.
- Người dùng đã xác nhận lỗi nút dừng được sửa. Kiểm tra thủ công mới hơn đã dừng và kết nối lại trong cùng session; transcript có 2 segment completed và cả session/presentation đều kết thúc thành công. Ảnh transcript bị cuộn nên vẫn cần xác nhận segment 2 là tiếng Việt nguồn và có bản dịch tiếng Anh.
- Ngày 2026-10-27: `py_compile` cho `main.py`, `live_session_router.py`, `gemini_live_service.py` thành công; GET thật `http://127.0.0.1:8000/live` trả 200 và UTF-8; browser hiển thị tiếng Việt đúng, checkbox ban đầu tắt, thao tác bật/tắt được, không có JavaScript console error. Đây là kiểm tra trang/UI; **chưa kiểm thử end-to-end microphone → Gemini → lưu segment** trong lượt này.
- Người dùng chạy live audio: VAD phát hiện 9 khoảng nghỉ, nhận 46 mảnh source/45 mảnh translation, nhưng Gemini không gửi end markers hoặc timing offsets (`0/0`), transcript bị ép lưu thành 1 segment lúc Stop. Bằng chứng này cho thấy dựa vào provider turn-completion để chia segment không phù hợp với luồng Live Translate liên tục.
- Sau khi thêm điều kiện dấu câu kết câu, người dùng chạy lại mic: phát hiện 13 khoảng nghỉ, VAD tự chốt 2 segment, gộp 6 khoảng nghỉ, cuối phiên lưu thêm 1 segment; tổng 3 segment `completed`, sequence liên tục, 0 missing. Ba ranh giới trong mẫu đều ở cuối câu/cụm hội thoại trọn vẹn, không còn các đoạn cụt như `Are you` / `below the`. Segment 1 và 3 chứa nhiều câu, nên hiện là các nhóm lời nói hoàn chỉnh chứ chưa phải mỗi câu một record. Đây là bằng chứng E2E mic → Gemini → lưu SQLite trong một lần chạy thật, chưa đủ để xác nhận độ ổn định qua nhiều giọng/độ dài.
- Người dùng thử đọc đoạn có tự sửa và lặp từ: transcript cuối giữ `9:00. Sorry, at 10:00` và `We need we need`, nhận 9 mảnh source + 9 mảnh translation; `missing_sequence_numbers=[]`, 1 segment `completed`. VAD chỉ phát hiện 2 khoảng nghỉ và không chốt tự động; segment được force-save khi Stop. Đây là bằng chứng assembly cuối không làm mất phần sửa/lặp trong mẫu này. Bản dịch Việt lược bỏ lần lặp “we need” theo cách dịch tự nhiên; source tiếng Anh vẫn giữ nguyên.
- Sau khi bổ sung reconciliation cho snapshot transcript tích lũy và flush preview khi dừng, người dùng đã xác nhận lỗi câu `Good morning, everyone. The meeting starts at 9:00 sorry, at 10:00.` được sửa: source cuối hiện đầy đủ một lần, không còn thiếu `at 10:00` hoặc lặp prefix. Trong lần chạy xác nhận, hai counter `source_snapshot_replacements` và `stale_source_snapshots_ignored` đều bằng 0; do đó output cuối đúng đã được chứng minh, nhưng chưa có bằng chứng rằng chính hai nhánh counter đó đã được kích hoạt.
- Cùng lần chạy trên phát hiện lỗi riêng: bản dịch Việt kết thúc ở `...xin lỗi, lúc` nhưng segment vẫn `completed`. `_save_live_segment` chỉ đánh dấu `completed` khi bản dịch không rỗng và kết thúc bằng dấu câu. Sau đó bổ sung bounded text-repair từ source hoàn chỉnh; nếu bản dịch vẫn cụt, source và bản dịch một phần được giữ lại với status `failed` để retry.
- Ngày 2026-10-28, người dùng kiểm tra lại và phát hiện bản dịch đã có dấu chấm nhưng mất phần sửa cuối: source có 3 mốc câu (`Good morning everyone. The meeting starts at 9:00, sorry. at 10:00.`), trong khi tiếng Việt mới có 2; VAD đã lưu segment trước khi Stop nên bước repair không chạy. Đã bổ sung kiểm tra số mốc kết câu để ngăn chốt bản dịch stream thiếu câu; lúc Stop sẽ dịch lại toàn source. Bản dịch văn bản đầy đủ có thể gộp câu do khác biệt ngôn ngữ; nếu repair trả văn bản mới có dấu câu thì chấp nhận, còn nếu không repair được thì giữ `failed`.
- Kiểm thử microphone thật sau sửa, session `66c02596-54be-4424-955d-e9ea3bcd70f7`: status thông báo `VAD chốt 0 segment`, `hoàn chỉnh lại 1 bản dịch`, đã lưu 1 segment. Source có `at 10:00` đầy đủ một lần; VN cũng có đoạn `lúc 10:00`; segment `completed`, `missing_sequence_numbers=[]`. Đây là xác nhận E2E cho regression thiếu phần cuối. Cách đặt dấu câu `xin lỗi. lúc 10:00` còn hơi gượng vì source transcription cũng tách câu sau `sorry.`; đây là chất lượng diễn đạt/dấu câu, không còn mất phần nội dung.
- Cùng lượt test thật ghi `stop_to_final_persisted_ms` sample count 1, p50/p95 2828 ms. Audio-to-source và audio-to-translation mỗi loại có 2 mẫu; p95 lần lượt 2906 ms và 3062 ms. Tập mẫu còn quá nhỏ để kết luận benchmark ổn định.
- Người dùng gọi kết thúc presentation và lấy summary-input trước khi kết thúc session; backend trả HTTP 409 với thông báo yêu cầu đúng thứ tự. Đây là kiểm tra lifecycle dự kiến, không phải lỗi endpoint.
- Người dùng thực hiện lại đúng thứ tự với session `66c02596-54be-4424-955d-e9ea3bcd70f7`: `POST /sessions/{id}/finish` trả 200, status `finished`, 1/1 segment completed; sau đó `POST /presentations/2a541ca0-7453-4aab-a6f5-6706d6af7a3d/finish` trả 200, status `finished`, 1/1 session; cuối cùng `GET /presentations/{id}/summary-input` trả 200 với một session, một segment và transcript EN/VN đầy đủ. Lifecycle finish → aggregate → export đã được kiểm tra thật qua Swagger.
- Người dùng tiếp tục kiểm tra đổi chiều/kết nối lại với session `880fdaa7-07db-4b80-9a78-4e5f52c13b30`: transcript trả 200, có 2 segment `completed`, 0 failed/pending, không thiếu sequence; sau đó finish session và finish presentation đều trả 200. Ảnh transcript chỉ cho thấy đầy đủ segment 1 (`source_language: en`); chưa xác nhận được segment 2 (`source_language: vi`, `source_text` tiếng Việt, `en` là bản dịch tiếng Anh).
- Ngày 2026-10-31: kiểm tra trực tiếp trang `/live?session_id=73a8ee52-f222-41fd-9acc-d71fb8b167b7`, GET transcript và GET trạng thái session. Transcript có 6/6 segment `completed`, 0 failed/pending, `missing_sequence_numbers=[]`; sequence 1–3 là nguồn `en` có bản dịch `vn`, sequence 4–6 là nguồn `vi` có bản dịch `en`. Đây xác nhận cả hai chiều đã đi qua và được lưu đúng field/order trong cùng session. Session vẫn `active` (`ended_at: null`); trang live khi mở lại đang `Chưa kết nối`, vì vậy lượt này chưa chứng minh việc finish/export hay đang có stream kết nối. Có vài lỗi chất lượng dịch cần chấm riêng: segment 2 thêm cụm “Không sao đâu” không tương ứng rõ với source; segment 3 “mượn ít ít” và segment 4 “students dear” chưa tự nhiên.
- Ngày 2026-10-31: kiểm tra tiếp session `5ed6790e-e33c-44f5-97a1-13c7d7dd6b04` trên giao diện `/live`: target English, trạng thái `Đã dừng`, source `Chào mọi người, cuộc họp bắt đầu lúc 10 giờ.` và translation `Hello everyone, the meeting starts at 10 o'clock.`. Transcript trả `ready=true`, 1/1 completed, 0 failed/pending, không thiếu sequence; `source_language: vi`, `en` là bản dịch. GET session xác nhận `finished`. Summary-input trả `presentation_status: finished`, 1 session/1 segment và transcript EN/VN chính xác. Presentation mới có ID `847eb782-43cc-44bd-b4e5-99491c1de49f`, khác presentation trước (`ec9dab27-b0ca-493d-a02d-81ff8e514526`), nên lượt này xác nhận tạo một presentation/session mới và hoàn tất/export; chưa phải kiểm tra session số 2 nằm trong presentation cũ.
- Ngày 2026-10-28: sửa Stop queue thành FIFO không loại bỏ frame; tách timeout queue drain, xử lý sender cancellation để vẫn final-save transcript, và chờ quiet window sau các mảnh transcript trễ. Tăng browser stop guard lên 90 giây. Regression coverage mới gồm thứ tự frame cuối / activity_end / sentinel.
- Ngày 2026-10-28: live page nạp lại segment đã lưu trước khi xin microphone; thêm lifecycle finish presentation và export bilingual summary-input cho toàn presentation; thêm bảng/endpoint thu thập p50/p95.
- Ngày 2026-10-28: sau khi sửa regression bản dịch có dấu chấm nhưng thiếu câu, `backend/.venv/Scripts/python.exe -m unittest discover -s tests -v` đạt 49/49. Có test riêng cho đếm mốc câu (bỏ qua giờ/thập phân), ngăn lưu stream có ít mốc câu, repair khi translation hiện tại đã có dấu chấm, lưu `failed` khi không sửa được và lưu `completed` khi full-source repair thành công. `py_compile` và `git diff --check` cũng đạt. Đây vẫn là offline/mocked, chưa gọi Gemini Live thật trong test.

### Chưa được xác nhận / chưa hoàn tất

- Đã có lượt E2E microphone → Gemini Live → Stop repair → SQLite thật xác nhận lỗi mất phần cuối được sửa; thêm một lượt dừng/kết nối lại có 2 segment completed. Bộ Windows hiện tại không chạy được test async WebSocket trong process kiểm thử: asyncio Proactor bị kẹt ở socketpair/accept trước khi test endpoint chạy; vì vậy suite automated vẫn dùng dữ liệu synthetic/Gemini mock. Cần xác nhận ngôn ngữ segment thứ hai và thêm nhiều lượt mic trên các giọng, câu dài/ngắn, hai chiều trước khi đánh giá ổn định.
- Câu chốt/cắt theo VAD + dấu câu là heuristic; đã có một số lần mic thật của người dùng trước các cập nhật gần nhất, nhưng chưa đo qua nhiều giọng, câu ngắn/dài, 1–2 giây lấy hơi và cả hai chiều sau bản sửa Stop mới.
- Bộ audio fixture cố định hiện là audio tổng hợp để kiểm tra format/VAD; chưa có bộ WAV tiếng người có nhãn hoặc rubric chấm độ chính xác EN↔VI. Snapshot `GET /api/v1/metrics/live-latency` ngày 2026-10-31 có 8 mẫu audio-to-source (p50 2906 ms, p95 5109 ms), 8 mẫu audio-to-translation (p50 3062 ms, p95 5250 ms), và 5 mẫu Stop-to-final (p50 2563 ms, p95 3031 ms). Đây là baseline mic thật còn nhỏ; p95 hiện gần bằng giá trị lớn nhất do số mẫu ít, chưa đủ để kết luận benchmark ổn định.
- Retry đã được xác nhận với provider thật qua Swagger ngày 2026-10-31 trên session `989d34e5-6154-4f5e-ad07-f6488d04e2ed`: segment số 4 ban đầu `failed`; `POST /retry` trả 200, giữ nguyên `segment_id` và `source_text`, chuyển thành `completed`; gọi lại transcript trả `ready=true`, 4/4 completed, 0 failed/pending, không thiếu sequence. Đây là bằng chứng retry thành công trên segment thật sự thất bại. Lượt trước đó với provider lỗi trả 502 và giữ nguyên source/status failed, nên đã có bằng chứng cho cả nhánh provider lỗi và nhánh retry thành công.
- Tùy chọn phát audio và khôi phục transcript trong giao diện chưa được thử lại qua trình duyệt/microphone sau thay đổi gần nhất.
- **Ngữ cảnh model khi reconnect:** trang khôi phục lại transcript đã lưu để người dùng vẫn thấy lịch sử; Gemini Live Translate hiện nhận audio, tài liệu riêng của model nói text input không hỗ trợ. Session resumption được tài liệu Live API chung mô tả, nhưng tài liệu riêng Live Translate không xác nhận khả năng tương thích. Chưa bật tính năng này cho model đã chọn để tránh làm lỗi stream đang hoạt động. Cần benchmark/chuyển sang model Live tổng quát nếu bắt buộc nạp text-history hoặc dùng session resumption.
- Live persistence đã cấp một `segment_id` ổn định cho transcript buffer hiện tại và dùng lại ID đó nếu chốt/lưu được gọi lại. Lưu cùng ID và cùng nội dung trả segment hiện có; tái sử dụng ID cho nội dung khác bị từ chối. Hai test SQLite kiểm tra replay không tạo hàng thứ hai và xung đột nội dung. Giới hạn: ID đang nằm trong bộ nhớ của WebSocket; chưa đảm bảo nhận diện replay sau khi process khởi động lại hoặc trên kết nối mới.
- Dữ liệu đầu vào cho T5 đã có theo từng session và toàn presentation; phần chạy model T5, UI/app của Thiện/Linh và kiểm thử đa thiết bị của Ngọc nằm ngoài phần TDKha.
- Backend FastAPI còn cảnh báo deprecation `@app.on_event("startup")`; không làm hỏng test/runtime nhưng có thể đổi sang lifespan ở đợt dọn kỹ thuật kế tiếp.
- Checklist workbook vẫn là snapshot thủ công 48 hạng mục ghi ngày 2026-10-24; số trạng thái trong đó không tự đồng bộ với code. Không dùng số này để khẳng định tiến độ sau các cập nhật ngày 2026-10-28.

## 3. Cấu hình/model đã rà soát

Chỉ ghi tên biến/model, **không ghi API key hoặc giá trị bí mật**.

- `backend/.env` hiện đặt `GEMINI_MODEL=gemini-3.5-flash-lite` và `GEMINI_FALLBACK_MODEL=gemini-3.6-flash`.
- Hai giá trị trên được `backend/gemini_service.py` dùng cho endpoint dịch **văn bản** qua `generate_content`.
- `backend/gemini_live_service.py` đọc `GEMINI_LIVE_MODEL`; nếu không đặt biến, vẫn dùng `gemini-3.5-live-translate-preview` như trước.
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
- Trang `/live` và WebSocket là prototype live audio gắn với `session_id`; segment live lưu vào SQLite và xuất được qua transcript/summary-input REST.
- `session_id` là định danh một phiên lưu trữ; `segment_id` là định danh bền vững của một câu/utterance đã chốt; `sequence_number` giữ thứ tự câu trong session. Live backend giữ một segment ID từ lúc bắt đầu buffer đến lúc lưu; nếu thao tác ghi được lặp lại trong cùng vòng đời buffer thì SQLite trả lại hàng cũ thay vì tăng sequence. Idempotency qua process restart/reconnect vẫn chưa được triển khai.
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
7. **Finish/export:** chỉ kết thúc session sau khi xử lý hết pending/failed/missing sequence; kết thúc presentation khi mọi session đã kết thúc; trả transcript hai ngôn ngữ có thứ tự và payload theo session hoặc toàn presentation cho T5.
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
- Tạo segment/sequence cho utterance final; chỉ persist final; giữ ID idempotent trong vòng đời buffer; đảm bảo transaction, uniqueness và thứ tự.
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
- Số trạng thái trong workbook được chốt lần tạo ngày 2026-10-24: 48 hạng mục, 2 đạt, 5 đang làm, 41 chưa bắt đầu. Workbook chưa được đồng bộ cho các thay đổi ngày 2026-10-28; trạng thái mới nhất nằm trong file này và kết quả chạy test.

## 9. Cách tiếp tục ở cuộc trò chuyện sau

1. Đọc `PROJECT_STATE.md` trước; đọc checklist realtime và source code liên quan nếu cần xác nhận chi tiết.
2. Xem tin nhắn mới nhất của người dùng là chỉ đạo tiếp tục/điều chỉnh lộ trình.
3. Chưa sửa code hoặc tạo thêm file cho đến khi người dùng yêu cầu. Khi được yêu cầu code, cung cấp trọn file/code và hướng dẫn chạy theo từng bước; không bắt người dùng tự tìm và thay những đoạn rời rạc.
4. Người dùng đã cho phép tự chạy và kiểm tra để sửa lỗi hiệu quả; có thể chạy test offline khi cần, nhưng phải phân biệt rõ test offline với kiểm thử Gemini/microphone thật.
5. Sau mỗi mốc, cập nhật file này bằng trạng thái và bằng chứng mới; không ghi “đã xong” chỉ dựa trên code tồn tại hoặc ví dụ Swagger chưa chạy đúng trường hợp.
6. Bằng chứng offline mới nhất: `backend/.venv/Scripts/python.exe -m unittest discover -s tests -v` đạt 51/51 ngày 2026-10-31; lượt chạy mới nhất mất 5.829 giây. Test dùng dữ liệu synthetic hoặc Gemini mock; không gọi Google Live thật. Sau thay đổi handoff, `py_compile` cho các module chính đạt; `git diff --check` không báo lỗi nội dung (chỉ cảnh báo chuẩn hóa LF/CRLF của `PROJECT_STATE.md`). GET `/health` trên server đang chạy vẫn trả 200 sau hot reload.
7. Đã xác nhận bằng mic thật lỗi mất chữ cuối được sửa và đã xác nhận finish session → finish presentation → export summary-input. Lượt `73a8ee52-f222-41fd-9acc-d71fb8b167b7` xác nhận 3 segment EN→VI rồi 3 segment VI→EN, 6/6 completed. Lượt kế `5ed6790e-e33c-44f5-97a1-13c7d7dd6b04` xác nhận VI→EN, kết thúc session/presentation và summary-input đúng. Lượt cuối dùng presentation mới; chưa có bằng chứng summary gom nhiều session trong cùng presentation. Retry với Gemini thật đã được xác nhận thành công trên segment `failed` thật ở session `989d34e5-6154-4f5e-ad07-f6488d04e2ed`. Các ưu tiên còn lại: quality set có nhãn, benchmark p50/p95 và kiểm chứng thêm các luồng reconnect/multi-session.
8. Ngày 2026-10-31, retry thật trên segment `failed` trả 200; cùng segment chuyển sang `completed`, giữ nguyên ID/source; transcript sau đó trả `ready=true`, 4/4 completed, không thiếu sequence. Lượt retry thật trước đó trên DB tạm nhận lỗi provider 502 và giữ nguyên source/status failed. Hai đường xử lý retry thành công/thất bại đều đã có bằng chứng.
9. Ngữ cảnh model khi reconnect vẫn là quyết định mở: không nạp text vào Live Translate vì tài liệu model nói text input không được hỗ trợ; cần kiểm tra session resumption trên đúng model hoặc benchmark model Live tổng quát trước khi tuyên bố đã khôi phục ngữ cảnh Gemini.

## 10. Archify đã cài và sơ đồ kiến trúc

- Ngày 2026-10-31 đã cài Archify v3.2.0 ở phạm vi project cho Codex tại `.agents/skills/archify`; `doctor` báo sẵn sàng. Đây là công cụ tài liệu/visual hóa, không nằm trong luồng chạy backend và không thay đổi kỹ thuật dịch.
- Ba candidate JSON và HTML độc lập nằm trong `.archify/architecture-realtime-translation-20261031-182805/`:
  - `realtime-translation-architecture.html`: kiến trúc browser → FastAPI → Gemini Live/text → SQLite, kèm VAD, transcript assembly, Stop drain, lifecycle/export và latency metrics.
  - `live-audio-sequence.html`: trao đổi audio, partial/final, drain khi Stop, repair bản dịch và lưu segment.
  - `segment-retry-lifecycle.html`: trạng thái received/completed/failed và điều kiện retry.
- Ngày 2026-10-31, sau khi thêm idempotency cho live segment, cả ba sơ đồ đã được làm mới theo source snapshot hiện tại và qua Archify validate, deliver, provenance check, browser-check. Đường phản hồi Gemini Live trong sơ đồ kiến trúc có ba khúc nhưng đã được xem ở 1440×900 light/dark: không che node/label và đọc được; sơ đồ sequence dài theo chiều dọc nên xem bằng cách cuộn. Ba HTML được tạo lại trong cùng thư mục artifact.
- Các nguồn trong sơ đồ được xác thực theo snapshot Git cục bộ tách biệt tại `C:\Users\tdkha\.codex\visualizations\2026\10\23\01a16a1a-a4c7-7b93-98f5-88e390d6a426\archify-source-snapshot-20261031-182805`, revision `604d631c0b179e575ed3053bc491a8c282219c32`. Snapshot chỉ chứa các file source cần trích dẫn, dùng `link_mode: local-only`, không phải commit đã push lên GitHub và không làm thay đổi nhánh chính. Khi source được sửa, cần cập nhật snapshot và finalize sơ đồ.
- Nội dung sơ đồ bằng tiếng Việt; nút điều khiển của Archify Viewer hiện còn tiếng Anh. Cài đặt tạo thêm `skills-lock.json` và thư mục `.agents/`; artifact đầu ra ở `.archify/`. Không có sửa backend trong bước cài Archify này.
- Ngày 2026-10-31 đã thêm idempotency theo `segment_id` ổn định trong vòng đời live buffer; replay cùng dữ liệu không tạo duplicate, xung đột ID bị từ chối, 51/51 test đạt. Idempotency sau reconnect/process restart vẫn chưa có. Retry Gemini thật đã được kiểm chứng thành công và khi provider lỗi vẫn giữ source. Handoff pack nay gồm contract REST/WebSocket/T5, setup, dependency và CORS allowlist có thể cấu hình. Việc tiếp theo của API core là xây quality set có nhãn, benchmark p50/p95 trên đủ mẫu, đánh giá VAD/partial reconciliation trên nhiều lượt và xác nhận multi-session summary-input. Archify minh họa kiến trúc nhưng không thay các bằng chứng kiểm thử runtime.


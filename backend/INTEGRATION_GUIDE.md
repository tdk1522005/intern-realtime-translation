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
{"type":"start","target_language_code":"vi","continue_fragment":true,"speech_output_enabled":false}
```

`target_language_code` là ngôn ngữ đích: `vi` nghĩa là dịch sang tiếng Việt; `en` nghĩa là dịch sang tiếng Anh. Backend xác định ngôn ngữ nguồn theo chiều ngược lại.
`continue_fragment` là tùy chọn, mặc định `true`. Nếu segment cuối trong cùng session/cùng chiều dịch đang ở trạng thái `fragment`, lời nói mới được nối vào lời gốc cũ và cả câu được dịch lại. Gửi `false` khi người dùng muốn bắt đầu ý mới; đoạn dở cũ vẫn được giữ nguyên.
`speech_output_enabled` là tùy chọn, mặc định `true` để tương thích client cũ. Gửi `false` nếu chỉ cần chữ: backend không chuyển các gói audio dịch lớn về client; transcript nguồn và bản dịch vẫn được gửi bình thường. Khi người dùng đổi lựa chọn trong lúc đang thu, gửi `{"type":"set_speech_output","enabled":true}` hoặc `false`.
`provider_activity_mode` là tùy chọn, mặc định `manual` để giữ cách chạy đã kiểm thử bằng microphone. Hai giá trị thử nghiệm là `automatic` (gửi liên tục cả khung âm thanh im lặng để Gemini tự phát hiện ngắt hơi) và `hybrid` (cũng gửi âm thanh liên tục, đồng thời gửi `audio_stream_end` khi VAD backend phát hiện ngắt hơi và lúc Stop). Chỉ thay chế độ này khi mở một WebSocket mới; dùng session mới cho phép so sánh A/B rõ ràng. `session_ready.vad.provider_activity_mode` xác nhận chế độ thực tế. Hai chế độ thử nghiệm đã qua test giả lập nhưng cần đo bằng Gemini và microphone thật trước khi áp dụng mặc định cho web/app.

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
    continue_fragment: true,
    speech_output_enabled: false,
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
| `session_ready` | Backend sẵn sàng; có `session_id`, `source_language`, `target_language`, `audio_format`, thông số VAD, `context_restore_status`, `context_restored`, số segment lịch sử, sequence cuối, `continuation_fragment_id` (nếu đang nối đoạn dở) và thời gian mở provider. |
| `input_partial` | Transcript nguồn đang cập nhật. Thay nội dung preview bằng `text`; không nối snapshot vào cuối. |
| `output_partial` | Bản dịch đang cập nhật. Cũng thay preview bằng `text`. |
| `segment_final` | Segment đã lưu/cập nhật; có `segment_id`, `sequence_number`, `source_text`, `source_language`, `en`, `vn`, `status`, `created_at`. Khi nối tiếp đoạn dở, event mới dùng lại `segment_id` cũ và thay nội dung. Field `source_confirmed_without_punctuation` được giữ để tương thích; chính sách hiện tại không tự chốt source thiếu dấu câu. |
| `translated_audio` | Chunk audio dịch dạng base64, có `mime_type`; backend chỉ gửi khi `speech_output_enabled=true`. |
| `speech_pause_detected`, `vad_diagnostic` | Thông tin chẩn đoán VAD; có thể bỏ qua trong giao diện sản phẩm. |
| `stop_draining` | Backend đang xả frame cuối và đợi transcript/bản dịch; chưa báo hoàn tất. |
| `stream_stopped` | Luồng đã dừng; có số segment lưu được, `fragments_saved`, `stop_source_confirmations` và số liệu drain. Session trong database vẫn `active` để có thể nối lại hoặc kết thúc sau. |
| `provider_go_away` | Gemini báo provider connection sắp đóng. Dừng gửi audio lên socket hiện tại; có thể mở lại WebSocket với cùng `session_id` và ngôn ngữ đích để backend thử resume. |
| `error` | Có `message` mô tả lỗi. Nếu socket đóng, tải transcript REST lại để đồng bộ trạng thái. |

Partial là preview có thể thay đổi; chỉ `segment_final` là dữ liệu chuẩn. Nếu gặp lại cùng `segment_id`, cập nhật bản ghi đó thay vì thêm hàng mới. Sắp xếp theo `sequence_number`, không dựa vào thứ tự event đến UI.
`segment_final.status="failed"` nghĩa là bản ghi đã lưu để chẩn đoán/xử lý tiếp, **không** có nghĩa bản dịch trong `en`/`vn` đáng tin hoặc được phép xem là kết quả cuối. Web/app nên gắn nhãn chưa hoàn chỉnh và chỉ đưa segment `completed` vào phần hiển thị bản dịch đã chốt.

`segment_final.status="fragment"` nghĩa là người dùng chủ động Stop sạch khi lời nói vẫn dở. Backend giữ nguyên `source_text` và field ngôn ngữ gốc; field đích chứa bản dịch **tạm** nếu Gemini đã trả về, hoặc `null` nếu chưa có chữ dịch nào. Ví dụ `source_text="Xin"`, `vn="Xin"`, `en="Thank you."` vẫn có thể xảy ra, nhưng `en` chỉ là dự đoán và có thể sai. Client phải gắn nhãn “tạm/đang nói dở”, không coi đây là bản dịch cuối. `fragment` không phải lỗi provider nên không gọi retry; nó không chặn kết thúc session/presentation. `failed` vẫn dành cho lỗi/mất bản dịch và chặn finish cho đến khi được xử lý.

Vì Gemini có thể trả 1–2 từ cuối sau khoảng nghỉ, source **không có dấu kết câu** khi Stop sạch luôn được giữ là `fragment`, kể cả khi bản dịch tạm có vẻ hợp lý. Ví dụ `Cảm ơn bạn vì đã đến tham dự` có thể được nói tiếp bằng `buổi tiệc...`; đánh dấu `completed` ngay sẽ khiến từ tiếp theo rơi vào segment khác. Đây là chính sách giữ dữ liệu để nối tiếp, không phải khẳng định mọi câu thiếu dấu chấm đều sai.

Trong cùng luồng đang nói, backend giữ mảnh dở để chờ tiếng nói kế tiếp. Nếu đã Stop, kết nối lại cùng session/chiều dịch với `continue_fragment=true` sẽ lấy segment `fragment` cuối và nối lời gốc mới vào đó. Khi câu ghép đủ hoàn chỉnh, backend dịch lại **toàn bộ** qua text Gemini rồi cập nhật chính `segment_id` cũ; client cần upsert theo ID. Nếu lời mới vẫn dở, segment ấy tiếp tục là `fragment`. Đổi chiều dịch hoặc gửi `continue_fragment=false` sẽ bắt đầu segment khác. Không tự xóa từ lặp: nếu người dùng nói “Xin” hai lần, transcript giữ cả hai. Một số câu dở có dấu chấm do ASR thêm vẫn có thể bị coi là hoàn chỉnh; quy tắc hiện nhận diện rõ các trường hợp như `Xin.` / `Please.`.

Để giảm việc chốt trước khi Gemini trả 1–2 từ cuối, mặc định backend chờ transcript yên 2500 ms sau ngắt hơi và 2500 ms lúc Stop. Preview trên WebSocket vẫn hiển thị ngay; thời gian chờ chỉ ảnh hưởng việc ghi segment cuối. Có thể đổi `LIVE_SEGMENT_SETTLE_MS` và `LIVE_STOP_QUIET_MS` trong cấu hình. Không có dấu mốc transcript cuối đáng tin từ model thì không thể bảo đảm 100% rằng từ đến sau cửa sổ chờ sẽ được gán đúng câu; cần kiểm tra bằng microphone thật và bộ audio chuẩn.


Khi reconnect cùng session và cùng chiều dịch, backend thử dùng Gemini resumption handle mới nhất. `session_ready.context_restored` cho biết provider context có thực sự được khôi phục hay không; các bản ghi transcript vẫn được lưu dù provider phải mở stream mới. Client chỉ gửi audio mới sau reconnect, không gửi lại các frame đã gửi trước đó. Binary audio hiện chưa mang frame ID nên backend không thể phân biệt audio replay với lời nói được lặp lại có chủ ý. Backend giữ `segment_id` đang chờ ghi qua reconnect và việc lưu segment/advance ID là idempotent trong SQLite. Nếu session đang có một WebSocket khác, kết nối thứ hai nhận event `error` và close code 1013.

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
| Metrics live và reconnect | `GET /api/v1/metrics/live-latency` |
| Xem thứ tự sự kiện Live để tìm lỗi chậm chữ | `GET /api/v1/sessions/{session_id}/live-diagnostics` |

`live-diagnostics` chỉ phục vụ kiểm tra core: trả tối đa 200 mốc của kết nối Live đã dừng gần nhất, tính bằng mili giây từ lúc mở kết nối. Các mốc gồm VAD bắt đầu/ngắt hơi, marker gửi tới Gemini, mảnh chữ Gemini trả, chữ backend gửi cho client, Stop và lưu segment. Không lưu âm thanh; `text_tail` chỉ giữ tối đa 80 ký tự cuối của mảnh chữ. Nếu chưa có kết nối nào dừng sau khi tính năng này được thêm, endpoint trả `404`.
Để xác định chữ cuối đến chậm do đâu, so mốc `local_pause`, `provider_activity_end` hoặc `provider_audio_stream_end`, `provider_source`/`provider_translation`, rồi `source_partial_sent`/`translation_partial_sent`. Nếu hai mốc cuối trùng thời gian, backend đã đẩy chữ ngay khi nhận từ Gemini. Ở chế độ `automatic`, chỉ có `provider_audio_stream_end` khi Stop; ở `hybrid`, có thêm mốc này mỗi lần VAD chốt khoảng nghỉ.

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

Finish trả `409` nếu còn segment lỗi/đang chờ, thiếu sequence, session chưa kết thúc hoặc presentation chưa sẵn sàng. Segment `fragment` vẫn được tính vào số segment và transcript nguồn có thứ tự. Trong summary-input, mỗi segment có `status`; với `fragment`, field đích có thể chứa bản dịch tạm, nhưng `transcript_en`/`transcript_vn` tổng hợp chỉ ghép bản dịch của segment `completed` và lời gốc của fragment. Bên T5 có thể đọc danh sách `segments` để biết đoạn nào chưa có bản dịch cuối; endpoint này chỉ tổng hợp dữ liệu, không chạy T5.

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
          "vn": "Cuộc họp bắt đầu lúc chín giờ.",
          "status": "completed"
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
- Gemini resumption handles nằm trong SQLite ở backend, được phân biệt theo session/model/ngôn ngữ đích và tự bỏ khi quá hạn; không chuyển handle cho client.
- Metrics endpoint có thêm p50/p95 `provider_connect_ms` và phân nhóm theo `fresh`, `resumed`, `resume_fallback`.
- `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL` dành cho dịch văn bản; `GEMINI_LIVE_MODEL` dành cho stream audio. Model live mặc định giữ nguyên model hiện tại nếu biến này không được đặt.
- Các biến `LIVE_*` trong `.env.example` tinh chỉnh VAD/drain; nếu bỏ trống, code dùng giá trị mặc định hiện có.

## 7. Kiểm tra backend trước khi bàn giao

Từ thư mục `backend`:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Các test này kiểm tra logic backend bằng fixture/mock; chúng không thay thế việc web/app chạy thử trên thiết bị thật hoặc việc chấm chất lượng dịch bằng audio của nhóm.

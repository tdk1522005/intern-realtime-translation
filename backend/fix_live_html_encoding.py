from pathlib import Path
import re
import shutil

path = Path("live.html")
text = path.read_text(encoding="utf-8")

def replace_one(pattern: str, replacement: str, label: str) -> None:
    global text
    text, count = re.subn(
        pattern,
        lambda _: replacement,
        text,
        count=1,
        flags=re.DOTALL,
    )
    if count != 1:
        raise SystemExit(f"Không tìm thấy nội dung cần sửa: {label}")

replace_one(
    r"<title>.*?</title>",
    "<title>Dịch giọng nói trực tiếp</title>",
    "title",
)
replace_one(
    r"<h1>.*?</h1>",
    "<h1>Dịch giọng nói trực tiếp</h1>",
    "heading",
)
replace_one(
    r'<p class="hint">.*?</p>',
    "<p class=\"hint\">Cho phép trình duyệt dùng microphone. Nói tự nhiên; bản dịch sẽ hiện và phát âm thanh khi Gemini trả kết quả.</p>",
    "description",
)
replace_one(
    r'<label for="target">.*?</label>',
    '<label for="target">Dịch sang</label>',
    "target label",
)
replace_one(
    r'<option value="vi">.*?</option>',
    '<option value="vi">Tiếng Việt</option>',
    "Vietnamese option",
)
replace_one(
    r'<button id="start">.*?</button>',
    '<button id="start">Bắt đầu</button>',
    "start button",
)
replace_one(
    r'<button id="stop" disabled>.*?</button>',
    '<button id="stop" disabled>Dừng</button>',
    "stop button",
)
replace_one(
    r'<span id="status">.*?</span>',
    '<span id="status">Chưa kết nối</span>',
    "status",
)

labels = iter([
    "Lời nói được nhận dạng",
    "Bản dịch",
])
text, count = re.subn(
    r'<div class="label">.*?</div>',
    lambda _: f'<div class="label">{next(labels)}</div>',
    text,
    count=2,
    flags=re.DOTALL,
)
if count != 2:
    raise SystemExit("Không tìm thấy đủ hai nhãn transcript.")

statuses = [
    "Có lỗi",
    "Đã dừng",
    "Đang xin quyền microphone…",
    "Đang nghe và dịch trực tiếp…",
    "Mất kết nối",
    "Đã ngắt kết nối",
    "Không thể bắt đầu",
    "Đang kết thúc lượt dịch…",
    "Đã dừng",
]
for status in statuses:
    text, count = re.subn(
        r"statusEl\.textContent\s*=\s*'[^']*';",
        lambda _, value=status: f"statusEl.textContent = {value!r};",
        text,
        count=1,
    )
    if count != 1:
        raise SystemExit("Không tìm thấy đủ các dòng trạng thái để sửa.")

error_message = (
    "Không kết nối được WebSocket. "
    "Kiểm tra server và thử lại."
)
text, count = re.subn(
    r"errorEl\.textContent\s*=\s*'Kh[^']*';",
    lambda _: f"errorEl.textContent = {error_message!r};",
    text,
    count=1,
)
if count != 1:
    raise SystemExit("Không tìm thấy dòng thông báo WebSocket để sửa.")

text, count = re.subn(
    r"statusEl\.textContent\.includes\('[^']*'\)",
    "statusEl.textContent.includes('Đang')",
    text,
    count=1,
)
if count != 1:
    raise SystemExit("Không tìm thấy điều kiện trạng thái kết nối.")

backup = Path("live.html.before_encoding_fix")
if not backup.exists():
    shutil.copy2(path, backup)

path.write_text(text, encoding="utf-8")
print(f"Đã sửa chữ tiếng Việt trong {path}.")
print(f"Bản dự phòng: {backup}")

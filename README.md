# SignalDesk

Web tiếng Việt để thu thập, xem lại và gán nhãn bình luận từ TikTok, YouTube và website. Ưu tiên kiểm duyệt theo bộ nhãn **0 CLEAN / 1 OFFENSIVE / 3 HATE** do người dùng cung cấp. Domain/chủ đề là một trường riêng.

## Chạy trên Windows

Yêu cầu Python 3.11+ và internet để cài thư viện/trình duyệt lần đầu.

```powershell
cd C:\Users\tyranno\Documents\Project\CommentTikok
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
```

Mở **http://127.0.0.1:8765**. Script tạo virtualenv, cài dependency và Chromium. Sau khi cài lần đầu, khởi động nhanh:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Linux/macOS:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python -m playwright install --with-deps chromium
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

## Sử dụng

1. **Bộ nhãn & domain**: xem/sửa định nghĩa; thêm/sửa/tắt domain.
2. **Nguồn thu thập**: chọn nền tảng, từ khóa tìm kiếm hoặc URL video; cấu hình số video, bình luận/video, replies và lịch lặp.
3. **Tác vụ & nhật ký**: chạy, dừng, xem lỗi và tiếp tục tác vụ. Lịch chỉ hoạt động khi server chạy.
4. **Kho bình luận**: tìm kiếm/lọc; mở câu để xem video, bình luận cha (nếu đã thu thập), gán nhãn và domain. Có gán/duyệt hàng loạt, loại khỏi tập và đưa về chờ duyệt.
5. **CSV / JSONL**: xuất theo bộ lọc hiện tại. CSV UTF-8 BOM có bảo vệ công thức spreadsheet; JSONL giữ nguyên text.

### Ảnh, sticker và dữ liệu bình luận đầy đủ

TikTok collector giữ cả bình luận chỉ có ảnh/sticker, emoji trong text, replies, thông tin tác giả, lượt thích, thời gian và toàn bộ payload gốc (bao gồm nhắc tên, liên kết và metadata do TikTok trả về). Ảnh được ưu tiên bản gốc, sticker được ưu tiên bản động; nếu phản hồi có tệp video/âm thanh, bộ thu thập cũng nhận diện URL của chúng. Tệp tải thành công lưu trong `data/media/`; tải lỗi vẫn giữ URL cùng trạng thái lỗi để không mất thông tin bình luận. Mỗi tệp tối đa 30 MB; downloader hỗ trợ các CDN TikTok đã nhận diện, định dạng ảnh raster, MP4/WebM và âm thanh thông dụng. Không bảo đảm lấy được dữ liệu mà TikTok không trả về hoặc tệp đã hết hạn.

Trong **Kho bình luận**, chọn **Có ảnh / tệp** để lọc. Có ảnh xem trước và nút tải tệp; mở bình luận để xem tệp cùng mục **Dữ liệu gốc đầy đủ**. CSV thêm cột JSON `attachments` và `raw`; JSONL xuất hai trường này dưới dạng mảng/object. Tệp media lưu riêng, không nhúng byte ảnh vào CSV/JSONL. Model hiện chỉ phân loại văn bản, chưa đọc nội dung ảnh; bình luận chỉ có tệp được giữ chờ duyệt.

Để tải lại tệp từ payload của bình luận đã có sau khi khởi động bản cập nhật:

```powershell
.\.venv\Scripts\python.exe scripts/backfill_comment_media.py
```

Chạy lại thao tác thu thập bằng **Thu thập ngay** trên nguồn để đọc lại video; **Tiếp tục** giữ checkpoint và bỏ qua video đã hoàn tất. Khi gặp ID đã có, bộ cào cập nhật tệp và dữ liệu gốc, giữ nhãn đã được người dùng duyệt.

Phím tắt trong màn hình duyệt: **0 / 1 / 3** chọn nhãn, **Ctrl+Enter** lưu, **Esc** đóng.

Khi bật **Hiện trình duyệt**, tác vụ gặp CAPTCHA/đăng nhập sẽ giữ cửa sổ đang cào và chờ tối đa 5 phút. Hoàn tất xác minh thủ công ngay trong cửa sổ đó và **giữ cửa sổ mở**; tác vụ tự tiếp tục trong cùng phiên. Có thể bấm **Dừng** trong lúc chờ. Nếu hết thời gian hoặc chạy ẩn, tác vụ chuyển sang Cần xử lý. Khi đó chọn **Mở TikTok xử lý** để mở đúng URL bằng cùng profile; hoàn tất xác minh, **đóng cửa sổ đăng nhập riêng**, rồi bấm **Tiếp tục**. JSON và ảnh chẩn đoán được lưu local trong `data/diagnostics` khi có lỗi.

Không tự đổ dữ liệu giả vào kho. Nút **Xem thử với dữ liệu mẫu** chủ động nhập 8 mẫu, platform `demo` để lọc riêng. Import JSON (mảng hoặc `{ "items": [...] }`), JSONL và CSV có header `text`; tùy chọn `platform`, `external_id`, `context`, `author`, `video_url`, `safety_label`. Nhãn nhập luôn chờ duyệt. Mỗi request tối đa 5.000 câu, UI tự chia lô. Giới hạn file UI 30 MB.

```json
[{"text":"Sản phẩm này chưa tốt, giá hơi cao.","platform":"import","external_id":"sample-1","context":"Review điện thoại","safety_label":0}]
```

## Bộ nhãn

| Mã | Nhãn | Quy tắc |
| --- | --- | --- |
| 0 | CLEAN | Bình luận bình thường, trung tính, tích cực hoặc tiêu cực nhưng không xúc phạm, miệt thị hay thù ghét. Có thể chê ý kiến/sản phẩm/màn trình diễn nếu không hạ nhục con người. |
| 1 | OFFENSIVE | Chửi bới, xúc phạm, chế giễu, hạ nhục, công kích cá nhân/nhóm, dùng từ tục/miệt thị, không dựa trên đặc điểm nhóm được bảo vệ. |
| 3 | HATE | Thù ghét, hạ nhục, loại trừ, đe dọa hoặc công kích vì đặc điểm nhóm được bảo vệ như giới tính, dân tộc/chủng tộc, quốc tịch, tôn giáo, xu hướng tính dục, bản dạng giới, khuyết tật. |

Thiếu ngữ cảnh thì giữ **chưa gán / chờ duyệt**, không ép thành CLEAN. Không có nhãn 2. Mọi sửa nhãn từ người duyệt có audit và version để tránh ghi đè thay đổi đồng thời. AI không thay nhãn đã duyệt.

## Kết nối model

Copy `.env.example` thành `.env` khi cần cấu hình secrets. Trong web **Kết nối & mô hình**, nhập base URL và tên model tương thích Chat Completions, hỗ trợ `response_format: json_object`. Ví dụ Ollama `http://localhost:11434/v1` với một model đã tải trên máy. API key nếu cần đặt trong `LLM_API_KEY` rồi khởi động lại.

Model nhận định nghĩa nhãn, các domain và ngữ cảnh video; trả JSON, được kiểm tra mã nhãn/domain và khoảng điểm. Điểm tin cậy là model tự báo, **chưa được hiệu chỉnh**; kết quả luôn chờ duyệt. Khi chưa có model, chỉ gợi ý domain theo từ khóa, không đoán nhãn an toàn, không tạo điểm tin cậy giả.

## Thu thập thực tế

- **YouTube**: tìm video qua yt-dlp; lấy bình luận qua YouTube Data API nếu có `YOUTUBE_API_KEY`, phân trang riêng bình luận gốc và replies. Nếu chưa có key, dùng yt-dlp subprocess có thể hủy. Không cần tải video.
- **TikTok**: Playwright tự mở Chromium profile riêng, tìm video theo từ khóa hoặc vào URL, **bấm tab Comments** (trang mới mặc định mở You may like), đọc phản hồi comment endpoint khi cuộn **ancestor có overflow scroll**, mở replies khi bật. Chỉ nhận bình luận thuộc đúng video và giữ ID bình luận cha. Vào **Kết nối & mô hình → Mở trình duyệt đăng nhập TikTok**, đăng nhập rồi đóng cửa sổ. Profile được tuần tự hóa để tránh nhiều worker cùng mở. Đã thử lấy bình luận thật từ URL người dùng cung cấp (xem VALIDATION.md), nhưng không bảo đảm lấy đủ số bình luận hiển thị trên ứng dụng. CAPTCHA/đăng nhập làm dừng batch để xử lý; 3 URL lỗi liên tiếp chuyển sang Cần xử lý. Khi lỗi, thông tin HTTP/mã API và đường dẫn JSON/ảnh chẩn đoán trong `data/diagnostics` được ghi vào nhật ký; không lưu cookie/headers/token trong JSON chẩn đoán.
- **Website**: nhập URL và CSS selector riêng của phần tử bình luận (ví dụ `.comment-content`). Tự cuộn và lấy text. Không tự biết cấu trúc mọi website; website có nút Load more/replies đặc thù cần bổ sung adapter. Chống trùng theo URL + text nên hai bình luận giống hệt trên cùng trang sẽ gộp.
- Kho lưu SQLite WAL, duy nhất `(platform, external_id)`; tiến độ theo video/URL. Khi hủy hoặc crash giữa video, lần tiếp tục đọc lại video đó rồi chống trùng. Tác vụ có lỗi giữ dữ liệu thu được. Lịch mới tạo lần chạy mới, không tái dùng checkpoint cũ.
- Hiện bản local một process, 1–8 worker async (mặc định 2). Không chạy uvicorn `--workers >1`. Collector trình duyệt dùng chung profile nên chạy tuần tự, YouTube có thể song song. Đây chưa phải hệ thống phân tán cho hàng triệu câu; cần benchmark, batching model và chuyển PostgreSQL/queue worker trước khi mở rộng lớn.

## Docker

```sh
docker compose up --build -d
```

Đặt `visible_browser=false` cho nguồn TikTok/Website trong container vì image không có desktop. Chức năng đăng nhập có cửa sổ dùng bản Windows/local. Dữ liệu mount `./data`. Nếu model chạy trên host Docker Desktop, dùng `http://host.docker.internal:11434/v1`.

## Kiểm chứng

Xem [VALIDATION.md](VALIDATION.md) để biết phần đã chạy thử và phần chưa xác minh. `requirements.lock` ghim môi trường đã kiểm thử; `requirements.txt` khai báo dependency trực tiếp.

## Cấu trúc và vận hành

```text
app/main.py          API, review, import/export, cấu hình
app/db.py            SQLite schema, khởi tạo bộ nhãn và domain
app/engine.py        Worker, scheduler, checkpoint, phân loại
app/collectors.py    YouTube, TikTok, website
app/classifier.py    Model tương thích OpenAI / gợi ý domain local
app/static/          Web HTML/CSS/JS, không cần Node để chạy
tests/               Kiểm thử workflow và tính toàn vẹn dữ liệu
data/                DB và browser profile, không đưa vào Git
```

Swagger: `/docs`. Sao lưu thư mục `data/` khi server dừng (hoặc dùng SQLite backup API khi đang chạy). `.env` và profile chứa thông tin đăng nhập, đã loại khỏi Git/Docker context. Mặc định bind loopback; nếu expose mạng phải có `SIGNALDESK_TOKEN`, HTTPS/reverse proxy và phân quyền bổ sung. Token nhập trong web được lưu ở sessionStorage. Bản hiện tại một người kiểm duyệt, không có tài khoản/phân quyền nhiều người.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check app/static/app.js
```

## Nguồn tham khảo và giấy phép

Mã ứng dụng được viết mới, MIT. Không sao chép/gộp mã nguồn nguyên repo; sử dụng dependency và adapter để tránh mang theo tính năng hoặc license không cần thiết.

- [Playwright](https://github.com/microsoft/playwright-python) — điều khiển trình duyệt, Apache-2.0.
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — tìm kiếm/thu thập YouTube, Unlicense (bản đóng gói có thể gồm thành phần khác).
- [YouTube API commentThreads.list](https://developers.google.com/youtube/v3/docs/commentThreads/list), [comments.list](https://developers.google.com/youtube/v3/docs/comments/list).
- [Argilla](https://github.com/argilla-io/argilla), [Crawlee](https://github.com/apify/crawlee-python), [Browser Use](https://github.com/browser-use/browser-use): tham khảo cách tổ chức workflow; chưa tích hợp runtime của các repo này. Web duyệt nhãn được xây trực tiếp trong SignalDesk.

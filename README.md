# Hệ thống tin công nghệ realtime — Gói deploy InfinityFree

Pure PHP + MySQL, **KHÔNG WordPress, KHÔNG plugin CMS**.
Giao diện giữ y chang bản preview: tab Tất cả / AI / Tool & Phần mềm / Phần cứng & Giá,
lọc ngôn ngữ EN/ZH/RU, tìm kiếm, nút cập nhật ngay, đếm ngược 15 phút, bảng giá 12 hãng.

## 0. Kiến trúc (vì sao lại chia 2 nơi)

| Thành phần | Chạy ở đâu | Vì sao |
|---|---|---|
| Frontend PHP + MySQL | InfinityFree | Chỉ PHP, 50.000 hits/ngày, file ≤ 1 MB |
| Collector Python + lịch 15 phút | GitHub Actions | **InfinityFree KHÔNG có cron** (tắt từ 09/08/2023), không SSH, không Node/Python. Scheduler bắt buộc chạy ngoài, đẩy dữ liệu vào MySQL qua `ingest.php` có secret token. |

Luồng: GitHub Actions (cron `*/15`) → `collector.py` quét RSS/API → dịch tóm tắt tiếng Việt (DeepSeek) → POST JSON về `public/ingest.php` → MySQL → `public/index.php` chỉ đọc DB và render.

## 1. File trong gói

```
tech-aggregator-deploy/
├── public/
│   ├── config.php      # Cấu hình: DB (env), token, cấp độ, mức xu, ngưỡng đột biến
│   ├── includes.php    # Session, CSRF, tài khoản, phân quyền, xu, cấp độ
│   ├── index.php       # Trang chính (giao diện preview + header xu/cấp + badge VIP)
│   ├── article.php     # Chi tiết tin — tin VIP bị khóa nếu chưa đủ điều kiện
│   ├── login.php / register.php / logout.php
│   ├── daily.php       # Nhận xu hằng ngày (POST, 1 lần/ngày)
│   ├── save.php        # Lưu / bỏ lưu tin (member trở lên)
│   ├── share.php       # Chia sẻ tin (+xu, rồi chuyển tới link gốc)
│   ├── unlock.php      # Mở khóa tin VIP bằng xu / mua VIP 1 tháng bằng xu
│   ├── admin.php       # Quản trị: user, cấp VIP, duyệt tin VIP, log xu
│   ├── ingest.php      # Endpoint collector POST JSON (Bearer token, dedup theo hash)
│   └── .htaccess       # Chặn truy cập trực tiếp config.php/includes.php
├── db/schema.sql       # sources, items (+is_vip, vip_xu_cost), prices,
│                       # users, user_levels, xu_transactions, saved_items, user_unlocks
├── collector/
│   ├── collector.py    # 12 RSS đợt 1 + GitHub Search + Product Hunt + Google News RSS
│   └── requirements.txt
├── scripts/
│   └── backup.sh       # Backup hàng tuần: code + dump MySQL -> file theo ngày, giữ 4 bản
├── .github/workflows/
│   ├── collect.yml     # cron */15 (tin) + 01:00 UTC hằng ngày (giá)
│   └── backup-weekly.yml # cron Chủ nhật 02:00 giờ VN -> upload Google Drive
├── .gitignore
└── README.md
```

## 2. Tài khoản & phân quyền

| Vai trò | Quyền |
|---|---|
| **Guest** (chưa đăng nhập) | Chỉ xem tin thường đầy đủ. Không xem tin VIP, không xem chi tiết VIP. |
| **Member** (đăng ký miễn phí) | Xem tin thường, lưu tin, nhận xu hằng ngày, đọc/chia sẻ để kiếm xu, mở khóa tin VIP bằng xu. |
| **VIP** | Xem cả tin VIP + tin thường. Hết hạn `vip_until` tự trở về member. |
| **Admin** | Quản lý user, cấp VIP, duyệt tin VIP, xem log giao dịch xu (`admin.php`). |

Đăng ký/đăng nhập/đăng xuất bằng PHP thuần: `password_hash`/`password_verify`,
`session_regenerate_id` khi đăng nhập, CSRF token cho mọi form POST.

## 3. Cấp độ user + hệ thống xu

Cấp độ tính theo **tổng xu đã kiếm được** (`total_earned`), đổi mốc trong `config.php` (`USER_LEVELS`):

| Cấp độ | Tổng xu đã kiếm (mặc định) |
|---|---|
| Mới | 0 |
| Đồng | 100 |
| Bạc | 300 |
| Vàng | 800 |
| Kim Cương | 2000 |

Kiếm xu (mặc định trong `config.php`, đổi số là xong):

| Cách kiếm | Xu | Giới hạn |
|---|---|---|
| Đăng ký mới | +20 xu | 1 lần |
| Đăng nhập / nhận xu hằng ngày | +10 xu | 1 lần/ngày |
| Đọc tin thường | +1 xu/bài | tối đa 10 xu/ngày |
| Chia sẻ tin | +5 xu/bài | tối đa 15 xu/ngày, mỗi bài 1 lần/ngày |

Dùng xu (mặc định đề xuất, **chưa có cổng thanh toán thật** — nạp tiền thật chưa làm):

| Dùng xu | Chi phí |
|---|---|
| Mở khóa 1 tin VIP theo bài | 10 xu/bài (`vip_xu_cost` trong bảng `items`) |
| Mua gói VIP 1 tháng bằng xu | 300 xu/tháng |

Mọi cộng/trừ xu đều ghi vào `xu_transactions` (số xu, số dư sau, lý do, tham chiếu, thời gian).

## 4. Tin VIP vs tin thường

- Bảng `items` có `is_vip` (0/1) và `vip_xu_cost` (mặc định 10).
- **Collector mặc định đánh `is_vip=0`.** Chỉ đánh VIP khi bài từ nguồn "pro"
  (`OpenAI News`, `DeepMind Blog`, `Hugging Face Blog`) VÀ tiêu đề chứa từ khóa
  AI/model (GPT, Gemini, Claude, agent, benchmark, SOTA...). Quy tắc này nằm trong
  `collector/collector.py` (`VIP_SOURCES`, `VIP_KEYWORDS`).
- **Admin có thể chuyển bất kỳ bài nào thành VIP** trong `admin.php` (nút Lên VIP / Hạ VIP).
- Frontend: tin VIP hiện badge `🔒 VIP · 10 xu`, bấm vào `article.php` sẽ yêu cầu
  đăng nhập / đủ xu / là VIP mới đọc được; tin thường ai cũng đọc được.

## 5. Giá cả — đọc kỹ trước khi hứa với người xem

- Bảng giá hiện **giá cổ phiếu của 12 hãng linh kiện/AI** (NVDA, AMD, Intel, TSMC,
  Micron, Broadcom, Qualcomm, ASML, Super Micro, Arm, Samsung, SK Hynix), kèm
  % thay đổi 1/7/30 ngày và cảnh báo đột biến (|% 7 ngày| ≥ 20% hoặc |% 30 ngày| ≥ 30%).
- Đây **không phải giá bán lẻ linh kiện**, vì không có API giá bán lẻ realtime miễn phí,
  hợp pháp, bao quát toàn cầu (Keepa API từ €49/tháng). Trang ghi rõ điều này.
- Collector lấy snapshot giá hằng ngày qua Stooq CSV (miễn phí, không key) lúc 01:00 UTC;
  % thay đổi tính từ các snapshot đã lưu trong bảng `prices`.

## 6. Deploy từng bước

### Bước 1 — Đăng ký InfinityFree (cần email của bạn)
1. Vào https://www.infinityfree.com/ → Sign Up bằng **email đăng ký: dungv3525@gmail.com**, xác minh email.
   - Quy trình xác thực: hệ thống sẽ đọc mail xác minh InfinityFree gửi tới Gmail
     dungv3525@gmail.com và xác nhận tự động (đã được bạn cho phép, không cần hỏi lại).
   - **KHÔNG hardcode mật khẩu email, KHÔNG ghi mật khẩu vào code.** Mật khẩu Gmail
     không nằm trong gói này và không bao giờ được đưa vào repo.
2. Tạo hosting account (chọn subdomain miễn phí, ví dụ `technews.wuaze.com`).
3. Trong control panel → **MySQL Databases** → tạo DB, ghi lại: host (`sqlXXX.infinityfree.com`),
   tên DB (`if0_xxxxxxxx_technews`), username, password.
4. Vào **phpMyAdmin** → chọn DB → **Import** file `db/schema.sql`.
5. Tạo tài khoản admin: đăng ký 1 tài khoản thường trên web, rồi trong phpMyAdmin chạy
   `UPDATE users SET role='admin' WHERE username='ten_cua_ban';`
   (Đổi mật khẩu admin ngay sau lần đăng nhập đầu tiên.)

### Bước 2 — Upload code lên InfinityFree
1. Trong `public/config.php`, sửa các giá trị `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASS`
   (hoặc đặt biến môi trường trong `.htaccess` bằng `SetEnv` — xem ghi chú trong file).
   Tạo `INGEST_TOKEN` ngẫu nhiên dài (ví dụ 32 ký tự), điền vào config.
2. Upload **nội dung thư mục `public/`** lên thư mục `htdocs/` của InfinityFree
   (dùng File Manager hoặc FTP). Không upload thư mục `collector/`, `.github/`, `db/`.
3. Mở web, kiểm tra trang chính hiện được (lúc đầu chưa có tin là bình thường).

### Bước 3 — Tạo GitHub repo cho collector (chạy lịch 15 phút)
1. Tạo repo **private** mới trên GitHub, upload toàn bộ gói này (trừ `public/config.php`
   nếu trong đó đã điền mật khẩu thật — file này đã có trong `.gitignore`).
2. Repo → Settings → Secrets and variables → Actions → thêm secrets:
   `INGEST_URL` (ví dụ `https://technews.wuaze.com/ingest.php`),
   `INGEST_TOKEN` (trùng token ở Bước 2),
   `LLM_API_KEY` (key DeepSeek để dịch tiếng Việt — không có key thì collector
   vẫn chạy nhưng giữ tiêu đề gốc),
   `PRODUCTHUNT_TOKEN` (không bắt buộc — không có thì bỏ qua Product Hunt),
   `GITHUB_TOKEN` (không bắt buộc — GitHub tự cấp sẵn trong Actions).
   Secrets cho backup hàng tuần (xem §7):
   `DB_HOST`, `DB_PORT` (mặc định 3306), `DB_NAME`, `DB_USER`, `DB_PASS` (để mysqldump — xem lưu ý MySQL remote ở §7),
   `BACKUP_PASSPHRASE` (mật khẩu mã hóa file backup, tự đặt, không dùng chung mật khẩu khác),
   `RCLONE_CONFIG_B64` (file `rclone.conf` đã base64, remote `gdrive` nối Google Drive
   của dungv3525@gmail.com — xem §7).
3. Actions → chọn workflow `collect-tech-news` → **Run workflow** để chạy thử.
   Kiểm tra: tin mới phải hiện trên web trong vòng 15 phút.

### Bước 4 — Kiểm tra end-to-end
- [ ] Trang chính hiện tin, tab AI/Tools/Hardware lọc đúng, tìm kiếm hoạt động.
- [ ] Đăng ký/đăng nhập được, header hiện xu + cấp độ, nhận xu hằng ngày 1 lần/ngày.
- [ ] Tin VIP khóa với guest, mở khóa bằng xu được, mua VIP tháng được.
- [ ] Admin vào được `admin.php`, cấp VIP, chuyển tin thành VIP, xem log xu.
- [ ] Bảng giá có số sau lần chạy 01:00 UTC (hoặc chạy tay workflow với `--prices`).
- [ ] GitHub Actions chạy xanh mỗi 15 phút, không lỗi 401 (sai token) / 403.

## 7. Backup hàng tuần lên Google Drive

- **Lịch:** Chủ nhật 02:00 giờ VN (19:00 UTC thứ Bảy), workflow `.github/workflows/backup-weekly.yml`.
  Chạy tay: Actions → `backup-weekly` → Run workflow.
- **Script:** `scripts/backup.sh` — nén toàn bộ code web + dump MySQL (`mysqldump`)
  thành 1 file `tech-aggregator-backup-YYYY-MM-DD.tar.gz` (thêm `.enc` nếu có
  `BACKUP_PASSPHRASE` — mã hóa AES-256-CBC bằng OpenSSL).
- **Upload Drive:** rclone copy lên thư mục `tech-aggregator-backups` trên Google Drive
  của **dungv3525@gmail.com**. Cấu hình rclone (`rclone.conf`, remote tên `gdrive`)
  được tạo 1 lần bằng `rclone config` trên máy của bạn (chọn Google Drive, đăng nhập
  bằng dungv3525@gmail.com), rồi base64 file đó và lưu vào GitHub Secret
  `RCLONE_CONFIG_B64`. **Không hardcode token, không commit `rclone.conf` vào repo.**
- **Giữ 4 bản gần nhất:** script tự xóa bản local cũ hơn 4 bản; workflow xóa trên Drive
  file cũ hơn 28 ngày (`rclone delete --min-age 28d`) — tương đương ~4 bản/tuần.
- **⚠️ An toàn:** backup **LOẠI TRỪ** `public/config.php`, `.env`, `.git` — tức là
  không chứa mật khẩu DB / INGEST_TOKEN dạng plain-text. File dump `.sql` chỉ chứa
  dữ liệu (tin, user, xu), không chứa mật khẩu. Mật khẩu mã hóa backup nằm trong
  GitHub Secret `BACKUP_PASSPHRASE`. Đừng upload backup chưa mã hóa lên nơi công khai.
- **Lưu ý MySQL remote:** MySQL của InfinityFree thường **chặn kết nối từ xa**, nên
  `mysqldump` chạy từ GitHub Actions có thể thất bại. Khi đó workflow vẫn backup code,
  ghi cảnh báo, không fail job. Phương án dự phòng cho dump DB: vào phpMyAdmin →
  chọn DB → **Export** → tải file `.sql` về, đặt tên theo ngày rồi upload tay lên
  thư mục Drive `tech-aggregator-backups` (1 lần/tuần, cùng lúc kiểm tra backup tự động).
- **Kiểm tra khôi phục (nên làm 1 lần/tháng):**
  1. Tải 1 bản backup gần nhất từ Drive về máy test (giải mã bằng `BACKUP_PASSPHRASE` nếu có `.enc`).
  2. Giải nén, import `db.sql` vào MySQL trống, sửa `config.php` trỏ tới DB test.
  3. Mở trang chính bằng PHP built-in server (`php -S localhost:8000 -t public`),
     kiểm tra tin, đăng nhập, tin VIP, bảng giá hiện đủ.
  4. Nếu trang chạy được thì backup đó dùng được; nếu không, báo lỗi và sửa ngay,
     đừng đợi tới lúc mất dữ liệu mới phát hiện.

## 8. Giới hạn đã biết

- InfinityFree: không cron, không SSH, không Node/Python, chỉ PHP 8.3/8.4,
  50.000 hits/ngày, file PHP/HTML/JS ≤ 1 MB, 5 GB disk.
- Reddit RSS đã tắt 13/11/2026 — không dùng Reddit làm nguồn chính.
- Huxiu, Hugging Face Blog, HF Daily Papers, GitHub API từng timeout khi kiểm tra
  qua mạng của bản prototype — collector retry 1 lần rồi bỏ qua, các nguồn còn lại vẫn chạy.
- Tin TQ (Zhihu, Weibo, Bilibili, JD, Taobao): anti-crawl mạnh, chỉ lấy qua RSSHub/DailyHot, tỉ lệ lỗi cao.
- Chưa có cổng thanh toán thật: nạp xu bằng tiền thật chưa làm, chỉ có kiếm xu miễn phí + mua VIP bằng xu.

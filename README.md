# PO/PR Reviewing — bản thi hackathon

Đây là cách đưa công cụ lên một GreenNode vServer bằng Docker.

Hãy tưởng tượng:

- **Nginx** là cửa trước. Nó đưa trang web cho trình duyệt và chuyển hồ sơ tới API.
- **API** là bạn nhỏ đọc chứng từ, OCR và kiểm tra kết quả JSON.
- **GreenNode MaaS** là bạn AI giúp API suy nghĩ.
- **Ổ tạm** là chiếc bàn. File được đặt lên bàn lúc làm việc rồi được dọn đi.

```text
Trình duyệt → Nginx → API → GreenNode MaaS
                         ↘ ổ tạm, làm xong thì xóa
```

## Bản này có gì và không có gì?

Có:

- API key chỉ ở backend, không đi xuống trình duyệt.
- Nginx và API cùng một tên miền, nên không cần mở CORS.
- Kiểm tra MIME và magic bytes ở backend.
- File tạm được xóa sau khi xử lý; khi API khởi động, nó cũng dọn mảnh tạm cũ bị bỏ quên.
- JSON từ AI được kiểm tra trước khi dùng.
- Container chạy bằng người dùng thường, filesystem chỉ đọc và bỏ toàn bộ Linux capabilities.
- Log chỉ ghi thông tin vận hành, không ghi nội dung hoặc tên file chứng từ.

Theo lựa chọn của bản thi, **không có**:

- đăng nhập và phân quyền;
- giới hạn số file, dung lượng file hoặc số trang PDF do ứng dụng đặt ra;
- quét malware;
- rate limit hoặc hạn mức AI;
- lưu file gốc lâu dài;
- database và Object Storage.

Vì vậy, ai biết đường link đều có thể dùng AI bằng key của bạn. Frontend cũng chứa sẵn mapping tài khoản và danh sách điều khoản NCC, nên người mở được trang có thể tải các dữ liệu đó từ mã JavaScript. Không cần thêm màn hình đăng nhập, nhưng với công cụ cá nhân bạn nên cho GreenNode vLB/security group chỉ nhận IP demo được phép; nếu giám khảo cần truy cập rộng thì mở trong đúng thời gian chấm rồi đóng lại.

Chỉ dùng file nội bộ mà bạn tin tưởng và nên tắt máy chủ sau cuộc thi.

Kết quả, mapping và quyết định vẫn nằm trong `localStorage` của đúng trình duyệt đang dùng. Xóa dữ liệu trình duyệt hoặc chuyển sang máy khác sẽ không tự mang dữ liệu theo; hãy dùng nút Sao lưu JSON/Xuất Excel trước khi kết thúc buổi thi.

File chứng từ mà backend biết đọc là PDF, PNG, JPG/JPEG, WebP, XML, TXT và CSV. “Không giới hạn” nói về số lượng, dung lượng và số trang; nó không biến một định dạng lạ thành định dạng backend hiểu được. File mapping vẫn dùng XLS/XLSX/CSV ở trình duyệt.

## Điều “không giới hạn” thật sự có nghĩa gì?

Ứng dụng và Nginx không tự đặt con số như “25 MB” hay “30 trang”. Nhưng không có chiếc hộp nào to vô tận:

- trình duyệt, RAM và ổ đĩa vẫn có sức chứa;
- GreenNode vLB có thể có giới hạn request hoặc idle timeout riêng;
- model `GreenNode/GreenMind-Medium-14B-R1` có context hữu hạn;
- MaaS có thể từ chối request quá lớn hoặc quá lâu;
- PDF rất dài phải được chia thành nhiều miếng, gọi AI nhiều lần rồi hợp nhất.

Vì thế câu đúng là: **“app không đặt giới hạn”**, không phải **“máy tính xử lý được file vô hạn”**.

## 1. Chuẩn bị GreenNode vServer

Một cấu hình khởi đầu dễ dùng cho buổi thi:

- Ubuntu LTS;
- 4 vCPU, 8 GB RAM;
- ít nhất 80 GB disk, và theo dõi dung lượng trong lúc demo;
- Docker Engine và Docker Compose plugin;
- firewall chỉ mở cổng SSH cho IP của bạn và cổng ứng dụng cho GreenNode vLB.

Nếu chưa có vLB, có thể mở cổng `8080` để thử bằng IP. Bản thử HTTP này không nên dùng để gửi chứng từ nhạy cảm.

## 2. Điền “chìa khóa AI”

Trong thư mục dự án, tạo `.env` từ file mẫu:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Điền ít nhất hai dòng được GreenNode cấp:

```dotenv
MAAS_BASE_URL=https://ENDPOINT-DO-GREENNODE-CAP/v1
MAAS_API_KEY=key-that-cua-ban
```

Model gợi ý ban đầu của gói này là:

```dotenv
MAAS_MODEL=GreenNode/GreenMind-Medium-14B-R1
```

Model trên là model text 32K trong ví dụ của GreenNode. Backend OCR tài liệu thành text trước khi gửi. GreenNode có thể đổi model hoặc endpoint khả dụng theo tài khoản, nên hãy dùng nút “Copy as code” trong Playground hoặc thông tin của nhà tài trợ để lấy đúng `MAAS_BASE_URL` và `MAAS_MODEL`.

Tài liệu công khai của GreenNode hiện minh họa một URL `http://`. Backend vẫn mặc định từ chối HTTP vì key và nội dung chứng từ sẽ đi không mã hóa. Chỉ khi GreenNode xác nhận endpoint tài trợ bắt buộc dùng HTTP mới đặt `ALLOW_INSECURE_MAAS_HTTP=true`; trước hết hãy yêu cầu endpoint HTTPS.

Không gửi `.env` qua chat, không commit nó vào Git và không cho API key vào ảnh chụp màn hình.

## 3. Xây và chạy

```bash
docker compose build --pull
docker compose up -d
docker compose ps
```

Mở:

```text
http://IP-CUA-VSERVER:8080
```

Nếu đổi `APP_HTTP_PORT`, dùng cổng mới.

## 4. Kiểm tra “các bạn nhỏ còn thức không”

Kiểm tra cửa trước:

```bash
curl -i http://127.0.0.1:8080/healthz
```

Kiểm tra API đang chạy:

```bash
curl -i http://127.0.0.1:8080/api/live
```

Kiểm tra API đã có đủ cấu hình MaaS:

```bash
curl -i http://127.0.0.1:8080/api/ready
```

`/api/live` chỉ nói “API còn sống”. `/api/ready` nói “API đã sẵn sàng làm bài”. Healthcheck không gọi MaaS, nên không tiêu token.

## 5. Bật HTTPS bằng GreenNode vLB

Cách dễ nhất:

1. Trỏ tên miền vào GreenNode vLB.
2. Gắn certificate TLS cho listener `443`.
3. Cho vLB chuyển tiếp tới private IP của vServer, cổng `8080`.
4. Chỉ cho security group của vServer nhận cổng `8080` từ vLB.
5. Đặt healthcheck của vLB là `/api/ready`.
6. Sau khi chắc chắn tên miền luôn có HTTPS, bỏ dấu `#` ở dòng HSTS trong `nginx.conf`, rồi build lại web.

Nếu vLB có body-size hoặc idle-timeout riêng, hãy chỉnh ở đó. `client_max_body_size 0` chỉ tắt giới hạn của Nginx trong gói này.

## 6. Xem log mà không nhìn vào giấy tờ

```bash
docker compose logs -f --tail=100 web api
```

Nginx chỉ ghi method, đường API, status, số byte và thời gian. API không nên ghi:

- tên hoặc nội dung file;
- OCR text;
- prompt và câu trả lời thô của model;
- eForm, MST, số tiền;
- header Authorization hoặc API key.

Docker tự xoay log, mỗi file tối đa 10 MB và giữ 3 file cho mỗi container.

## 7. File tạm đi đâu?

Docker tạo volume `po_pr_temp`. API đặt file đang xử lý ở `/work/tmp`, rồi xóa trong mọi trường hợp: thành công, lỗi hoặc người dùng hủy request. Các biến `TMPDIR`, `TMP` và `TEMP` cũng trỏ vào đây, nên file upload lớn không bị đẩy vào RAM. Khi container khởi động lại, API dọn ngay mọi thư mục `po-pr-request-*` còn sót. Cách dọn ngay này đúng với Compose một API replica; nếu sau này chạy nhiều replica dùng chung volume thì phải thêm thời gian chờ an toàn.

Volume nằm trên disk để PDF lớn không làm đầy RAM. Nhưng disk vẫn có thể đầy, nên trước buổi thi hãy kiểm tra:

```bash
df -h
docker system df
```

Không chạy `docker compose down -v` nếu chưa hiểu rõ: `-v` xóa cả volume tạm.

## 8. Smoke test trước khi lên sân khấu

Làm lần lượt:

1. Trang chủ và ba health endpoint trả về `200`.
2. Trong DevTools, không có request tới CDN hoặc MaaS từ trình duyệt.
3. Tìm trong source frontend không thấy API key.
4. Upload một PDF hơn 30 trang và kiểm tra trang sau trang 30 thực sự được đọc.
5. Upload một file hơn 25 MB và xác nhận không nhận lỗi `413` từ Nginx/vLB.
6. Đổi tên một file giả thành `.pdf`; backend phải từ chối vì magic bytes sai.
7. Dùng PDF hỏng; giao diện phải báo lỗi dễ hiểu và API vẫn sống.
8. Làm AI trả JSON sai; kết quả sai không được lưu.
9. Hủy upload giữa đường; thư mục tạm phải được dọn.
10. Xem log và chắc chắn không có tên/nội dung chứng từ hoặc API key.

## 9. Cập nhật và quay lại bản cũ

Trước mỗi lần cập nhật, ghi lại commit đang chạy:

```bash
git rev-parse HEAD
docker compose ps
```

Sau đó:

```bash
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose ps
```

Quay lại bản cũ nếu trang chủ/health hỏng, mẫu chứng từ tốt không chạy, JSON validation lỗi hàng loạt hoặc file tạm không được xóa:

```bash
git checkout COMMIT-CU
docker compose build
docker compose up -d
```

Với một bản thi quan trọng, tốt hơn là gắn tag cho commit tốt và thử rollback một lần trước ngày thi.

## 10. Lỗi thường gặp

### Docker báo thiếu `MAAS_BASE_URL` hoặc `MAAS_API_KEY`

Bạn chưa tạo `.env`, hoặc endpoint/key đang để trống. Điền đúng hai giá trị GreenNode cấp rồi chạy lại.

### `/api/live` là 200 nhưng `/api/ready` là 503

API còn sống nhưng thiếu base URL, key hoặc model. Kiểm tra `.env`, sau đó:

```bash
docker compose up -d --force-recreate api
```

### Upload nhận `413 Request Entity Too Large`

Nginx trong gói này đã tắt giới hạn. Lỗi có thể đến từ GreenNode vLB hoặc một proxy khác đứng phía trước.

### Upload rất lâu rồi timeout

PDF lớn cần OCR và nhiều lượt MaaS. Kiểm tra log, disk, cấu hình timeout của vLB và biến `MAAS_TIMEOUT_SECONDS`. Tăng timeout không làm context của model lớn hơn.

### API không ghi được `/work/tmp`

Kiểm tra volume:

```bash
docker compose config
docker compose exec api sh -c 'test -w /work/tmp && echo OK'
```

Container cố ý dùng user `10001` và filesystem chỉ đọc. Chỉ `/tmp` và `/work/tmp` được phép ghi.

## 11. Chạy bộ kiểm thử khi sửa code

Các thư viện chạy thật đã được khóa phiên bản trong `backend/requirements.txt`. Thư viện chỉ dùng để test nằm riêng trong `backend/requirements-dev.txt`, nên không bị đưa vào image API.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements-dev.txt
pytest -q
```

Kết quả của gói bàn giao này: `32 passed`.

## Tài liệu GreenNode tham khảo

- MaaS API: <https://helpdesk.greennode.ai/portal/en/kb/articles/greennode-maas-api>
- API docs/Playground: <https://aiplatform.console.greennode.ai/api-docs/maas>
- Danh sách model: <https://helpdesk.greennode.ai/portal/en/kb/articles/supported-models>

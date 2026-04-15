# QAPI Crypto Service

Project tách riêng từ QAPI cho các tính năng crypto:
- Bot theo dõi giá + tín hiệu (`/bots/run/{symbol}`)
- Dashboard symbol (`/bots/{symbol}`)
- Big trades dashboard (`/bots/big`)
- Bot scheduler chạy cố định 2 cặp: `ETHUSDT`, `BTCUSDT`

## Local run

```bash
cd crypto-service
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Mở:
- `http://localhost:8000/bots/ETHUSDT`
- `http://localhost:8000/bots/BTCUSDT`
- `http://localhost:8000/bots/big`
- `http://localhost:8000/health`

## ENV cần cho production

- `SUPABASE_URL`
- `SUPABASE_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`
- `RSI_PERIOD` (mặc định `14`)
- `TRACKER_INTERVAL` (mặc định `4h`)
- `TRACKER_CHECK_MINUTES` (mặc định `10`)
- `PUSHOVER_TOKEN` / `PUSHOVER_USER` / `PUSHOVER_DEVICE` (optional)

## Deploy lên Koyeb

### Cách 1: Deploy từ folder `crypto-service` với Dockerfile
1. Push code mới lên GitHub.
2. Trên Koyeb, tạo Web Service từ repo `quyendang/QAPI`.
3. Chọn Root Directory: `crypto-service`.
4. Koyeb sẽ build bằng `crypto-service/Dockerfile`.
5. Set ENV ở Koyeb theo danh sách bên trên.
6. Deploy và kiểm tra `/health`.

### Cách 2: Dùng `koyeb.yaml`
- Đặt root deploy là `crypto-service` và dùng file `koyeb.yaml` trong folder này.

## Ghi chú DB

Service này dùng các bảng Supabase đang có trong project cũ:
- `big_trades`

Nếu chưa có dữ liệu `big_trades`, trang `/bots/big` vẫn render nhưng sẽ hiện trạng thái chưa có data.

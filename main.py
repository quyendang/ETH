import os
import logging
import subprocess
import datetime
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = FastAPI()
templates = Jinja2Templates(directory="templates")

logging.basicConfig(level=logging.INFO)

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL và SUPABASE_KEY phải được thiết lập trong biến môi trường.")

supabase: Client = create_client(supabase_url, supabase_key)

# Thêm các biến môi trường cần thiết cho backup
# Bạn cần set các env var này trên Render.com:
# - POSTGRES_DB_URL: Connection string đầy đủ đến DB Supabase (ví dụ: postgresql://[user]:[password]@[host]:[port]/[dbname])
# - GOOGLE_DRIVE_FOLDER_ID: ID của folder trên Google Drive để upload backup
# - GOOGLE_SERVICE_ACCOUNT_KEY: Nội dung JSON của service account key (dán toàn bộ JSON string)

POSTGRES_DB_URL = os.environ.get("POSTGRES_DB_URL")
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
GOOGLE_SERVICE_ACCOUNT_KEY = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")

if not POSTGRES_DB_URL or not GOOGLE_DRIVE_FOLDER_ID or not GOOGLE_SERVICE_ACCOUNT_KEY:
    raise ValueError("POSTGRES_DB_URL, GOOGLE_DRIVE_FOLDER_ID, và GOOGLE_SERVICE_ACCOUNT_KEY phải được thiết lập.")

# Hàm để backup database
def backup_supabase_to_drive():
    try:
        # Tạo tên file backup với timestamp
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = f"supabase_backup_{timestamp}.sql"

        # Sử dụng pg_dump để dump database (giả sử pg_dump đã có sẵn; nếu không, cần install postgres client trên Render qua build command)
        # Lưu ý: Trên Render, bạn có thể thêm build command: apt-get update && apt-get install -y postgresql-client
        subprocess.run(["pg_dump", POSTGRES_DB_URL, "-f", backup_file], check=True)

        # Authenticate Google Drive API với service account
        credentials = service_account.Credentials.from_service_account_info(
            eval(GOOGLE_SERVICE_ACCOUNT_KEY),  # Chuyển JSON string thành dict
            scopes=["https://www.googleapis.com/auth/drive.file"]
        )
        service = build("drive", "v3", credentials=credentials)

        # Upload file lên Google Drive
        file_metadata = {
            "name": backup_file,
            "parents": [GOOGLE_DRIVE_FOLDER_ID]
        }
        media = MediaFileUpload(backup_file, mimetype="application/sql")
        service.files().create(body=file_metadata, media_body=media, fields="id").execute()

        # Xóa file tạm sau khi upload
        os.remove(backup_file)

        logging.info(f"Backup thành công: {backup_file} uploaded to Google Drive.")

    except Exception as e:
        logging.error(f"Lỗi khi backup: {str(e)}")

# Khởi tạo scheduler
scheduler = BackgroundScheduler()
scheduler.start()
scheduler.add_job(
    backup_supabase_to_drive,
    trigger=IntervalTrigger(hours=24),
    id="supabase_backup_job",
    name="Backup Supabase DB every 24 hours",
    replace_existing=True
)

# Chạy backup ngay lần đầu khi app start (tùy chọn, có thể comment nếu không cần)
backup_supabase_to_drive()

@app.get("/", response_class=HTMLResponse)
def homepage(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})

@app.get("/share", response_class=HTMLResponse)
async def share_lesson(
    request: Request,
    id: str = Query(..., description="Lesson short_id"),
    c: str = Query("", description="Ẩn nội dung cột khi hiển thị, vd: 1,2,4"),
    p: str = Query("", description="Ẩn nội dung cột khi in, vd: 4,5"),
):
    return await process_lesson(request, id, c, p)

@app.get("/{short_id}", response_class=HTMLResponse)
async def share_lesson_by_short_id(
    request: Request,
    short_id: str,
    c: str = Query("", description="Ẩn nội dung cột khi hiển thị, vd: 1,2,4"),
    p: str = Query("", description="Ẩn nội dung cột khi in, vd: 4,5"),
):
    return await process_lesson(request, short_id, c, p)

async def process_lesson(request: Request, short_id: str, c: str, p: str):
    try:
        # Lấy lesson theo short_id
        lesson_resp = (
            supabase.table("lessons")
            .select("id, name")
            .eq("short_id", short_id)
            .single()
            .execute()
        )

        if not lesson_resp.data:
            raise ValueError(f"Lesson with short_id={short_id} not found")

        lesson_id = lesson_resp.data["id"]
        lesson_name = lesson_resp.data.get("name", f"Lesson {short_id}")

        # Lấy words theo lesson_id
        response = (
            supabase.table("words")
            .select("*")
            .eq("lesson_id", lesson_id)
            .order("latest_update", desc=True)
            .execute()
        )

        words_list = [
            {
                "word": row.get("word"),
                "type": row.get("type"),
                "pronunciation": row.get("pronunciation"),
                "meaning": row.get("meaning"),
                "translate": row.get("translate"),
                "example": row.get("example"),
                "word_voice": row.get("word_voice"),
                "eg_voice": row.get("eg_voice"),
                "trans_voice": row.get("trans_voice"),
                "df_voice": row.get("df_voice"),
            }
            for row in response.data
        ]

        # Parse params c, p
        hide_columns = [int(x) for x in c.split(",") if x.isdigit()]
        hide_columns_print = [int(x) for x in p.split(",") if x.isdigit()]

    except Exception as e:
        logging.error(f"[ERROR] Fetching data: {str(e)}")
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error": str(e)
            },
        )

    return templates.TemplateResponse(
        "share.html",
        {
            "request": request,
            "words": words_list,
            "lesson_id": short_id,
            "lesson_name": lesson_name,
            "hide_columns": hide_columns,
            "hide_columns_print": hide_columns_print,
        },
    )

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        reload=True,
        workers=1
    )

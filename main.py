import os
import logging
import requests
import random
from datetime import datetime
from fastapi import FastAPI, Query, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler

app = FastAPI()
templates = Jinja2Templates(directory="templates")

logging.basicConfig(level=logging.INFO)

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL và SUPABASE_KEY phải được thiết lập trong biến môi trường.")

supabase: Client = create_client(supabase_url, supabase_key)

@app.get("/", response_class=HTMLResponse)
async def homepage(
    request: Request,
    userid: str | None = Query(None),
    groupid: str | None = Query(None),
    lessonid: str | None = Query(None),
    column: str | None = Query(None),
    print: str | None = Query(None),
    sort: str | None = Query(None)
):
    if userid:
        return templates.TemplateResponse(
            "firebase.html",
            {
                "request": request,
                "userid": userid,
                "groupid": groupid,
                "lessonid": lessonid,
                "column": column,
                "print": print,
                "sort": sort
            }
        )
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
            .eq("short_id", short_id.replace("!", ""))
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
            .order("latest_update", desc=False)
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

        # Shuffle words_list if short_id contains '!'
        if "!" in short_id:
            random.shuffle(words_list)

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

@app.get("/keys", response_class=HTMLResponse)
async def keys_page(request: Request):
    try:
        response = supabase.table("ttskeys").select("*").execute()
        keys = response.data
        for key in keys:
            if key["api_key"] and len(key["api_key"]) > 4:
                key["api_key"] = key["api_key"][:-4] + "****"
    except Exception as e:
        logging.error(f"[ERROR] Fetching keys: {str(e)}")
        keys = []
    return templates.TemplateResponse("key.html", {"request": request, "keys": keys})

@app.post("/keys")
async def add_key(
    request: Request,
    api_key: str = Form(...),
    base_url: str = Form(...),
    provider: str = Form(...)
):
    try:
        data = {
            "api_key": api_key,
            "base_url": base_url,
            "provider": provider,
            "created_at": "now()",
            "is_live": True,
            "balance": 0.0,
            "description": ""
        }
        response = supabase.table("ttskeys").insert(data).execute()
        return templates.TemplateResponse("key.html", {"request": request, "success": "Key added successfully", "keys": response.data})
    except Exception as e:
        logging.error(f"[ERROR] Adding key: {str(e)}")
        return templates.TemplateResponse("key.html", {"request": request, "error": str(e)})

@app.post("/delete_key/{id}")
async def delete_key(request: Request, id: str):
    try:
        response = supabase.table("ttskeys").delete().eq("id", id).execute()
        return templates.TemplateResponse("key.html", {"request": request, "success": "Key deleted successfully"})
    except Exception as e:
        logging.error(f"[ERROR] Deleting key: {str(e)}")
        return templates.TemplateResponse("key.html", {"request": request, "error": str(e)})

def update_elevenlabs_keys():
    try:
        # Lấy danh sách key từ ttskeys
        response = supabase.table("ttskeys").select("*").execute()
        keys = response.data

        for key in keys:
            if key["provider"] == "Elevenlabs":
                # Gọi API để lấy thông tin subscription
                api_url = "https://api.elevenlabs.io/v1/user/subscription"
                headers = {"xi-api-key": key["api_key"]}
                api_response = requests.get(api_url, headers=headers).json()

                # Tính toán và cập nhật
                character_limit = api_response["character_limit"]
                character_count = api_response["character_count"]
                balance = character_limit - character_count
                is_live = balance > 10
                next_reset = datetime.fromtimestamp(api_response["next_character_count_reset_unix"]).strftime('%Y-%m-%d %H:%M:%S')
                description = f"{api_response['tier']} - {next_reset}"

                # Cập nhật vào Supabase
                supabase.table("ttskeys").update({
                    "balance": balance,
                    "is_live": is_live,
                    "description": description
                }).eq("id", key["id"]).execute()

        logging.info("Updated Elevenlabs keys successfully")
    except Exception as e:
        logging.error(f"[ERROR] Updating Elevenlabs keys: {str(e)}")

update_elevenlabs_keys()
# Cấu hình scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(update_elevenlabs_keys, 'interval', hours=1)
scheduler.start()

if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        reload=True,
        workers=1
    )

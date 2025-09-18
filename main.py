import os
import logging
import requests
import random
import json
import base64
import time
import re
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Query, Request, Form, HTTPException, Response, Depends, Header
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler
from typing import Optional, List, Any, Dict
from pydantic import BaseModel


app = FastAPI()
templates = Jinja2Templates(directory="templates")
BASE_DIR = Path(__file__).resolve().parent
APP_ADS_PATH = BASE_DIR / "app-ads.txt"  # đổi nếu bạn để nơi khác
logging.basicConfig(level=logging.INFO)
supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")
supabase_service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
admin_api_key = os.environ.get("ADMIN_API_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL và SUPABASE_KEY phải được thiết lập trong biến môi trường.")

if not supabase_service_key or not admin_api_key:
    raise ValueError("SUPABASE_SERVICE_ROLE_KEY và ADMIN_API_KEY phải được thiết lập trong biến môi trường.")

supabase: Client = create_client(supabase_url, supabase_key)
supabase_admin: Client = create_client(supabase_url, supabase_service_key)

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

def verify_api_key(x_api_key: str | None = Header(default=None)):
    if x_api_key != admin_api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

class ClearResult(BaseModel):
    status: str
    userid: str
    rpc_result: Dict[str, Any] | None = None
    auth_deleted: bool
    note: str | None = None

class UserCounts(BaseModel):
    groups: int
    lessons: int
    words: int

class UserWithCounts(BaseModel):
    id: str
    email: Optional[str] = None
    counts: UserCounts

class UsersListResponse(BaseModel):
    page: int
    per_page: int
    total: int
    users: List[UserWithCounts]

class UserStatsResponse(BaseModel):
    userid: str
    email: str | None = None
    counts: Dict[str, int]

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

@app.get("/app-ads.txt", include_in_schema=False)
def get_app_ads():
    if not APP_ADS_PATH.exists():
        raise HTTPException(status_code=404, detail="app-ads.txt not found")
    # Gợi ý thêm Cache-Control 1 ngày
    headers = {"Cache-Control": "public, max-age=86400"}
    return FileResponse(APP_ADS_PATH, media_type="text/plain; charset=utf-8", headers=headers)


@app.get("/users", response_model=UsersListResponse)
async def list_users_rpc(
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=1000),
    search: str | None = Query(None, description="Lọc theo email (ILIKE)"),
    authorized: bool = Depends(verify_api_key),
):
    try:
        payload = {"p_page": page, "p_per_page": per_page, "p_search": search}
        resp = supabase_admin.rpc("admin_list_users", payload).execute()
        data = getattr(resp, "data", None)
        if not isinstance(data, dict):
            import json as _json
            data = _json.loads(data) if isinstance(data, str) else {}
        return UsersListResponse(**data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"admin_list_users failed: {e}")

@app.get("/user", response_model=UserStatsResponse)
async def get_user_stats_rpc(
    userid: str = Query(..., description="Supabase Auth user id (UUID)"),
    authorized: bool = Depends(verify_api_key),
):
    try:
        # (khuyến nghị) validate UUID như /remove
        resp = supabase_admin.rpc("admin_get_user_stats", {"target_user_id": userid}).execute()
        data = getattr(resp, "data", None)
        if not isinstance(data, dict):
            import json as _json
            data = _json.loads(data) if isinstance(data, str) else {}
        return UserStatsResponse(**data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"admin_get_user_stats failed: {e}")

@app.delete("/remove", response_model=ClearResult)
async def remove_user(
    userid: str = Query(..., description="Supabase Auth user id (UUID)"),
    authorized: bool = Depends(verify_api_key),
):
    """
    Xoá toàn bộ dữ liệu user bằng RPC admin_clear_user_data(target_user_id uuid)
    rồi xoá user khỏi Supabase Auth (admin). Thứ tự: RPC -> Auth.
    """
    if not UUID_RE.match(userid):
        raise HTTPException(status_code=422, detail="Invalid UUID format for userid")

    # 1) RPC xoá dữ liệu ứng dụng (security definer yêu cầu service_role)
    try:
        rpc_resp = supabase_admin.rpc("admin_clear_user_data", {"target_user_id": userid}).execute()
        if hasattr(rpc_resp, "model_dump"):
            raw = rpc_resp.model_dump()
            rpc_data = raw.get("data", raw)
        else:
            rpc_data = getattr(rpc_resp, "data", None) or {}
    except Exception as e:
        # Không xoá Auth nếu RPC thất bại để tránh mồ côi dữ liệu
        raise HTTPException(status_code=500, detail=f"RPC admin_clear_user_data failed: {e}")

    # 2) Xoá user khỏi Supabase Auth
    try:
        supabase_admin.auth.admin.delete_user(userid)
        auth_deleted = True
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Auth delete failed after RPC succeeded: {e}")

    return ClearResult(
        status="ok",
        userid=userid,
        rpc_result=rpc_data if isinstance(rpc_data, dict) else {"data": rpc_data},
        auth_deleted=auth_deleted,
        note="RPC done first, then Auth deleted.",
    )

@app.get("/share", response_class=HTMLResponse)
async def share_lesson(
    request: Request,
    id: str = Query(..., description="Lesson short_id"),
    c: str = Query("", description="Ẩn nội dung cột khi hiển thị, vd: 1,2,4"),
    p: str = Query("", description="Ẩn nội dung cột khi in, vd: 4,5"),
):
    return await process_lesson(request, id, c, p)


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

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return templates.TemplateResponse("fasteng-privacy-policy.html", {"request": request})

@app.get("/privacypolicy", response_class=HTMLResponse)
async def privacypolicy_page(request: Request):
    return templates.TemplateResponse("fasteng-privacy-policy.html", {"request": request})

@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    return templates.TemplateResponse("fasteng-terms.html", {"request": request})

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


@app.get("/{short_id}", response_class=HTMLResponse)
async def share_lesson_by_short_id(
    request: Request,
    short_id: str,
    c: str = Query("", description="Ẩn nội dung cột khi hiển thị, vd: 1,2,4"),
    p: str = Query("", description="Ẩn nội dung cột khi in, vd: 4,5"),
):
    return await process_lesson(request, short_id, c, p)
    
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

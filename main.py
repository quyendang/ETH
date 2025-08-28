import os
import logging
import requests
import random
import json
import base64
from datetime import datetime
from fastapi import FastAPI, Query, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler
from typing import Optional, List, Dict
import jwt
app = FastAPI()
templates = Jinja2Templates(directory="templates")

logging.basicConfig(level=logging.INFO)
APP_ID = "6749817128"  # cố định theo yêu cầu
ASC_API_BASE = "https://api.appstoreconnect.apple.com/v1"
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


def _load_private_key() -> str:
    """
    Trả về private key (.p8) dưới dạng string.
    Ưu tiên ASC_P8_KEY (raw hoặc base64). Nếu không có, dùng ASC_P8_PATH.
    """
    if P8_INLINE:
        # thử decode base64, nếu fail thì coi như raw
        try:
            return base64.b64decode(P8_INLINE).decode("utf-8")
        except Exception:
            return P8_INLINE
    if not P8_PATH:
        raise RuntimeError("Missing ASC_P8_PATH or ASC_P8_KEY")
    with open(P8_PATH, "r") as f:
        return f.read()


def make_jwt() -> str:
    if not ISSUER_ID or not KEY_ID:
        raise RuntimeError("Missing ASC_ISSUER_ID or ASC_KEY_ID")
    private_key = _load_private_key()
    now = int(time.time())
    payload = {
        "iss": ISSUER_ID,
        "exp": now + 20 * 60,  # token tối đa 20 phút
        "aud": "appstoreconnect-v1",
    }
    headers = {
        "kid": KEY_ID,
        "alg": "ES256",
        "typ": "JWT",
    }
    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)


def asc_get(url: str, token: str) -> Dict:
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


def fetch_all_builds_for_app(app_id: str, token: str, limit: int = 200) -> List[Dict]:
    """
    Lấy tất cả builds qua phân trang (links.next).
    """
    url = f"{ASC_API_BASE}/builds?filter[app]={app_id}&include=preReleaseVersion&limit={limit}"
    builds = []
    while True:
        data = asc_get(url, token)
        builds.extend(data.get("data", []))
        next_link = data.get("links", {}).get("next")
        if not next_link:
            break
        url = next_link
    return builds


def parse_iso(ts: Optional[str]):
    from datetime import datetime
    if not ts:
        return None
    try:
        # ví dụ: "2024-08-20T10:11:12Z"
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def version_key(v: Optional[str]):
    """
    Chuyển "1.10.3" -> (1,10,3) để sort; không phụ thuộc packaging.
    """
    if not v:
        return tuple()
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            # nếu có hậu tố (beta, rc), đẩy xuống sau số
            parts.append(float("inf"))
    return tuple(parts)


def pick_latest_build(builds: List[Dict]) -> Optional[Dict]:
    if not builds:
        return None

    # Ưu tiên build VALID & chưa hết hạn
    valid = [
        b for b in builds
        if b.get("attributes", {}).get("processingState") == "VALID"
        and b.get("attributes", {}).get("expired") is False
    ]
    pool = valid if valid else builds

    def sort_key(b: Dict):
        attr = b.get("attributes", {})
        up = parse_iso(attr.get("uploadedDate"))  # datetime hoặc None
        ver = version_key(attr.get("version"))
        try:
            bn = int(attr.get("buildNumber", "0"))
        except ValueError:
            bn = 0
        # sort theo uploadedDate trước, sau đó version, rồi buildNumber
        return (up or parse_iso("1970-01-01T00:00:00Z"), ver, bn)

    return sorted(pool, key=sort_key)[-1]


@app.get("/build")
def get_latest_build_version():
    """
    Trả về {"version": "<marketing_version>"} của build TestFlight mới nhất cho app_id 6749817128.
    """
    try:
        token = make_jwt()
        builds = fetch_all_builds_for_app(APP_ID, token)
        latest = pick_latest_build(builds)
        if not latest:
            raise HTTPException(status_code=404, detail="No builds found for the app.")
        version = latest.get("attributes", {}).get("version")
        if not version:
            raise HTTPException(status_code=502, detail="Latest build has no version field.")
        return {"version": version}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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

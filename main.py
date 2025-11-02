import os
import logging
import requests
import random
import json
import uuid
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
# ==== RSI BOT (Inline, Dual Symbols ETHUSDT+BTCUSDT) ==========================
import math
from fastapi import APIRouter

# --- Configuration via Environment Variables ---
PUSHOVER_TOKEN = os.getenv("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.getenv("PUSHOVER_USER", "")
PUSHOVER_DEVICE = os.getenv("PUSHOVER_DEVICE", "")  # optional

# You can override via env: RSI_SYMBOLS="ETHUSDT,BTCUSDT"
_RSI_SYMBOLS = [s.strip() for s in os.getenv("RSI_SYMBOLS", "ETHUSDT,BTCUSDT").split(",") if s.strip()]
if not _RSI_SYMBOLS:
    _RSI_SYMBOLS = ["ETHUSDT", "BTCUSDT"]

RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_CHECK_MINUTES = int(os.getenv("RSI_CHECK_MINUTES", "5"))

# Timeframes to check
_RSI_TIMEFRAMES = {"1h": "1h", "4h": "4h", "1d": "1d"}

# State & cache
_rsi_last_state = {sym: {tf: "unknown" for tf in _RSI_TIMEFRAMES.keys()} for sym in _RSI_SYMBOLS}
_rsi_last_values = {}   # {tf: {sym: {"price":..., "rsi":...}}}
_rsi_last_run = 0.0

_rsi_router = APIRouter()

def _rsi_wilder(closes, period=14):
    if len(closes) < period + 1:
        raise ValueError("Not enough data to compute RSI")
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))

def _rsi_fetch_klines(symbol, interval, limit=200):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()

def _rsi_latest(symbol, interval, period):
    kl = _rsi_fetch_klines(symbol, interval, limit=max(200, period*5))
    closes = [float(k[4]) for k in kl]
    rsi = _rsi_wilder(closes, period=period)
    price = closes[-1]
    return price, rsi

def _pushover_notify(title, message):
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        return
    data = {"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER, "title": title, "message": message, "priority": 0, sound: "cash"}
    if PUSHOVER_DEVICE:
        data["device"] = PUSHOVER_DEVICE
    try:
        requests.post("https://api.pushover.net/1/messages.json", data=data, timeout=15)
    except Exception:
        pass

def _fmt_dual(tf, condition, snapshot):
    # snapshot: {sym: {"price": p, "rsi": r}}
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") + "Z"
    lines = [f"TF: {tf} | Cond: {condition} | RSI({RSI_PERIOD}) | {ts}"]
    # Keep stable order: ETHUSDT first if present
    ordered = sorted(snapshot.items(), key=lambda kv: (0 if kv[0].upper()=="ETHUSDT" else 1, kv[0]))
    for sym, v in ordered:
        if "price" in v and "rsi" in v:
            lines.append(f"{sym}: Price {v['price']:.2f} | RSI {v['rsi']:.2f}")
        else:
            lines.append(f"{sym}: error {v.get('error','unknown')}")
    return "\n".join(lines)

def _rsi_check_once():
    global _rsi_last_state, _rsi_last_values, _rsi_last_run
    snap_all = {}  # per timeframe
    for tf, interval in _RSi_TIMEFRAMES if False else _RSI_TIMEFRAMES.items():
        tf_snap = {}
        # Fetch both symbols
        for sym in _RSI_SYMBOLS:
            try:
                price, rsi = _rsi_latest(sym, interval, RSI_PERIOD)
                tf_snap[sym] = {"price": price, "rsi": rsi}
            except Exception as e:
                tf_snap[sym] = {"error": str(e)}

        # Evaluate transitions per symbol; when any symbol crosses 30/70, push ONE notif per symbol that crossed,
        # including BOTH symbols' data in the message.
        for sym in _RSI_SYMBOLS:
            v = tf_snap.get(sym, {})
            rsi = v.get("rsi")
            if rsi is None:
                continue
            prev = _rsi_last_state.get(sym, {}).get(tf, "unknown")
            if rsi < 30 and prev != "oversold":
                _pushover_notify(f"RSI Oversold {tf} — {sym}", _fmt_dual(tf, "<30", tf_snap))
                _rsi_last_state[sym][tf] = "oversold"
            elif rsi > 70 and prev != "overbought":
                _pushover_notify(f"RSI Overbought {tf} — {sym}", _fmt_dual(tf, ">70", tf_snap))
                _rsi_last_state[sym][tf] = "overbought"
            elif 30 <= rsi <= 70 and prev != "normal":
                _rsi_last_state[sym][tf] = "normal"

        snap_all[tf] = tf_snap

    _rsi_last_values = snap_all
    _rsi_last_run = time.time()
    return snap_all

@_rsi_router.get("/rsi-status")
def rsi_status():
    return {
        "symbols": _RSI_SYMBOLS,
        "period": RSI_PERIOD,
        "timeframes": _RSI_TIMEFRAMES,
        "last_run_utc": datetime.utcfromtimestamp(_rsi_last_run).strftime("%Y-%m-%d %H:%M:%S") if _rsi_last_run else None,
        "values": _rsi_last_values,
        "state": _rsi_last_state,
        "check_every_minutes": RSI_CHECK_MINUTES,
    }

def init_inline_rsi_dual(app, scheduler=None):
    # Attach routes
    app.include_router(_rsi_router, prefix="/bots", tags=["bots"])
    if scheduler is not None:
        try:
            scheduler.add_job(_rsi_check_once, "interval", minutes=RSI_CHECK_MINUTES, id="rsi_check_dual", replace_existing=True, next_run_time=datetime.utcnow())
        except Exception:
            scheduler.add_job(_rsi_check_once, "interval", minutes=RSI_CHECK_MINUTES, id="rsi_check_dual", replace_existing=True)
    else:
        import threading
        def _loop():
            while True:
                try:
                    _rsi_check_once()
                except Exception:
                    pass
                time.sleep(RSI_CHECK_MINUTES * 60)
        threading.Thread(target=_loop, daemon=True).start()
# ==== /RSI BOT (Inline, Dual Symbols) =========================================



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
SALT = "548efb19-9741-4e81-9ad1-dddbe062649d"
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
    last_sign_in_at: Optional[str] = None
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
    # Yêu cầu có lessonid để sinh lesson_id
    if not lessonid:
        return templates.TemplateResponse("landing.html", {"request": request})

    # 1) Tạo lesson_id từ lessonid + SALT bằng uuid5 (namespace DNS)
    lesson_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, lessonid + SALT))

    # 2) Decode base64 cho column/print -> list[int]
    hide_columns = _decode_b64_csv_to_ints(column)
    hide_columns_print = _decode_b64_csv_to_ints(print)

    # 3) Render như /{short_id} nhưng truy vấn theo id
    return await _process_lesson_by_id(request, lesson_id, hide_columns, hide_columns_print)
    # if userid:
    #     return templates.TemplateResponse(
    #         "firebase.html",
    #         {
    #             "request": request,
    #             "userid": userid,
    #             "groupid": groupid,
    #             "lessonid": lessonid,
    #             "column": column,
    #             "print": print,
    #             "sort": sort
    #         }
    #     )
    # return templates.TemplateResponse("landing.html", {"request": request})

def _decode_b64_csv_to_ints(b64text: str | None) -> list[int]:
    """
    Giải mã base64 (URL-safe) -> chuỗi CSV -> list[int].
    Trả về [] nếu trống/không hợp lệ.
    """
    if not b64text:
        return []
    try:
        # Bổ sung padding cho chuẩn base64 nếu thiếu
        padding = "=" * (-len(b64text) % 4)
        raw = base64.urlsafe_b64decode((b64text + padding).encode("utf-8")).decode("utf-8")
        return [int(x) for x in raw.split(",") if x.strip().isdigit()]
    except Exception as ex:
        logging.warning(f"[WARN] Invalid base64 '{b64text}': {ex}")
        return []

async def _process_lesson_by_id(request: Request, lesson_id: str, hide_columns: list[int], hide_columns_print: list[int]):
    try:
        # Lấy lesson theo id
        lesson_resp = (
            supabase.table("lessons")
            .select("id, name")
            .eq("id", lesson_id)
            .single()
            .execute()
        )

        if not lesson_resp.data:
            raise ValueError(f"Lesson with id={lesson_id} not found")

        db_lesson_id = lesson_resp.data["id"]
        lesson_name = lesson_resp.data.get("name", f"Lesson {lesson_id}")

        # Lấy words theo lesson_id
        response = (
            supabase.table("words")
            .select("*")
            .eq("lesson_id", db_lesson_id)
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
            for row in (response.data or [])
        ]

    except Exception as e:
        logging.error(f"[ERROR] Fetching data by lesson_id: {str(e)}")
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "error": str(e)},
        )

    return templates.TemplateResponse(
        "share.html",
        {
            "request": request,
            "words": words_list,
            "lesson_id": lesson_id,           # hiển thị lesson_id đã sinh
            "lesson_name": lesson_name,
            "hide_columns": hide_columns,
            "hide_columns_print": hide_columns_print,
        },
    )

@app.get("/firebase", response_class=HTMLResponse)
async def firebase(
    request: Request,
    userid: str | None = Query(None),
    groupid: str | None = Query(None),
    lessonid: str | None = Query(None),
    column: str | None = Query(None, description="Base64 URL-safe chuỗi CSV, ví dụ: 'MSwyLDQ=' ~ '1,2,4'"),
    print: str | None = Query(None, description="Base64 URL-safe chuỗi CSV, ví dụ: 'NCw1' ~ '4,5'"),
    sort: str | None = Query(None)
):
    # Yêu cầu có lessonid để sinh lesson_id
    if not lessonid:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "error": "Missing required query param: lessonid"},
        )

    # 1) Tạo lesson_id từ lessonid + SALT bằng uuid5 (namespace DNS)
    lesson_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, lessonid + SALT))

    # 2) Decode base64 cho column/print -> list[int]
    hide_columns = _decode_b64_csv_to_ints(column)
    hide_columns_print = _decode_b64_csv_to_ints(print)

    # 3) Render như /{short_id} nhưng truy vấn theo id
    return await _process_lesson_by_id(request, lesson_id, hide_columns, hide_columns_print)


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

@app.delete("/removeData", response_model=ClearResult)
async def remove_user_data(
    userid: str = Query(..., description="Supabase Auth user id (UUID)"),
    authorized: bool = Depends(verify_api_key),
):
    """
    Xoá toàn bộ dữ liệu user bằng RPC admin_clear_user_data(target_user_id uuid)
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

    return ClearResult(
        status="ok",
        userid=userid,
        rpc_result=rpc_data if isinstance(rpc_data, dict) else {"data": rpc_data},
        auth_deleted=False,
        note="RPC only.",
    )



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

@app.get("/geteid")
def get_eid(version: str = "v9.2.0"):
    try:
        headers = {
            'Sec-Fetch-Site': 'none',
            'Connection': 'keep-alive',
            'Sec-Fetch-Mode': 'navigate',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148',
            'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
            'Sec-Fetch-Dest': 'document'
        }

        url = f'https://googleads.g.doubleclick.net/mads/static/sdk/native/sdk-core-v40.html?sdk=afma-sdk-i-{version}'
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()  # báo lỗi nếu HTTP code != 200

        html_content = response.text

        sdkLoaderEID_match = re.search(r'var sdkLoaderEID = "([^"]+)"', html_content)
        sdkLoaderEID2_match = re.search(r',e.includes\("([^"]+)"\)', html_content)

        sdkLoaderEID = sdkLoaderEID_match.group(1) if sdkLoaderEID_match else None
        sdkLoaderEID2 = sdkLoaderEID2_match.group(1) if sdkLoaderEID2_match else None

        return {"sdkLoaderEID": sdkLoaderEID, "sdkLoaderEID2": sdkLoaderEID2, "version": version}
    except Exception as e:
        logging.error(f"Error fetching EID values: {str(e)}")
        return {"sdkLoaderEID": "318502621", "sdkLoaderEID2": "318500618", "version": "v9.2.0"}

@app.get("/{short_id}", response_class=HTMLResponse)
async def share_lesson_by_short_id(
    request: Request,
    short_id: str,
    c: str = Query("", description="Ẩn nội dung cột khi hiển thị, vd: 1,2,4"),
    p: str = Query("", description="Ẩn nội dung cột khi in, vd: 4,5"),
):
    return await process_lesson(request, short_id, c, p)
    
# Cấu hình scheduler
scheduler = BackgroundScheduler()
init_inline_rsi_dual(app, scheduler)
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

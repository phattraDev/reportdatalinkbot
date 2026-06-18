import os
import sys
import base64
import hashlib
import hmac
import json
import time
import httpx
from datetime import datetime, date, timezone, timedelta
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database import get_db, Report, KnownName, BotUser, SummaryOverride, init_db
from cache import invalidate_user_cache

# Timezone offset (default UTC+7 for Cambodia/Thailand)
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "7"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))

from contextlib import asynccontextmanager
from telegram import Update

telegram_app = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("VERCEL") == "1":
        global telegram_app
        from bot.main import setup_application
        telegram_app = setup_application()
        await telegram_app.initialize()
        await telegram_app.start()
        yield
        if telegram_app:
            await telegram_app.stop()
            await telegram_app.shutdown()
    else:
        yield

app = FastAPI(title="Telegram Bot Dashboard API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "admin123")
TOKEN_TTL_DAYS = int(os.getenv("DASHBOARD_TOKEN_DAYS", "30"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
_TOKEN_SECRET = os.getenv(
    "DASHBOARD_SECRET",
    hashlib.sha256(f"{DASHBOARD_USER}:{DASHBOARD_PASS}".encode()).hexdigest(),
)


def _create_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": int(time.time()) + TOKEN_TTL_DAYS * 86400,
    }
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    sig = hmac.new(
        _TOKEN_SECRET.encode(), payload_b64.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload_b64}.{sig}"


def _verify_token_string(token: str) -> bool:
    if not token or "." not in token:
        return False
    payload_b64, sig = token.rsplit(".", 1)
    expected = hmac.new(
        _TOKEN_SECRET.encode(), payload_b64.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    pad = (-len(payload_b64)) % 4
    if pad:
        payload_b64 += "=" * pad
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (json.JSONDecodeError, ValueError):
        return False
    if payload.get("sub") != DASHBOARD_USER:
        return False
    if payload.get("exp", 0) < time.time():
        return False
    return True


@app.post("/api/login")
def login(payload: dict):
    username = payload.get("username", "")
    password = payload.get("password", "")
    if username == DASHBOARD_USER and password == DASHBOARD_PASS:
        return {"token": _create_token(username)}
    raise HTTPException(status_code=401, detail="Invalid credentials")


def verify_token(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not _verify_token_string(token):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _normalize_photo_url(photo_url: str | None) -> str | None:
    """Ensure stored Telegram avatar path is a usable absolute URL."""
    if not photo_url:
        return None
    clean = photo_url.strip()
    if not clean:
        return None
    if clean.startswith("http://") or clean.startswith("https://"):
        return clean
    if not BOT_TOKEN:
        return None
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{clean.lstrip('/')}"


def _fresh_photo_url_from_file_id(file_id: str | None) -> str | None:
    """Resolve a fresh Telegram file URL from a stored file_id."""
    if not file_id or not BOT_TOKEN:
        return None
    try:
        with httpx.Client(timeout=8.0) as client:
            r = client.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                params={"file_id": file_id},
            )
        data = r.json() if r.status_code == 200 else {}
        file_path = data.get("result", {}).get("file_path")
        if not file_path:
            return None
        return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path.lstrip('/')}"
    except Exception:
        return None


@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db), _=Depends(verify_token)):
    total_reports = db.query(Report).count()
    total_names = db.query(KnownName).count()
    today = date.today()
    today_reports = db.query(Report).filter(
        Report.created_at >= datetime(today.year, today.month, today.day)
    ).count()
    return {
        "total_reports": total_reports,
        "total_names": total_names,
        "today_reports": today_reports,
    }


@app.get("/api/reports")
def get_reports(limit: int = 50, db: Session = Depends(get_db), _=Depends(verify_token)):
    reports = db.query(Report).order_by(Report.created_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "username": r.username,
            "target_name": r.target_name,
            "link": r.link,
            "action": r.action,
            "action_detail": r.action_detail,
            "created_at": r.created_at.isoformat(),
        }
        for r in reports
    ]


@app.get("/api/names")
def get_names(db: Session = Depends(get_db), _=Depends(verify_token)):
    names = db.query(KnownName).order_by(KnownName.usage_count.desc()).all()
    return [
        {
            "id": n.id,
            "name": n.name,
            "usage_count": n.usage_count,
            "last_used": n.last_used.isoformat(),
        }
        for n in names
    ]


# In-memory override removed — overrides are now stored in DB via SummaryOverride table

@app.get("/api/summary/log")
def get_summary_log(db: Session = Depends(get_db), _=Depends(verify_token)):
    """Return all daily summaries — overrides where saved, auto-generated otherwise."""
    from collections import defaultdict
    from sqlalchemy import func

    # Get report counts per day (may be 0 for days where /summary already cleared them)
    report_rows = db.query(
        func.date(Report.created_at).label("day"),
        func.count(Report.id).label("total")
    ).group_by(func.date(Report.created_at)).all()
    report_totals = {str(r.day): r.total for r in report_rows}

    # Load all overrides — these are the primary source of truth for past days
    all_overrides = db.query(SummaryOverride).order_by(SummaryOverride.date_key.desc()).all()

    # Collect all unique dates: overrides + days with reports
    all_dates = sorted(
        set(list(report_totals.keys()) + [o.date_key for o in all_overrides]),
        reverse=True
    )

    override_map = {o.date_key: o.summary for o in all_overrides}

    result = []
    for day_str in all_dates:
        total = report_totals.get(day_str, 0)

        if day_str in override_map:
            # Use saved summary (either dashboard-edited or bot-generated after /summary)
            result.append({"date": day_str, "total": total, "summary": override_map[day_str], "edited": True})
        else:
            # Still has reports but no override yet — auto-generate live
            day_dt = datetime.strptime(day_str, "%Y-%m-%d")
            day_reports = db.query(Report).filter(
                Report.created_at >= day_dt,
                Report.created_at < day_dt + timedelta(days=1)
            ).all()
            name_links: dict = defaultdict(int)
            name_detail: dict = {}

            def _extract_num(detail):
                import re as _re
                m = _re.search(r"(\d+(?:\.\d+)?)", detail or "")
                return float(m.group(1)) if m else 0.0

            for r in day_reports:
                name_links[r.target_name] += 1
                if r.action_detail:
                    current = name_detail.get(r.target_name, "")
                    if _extract_num(r.action_detail) >= _extract_num(current):
                        name_detail[r.target_name] = r.action_detail

            display_date = day_dt.strftime("%d/%m/%Y")
            lines = [f"+គោរពរាយការណ៍ជូនមេ របាយការណ៍ការការងារ Link ថ្ងៃ {display_date}", "", "+ ការងារ : comment"]
            for name, count in name_links.items():
                detail = name_detail.get(name, "")
                lines.append("")
                lines.append(f"- ការងារ: Link {name} {count} link ក្នុង 1 link បានដាក់ចេញ{' ' + detail if detail else ''}")
            lines.append("")
            lines.append(f"សរុបមានចំនួន {sum(name_links.values())} link")
            lines.append("សូមគោរពអរគុណមេ🙏🙏")
            result.append({"date": day_str, "total": total, "summary": "\n".join(lines), "edited": False})

    return result


@app.delete("/api/summary/day/{date_key}")
def delete_day_summary(date_key: str, db: Session = Depends(get_db), _=Depends(verify_token)):
    """Delete all reports and any override for a given day (YYYY-MM-DD)."""
    try:
        day_dt = datetime.strptime(date_key, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")
    db.query(Report).filter(
        Report.created_at >= day_dt,
        Report.created_at < day_dt + timedelta(days=1)
    ).delete()
    db.query(SummaryOverride).filter(SummaryOverride.date_key == date_key).delete()
    db.commit()
    return {"success": True}


@app.delete("/api/summary/today")
def reset_today_summary(db: Session = Depends(get_db), _=Depends(verify_token)):
    today = date.today().isoformat()
    db.query(SummaryOverride).filter(SummaryOverride.date_key == today).delete()
    db.commit()
    return {"success": True}


@app.put("/api/summary/today")
def update_today_summary(payload: dict, db: Session = Depends(get_db), _=Depends(verify_token)):
    text = payload.get("summary", "")
    today = date.today().isoformat()
    row = db.query(SummaryOverride).filter(SummaryOverride.date_key == today).first()
    if row:
        row.summary = text
        row.updated_at = datetime.utcnow()
    else:
        db.add(SummaryOverride(date_key=today, summary=text))
    db.commit()
    return {"success": True}


@app.get("/api/summary/today")
def get_today_summary(db: Session = Depends(get_db), _=Depends(verify_token)):
    from collections import defaultdict
    today = date.today()

    # Return manually edited override if present in DB
    override = db.query(SummaryOverride).filter(SummaryOverride.date_key == today.isoformat()).first()
    if override:
        total = sum(1 for line in override.summary.splitlines() if line.strip().startswith("- ការងារ:"))
        return {"summary": override.summary, "total": total}

    reports = db.query(Report).filter(
        Report.created_at >= datetime(today.year, today.month, today.day)
    ).all()

    # Use local timezone for display
    today_local = datetime.now(LOCAL_TZ).date()
    today_str = today_local.strftime("%d/%m/%Y")
    lines = [f"+គោរពរាយការណ៍ជូនមេ របាយការណ៍ការការងារ Link ថ្ងៃ {today_str}", "", "+ ការងារ : comment"]

    name_links: dict = defaultdict(int)
    name_detail: dict = {}

    def _extract_num(detail: str) -> float:
        """Extract the leading number from a detail string like '30 cm like' → 30.0"""
        import re as _re
        m = _re.search(r"(\d+(?:\.\d+)?)", detail or "")
        return float(m.group(1)) if m else 0.0

    for r in reports:
        name_links[r.target_name] += 1
        if r.action_detail:
            current = name_detail.get(r.target_name, "")
            if _extract_num(r.action_detail) >= _extract_num(current):
                name_detail[r.target_name] = r.action_detail

    for name, count in name_links.items():
        detail = name_detail.get(name, "")
        lines.append("")
        lines.append(f"- ការងារ: Link {name} {count} link ក្នុង 1 link បានដាក់ចេញ{' ' + detail if detail else ''}")

    total = sum(name_links.values())
    lines.append("")
    lines.append(f"សរុបមានចំនួន {total} link")
    lines.append("សូមគោរពអរគុណមេ🙏🙏")

    return {"summary": "\n".join(lines), "total": total}


@app.post("/api/names")
def create_name(payload: dict, db: Session = Depends(get_db), _=Depends(verify_token)):
    name = payload.get("name", "").strip()
    if not name:
        return {"error": "Name is required"}
    existing = db.query(KnownName).filter(KnownName.name == name).first()
    if existing:
        return {"error": "Name already exists"}
    db.add(KnownName(name=name))
    db.commit()
    return {"success": True}


@app.delete("/api/names/{name_id}")
def delete_name(name_id: int, db: Session = Depends(get_db), _=Depends(verify_token)):
    name = db.query(KnownName).filter(KnownName.id == name_id).first()
    if not name:
        return {"error": "Not found"}
    db.delete(name)
    db.commit()
    return {"success": True}


@app.get("/api/users")
def get_users(db: Session = Depends(get_db), _=Depends(verify_token)):
    users = db.query(BotUser).order_by(BotUser.last_seen.desc()).all()
    has_updates = False
    for u in users:
        fresh = _fresh_photo_url_from_file_id(u.photo_file_id)
        final_url = fresh or _normalize_photo_url(u.photo_url)
        if final_url != u.photo_url:
            u.photo_url = final_url
            has_updates = True
    if has_updates:
        db.commit()
    return [
        {
            "id": u.id,
            "user_id": u.user_id,
            "username": u.username,
            "photo_url": u.photo_url,
            "allowed": u.allowed,
            "first_seen": u.first_seen.isoformat(),
            "last_seen": u.last_seen.isoformat(),
        }
        for u in users
    ]


@app.patch("/api/users/{user_id}/set")
async def set_user_access(user_id: int, payload: dict, db: Session = Depends(get_db), _=Depends(verify_token)):
    import httpx
    u = db.query(BotUser).filter(BotUser.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    
    new_state = payload.get("allowed")
    if not isinstance(new_state, bool):
        raise HTTPException(status_code=400, detail="allowed must be true or false")
    
    u.allowed = new_state
    db.commit()

    # Invalidate cache so bot picks up new permission immediately
    invalidate_user_cache(u.user_id)

    # Notify user when approved
    if u.allowed:
        BOT_TOKEN = os.getenv("BOT_TOKEN")
        msg = "✅ អ្នកត្រូវបានអនុម័តហើយ! សូមប្រើ /start ដើម្បីចាប់ផ្តើម។\nYou have been approved! Use /start to begin."
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": u.user_id, "text": msg}
                )
        except Exception:
            pass

    return {"user_id": u.user_id, "allowed": u.allowed}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), _=Depends(verify_token)):
    u = db.query(BotUser).filter(BotUser.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    db.delete(u)
    db.commit()
    return {"success": True}


@app.get("/api/health")
@app.get("/health")
def health():
    """Public ping target — use with cron/UptimeRobot to prevent free-tier spin-down."""
    return {"ok": True}


@app.post("/webhook")
async def telegram_webhook(request: Request):
    if not telegram_app:
        raise HTTPException(status_code=503, detail="Telegram application not initialized")
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return {"ok": False}


@app.get("/api/set_webhook")
async def set_webhook(url: str, _=Depends(verify_token)):
    if not telegram_app:
        raise HTTPException(status_code=503, detail="Telegram application not initialized")
    if not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Webhook URL must use HTTPS")
    webhook_url = f"{url.rstrip('/')}/webhook"
    result = await telegram_app.bot.set_webhook(webhook_url)
    return {"success": result, "url": webhook_url}


# Serve dashboard
dashboard_path = os.path.join(os.path.dirname(__file__), "..", "dashboard")
app.mount("/", StaticFiles(directory=dashboard_path, html=True), name="dashboard")

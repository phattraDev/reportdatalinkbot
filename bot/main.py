import os
import sys
import re
import logging
import time
import asyncio
from datetime import datetime, date, timezone, timedelta
from collections import defaultdict

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.request import HTTPXRequest

load_dotenv()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import SessionLocal, Report, KnownName, BotUser, SummaryOverride, init_db
from bot.parser import parse_report_message
from cache import get_user_cache

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Comma-separated chat IDs to forward summary to, e.g. "-1001234567890,-1009876543210"
_raw_forward = os.getenv("FORWARD_CHAT_IDS", os.getenv("FORWARD_CHAT_ID", ""))
FORWARD_CHAT_IDS: list[int] = [
    int(x.strip()) for x in _raw_forward.split(",") if x.strip().lstrip("-").isdigit()
]
# Timezone offset (default UTC+7 for Cambodia/Thailand)
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "7"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))

_processed_ids: set[int] = set()
_user_cache = get_user_cache()  # Shared cache
CACHE_TTL = 300  # 5 minutes

RESERVED = {"names", "summary", "start", "clear", "help", "forward"}


# ── DB helpers ────────────────────────────────────────────────────────────────

def save_report(parsed: dict, user_id: str, username: str, raw: str) -> bool:
    db = SessionLocal()
    try:
        report = Report(
            user_id=user_id,
            username=username,
            target_name=parsed["target_name"],
            link=parsed["link"],
            action=parsed["action"],
            action_detail=parsed["action_detail"],
            raw_message=raw,
        )
        db.add(report)
        known = db.query(KnownName).filter(KnownName.name == parsed["target_name"]).first()
        if known:
            known.usage_count += 1
            known.last_used = datetime.utcnow()
        else:
            db.add(KnownName(name=parsed["target_name"]))
        db.commit()
        return True
    finally:
        db.close()


def register_user(user_id: str, username: str) -> tuple[bool, bool, bool]:
    """Register user if new, update last_seen. Returns (allowed, is_new, needs_photo_refresh)."""
    # Check cache first
    now = time.time()
    if user_id in _user_cache:
        allowed, cached_time = _user_cache[user_id]
        if now - cached_time < CACHE_TTL:
            return (allowed, False, False)
    
    db = SessionLocal()
    try:
        u = db.query(BotUser).filter(BotUser.user_id == user_id).first()
        if u:
            u.last_seen = datetime.utcnow()
            u.username = username
            db.commit()
            _user_cache[user_id] = (u.allowed, now)
            needs_photo_refresh = not bool(u.photo_file_id)
            return (u.allowed, False, needs_photo_refresh)
        else:
            db.add(BotUser(user_id=user_id, username=username, allowed=False))
            db.commit()
            _user_cache[user_id] = (False, now)
            return (False, True, True)
    finally:
        db.close()


def _build_photo_url(file_path: str | None) -> str | None:
    """Normalize Telegram file path into an absolute URL."""
    if not file_path:
        return None
    if file_path.startswith("http://") or file_path.startswith("https://"):
        return file_path
    if not BOT_TOKEN:
        return None
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path.lstrip('/')}"


async def fetch_and_store_photo(bot, user_id: str):
    """Fetch Telegram profile photo and store the URL."""
    try:
        photos = await bot.get_user_profile_photos(user_id=int(user_id), limit=1)
        logger.info(f"[PHOTO] user={user_id} total={photos.total_count}")
        if photos.total_count == 0:
            return
        file_id = photos.photos[0][0].file_id
        file = await bot.get_file(file_id)
        photo_url = _build_photo_url(file.file_path)
        if not photo_url:
            logger.warning(f"[PHOTO] user={user_id} no usable file path")
            return
        logger.info(f"[PHOTO] user={user_id} url={photo_url}")
        db = SessionLocal()
        try:
            u = db.query(BotUser).filter(BotUser.user_id == user_id).first()
            if u:
                u.photo_file_id = file_id
                u.photo_url = photo_url
                db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[PHOTO] failed for user={user_id}: {e}")


def get_today_reports():
    db = SessionLocal()
    try:
        today = date.today()
        return db.query(Report).filter(
            Report.created_at >= datetime(today.year, today.month, today.day)
        ).all()
    finally:
        db.close()


async def save_report_async(parsed: dict, user_id: str, username: str, raw: str, msg):
    """Save report in background and react to message."""
    try:
        saved = save_report(parsed, user_id, username, raw)
        if saved:
            await msg.set_reaction("✍")
    except Exception as e:
        logger.error(f"Failed to save report: {e}")


async def save_report_async_callback(parsed: dict, user_id: str, username: str, raw: str, bot, chat_id: int, msg_id: int):
    """Save report from callback and react to original message."""
    try:
        saved = save_report(parsed, user_id, username, raw)
        if saved and msg_id:
            try:
                await bot.set_message_reaction(
                    chat_id=chat_id,
                    message_id=msg_id,
                    reaction=["✍"]
                )
            except Exception as e:
                logger.warning(f"Failed to react: {e}")
    except Exception as e:
        logger.error(f"Failed to save report from callback: {e}")




def _build_name_picker(known_names: list[KnownName]) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for n in known_names:
        row.append(InlineKeyboardButton(n.name, callback_data=f"pick:{n.name}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✏️ វាយឈ្មោះផ្ទាល់", callback_data="pick:__manual__")])
    return InlineKeyboardMarkup(buttons)


def _get_known_names() -> list[KnownName]:
    db = SessionLocal()
    try:
        return db.query(KnownName).order_by(KnownName.usage_count.desc()).all()
    finally:
        db.close()


# ── Handlers ──────────────────────────────────────────────────────────────────

async def handle_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    # Log chat_id for debugging
    if msg.chat:
        logger.info(f"[CHAT_ID] chat_id={msg.chat.id} chat_type={msg.chat.type} chat_title={msg.chat.title or 'N/A'}")

    # Fast duplicate check
    if msg.message_id in _processed_ids:
        return
    _processed_ids.add(msg.message_id)
    if len(_processed_ids) > 500:
        _processed_ids.clear()

    # Fast user check
    user = update.effective_user
    if not user:
        return
    
    allowed, is_new, needs_photo_refresh = register_user(str(user.id), user.username or user.full_name)
    if is_new or needs_photo_refresh:
        # Run photo fetch in background, don't wait
        asyncio.create_task(fetch_and_store_photo(context.bot, str(user.id)))
    if not allowed:
        return  # silently ignore blocked users

    text = msg.text or msg.caption or ""
    if not text:
        return

    # Fast command check — skip ALL slash commands, let CommandHandler handle them
    if text.startswith('/'):
        first_token = text.strip().split()[0].lower()
        # Only continue if it looks like a /name + link report (has newline or URL)
        if '\n' not in text and 'http' not in text:
            return

    # Fast content check - combine regex
    has_url = 'http' in text
    has_slash_name = '\n/' in text or text.startswith('/')
    is_photo = bool(msg.photo or msg.document)

    if not has_url and not has_slash_name and not is_photo:
        return

    # Only parse if we have potential content
    parsed = parse_report_message(text)
    if not parsed:
        logger.info(f"[SKIP] Failed to parse message")
        return

    logger.info(f"[PARSED] name={parsed['target_name']} link={parsed['link'][:50] if parsed['link'] else 'None'}")

    # No name found but has URL → show inline name picker
    if parsed["target_name"] == "មិនស្គាល់" and has_url:
        known = _get_known_names()
        if known:
            context.user_data["pending_link"] = parsed["link"]
            context.user_data["pending_raw"] = text
            context.user_data["pending_msg_id"] = msg.message_id
            await msg.reply_text(
                "🔗 Link\n👇 ជ្រើសរើសឈ្មោះ:",
                reply_markup=_build_name_picker(known),
                reply_to_message_id=msg.message_id,
            )
            return

    # Save report in background
    asyncio.create_task(save_report_async(parsed, str(user.id), user.username or user.full_name, text, msg))


async def pick_name_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "pick:__manual__":
        await query.edit_message_text("✏️ សូមវាយ /ឈ្មោះ ហើយផ្ញើ link ម្តងទៀត")
        context.user_data.pop("pending_link", None)
        context.user_data.pop("pending_raw", None)
        context.user_data.pop("pending_msg_id", None)
        context.user_data.pop("pending_selected_name", None)
        return

    name = query.data.replace("pick:", "", 1)
    link = context.user_data.pop("pending_link", None)
    raw = context.user_data.pop("pending_raw", "")
    original_msg_id = context.user_data.pop("pending_msg_id", None)
    if not link:
        # Recover state when user is re-picking name from confirmation step.
        pending = context.user_data.get("pending_selected_name") or {}
        link = pending.get("link")
        raw = raw or pending.get("raw", "")
        original_msg_id = original_msg_id or pending.get("msg_id")

    logger.info(f"[CALLBACK] name={name} link={link[:50] if link else 'None'} msg_id={original_msg_id}")

    if not link:
        await query.edit_message_text("⚠️ Link បានផុតកំណត់ សូមផ្ញើម្តងទៀត")
        return

    context.user_data["pending_selected_name"] = {
        "name": name,
        "link": link,
        "raw": raw,
        "msg_id": original_msg_id,
    }

    confirm_buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ បញ្ជាក់", callback_data="name_confirm:ok")],
        [InlineKeyboardButton("🔁 ប្តូរឈ្មោះ", callback_data="name_confirm:change")],
        [InlineKeyboardButton("❌ បោះបង់", callback_data="name_confirm:cancel")],
    ])
    await query.edit_message_text(
        f"ឈ្មោះដែលបានជ្រើស៖ {name}\nតើចង់បញ្ជាក់ឬប្តូរឈ្មោះ?",
        reply_markup=confirm_buttons,
    )


async def name_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    action = query.data.replace("name_confirm:", "", 1)
    logger.info(f"[NAME_CONFIRM] action={action}")
    pending = context.user_data.get("pending_selected_name")

    if not pending:
        await query.edit_message_text("⚠️ ព័ត៌មានផុតកំណត់។ សូមផ្ញើ link ម្តងទៀត។")
        return

    if action == "change":
        known = _get_known_names()
        if not known:
            await query.edit_message_text("មិនទាន់មានឈ្មោះណាមួយទេ។")
            return
        context.user_data["pending_link"] = pending.get("link")
        context.user_data["pending_raw"] = pending.get("raw", "")
        context.user_data["pending_msg_id"] = pending.get("msg_id")
        await query.edit_message_text(
            "🔁 សូមជ្រើសឈ្មោះថ្មី:",
            reply_markup=_build_name_picker(known),
        )
        return

    if action == "cancel":
        context.user_data.pop("pending_selected_name", None)
        await query.edit_message_text("បានបោះបង់។ សូមផ្ញើ link ម្តងទៀត។")
        return

    name = pending.get("name")
    raw = pending.get("raw", "")
    original_msg_id = pending.get("msg_id")
    parsed = parse_report_message(f"/{name}\n{raw}")
    if not parsed:
        parsed = {
            "target_name": name,
            "link": pending.get("link"),
            "action": "comment",
            "action_detail": "",
        }

    user = query.from_user
    asyncio.create_task(save_report_async_callback(
        parsed, str(user.id), user.username or user.full_name, raw,
        context.bot, query.message.chat_id, original_msg_id
    ))
    context.user_data.pop("pending_selected_name", None)
    try:
        await query.message.delete()
    except Exception:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


async def unknown_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ack any unmatched callback so Telegram UI never hangs."""
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer("សូមព្យាយាមម្តងទៀត", show_alert=False)
    except Exception:
        pass
    logger.warning(f"[CALLBACK_UNKNOWN] data={query.data}")


def _build_summary_text(reports) -> str:
    """Build the summary text from a list of Report objects."""
    today_local = datetime.now(LOCAL_TZ).date()
    today_str = today_local.strftime("%d/%m/%Y")
    lines = [f"+គោរពរាយការណ៍ជូនមេ របាយការណ៍ការការងារ Link ថ្ងៃ {today_str}", "", "+ ការងារ : comment"]

    name_links: dict = defaultdict(int)
    name_detail: dict = {}

    def _extract_num(detail: str) -> float:
        m = re.search(r"(\d+(?:\.\d+)?)", detail or "")
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

    lines.append("")
    lines.append(f"សរុបមានចំនួន {sum(name_links.values())} link")
    lines.append("សូមគោរពអរគុណមេ🙏🙏")
    return "\n".join(lines)


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        allowed, is_new, needs_photo_refresh = register_user(str(user.id), user.username or user.full_name)
        if is_new or needs_photo_refresh:
            asyncio.create_task(fetch_and_store_photo(context.bot, str(user.id)))
        if not allowed:
            return

    today = date.today()

    # Check for a dashboard-edited override first
    db = SessionLocal()
    try:
        override = db.query(SummaryOverride).filter(SummaryOverride.date_key == today.isoformat()).first()
        if override:
            await update.message.reply_text(override.summary)
            db.query(Report).filter(Report.created_at >= datetime(today.year, today.month, today.day)).delete()
            db.commit()
            return
    finally:
        db.close()

    reports = get_today_reports()
    if not reports:
        await update.message.reply_text("មិនមានរបាយការណ៍សម្រាប់ថ្ងៃនេះទេ។")
        return

    summary_text = _build_summary_text(reports)
    await update.message.reply_text(summary_text)

    # Save to override so Daily Log retains it, then delete reports
    db = SessionLocal()
    try:
        today = date.today()
        row = db.query(SummaryOverride).filter(SummaryOverride.date_key == today.isoformat()).first()
        if row:
            row.summary = summary_text
            row.updated_at = datetime.utcnow()
        else:
            db.add(SummaryOverride(date_key=today.isoformat(), summary=summary_text))
        db.query(Report).filter(Report.created_at >= datetime(today.year, today.month, today.day)).delete()
        db.commit()
    finally:
        db.close()



async def clear_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        allowed, is_new, needs_photo_refresh = register_user(str(user.id), user.username or user.full_name)
        if is_new or needs_photo_refresh:
            asyncio.create_task(fetch_and_store_photo(context.bot, str(user.id)))
        if not allowed:
            return  # silently ignore blocked users
    
    db = SessionLocal()
    try:
        today = date.today()
        deleted = db.query(Report).filter(
            Report.created_at >= datetime(today.year, today.month, today.day)
        ).delete()
        db.commit()
        await update.message.reply_text(f"🗑️ បានលុប {deleted} entries។")
    finally:
        db.close()


async def forward_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"[FORWARD] Handler called by user={update.effective_user.id if update.effective_user else 'unknown'}")
    user = update.effective_user
    if user:
        allowed, is_new, needs_photo_refresh = register_user(str(user.id), user.username or user.full_name)
        if is_new or needs_photo_refresh:
            asyncio.create_task(fetch_and_store_photo(context.bot, str(user.id)))
        if not allowed:
            logger.info(f"[FORWARD] User {user.id} not allowed")
            return

    if not FORWARD_CHAT_IDS:
        await update.message.reply_text(
            "⚠️ មិនទាន់កំណត់ FORWARD_CHAT_IDS ទេ។\n"
            "Add FORWARD_CHAT_IDS=-100xxxxxxxxx in your .env file.\n"
            "(Comma-separate multiple IDs)"
        )
        return

    today = date.today()
    today_local = datetime.now(LOCAL_TZ).date()
    today_str = today_local.strftime("%d/%m/%Y")
    bot = context.bot

    # Use override if available, otherwise build from live reports
    db = SessionLocal()
    try:
        override = db.query(SummaryOverride).filter(SummaryOverride.date_key == today.isoformat()).first()
        override_text = override.summary if override else None
    finally:
        db.close()

    reports = get_today_reports()

    if not reports and not override_text:
        await update.message.reply_text("មិនមានរបាយការណ៍សម្រាប់ថ្ងៃនេះទេ។")
        return

    # Build per-name detail from live reports (for individual link sections)
    name_reports: dict = defaultdict(list)
    name_detail: dict = {}

    def _extract_num(detail: str) -> float:
        m = re.search(r"(\d+(?:\.\d+)?)", detail or "")
        return float(m.group(1)) if m else 0.0

    for r in reports:
        name_reports[r.target_name].append(r)
        if r.action_detail:
            current = name_detail.get(r.target_name, "")
            if _extract_num(r.action_detail) >= _extract_num(current):
                name_detail[r.target_name] = r.action_detail

    total_links = sum(len(v) for v in name_reports.values()) if name_reports else 0

    # Final summary text — prefer override (dashboard-edited), else build fresh
    summary_text = override_text or _build_summary_text(reports)

    sent, failed = 0, 0
    for chat_id in FORWARD_CHAT_IDS:
        try:
            # ── Header ──────────────────────────────────────────────────────
            header = (
                f"📋 *របាយការណ៍ការងារ Link ថ្ងៃ {today_str}*\n"
                f"សរុប: *{total_links} link* | {len(name_reports)} ឈ្មោះ"
            )
            await bot.send_message(chat_id=chat_id, text=header, parse_mode="Markdown")
            await asyncio.sleep(0.4)

            # ── Per-name sections with individual links ──────────────────────
            for name, reps in name_reports.items():
                detail = name_detail.get(name, "")
                section_lines = [f"👤 *{name}* — {len(reps)} link{(' | ' + detail) if detail else ''}"]
                for i, r in enumerate(reps, 1):
                    if r.link:
                        # Escape special MarkdownV2 chars in the link
                        safe_link = r.link.replace(".", "\\.").replace("-", "\\-").replace("_", "\\_")
                        section_lines.append(f"{i}\\. {safe_link}")
                await bot.send_message(
                    chat_id=chat_id,
                    text="\n".join(section_lines),
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True,
                )
                await asyncio.sleep(0.3)

            # ── Final summary total ──────────────────────────────────────────
            await bot.send_message(
                chat_id=chat_id,
                text=summary_text,
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception as e:
            logger.warning(f"[FORWARD] failed chat_id={chat_id}: {e}")
            failed += 1

    if failed == 0:
        await update.message.reply_text(
            f"✅ បានផ្ញើ {total_links} link ({len(name_reports)} ឈ្មោះ) "
            f"ទៅ {sent} chat{'s' if sent > 1 else ''} ដោយជោគជ័យ។"
        )
    else:
        await update.message.reply_text(
            f"⚠️ ផ្ញើបាន {sent} chat, បរាជ័យ {failed} chat។\nCheck bot logs for details."
        )

async def names_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        allowed, is_new, needs_photo_refresh = register_user(str(user.id), user.username or user.full_name)
        if is_new or needs_photo_refresh:
            asyncio.create_task(fetch_and_store_photo(context.bot, str(user.id)))
        if not allowed:
            return  # silently ignore blocked users
    
    db = SessionLocal()
    try:
        known = db.query(KnownName).order_by(KnownName.usage_count.desc()).all()
    finally:
        db.close()
    if not known:
        await update.message.reply_text("មិនទាន់មានឈ្មោះណាមួយទេ។")
        return
    names_text = "\n".join([f"• {n.name}" for n in known])
    await update.message.reply_text(f"📋 ឈ្មោះទាំងអស់:\n{names_text}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        allowed, is_new, needs_photo_refresh = register_user(str(user.id), user.username or user.full_name)
        # Force-refresh on /start so existing users can recover stale/broken avatars
        # without needing admin deletion/re-registration.
        asyncio.create_task(fetch_and_store_photo(context.bot, str(user.id)))
        if not allowed:
            await update.message.reply_text(
                "⏳ សំណើរបស់អ្នកកំពុងរង់ចាំការអនុម័ត។\nYour request is pending approval."
            )
            return
    msg = (
        "👋 សួស្តី!\n\n"
        "📌 ផ្ញើ /ឈ្មោះ + link + សកម្មភាព\n"
        "📋 /names - បង្ហាញឈ្មោះ\n"
        "📊 /summary - របាយការណ៍ថ្ងៃនេះ"
    )
    await update.message.reply_text(msg)


# ── Main ──────────────────────────────────────────────────────────────────────

async def post_init(app):
    """Set bot commands menu."""
    commands = [
        BotCommand("start",   "Start bot"),
        BotCommand("names",   "Show all names"),
        BotCommand("summary", "Today's summary"),
        BotCommand("forward", "Forward reports to chat"),
        BotCommand("clear",   "Clear today's reports"),
    ]
    await app.bot.set_my_commands(commands)
    logger.info("Bot commands menu set")


def main():
    init_db()
    logger.info(f"[INIT] FORWARD_CHAT_IDS={FORWARD_CHAT_IDS}")
    request = HTTPXRequest(connection_pool_size=8, httpx_kwargs={"verify": False})
    get_updates_request = HTTPXRequest(connection_pool_size=8, httpx_kwargs={"verify": False})
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(post_init)
        .build()
    )
    # Register command handlers FIRST with explicit priority
    app.add_handler(CommandHandler("start", start), group=0)
    app.add_handler(CommandHandler("summary", summary), group=0)
    app.add_handler(CommandHandler("forward", forward_reports), group=0)
    logger.info("[INIT] Registered /forward command handler")
    app.add_handler(CommandHandler("clear", clear_today), group=0)
    app.add_handler(CommandHandler("names", names_menu), group=0)
    
    # Callback handlers in group 1
    app.add_handler(CallbackQueryHandler(pick_name_callback, pattern="^pick:"), group=1)
    app.add_handler(CallbackQueryHandler(name_confirm_callback, pattern="^name_confirm:"), group=1)
    app.add_handler(CallbackQueryHandler(unknown_callback), group=1)
    
    # Message handler LAST in group 2 (lowest priority)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_report_command), group=2)
    
    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

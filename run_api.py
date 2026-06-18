import os
import asyncio
import logging
import uvicorn
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class NoSignalServer(uvicorn.Server):
    """Uvicorn server that doesn't install signal handlers (safe for asyncio.gather)."""
    def install_signal_handlers(self):
        pass


async def run_bot_async():
    from telegram import Update, BotCommand
    from telegram.ext import (
        ApplicationBuilder, MessageHandler, CommandHandler,
        CallbackQueryHandler, filters
    )
    from telegram.request import HTTPXRequest
    from bot.main import (
        start, summary, clear_today, names_menu, forward_reports,
        pick_name_callback, name_confirm_callback, unknown_callback, handle_report_command
    )
    from database import init_db

    while True:
        try:
            init_db()
            BOT_TOKEN = os.getenv("BOT_TOKEN")
            request = HTTPXRequest(connection_pool_size=8, httpx_kwargs={"verify": False})
            get_updates_request = HTTPXRequest(connection_pool_size=8, httpx_kwargs={"verify": False})

            app = (
                ApplicationBuilder()
                .token(BOT_TOKEN)
                .request(request)
                .get_updates_request(get_updates_request)
                .build()
            )
            # Register command handlers FIRST with explicit priority
            app.add_handler(CommandHandler("start", start), group=0)
            app.add_handler(CommandHandler("summary", summary), group=0)
            app.add_handler(CommandHandler("forward", forward_reports), group=0)
            app.add_handler(CommandHandler("clear", clear_today), group=0)
            app.add_handler(CommandHandler("names", names_menu), group=0)
            
            # Callback handlers in group 1
            app.add_handler(CallbackQueryHandler(pick_name_callback, pattern="^pick:"), group=1)
            app.add_handler(CallbackQueryHandler(name_confirm_callback, pattern="^name_confirm:"), group=1)
            app.add_handler(CallbackQueryHandler(unknown_callback), group=1)
            
            # Message handler LAST in group 2 (lowest priority)
            app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_report_command), group=2)

            logger.info("Bot is running...")
            async with app:
                await app.start()
                # Set bot commands menu
                commands = [
                    BotCommand("start", "ចាប់ផ្តើម - Start bot"),
                    BotCommand("names", "បង្ហាញឈ្មោះ - Show all names"),
                    BotCommand("summary", "របាយការណ៍ថ្ងៃនេះ - Today's summary"),
                    BotCommand("forward", "ផ្ញើរបាយការណ៍ - Forward reports to chat"),
                    BotCommand("clear", "លុបរបាយការណ៍ - Clear today's reports"),
                ]
                await app.bot.set_my_commands(commands)
                logger.info("Bot commands menu set")
                await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
                await asyncio.Event().wait()  # run forever
        except Exception as e:
            logger.error(f"Bot crashed: {e}, restarting in 5s...")
            await asyncio.sleep(5)


async def main():
    port = int(os.getenv("PORT", 8000))
    config = uvicorn.Config("api.main:app", host="0.0.0.0", port=port, use_colors=False)
    server = NoSignalServer(config)

    await asyncio.gather(
        server.serve(),
        run_bot_async(),
    )


if __name__ == "__main__":
    asyncio.run(main())

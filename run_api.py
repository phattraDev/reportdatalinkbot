import os
import asyncio
import uvicorn
from dotenv import load_dotenv

load_dotenv()

class NoSignalServer(uvicorn.Server):
    """Uvicorn server that doesn't install signal handlers (safe for asyncio.gather)."""
    def install_signal_handlers(self):
        pass

async def run_bot_async():
    from bot.main import setup_application
    from telegram import Update
    import logging
    
    logger = logging.getLogger(__name__)
    
    while True:
        try:
            app = setup_application()
            logger.info("Bot is running in polling mode...")
            async with app:
                await app.start()
                await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
                await asyncio.Event().wait()
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
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

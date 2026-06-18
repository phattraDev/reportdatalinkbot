import os
import uvicorn

def main():
    port = int(os.getenv("PORT", 8000))
    # In a local environment with webhooks, the bot expects incoming HTTP requests.
    # For a full local test, use a tool like ngrok to forward requests to port 8000.
    uvicorn.run("api.main:app", host="0.0.0.0", port=port, reload=False)

if __name__ == "__main__":
    main()

"""Run the configured deepbox server with `python -m server`."""
import uvicorn

from server.app.config import settings


if __name__ == "__main__":
    uvicorn.run(
        "server.app.main:app",
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )

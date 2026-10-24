import asyncio
import logging
import os
import socket

import uvicorn

from .app import make_apps
from .service import Service, Settings


def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    service = Service(Settings.from_env())
    public, admin = make_apps(service)
    public_port = int(os.environ.get("BOOSTY_PORT", "8080"))
    admin_port = int(os.environ.get("BOOSTY_ADMIN_PORT", "8081"))
    if public_port == admin_port:
        raise ValueError("Public and admin ports must differ")

    async def application(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await service.close()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        else:
            selected = admin if scope["server"][1] == admin_port else public
            await selected(scope, receive, send)

    sockets = []
    try:
        for host, port in ((os.environ.get("BOOSTY_BIND", "0.0.0.0"), public_port),
                           (os.environ.get("BOOSTY_ADMIN_BIND", "127.0.0.1"), admin_port)):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen(64)
            sockets.append(listener)
        server = uvicorn.Server(uvicorn.Config(application, access_log=False, proxy_headers=False,
            server_header=False, lifespan="on", timeout_keep_alive=5, limit_concurrency=24))
        asyncio.run(server.serve(sockets=sockets))
    finally:
        for listener in sockets:
            listener.close()


if __name__ == "__main__":
    main()

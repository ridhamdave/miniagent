"""
Entry point for `python -m miniagent` and `miniagent start` CLI.

Starts the gateway server on the configured host/port.
The browser server is a separate process — see browser/server.py.
"""

import uvicorn

from .config import get_config
from .gateway import create_gateway_app


def main() -> None:
    config = get_config()
    app = create_gateway_app()
    uvicorn.run(
        app,
        host=config.gateway.host,
        port=config.gateway.port,
        log_level=config.log_level,
    )


if __name__ == "__main__":
    main()

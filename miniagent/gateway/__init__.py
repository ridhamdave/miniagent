"""
gateway/ — FastAPI WebSocket server, connection lifecycle, RPC handlers.

Public API:
  create_gateway_app() — returns the configured FastAPI app.
"""

from .server import create_gateway_app

__all__ = ["create_gateway_app"]

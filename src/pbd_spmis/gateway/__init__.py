"""Privacy gateway: a reverse proxy that enforces the control plane in front of any MIS API."""

from .app import GatewayConfig, create_gateway

__all__ = ["GatewayConfig", "create_gateway"]

from __future__ import annotations

from typing import Protocol


class HubPlugin(Protocol):
    """Lightweight plugin contract for the Pi Hub."""

    plugin_id: str
    display_name: str

    def start(self) -> None: ...

    def shutdown(self) -> None: ...

    def register_routes(self, app) -> None: ...

    def dashboard_html(self) -> str: ...

    def dashboard_js(self) -> str: ...

    def dashboard_init_js(self) -> str: ...

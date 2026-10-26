"""web — 本地结果 → 网页看板（纯标准库，零新增依赖）。"""

from __future__ import annotations

from web.server import collect, render_index, serve

__all__ = ["serve", "render_index", "collect"]

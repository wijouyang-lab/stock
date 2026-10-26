"""
launcher.py — PyInstaller 打包入口

行为
----
- **双击（不带参数）**：打开图形界面（tkinter 窗口）。
- **带参数**：原样转交给 ``main.py`` 的命令行入口，例如
  ``cn-stock-screener.exe --self-test``
  ``cn-stock-screener.exe --cli --local-only --score``
  ``cn-stock-screener.exe --watch --loop``
  ``cn-stock-screener.exe --scheduled --mode full``   ← 计划任务用这个

窗口模式下没有控制台，``sys.stdout`` 为 ``None``，任何 ``print()`` 都会抛异常。
所以进入程序前先把它重定向到 ``output/logs/gui_output.log``（可随时查看）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = _base_dir()
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _ensure_streams() -> None:
    """窗口模式（--noconsole）下把 stdout/stderr 兜住，避免 print() 崩溃。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_dir = BASE_DIR / "output" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / "gui_output.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except Exception:  # noqa: BLE001
        import io

        stream = io.StringIO()
    if sys.stdout is None:
        sys.stdout = stream          # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = stream          # type: ignore[assignment]


def main(argv: list[str]) -> int:
    _ensure_streams()

    if argv:
        from main import main as entry

        return entry(argv)

    # 无参数 = 双击 → 打开图形界面
    from gui.app import launch

    return launch(BASE_DIR)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

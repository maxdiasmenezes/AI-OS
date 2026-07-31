"""
system_status action handler: a read-only machine/service snapshot. Needs
no local configuration - always available regardless of whether
kernel/config/tools.yaml exists or what it contains.
"""

import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil

from kernel.tools.types import ActionRequest, ActionResult

_OLLAMA_URL = "http://localhost:11434/api/tags"
_NGROK_URL = "http://127.0.0.1:4040/api/tunnels"
_HTTP_TIMEOUT_SECONDS = 2.0

# kernel/tools/handlers/system_status.py -> ... -> project root's drive
_DISK_ROOT = Path(__file__).resolve().anchor


def _reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT_SECONDS):
            return True
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False


def _format_duration(seconds: float) -> str:
    total_minutes = int(seconds // 60)
    days, remainder_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder_minutes, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def run(request: ActionRequest, tools_config) -> ActionResult:
    cpu_percent = psutil.cpu_percent(interval=0.2)
    memory = psutil.virtual_memory()
    disk = shutil.disk_usage(_DISK_ROOT)
    uptime_seconds = max(0.0, time.time() - psutil.boot_time())

    ollama_status = "reachable" if _reachable(_OLLAMA_URL) else "unreachable"
    ngrok_status = "reachable" if _reachable(_NGROK_URL) else "unreachable"

    lines = [
        f"CPU: {cpu_percent:.0f}%",
        f"Memory: {memory.percent:.0f}% used",
        f"Disk: {disk.used / disk.total * 100:.0f}% used",
        f"Uptime: {_format_duration(uptime_seconds)}",
        f"Ollama: {ollama_status}",
        f"ngrok: {ngrok_status}",
        "AI-OS: running",
    ]
    return ActionResult(success=True, message="\n".join(lines), outcome="executed")

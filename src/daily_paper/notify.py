"""Desktop notification when an edition is ready.

On WSL: a Windows toast through powershell.exe whose click opens the edition in
the default browser (protocol activation). Elsewhere: notify-send. Best-effort,
never raises.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from typing import Any
from xml.sax.saxutils import escape

# PowerShell's registered AppUserModelID: lets the toast surface without an installed app.
WIN_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def _is_wsl() -> bool:
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def edition_url(eid: str, settings: dict[str, Any]) -> str:
    srv = settings["server"]
    host = "localhost" if srv["host"] in ("127.0.0.1", "0.0.0.0") else srv["host"]
    return f"http://{host}:{srv['port']}/e/{eid}/"


def _toast_xml(title: str, lines: list[str], url: str) -> str:
    texts = "".join(f"<text>{escape(t)}</text>" for t in [title, *lines][:3])
    link = escape(url, {'"': "&quot;"})
    return (
        f'<toast activationType="protocol" launch="{link}" duration="long">'
        f'<visual><binding template="ToastGeneric">{texts}</binding></visual>'
        f'<actions><action content="Lire" activationType="protocol" arguments="{link}"/></actions>'
        "</toast>"
    )


def _powershell_toast(xml: str) -> list[str] | None:
    pwsh = shutil.which("powershell.exe") or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    if not os.path.exists(pwsh) and not shutil.which(pwsh):
        return None
    xml_ps = xml.replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]>$null;"
        "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]>$null;"
        "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml('{xml_ps}');"
        "$t=[Windows.UI.Notifications.ToastNotification]::new($x);"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{WIN_APP_ID}').Show($t)"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [pwsh, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def send(title: str, lines: list[str], url: str) -> bool:
    try:
        if _is_wsl():
            argv = _powershell_toast(_toast_xml(title, lines, url))
            if argv:
                return subprocess.run(argv, capture_output=True, timeout=30).returncode == 0
        if shutil.which("notify-send"):
            return subprocess.run(["notify-send", "-a", "Daily Paper", title, "\n".join([*lines, url])],
                                  capture_output=True, timeout=10).returncode == 0
    except Exception:
        pass
    return False


def edition_ready(eid: str, date_label: str, titles: list[str], settings: dict[str, Any]) -> bool:
    teaser = " · ".join(titles[:3])
    return send(f"{settings.get('name', 'Daily Paper')} — {date_label}", [teaser], edition_url(eid, settings))

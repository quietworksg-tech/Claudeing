"""Alert delivery. Every channel is best-effort: a broken webhook must never
take down the tracker."""

from __future__ import annotations

import json
import logging
import shutil
import smtplib
import subprocess
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from flighttracker.config import Settings
from flighttracker.models import Alert, Route

log = logging.getLogger("flighttracker.notify")

ICONS = {"threshold": "\U0001f3af", "all_time_low": "\U0001f4c9", "drop": "\U0001f4b8"}


def format_alert(route: Route, alert: Alert, deep_link: str | None = None) -> str:
    icon = ICONS.get(alert.kind, "✈")
    lines = [
        f"{icon} {route.name} - {alert.currency} {alert.price:,.2f}",
        alert.message,
    ]
    if deep_link:
        lines.append(deep_link)
    return "\n".join(lines)


class Notifier:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send(self, route: Route, alert: Alert, deep_link: str | None = None) -> list[str]:
        """Deliver to every configured channel; returns the ones that succeeded."""
        text = format_alert(route, alert, deep_link)
        sent: list[str] = []
        for name, fn in (
            ("console", self._console),
            ("webhook", self._webhook),
            ("email", self._email),
            ("file", self._file),
            ("desktop", self._desktop),
        ):
            try:
                if fn(route, alert, text):
                    sent.append(name)
            except Exception as exc:                      # noqa: BLE001 - never fatal
                log.warning("notification channel %s failed: %s", name, exc)
        return sent

    # ------------------------------------------------------------ channels

    def _console(self, route: Route, alert: Alert, text: str) -> bool:
        print(f"\n{'=' * 62}\n{text}\n{'=' * 62}", flush=True)
        return True

    def _webhook(self, route: Route, alert: Alert, text: str) -> bool:
        url = self.settings.webhook_url
        if not url:
            return False
        payload = {
            "text": text,                                 # Slack / Discord compatible
            "route": route.name,
            "origin": route.origin,
            "destination": route.destination,
            "depart_date": route.depart_date.isoformat(),
            "return_date": route.return_date.isoformat() if route.return_date else None,
            "kind": alert.kind,
            "price": alert.price,
            "currency": alert.currency,
            "ts": alert.ts.isoformat(),
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15):
            return True

    def _email(self, route: Route, alert: Alert, text: str) -> bool:
        s = self.settings
        if not (s.smtp_host and s.email_to):
            return False
        msg = EmailMessage()
        msg["Subject"] = f"[flighttracker] {route.name} - {alert.currency} {alert.price:,.0f}"
        msg["From"] = s.email_from or s.smtp_user
        msg["To"] = s.email_to
        msg.set_content(text)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
            smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
        return True

    def _file(self, route: Route, alert: Alert, text: str) -> bool:
        path = self.settings.alert_log
        if not path:
            return False
        record = alert.to_dict() | {"route": route.name}
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        return True

    def _desktop(self, route: Route, alert: Alert, text: str) -> bool:
        if not self.settings.desktop_notify:
            return False
        title = f"{route.name} {alert.currency} {alert.price:,.0f}"
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", title, alert.message], check=False, timeout=10)
            return True
        if shutil.which("osascript"):
            script = f'display notification {json.dumps(alert.message)} with title {json.dumps(title)}'
            subprocess.run(["osascript", "-e", script], check=False, timeout=10)
            return True
        return False

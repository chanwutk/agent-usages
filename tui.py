#!/usr/bin/env python3
"""Terminal view of the usage database. Also starts server.py's poller and page."""

import errno
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import server
from rich.console import Console, Group
from rich.columns import Columns
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

NAMES = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor"}
KINDS = {"session": "5-hour session", "week": "This week", "period": "Billing period"}
WINDOW_SECONDS = {"session": 5 * 3600, "week": 7 * 24 * 3600}


def resets_in(resets_at, now):
    if not resets_at:
        return "N/A"
    seconds = (server.parse_stamp(resets_at) - now).total_seconds()
    if seconds <= 0:
        return "resets in 0m"
    mins = round(seconds / 60)
    days, mins = divmod(mins, 24 * 60)
    hours, minutes = divmod(mins, 60)
    if days:
        return f"resets in {days}d {hours}h"
    if hours:
        return f"resets in {hours}h {minutes}m"
    return f"resets in {minutes}m"


def time_elapsed(kind, resets_at, starts_at, now):
    if not resets_at:
        return None
    end = server.parse_stamp(resets_at)
    if starts_at:
        start = server.parse_stamp(starts_at)
    elif kind in WINDOW_SECONDS:
        start = datetime.fromtimestamp(end.timestamp() - WINDOW_SECONDS[kind], timezone.utc)
    else:
        return None
    span = (end - start).total_seconds()
    if span <= 0:
        return None
    pct = ((now - start).total_seconds() / span) * 100
    return max(0.0, min(100.0, pct))


def usage_bar(used, elapsed, width):
    """Rich progress bar (━). | is how far the window's clock has run."""
    used = max(0.0, min(100.0, used))
    color = "red" if used >= 100 else "dark_orange" if used >= 80 else "green"
    filled = round(width * used / 100)
    pace = None if elapsed is None else min(width - 1, round(width * elapsed / 100))
    bar = Text()
    for i in range(width):
        if i == pace:
            bar.append("|", style="bold white")
        else:
            bar.append("━", style=color if i < filled else "grey37")
    return bar


def footer(provider):
    when = provider["fetched_at"].replace("T", " ").replace("Z", " UTC") if provider["fetched_at"] else ""
    if provider["ok"] is False:
        if when and provider["windows"]:
            return f"{provider['message']} · last sample {when}"
        return provider["message"] or "fetch failed"
    if not provider["fetched_at"]:
        return provider["message"] or ""
    return f"Updated {when}"


def provider_panel(provider, now, width):
    rows = []
    for window in provider["windows"]:
        kind = KINDS.get(window["kind"], window["kind"])
        label = f"{window['scope']} · {kind}" if window["scope"] else kind
        head = Table.grid(expand=True)
        head.add_column(ratio=1)
        head.add_column(justify="right")
        head.add_row(label, resets_in(window["resets_at"], now))
        rows.append(head)
        rows.append(
            usage_bar(
                window["used_percent"],
                time_elapsed(window["kind"], window["resets_at"], window["starts_at"], now),
                width,
            )
        )
    if not provider["windows"] and provider["ok"] is not False:
        rows.append(Text("No samples yet"))
    foot = Text(footer(provider), style="red" if provider["ok"] is False else "dim")
    rows.append(foot)
    return Panel(Group(*rows), title=NAMES.get(provider["id"], provider["id"]))


def start_server():
    """Bind the page and poll thread. None if something is already listening."""
    with server.db_lock:
        con = server.connect()
        try:
            server.init_db(con)
            con.commit()
        finally:
            con.close()
    try:
        httpd = ThreadingHTTPServer((server.HOST, server.PORT), server.Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        return None
    threading.Thread(target=server.loop, daemon=True).start()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def stop_server(httpd):
    if httpd is None:
        return
    server.stop.set()
    httpd.shutdown()
    httpd.server_close()


def view(console_width):
    now = server.now_utc()
    bar_width = max(10, min(40, console_width - 4))
    panels = [provider_panel(p, now, bar_width) for p in server.usage_payload()["providers"]]
    title = Text.assemble(
        ("Agent usage", "bold"),
        (f"  http://{server.HOST}:{server.PORT}", "dim"),
    )
    return Group(title, Columns(panels, equal=True))


def check():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    assert resets_in(None, now) == "N/A"
    assert resets_in("2026-10-29T13:30:00Z", now) == "resets in 1h 30m"
    assert time_elapsed("session", "2026-10-29T13:00:00Z", None, now) == 80.0
    plain = usage_bar(50, 80, 10).plain
    assert plain == "━━━━━━━━|━", plain
    assert usage_bar(100, None, 4).plain == "━━━━"
    assert "red" in str(usage_bar(100, None, 4).spans[0].style)
    print("ok")


def main():
    if "--check" in sys.argv:
        check()
        return
    console = Console()
    if not sys.stdout.isatty():
        console.print(view(console.width))
        return
    httpd = start_server()
    try:
        with Live(view(console.width), console=console, screen=True, refresh_per_second=4) as live:
            while True:
                time.sleep(1)
                live.update(view(console.width))
    except KeyboardInterrupt:
        return
    finally:
        stop_server(httpd)


if __name__ == "__main__":
    main()

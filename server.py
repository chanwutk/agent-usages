#!/usr/bin/env python3
"""Poll Claude, Codex, and Cursor quotas into sqlite and serve them."""

import base64
import json
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "usage.db"
WEB = ROOT / "web"
HOST, PORT = "127.0.0.1", 8787
POLL_SECONDS = 60
STAMP = "%Y-%m-%dT%H:%M:%SZ"
PROVIDERS = ("claude", "codex", "cursor")
KIND_ORDER = {"session": 0, "week": 1, "period": 2}
HINT_401 = {
    "claude": "HTTP 401 — open Claude Code once",
    "codex": "HTTP 401 — run codex once",
    "cursor": "HTTP 401 — run agent login",
}

# ponytail: no oauth refresh. A 401 stays on the card until that CLI renews its token.

SCHEMA = """
CREATE TABLE IF NOT EXISTS sample (
  id INTEGER PRIMARY KEY,
  fetched_at TEXT NOT NULL,
  provider TEXT NOT NULL,
  kind TEXT NOT NULL,
  scope TEXT NOT NULL,
  used_percent REAL NOT NULL,
  resets_at TEXT,
  starts_at TEXT
);
CREATE INDEX IF NOT EXISTS sample_lookup
  ON sample(provider, kind, scope, fetched_at);
CREATE TABLE IF NOT EXISTS fetch_status (
  provider TEXT PRIMARY KEY,
  ok INTEGER NOT NULL,
  message TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
"""

db_lock = threading.Lock()
poll_lock = threading.Lock()
stop = threading.Event()


def stamp(dt):
    return dt.astimezone(timezone.utc).strftime(STAMP)


def now_utc():
    return datetime.now(timezone.utc)


def parse_stamp(value):
    return datetime.strptime(value, STAMP).replace(tzinfo=timezone.utc)


def norm_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = float(value)
        seconds = n / 1000 if n > 10_000_000_000 else n
        return stamp(datetime.fromtimestamp(seconds, timezone.utc))
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return stamp(dt)


def pct(value):
    return round(float(value), 2)


def bar(kind, scope, used, resets, starts=None):
    return {
        "kind": kind,
        "scope": scope or "",
        "used_percent": pct(used),
        "resets_at": norm_time(resets),
        "starts_at": norm_time(starts),
    }


def parse_claude(body):
    limits = body.get("limits")
    parsed = []
    if isinstance(limits, list) and limits:
        for item in limits:
            if not isinstance(item, dict):
                continue
            raw = str(item.get("kind") or "").lower()
            if raw in ("session", "five_hour", "5h"):
                kind = "session"
            elif "week" in raw or raw.startswith("seven_day"):
                kind = "week"
            else:
                continue
            used = item.get("percent", item.get("utilization"))
            if used is None:
                continue
            scope = ""
            model = (item.get("scope") or {}).get("model") if isinstance(item.get("scope"), dict) else None
            if isinstance(model, dict):
                scope = model.get("display_name") or model.get("name") or ""
            parsed.append(bar(kind, scope, used, item.get("resets_at")))
        if parsed:
            return parsed
    for key, val in body.items():
        if not isinstance(val, dict):
            continue
        if key == "five_hour":
            kind, scope = "session", ""
        elif key == "seven_day":
            kind, scope = "week", ""
        elif key.startswith("seven_day_"):
            kind, scope = "week", key[len("seven_day_") :]
        else:
            continue
        used = val.get("utilization", val.get("percent"))
        if used is None:
            continue
        parsed.append(bar(kind, scope, used, val.get("resets_at")))
    return parsed


def _codex_kind(seconds):
    if seconds == 18000:
        return "session"
    if seconds == 604800:
        return "week"
    return None


def _codex_windows(node, scope):
    if not isinstance(node, dict):
        return []
    found = []
    for key in ("primary_window", "secondary_window"):
        window = node.get(key)
        if not isinstance(window, dict):
            continue
        kind = _codex_kind(window.get("limit_window_seconds"))
        used = window.get("used_percent")
        if kind is None or used is None:
            continue
        found.append(bar(kind, scope, used, window.get("reset_at")))
    return found


def parse_codex(body):
    parsed = _codex_windows(body.get("rate_limit") or {}, "")
    for extra in body.get("additional_rate_limits") or []:
        if not isinstance(extra, dict):
            continue
        scope = extra.get("title") or extra.get("id") or "extra"
        parsed.extend(_codex_windows(extra, scope))
    return parsed


def _credit_num(value):
    if value is None or value == "":
        return None
    return float(value)


def codex_credits(body):
    """Spend-control balance. None when this account has no credit limit."""
    pool = ((body.get("spend_control") or {}).get("individual_limit") or {})
    credits = body.get("credits") or {}
    if credits.get("unlimited"):
        return {"remaining": None, "limit": None, "resets_at": None, "unlimited": True}
    if pool.get("unit") == "credit" and _credit_num(pool.get("remaining")) is not None:
        return {
            "remaining": _credit_num(pool.get("remaining")),
            "limit": _credit_num(pool.get("limit")),
            "resets_at": norm_time(pool.get("reset_at")),
            "unlimited": False,
        }
    if _credit_num(credits.get("balance")) is not None:
        return {
            "remaining": _credit_num(credits.get("balance")),
            "limit": None,
            "resets_at": None,
            "unlimited": False,
        }
        return None


def credit_period_start(resets_at):
    """UTC calendar month ending at resets_at. None unless that instant is 00:00 on the 1st."""
    if not resets_at:
        return None
    end = parse_stamp(resets_at)
    if (end.day, end.hour, end.minute, end.second) != (1, 0, 0, 0):
        return None
    if end.month == 1:
        start = end.replace(year=end.year - 1, month=12)
    else:
        start = end.replace(month=end.month - 1)
    return stamp(start)


def parse_cursor(body):
    plan = body.get("planUsage") or body.get("plan_usage") or {}
    total = plan.get("totalPercentUsed", plan.get("total_percent_used"))
    if total is None:
        return []
    resets = body.get("billingCycleEnd", body.get("billing_cycle_end"))
    starts = body.get("billingCycleStart", body.get("billing_cycle_start"))
    parsed = [bar("period", "", total, resets, starts)]
    for scope, camel, snake in (
        ("auto", "autoPercentUsed", "auto_percent_used"),
        ("api", "apiPercentUsed", "api_percent_used"),
    ):
        used = plan.get(camel, plan.get(snake))
        if used is None or abs(float(used) - float(total)) <= 0.05:
            continue
        parsed.append(bar("period", scope, used, resets, starts))
    return parsed


def keychain(service, account=None):
    cmd = ["security", "find-generic-password", "-s", service, "-w"]
    if account:
        cmd = ["security", "find-generic-password", "-a", account, "-s", service, "-w"]
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"keychain item {service!r} not found") from e


def keychain_json(service, account=None):
    raw = keychain(service, account)
    if raw.startswith("go-keyring-base64:"):
        raw = base64.b64decode(raw.split(":", 1)[1]).decode()
    return json.loads(raw)


def request_json(method, url, headers, body=None):
    data = None if body is None else json.dumps(body).encode()
    if body is not None:
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}") from e


def fetch_claude():
    token = keychain_json("Claude Code-credentials")["claudeAiOauth"]["accessToken"]
    body = request_json(
        "GET",
        "https://api.anthropic.com/api/oauth/usage",
        {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
    )
    return parse_claude(body), None


def fetch_codex():
    tokens = json.loads((Path.home() / ".codex" / "auth.json").read_text())["tokens"]
    body = request_json(
        "GET",
        "https://chatgpt.com/backend-api/wham/usage",
        {
            "Authorization": f"Bearer {tokens['access_token']}",
            "ChatGPT-Account-Id": tokens["account_id"],
        },
    )
    return parse_codex(body), codex_credits(body)


def fetch_cursor():
    token = keychain("cursor-access-token")
    body = request_json(
        "POST",
        "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
        {"Authorization": f"Bearer {token}", "Connect-Protocol-Version": "1"},
        {},
    )
    return parse_cursor(body), None


FETCHERS = {
    "claude": fetch_claude,
    "codex": fetch_codex,
    "cursor": fetch_cursor,
}


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(con):
    con.executescript(SCHEMA)
    cols = {row[1] for row in con.execute("PRAGMA table_info(sample)")}
    if "starts_at" not in cols:
        con.execute("ALTER TABLE sample ADD COLUMN starts_at TEXT")
    status_cols = {row[1] for row in con.execute("PRAGMA table_info(fetch_status)")}
    for name, decl in (
        ("credits_remaining", "REAL"),
        ("credits_limit", "REAL"),
        ("credits_resets_at", "TEXT"),
        ("credits_unlimited", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in status_cols:
            con.execute(f"ALTER TABLE fetch_status ADD COLUMN {name} {decl}")
    drop_history(con)


def drop_history(con, provider=None):
    """Keep only the newest poll per provider. Older rows existed for the sparkline."""
    if provider is None:
        con.execute(
            "DELETE FROM sample WHERE provider NOT IN (%s)" % ",".join("?" * len(PROVIDERS)),
            PROVIDERS,
        )
        con.execute(
            "DELETE FROM fetch_status WHERE provider NOT IN (%s)" % ",".join("?" * len(PROVIDERS)),
            PROVIDERS,
        )
        where = ""
        args = ()
    else:
        where = " AND provider = ?"
        args = (provider,)
    con.execute(
        f"""
        DELETE FROM sample
        WHERE fetched_at < (
          SELECT MAX(s2.fetched_at) FROM sample s2 WHERE s2.provider = sample.provider
        ){where}
        """,
        args,
    )


def _status(con, provider, ok, message, fetched_at):
    con.execute(
        """
        INSERT INTO fetch_status (provider, ok, message, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(provider) DO UPDATE SET
          ok=excluded.ok, message=excluded.message, fetched_at=excluded.fetched_at
        """,
        (provider, ok, message[:300], fetched_at),
    )


def _credits(con, provider, credits):
    con.execute(
        """
        UPDATE fetch_status
        SET credits_remaining=?, credits_limit=?, credits_resets_at=?, credits_unlimited=?
        WHERE provider=?
        """,
        (
            None if credits is None else credits["remaining"],
            None if credits is None else credits["limit"],
            None if credits is None else credits["resets_at"],
            0 if credits is None else int(credits["unlimited"]),
            provider,
        ),
    )


def poll_once():
    """Fetch outside the db lock so the page can read while a provider is in flight."""
    with poll_lock:
        fetched_at = stamp(now_utc())
        for provider in PROVIDERS:
            try:
                windows, credits = FETCHERS[provider]()
                error = None if windows else "no quota windows in response"
            except Exception as e:
                windows, credits = None, None
                error = HINT_401[provider] if str(e) == "HTTP 401" else str(e)
            with db_lock:
                con = connect()
                try:
                    if error:
                        _status(con, provider, 0, error, fetched_at)
                    else:
                        con.executemany(
                            """
                            INSERT INTO sample
                              (fetched_at, provider, kind, scope, used_percent, resets_at, starts_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            [
                                (
                                    fetched_at,
                                    provider,
                                    w["kind"],
                                    w["scope"],
                                    w["used_percent"],
                                    w["resets_at"],
                                    w["starts_at"],
                                )
                                for w in windows
                            ],
                        )
                        _status(con, provider, 1, "", fetched_at)
                        _credits(con, provider, credits)
                        drop_history(con, provider)
                    con.commit()
                finally:
                    con.close()


def usage_payload():
    with db_lock:
        con = connect()
        try:
            rows = con.execute(
                """
                SELECT provider, kind, scope, used_percent, resets_at, starts_at, fetched_at
                FROM sample
                ORDER BY id ASC
                """
            ).fetchall()
            status = {r["provider"]: r for r in con.execute("SELECT * FROM fetch_status")}
        finally:
            con.close()
    grouped = {provider: [] for provider in PROVIDERS}
    for row in rows:
        grouped.setdefault(row["provider"], []).append(row)
    providers = []
    for provider in PROVIDERS:
        windows = [
            {
                "kind": row["kind"],
                "scope": row["scope"],
                "used_percent": row["used_percent"],
                "resets_at": row["resets_at"],
                "starts_at": row["starts_at"],
            }
            for row in grouped.get(provider, [])
        ]
        windows.sort(key=lambda w: (KIND_ORDER.get(w["kind"], 9), w["scope"] != "", w["scope"]))
        st = status.get(provider)
        credits = None
        if st is not None and (st["credits_unlimited"] or st["credits_remaining"] is not None):
            credits = {
                "remaining": st["credits_remaining"],
                "limit": st["credits_limit"],
                "resets_at": st["credits_resets_at"],
                "unlimited": bool(st["credits_unlimited"]),
            }
        providers.append(
            {
                "id": provider,
                "ok": None if st is None else bool(st["ok"]),
                "message": "checking…" if st is None else st["message"],
                "fetched_at": None if st is None else st["fetched_at"],
                "windows": windows,
                "credits": credits,
            }
        )
    return {"poll_seconds": POLL_SECONDS, "providers": providers}


def loop():
    while not stop.is_set():
        try:
            poll_once()
        except Exception as e:
            print(f"poll failed: {e}", file=sys.stderr)
        stop.wait(POLL_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/usage":
            self._send(200, usage_payload())
            return
        self._static("/index.html" if path == "/" else path)

    def do_POST(self):
        if urlparse(self.path).path != "/api/refresh":
            self.send_error(404)
            return
        poll_once()
        self._send(200, usage_payload())

    def _send(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _static(self, path):
        root = WEB.resolve()
        target = (WEB / path.lstrip("/")).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            self.send_error(404)
            return
        ext = target.suffix.lower()
        mime = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}.get(
            ext, "application/octet-stream"
        )
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def check():
    claude = parse_claude(
        {
            "five_hour": {"utilization": 19, "resets_at": "2026-01-17T15:00:00Z"},
            "seven_day": {"utilization": 45, "resets_at": "2026-01-20T12:00:00Z"},
            "seven_day_sonnet": {"utilization": 3, "resets_at": "2026-01-25T00:00:00Z"},
            "extra_usage": {"is_enabled": False},
        }
    )
    assert [(w["kind"], w["scope"], w["used_percent"]) for w in claude] == [
        ("session", "", 19.0),
        ("week", "", 45.0),
        ("week", "sonnet", 3.0),
    ], claude
    scoped = parse_claude(
        {
            "limits": [
                {"kind": "session", "percent": 27.5, "resets_at": "2026-07-15T09:29:59Z"},
                {
                    "kind": "weekly_scoped",
                    "percent": 44,
                    "resets_at": "2026-07-18T18:59:59Z",
                    "scope": {"model": {"display_name": "Fable"}},
                },
            ]
        }
    )
    assert [(w["kind"], w["scope"], w["used_percent"]) for w in scoped] == [
        ("session", "", 27.5),
        ("week", "Fable", 44.0),
    ], scoped
    live = parse_claude(
        {
            "limits": [
                {"kind": "session", "percent": 0, "resets_at": None, "is_active": False},
                {
                    "kind": "weekly_all",
                    "percent": 100,
                    "resets_at": "2026-11-03T00:00:00.584506+00:00",
                    "is_active": True,
                },
            ]
        }
    )
    assert [(w["kind"], w["scope"], w["used_percent"], w["resets_at"]) for w in live] == [
        ("session", "", 0.0, None),
        ("week", "", 100.0, "2026-11-03T00:00:00Z"),
    ], live

    codex = parse_codex(
        {
            "rate_limit": {
                "primary_window": {"used_percent": 9, "limit_window_seconds": 604800, "reset_at": 1784530149},
                "secondary_window": {"used_percent": 20, "limit_window_seconds": 18000, "reset_at": 1784000000},
            },
            "additional_rate_limits": [
                {
                    "title": "Codex Spark",
                    "primary_window": {"used_percent": 4, "limit_window_seconds": 18000, "reset_at": 1784000000},
                }
            ],
        }
    )
    assert [(w["kind"], w["scope"], w["used_percent"]) for w in codex] == [
        ("week", "", 9.0),
        ("session", "", 20.0),
        ("session", "Codex Spark", 4.0),
    ], codex
    credits = codex_credits(
        {
            "spend_control": {
                "individual_limit": {
                    "unit": "credit",
                    "limit": "5000",
                    "remaining": "4547.2",
                    "reset_at": 1784530149,
                }
            }
        }
    )
    assert credits["remaining"] == 4547.2 and credits["limit"] == 5000.0
    assert credits["resets_at"] == norm_time(1784530149)
    assert credit_period_start("2026-11-01T00:00:00Z") == "2026-10-01T00:00:00Z"
    assert credit_period_start("2026-01-01T00:00:00Z") == "2025-12-01T00:00:00Z"
    assert credit_period_start("2026-11-01T12:00:00Z") is None
    assert codex_credits({"credits": {"unlimited": True}})["unlimited"]
    assert codex_credits({}) is None

    cursor = parse_cursor(
        {
            "billingCycleStart": "2026-10-18T06:32:13.504Z",
            "billingCycleEnd": "2026-11-01T00:00:00.000Z",
            "planUsage": {"totalPercentUsed": 10.19, "autoPercentUsed": 10.19, "apiPercentUsed": 3.16},
        }
    )
    assert [(w["kind"], w["scope"], w["used_percent"]) for w in cursor] == [
        ("period", "", 10.19),
        ("period", "api", 3.16),
    ], cursor
    assert {w["starts_at"] for w in cursor} == {"2026-10-18T06:32:13Z"}

    con = sqlite3.connect(":memory:")
    init_db(con)
    for when, used in (("2026-10-29T11:00:00Z", 1), ("2026-10-29T12:00:00Z", 2)):
        con.execute(
            "INSERT INTO sample (fetched_at, provider, kind, scope, used_percent, resets_at) VALUES (?,?,?,?,?,?)",
            (when, "claude", "week", "", used, None),
        )
    con.execute(
        "INSERT INTO sample (fetched_at, provider, kind, scope, used_percent, resets_at) VALUES (?,?,?,?,?,?)",
        ("2026-10-29T12:00:00Z", "gemini", "week", "", 9, None),
    )
    drop_history(con)
    left = [(r[0], r[1]) for r in con.execute("SELECT provider, used_percent FROM sample ORDER BY provider")]
    assert left == [("claude", 2)], left
    print("ok")


def main():
    if "--check" in sys.argv:
        check()
        return
    with db_lock:
        con = connect()
        try:
            init_db(con)
            con.commit()
        finally:
            con.close()
    threading.Thread(target=loop, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"http://{HOST}:{PORT}  poll {POLL_SECONDS}s")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        stop.set()
        httpd.shutdown()


if __name__ == "__main__":
    main()

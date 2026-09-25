import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import chat, playbook, prompted
from .ai import classify, write_first_message, note_first_accepted, note_first_refused, winning_first_messages, seed_winners_from_database
from .profile_parse import contact_blocked_reason, KOREA_SKIP_REASON
from .config import settings
from .github import settings_ready
from .db import ACCOUNT_ID_PATTERN, backup_database, connection, ensure_account, get_state, init_db, known_accounts, set_state, utc_now, vacuum
from .mcp_client import result_text, AlreadyMessaged, ConversationUnavailable, HimalayasMCP, HimalayasUnavailable, letters_only, HimalayasRejected, LoginExpired, MCPError, authorization_url, authorized_accounts, exchange_code, login_state, oauth_status
from .supabase_client import SupabaseError, SupabaseLedger, TalentProfiles, SENT as PROFILE_SENT, SKIP as PROFILE_SKIP, QUEUED as PROFILE_QUEUED, FAILED as PROFILE_FAILED

LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}  # "testclient" is the address the test client reports. A real connection never has it
LOGIN_FAILURES_BEFORE_BLOCK = 8
LOGIN_BLOCK_SECONDS = 600
failed_logins: dict[str, list[float]] = {}


SESSION_DAYS = 7
SESSION_COOKIE = "him_session"
OPEN_PATHS = {"/login", "/api/login", "/api/logout"}  # reachable without a login (the login page and its calls)


def session_key() -> bytes:
    """Signs the login cookie. It comes from the login itself, so a new password ends every session, and nothing needs storing."""
    return hashlib.sha256(f"him-session|{settings.admin_username}|{settings.admin_password}".encode()).digest()


def make_session() -> str:
    expires = str(int(time.time()) + SESSION_DAYS * 86400)
    return f"{expires}.{hmac.new(session_key(), expires.encode(), hashlib.sha256).hexdigest()}"


def valid_session(value: str) -> bool:
    expires, _, signature = value.partition(".")
    if not expires.isdigit() or int(expires) < time.time() or not settings.admin_password:
        return False
    return secrets.compare_digest(signature, hmac.new(session_key(), expires.encode(), hashlib.sha256).hexdigest())


def cookie_value(header: str, name: str) -> str:
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


def login_address(headers: dict, client: str) -> str:
    return (headers.get("x-forwarded-for") or headers.get("forwarded") or client).split(",")[0].strip()


def login_blocked(address: str) -> bool:
    now = time.monotonic()
    failed_logins[address] = [t for t in failed_logins.get(address, []) if now - t < LOGIN_BLOCK_SECONDS]
    return len(failed_logins[address]) >= LOGIN_FAILURES_BEFORE_BLOCK


def credentials_ok(user: str, password: str) -> bool:
    return bool(settings.admin_password) and bool(secrets.compare_digest(user.encode(), settings.admin_username.encode()) & secrets.compare_digest(password.encode(), settings.admin_password.encode()))


class RemoteAccessGuard:
    """Requests from this machine (the extension, the local browser) pass. Every other request needs to be signed in: a login cookie
    from the login page, or HTTP Basic for scripts. A request that came through a proxy counts as remote, even though the proxy itself is local.
    A browser page request without a login goes to the login page. A script or API call gets 401."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}
        client = (scope.get("client") or ("", 0))[0]
        forwarded = headers.get("x-forwarded-for") or headers.get("forwarded")
        path = scope.get("path", "")
        if (client in LOCAL_CLIENTS and not forwarded) or path in OPEN_PATHS:
            return await self.app(scope, receive, send)
        address = login_address(headers, client)
        if settings.admin_password:
            if valid_session(cookie_value(headers.get("cookie", ""), SESSION_COOKIE)):
                return await self.app(scope, receive, send)
            given = headers.get("authorization", "")
            if given.lower().startswith("basic ") and not login_blocked(address):
                try:
                    user, _, password = base64.b64decode(given.split(" ", 1)[1]).decode("utf-8").partition(":")
                except Exception:
                    user = password = ""
                if credentials_ok(user, password):
                    failed_logins.pop(address, None)
                    return await self.app(scope, receive, send)
                failed_logins[address] = failed_logins.get(address, []) + [time.monotonic()]
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        wants_page = scope.get("method") == "GET" and "text/html" in headers.get("accept", "") and not path.startswith("/api/")
        if not settings.admin_password:
            status, message, extra = 403, "Remote access is off. Set ADMIN_PASSWORD in settings.env on the server.", []
        elif wants_page:
            await send({"type": "http.response.start", "status": 302, "headers": [(b"location", b"/login"), (b"content-length", b"0")]})
            return await send({"type": "http.response.body", "body": b""})
        else:
            status, message, extra = 401, "Login required", []
        body = json.dumps({"detail": message}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *extra]})
        await send({"type": "http.response.body", "body": body})


app = FastAPI(title="Himalayas Hiring Assistant", version="0.1.0")
app.add_middleware(RemoteAccessGuard)
reply_monitor_task: asyncio.Task | None = None
# One backend serves every extension (one per Chrome profile / Himalayas account). All runtime state is kept per account.
automation_tasks: dict[str, asyncio.Task] = {}
automation_states: dict[str, dict] = {}
reply_monitor_states: dict[str, dict] = {}
delivery_states: dict[str, dict] = {}
dashboard_subscribers: dict[asyncio.Queue, str] = {}


def automation_state(account_id: str) -> dict:
    return automation_states.setdefault(
        account_id,
        {"running": False, "page": None, "after_id": 0, "status": "idle", "queued": 0, "sent": 0, "skipped": 0},
    )


def reply_monitor_state(account_id: str) -> dict:
    return reply_monitor_states.setdefault(account_id, {"running": False, "status": "idle", "detected": 0})


def delivery_state(account_id: str) -> dict:
    return delivery_states.setdefault(account_id, {
        "running": True, "status": "idle", "detail": "", "current_id": None, "current_name": None,
        "latest_recipient": None, "latest_status": None, "latest_error": None, "latest_at": None,
    })


OPS_LOG_KEEP = 500


def note_ops(account_id: str, level: str, hint: str, detail: str = "") -> None:
    """Append one Control-center log row. Hint is short; detail is the full reason (shown in the log panel)."""
    label = account_label(account_id) if account_id else "Server"
    text = (detail or hint or "").strip()
    short = (hint or text.split(".")[0] or "note").strip()[:80]
    with connection() as conn:
        conn.execute(
            "INSERT INTO ops_log (account_id, account_label, level, hint, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (account_id or "", label or account_id or "Server", level, short, text[:2000], utc_now()),
        )
        # Keep the table small.
        conn.execute(
            "DELETE FROM ops_log WHERE id NOT IN (SELECT id FROM ops_log ORDER BY id DESC LIMIT ?)",
            (OPS_LOG_KEEP,),
        )
    publish_dashboard_update("ops_log", account_id or "")


def hold_hint(reason: str) -> str:
    """Short status for account cards. Full text belongs in the ops log."""
    lower = (reason or "").lower()
    if "rate limit" in lower or "messages/minute" in lower:
        return "on hold · rate limit"
    if "daily limit" in lower:
        return "on hold · daily limit"
    if "ledger" in lower or "supabase" in lower:
        return "on hold · ledger"
    if "login" in lower or "expired" in lower or "authorization" in lower or "reconnect" in lower:
        return "on hold · login"
    if "not opening" in lower or "cannot reopen" in lower or "outage" in lower:
        return "on hold · Himalayas outage"
    if "refused" in lower and ("in a row" in lower or "in " in lower and "minute" in lower):
        return "on hold · pacing"
    if "korea" in lower:
        return "skipped · Korea"
    if reason.startswith("on hold"):
        return "on hold"
    line = reason.split("\n")[0].strip()
    return line[:48] + ("…" if len(line) > 48 else "")


def is_rate_limit_error(exc: Exception | str) -> bool:
    text = str(exc).lower()
    return "rate limit" in text or "messages/minute" in text or ("please wait" in text and "reject" in text)


last_seen_written: dict[str, float] = {}
# Extension heartbeats about every 30s. Past this (with no uninstall ping), the Control center treats it as gone.
EXTENSION_ONLINE_SECONDS = 90


def touch_seen(account_id: str) -> None:
    """Remember when an extension last called the server. Written to the database at most every 30 seconds."""
    now = time.monotonic()
    if now - last_seen_written.get(account_id, -1000.0) < 30:
        return
    with connection() as conn:
        row = conn.execute("SELECT last_seen FROM accounts WHERE id=?", (account_id,)).fetchone()
        previous = row["last_seen"] if row else None
        conn.execute("UPDATE accounts SET last_seen=? WHERE id=?", (utc_now(), account_id))
    last_seen_written[account_id] = now
    # Control center mirrors live extensions: announce when a profile appears or comes back online.
    if not extension_online(previous):
        publish_dashboard_update("extension_online", account_id)


def mark_extension_offline(account_id: str) -> None:
    """Clear presence so the Control center shows the profile as offline immediately."""
    last_seen_written.pop(account_id, None)
    with connection() as conn:
        conn.execute("UPDATE accounts SET last_seen=NULL WHERE id=?", (account_id,))


def extension_online(last_seen: str | None) -> bool:
    if not last_seen:
        return False
    try:
        seen = datetime.fromisoformat(last_seen)
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - seen).total_seconds() <= EXTENSION_ONLINE_SECONDS


def optional_account(
    x_account_id: Annotated[str | None, Header()] = None,
    x_admin: Annotated[str | None, Header()] = None,
    account_id: str | None = Query(default=None),
) -> str | None:
    value = x_account_id or account_id
    if value is None:
        return None
    if not ACCOUNT_ID_PATTERN.match(value):
        raise HTTPException(status_code=400, detail="Invalid account id")
    ensure_account(value)
    if not x_admin:  # the admin dashboard acts on an account, but it is not that account's extension
        touch_seen(value)
    return value


def account_id_only(
    x_account_id: Annotated[str | None, Header()] = None,
    account_id: str | None = Query(default=None),
) -> str:
    """Resolve the account for long-lived streams without refreshing extension presence."""
    value = x_account_id or account_id
    if value is None:
        raise HTTPException(status_code=400, detail="X-Account-Id header is required")
    if not ACCOUNT_ID_PATTERN.match(value):
        raise HTTPException(status_code=400, detail="Invalid account id")
    ensure_account(value)
    return value


def get_account(account_id: Annotated[str | None, Depends(optional_account)]) -> str:
    if account_id is None:
        raise HTTPException(status_code=400, detail="X-Account-Id header is required")
    return account_id


Account = Annotated[str, Depends(get_account)]
StreamAccount = Annotated[str, Depends(account_id_only)]


class CampaignRequest(BaseModel):
    candidate_ids: list[int] = Field(default_factory=list)
    page: int | None = Field(default=None, ge=1)


class ReplyRequest(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class SettingsUpdate(BaseModel):
    openrouter_api_key: str | None = None
    openrouter_model: str | None = None
    openrouter_base_url: str | None = None
    himalayas_mcp_url: str | None = None
    himalayas_mcp_token: str | None = None
    supabase_url: str | None = None
    supabase_key: str | None = None
    supabase_table: str | None = None
    github_token: str | None = None
    github_api_url: str | None = None
    github_owner: str | None = None
    github_repo: str | None = None
    github_ai_repo: str | None = None
    auto_send: bool | None = None
    min_message_delay_seconds: int | None = Field(default=None, ge=5, le=86400)
    max_message_delay_seconds: int | None = Field(default=None, ge=5, le=86400)
    reply_poll_interval_seconds: int | None = Field(default=None, ge=10, le=3600)
    daily_dm_limit: int | None = Field(default=None, ge=0, le=100000)
    delivery_poll_interval_seconds: int | None = Field(default=None, ge=1, le=300)
    profile_fetch_concurrency: int | None = Field(default=None, ge=1, le=20)
    message_generation_concurrency: int | None = Field(default=None, ge=1, le=20)
    reply_processing_concurrency: int | None = Field(default=None, ge=1, le=20)


SECRET_SETTINGS = {"openrouter_api_key", "himalayas_mcp_token", "supabase_key", "github_token"}


def masked(value: str) -> str:
    return f"{value[:4]}...{value[-4:]}" if len(value) > 8 else ("••••••••" if value else "")


def public_settings() -> dict:
    fields = SettingsUpdate.model_fields
    result = {}
    for name in fields:
        value = getattr(settings, name)
        result[name] = masked(value) if name in SECRET_SETTINGS else value
    return result


def update_env_file(values: dict) -> None:
    configured_path = settings.model_config.get("env_file", ".env")
    if isinstance(configured_path, tuple):
        configured_path = configured_path[-1]
    env_path = Path(configured_path)
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = existing.splitlines()
    updated = set()
    for index, line in enumerate(lines):
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        field = key.lower()
        if field in values:
            lines[index] = f"{key.upper()}={values[field]}"
            updated.add(field)
    for field, value in values.items():
        if field not in updated:
            lines.append(f"{field.upper()}={value}")
    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def account_label(account_id: str) -> str:
    with connection() as conn:
        row = conn.execute("SELECT label FROM accounts WHERE id=?", (account_id,)).fetchone()
    return row["label"] if row else ""


async def note_ledger(account_id: str, candidate: dict, status: str, message_id: int | None = None, body: str = "", error: str | None = None) -> bool:
    """Show a queued or failed first message in the shared ledger. Best effort: it must never stop queueing or sending.

    Returns False when a "queued" claim was refused because another account already sent or queued this member.
    """
    try:
        details = {**candidate, "account_id": account_id, "account_label": account_label(account_id)}
        return await SupabaseLedger().record_status(details, status, message_id, body, error)
    except Exception as exc:
        print(f"Ledger status note failed for {candidate.get('external_id')}: {exc}")
        return True  # do not cancel local work when the ledger itself is unreachable


def parse_candidate(row) -> dict:
    item = dict(row)
    item["stack"] = json.loads(item.pop("stack_json"))
    return item


def publish_dashboard_update(reason: str, account_id: str) -> None:
    event = {"reason": reason, "account_id": account_id, "at": utc_now()}
    for subscriber, subscriber_account in tuple(dashboard_subscribers.items()):
        if subscriber_account not in (account_id, "*"):
            continue
        try:
            subscriber.put_nowait(event)
        except asyncio.QueueFull:
            pass


def compact_scheduled_queue() -> None:
    """At startup, first messages that are waiting become due right away. The gap between them is enforced when they are sent
    (`next_first_message_at`), so the schedule must not hold its own spacing: a spacing fixed at queue time goes stale when the
    interval setting changes, or after a pause or a restart. Replies keep their own delay."""
    with connection() as conn:
        conn.execute(
            "UPDATE messages SET send_after=? WHERE direction='outbound' AND status='scheduled' AND stage='first_sent' AND send_after > ? AND " + TRUE_FIRST_SQL,
            ((datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),) * 2,
        )


def restore_cooldowns() -> None:
    """After a restart, keep the gap since the last first message that was sent. It is remembered only in memory otherwise."""
    with connection() as conn:
        rows = conn.execute("SELECT account_id, MAX(sent_at) AS last FROM messages WHERE direction='outbound' AND status='sent' AND stage='first_sent' AND sent_at IS NOT NULL GROUP BY account_id").fetchall()
    for row in rows:
        elapsed = (datetime.now(timezone.utc) - stored_time(row["last"])).total_seconds()
        remaining = first_message_gap() - elapsed
        if remaining > 0:
            next_first_message_at[row["account_id"]] = time.monotonic() + remaining


async def supervised(name: str, factory) -> None:
    """Run a background loop for ever. If it crashes for any reason it starts again after a few seconds. Only a shutdown ends it."""
    crashes = 0
    while True:
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            crashes += 1
            loop_crashes[name] = {"count": crashes, "last": f"{type(exc).__name__}: {str(exc)[:160]}", "at": utc_now()}
            print(f"Background loop '{name}' crashed ({exc!r}). Starting it again in 5 seconds.")
            await asyncio.sleep(5)


loop_crashes: dict[str, dict] = {}


@app.on_event("startup")
async def startup() -> None:
    global reply_monitor_task
    init_db()
    playbook.load_active()
    seed_winners_from_database()  # first-message style memory from every already-accepted send
    compact_scheduled_queue()
    restore_cooldowns()
    upgrade_pending_intros()
    skip_korea_queued_messages()
    asyncio.create_task(supervised("delivery", delivery_loop))
    asyncio.create_task(supervised("himalayas check", himalayas_check_loop))
    reply_monitor_task = asyncio.create_task(supervised("reply monitor", reply_monitor_loop))
    resume_automation()


@app.get("/api/health")
async def health(account_id: Annotated[str | None, Depends(optional_account)]) -> dict:
    if account_id is None:
        return {"ok": True}
    return {"ok": True, "auto_send": settings.auto_send, "himalayas_authorized": oauth_status(account_id), "himalayas_login": login_state(account_id), "hold": hold_reason(account_id), "paused": is_paused(account_id), "automation": automation_state(account_id), "reply_monitor": reply_monitor_state(account_id), "delivery": delivery_state(account_id), "loop_restarts": loop_crashes, "daily": daily_status(account_id), "company": get_state(f"himalayas_company:{account_id}")}


@app.get("/api/extension/uninstalled")
async def extension_uninstalled(account_id: str = Query(..., min_length=8, max_length=64)) -> HTMLResponse:
    """Chrome opens this URL when the extension is removed. Drop the profile from the Control center at once."""
    if not ACCOUNT_ID_PATTERN.match(account_id):
        return HTMLResponse("<!doctype html><title>Invalid</title><p>Invalid account.</p>", status_code=400)
    with connection() as conn:
        row = conn.execute("SELECT id FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        return HTMLResponse("<!doctype html><title>Removed</title><p>This profile is already gone. You can close this tab.</p>")
    # Uninstall clears chrome.storage, so this account id can never reconnect. Remove it from the mirror.
    delete_account_data(account_id)
    publish_dashboard_update("account_removed", account_id)
    return HTMLResponse(
        "<!doctype html><title>Extension removed</title><p>Profile removed from the Control center. You can close this tab.</p>",
        headers={"Cache-Control": "no-store"},
    )


def is_paused(account_id: str) -> bool:
    with connection() as conn:
        row = conn.execute("SELECT paused FROM accounts WHERE id=?", (account_id,)).fetchone()
    return bool(row and row["paused"])


class AccountUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    hiring_client_description: str | None = Field(default=None, max_length=4000)


@app.get("/api/account")
async def get_account_info(account: Account) -> dict:
    from .hiring_client import get_hiring_client
    with connection() as conn:
        row = conn.execute("SELECT id, label FROM accounts WHERE id=?", (account,)).fetchone()
    client = get_hiring_client(account)
    return {
        **dict(row),
        "himalayas_authorized": oauth_status(account),
        "himalayas_login": login_state(account),
        "hiring_client": client,
    }


@app.put("/api/account")
async def rename_account(update: AccountUpdate, account: Account) -> dict:
    from .hiring_client import set_hiring_client
    if update.label is not None:
        with connection() as conn:
            conn.execute("UPDATE accounts SET label=? WHERE id=?", (update.label.strip(), account))
    if update.hiring_client_description is not None:
        set_hiring_client(account, update.hiring_client_description)
    return await get_account_info(account)


@app.get("/api/accounts")
async def list_accounts() -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT a.id, a.label,
            (SELECT COUNT(*) FROM candidates WHERE account_id=a.id) AS candidates,
            (SELECT COUNT(*) FROM messages WHERE account_id=a.id AND direction='outbound' AND status='sent') AS sent,
            (SELECT COUNT(*) FROM messages WHERE account_id=a.id AND direction='outbound' AND status IN ('scheduled', 'approved')) AS queued
            FROM accounts a ORDER BY a.created_at"""
        ).fetchall()
    return [{**dict(row), "himalayas_authorized": oauth_status(row["id"]), "automation": automation_state(row["id"])["status"]} for row in rows]


@app.get("/api/settings")
async def get_settings() -> dict:
    return public_settings()


WRONG_KEY_PREFIXES = {
    "supabase_key": (("sk-or-", "an OpenRouter key"), ("ghp_", "a GitHub token"), ("github_pat_", "a GitHub token"), ("gho_", "a GitHub token")),
    "openrouter_api_key": (("ghp_", "a GitHub token"), ("github_pat_", "a GitHub token"), ("eyJ", "a Supabase key")),
    "github_token": (("sk-or-", "an OpenRouter key"), ("eyJ", "a Supabase key")),
}
SECRET_LABELS = {"openrouter_api_key": "OpenRouter API key", "supabase_key": "Supabase key", "github_token": "GitHub token"}


async def check_settings(values: dict) -> tuple[list[str], list[str]]:
    """Refuse a secret that is clearly the wrong kind, a copy of another secret, or rejected by its own service.
    Returns (errors, warnings). Errors stop the save. A service that cannot be reached only gives a warning."""
    errors: list[str] = []
    warnings: list[str] = []
    effective = lambda name: values.get(name, getattr(settings, name))
    for name, prefixes in WRONG_KEY_PREFIXES.items():
        value = values.get(name)
        for prefix, kind in prefixes if value else ():
            if value.startswith(prefix):
                errors.append(f"The {SECRET_LABELS[name]} field contains {kind}. Paste the correct value.")
    for name in SECRET_LABELS:
        if name in values and any(other != name and effective(other) == values[name] for other in SECRET_LABELS if effective(other)):
            other = next(o for o in SECRET_LABELS if o != name and effective(o) == values[name])
            errors.append(f"The {SECRET_LABELS[name]} and the {SECRET_LABELS[other]} contain the same value. Browser autofill may have filled one of them.")
    if "supabase_url" in values and values["supabase_url"] and not values["supabase_url"].startswith("https://"):
        errors.append("The Supabase URL must start with https://")
    if errors:
        return errors, warnings
    async with httpx.AsyncClient(timeout=15) as client:
        if ("supabase_key" in values or "supabase_url" in values) and effective("supabase_url") and effective("supabase_key"):
            key = effective("supabase_key")
            try:
                profiles_table = effective("supabase_profiles_table") or "talent_profiles"
                response = await client.get(
                    f"{effective('supabase_url').rstrip('/')}/rest/v1/{profiles_table}",
                    params={"select": "id", "limit": 1},
                    headers={"apikey": key, "Authorization": f"Bearer {key}"},
                )
                if response.status_code in (401, 403):
                    errors.append(f"Supabase rejected this key ({response.status_code}). Use the anon (publishable) key from Supabase > Project Settings > API.")
                elif response.status_code == 404:
                    warnings.append("Supabase accepted the key, but talent_profiles was not found. Run supabase_talent_profiles.sql in the Supabase SQL Editor.")
                elif response.is_error:
                    warnings.append(f"Supabase answered {response.status_code}, so the key could not be verified.")
            except httpx.HTTPError:
                warnings.append("Could not reach Supabase to verify the key.")
        if values.get("openrouter_api_key"):
            try:
                response = await client.get(f"{effective('openrouter_base_url').rstrip('/')}/auth/key", headers={"Authorization": f"Bearer {values['openrouter_api_key']}"})
                if response.status_code in (401, 403):
                    errors.append(f"OpenRouter rejected this API key ({response.status_code}).")
            except httpx.HTTPError:
                warnings.append("Could not reach OpenRouter to verify the key.")
        if any(name in values for name in ("github_token", "github_owner", "github_repo", "github_ai_repo")) and effective("github_token"):
            headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {effective('github_token')}", "X-GitHub-Api-Version": "2022-11-28"}
            base = effective("github_api_url").rstrip("/")
            try:
                response = await client.get(f"{base}/user", headers=headers)
                if response.status_code in (401, 403):
                    errors.append(f"GitHub rejected this token ({response.status_code}).")
                elif effective("github_owner"):
                    for label, repo_name in (("tech assessment", effective("github_repo") or "Tech_Assessment"), ("AI assessment", effective("github_ai_repo") or "AI_Assessment")):
                        if not repo_name:
                            continue
                        repo = await client.get(f"{base}/repos/{effective('github_owner')}/{repo_name}", headers=headers)
                        if repo.status_code == 404:
                            errors.append(f"GitHub cannot find the {label} repository {effective('github_owner')}/{repo_name}, or the token has no access to it.")
                        elif not repo.is_error and not (repo.json().get("permissions") or {}).get("admin"):
                            warnings.append(f"The token can see {repo_name} but may not be allowed to add collaborators. Invitations need admin access.")
            except httpx.HTTPError:
                warnings.append("Could not reach GitHub to verify the token.")
    return errors, warnings


# Settings that are checked together against one outside service. A wrong value in one group never stops another group from being saved.
SETTING_GROUPS = {
    "GitHub": {"github_token", "github_owner", "github_repo", "github_ai_repo", "github_api_url"},
    "Supabase": {"supabase_url", "supabase_key", "supabase_table"},
    "OpenRouter": {"openrouter_api_key", "openrouter_model", "openrouter_base_url"},
}


def duplicate_secret_errors(values: dict) -> dict[str, str]:
    """Two secrets changed to the same value in one save is almost always browser autofill. (A secret that copies an already saved one is
    caught by the group check.) Returns {setting name: message} for the second one."""
    found = {}
    names = [n for n in SECRET_LABELS if n in values]
    for index, name in enumerate(names):
        other = next((o for o in names[:index] if values[o] == values[name]), None)
        if other:
            found[name] = f"The {SECRET_LABELS[name]} and the {SECRET_LABELS[other]} contain the same value. Browser autofill may have filled one of them."
    return found


@app.put("/api/settings")
async def save_settings(update: SettingsUpdate) -> dict:
    """Save what changed. A value that is the same as the saved one is left alone and not checked again. Each group (GitHub, Supabase,
    OpenRouter, everything else) is checked and saved on its own, so one wrong value cannot undo the other changes in the same save."""
    values = update.model_dump(exclude_none=True)
    for name, value in list(values.items()):
        if name in SECRET_SETTINGS and (not value or set(value) == {"•"} or value == masked(getattr(settings, name))):
            values.pop(name, None)
        elif getattr(settings, name) == value:
            values.pop(name, None)  # nothing changed
    problems: list[str] = []
    warnings: list[str] = []
    for name, message in duplicate_secret_errors(values).items():
        problems.append(message)
        values.pop(name, None)
    saved: dict = {}
    buckets = {label: {n: v for n, v in values.items() if n in names} for label, names in SETTING_GROUPS.items()}
    buckets["other"] = {n: v for n, v in values.items() if not any(n in names for names in SETTING_GROUPS.values())}
    for label, group in buckets.items():
        if not group:
            continue
        errors, group_warnings = ([], []) if label == "other" else await check_settings(group)
        warnings += group_warnings
        if errors:  # this group is not saved, so a wrong value can never replace a working one
            problems += [f"{label} not saved: {error}" for error in errors]
        else:
            saved.update(group)
    if problems and not saved:
        raise HTTPException(status_code=400, detail=" ".join(problems))
    for name, value in saved.items():
        setattr(settings, name, value)
    if saved:
        update_env_file(saved)
        holds.pop("ledger", None)  # a new key may fix the problem. Try again right away.
        SupabaseLedger._status_ready = None
        TalentProfiles._columns = None
        for subscriber_account in set(dashboard_subscribers.values()):
            publish_dashboard_update("settings_updated", subscriber_account)
    return {**public_settings(), "warnings": warnings, "errors": problems, "saved": sorted(saved)}


@app.get("/api/auth/start")
async def auth_start(account: Account) -> RedirectResponse:
    try:
        return RedirectResponse(await authorization_url(account))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not start Himalayas authorization: {exc}") from exc


@app.get("/api/auth/callback", response_class=HTMLResponse)
async def auth_callback(code: str | None = Query(default=None), state: str | None = Query(default=None), error: str | None = Query(default=None)) -> str:
    if error:
        return f"<h1>Himalayas authorization failed</h1><p>{error}</p>"
    if not code or not state:
        raise HTTPException(status_code=400, detail="OAuth callback requires code and state")
    try:
        await exchange_code(code, state)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not save Himalayas authorization: {exc}") from exc
    return "<h1>Himalayas authorization complete</h1><p>You can close this tab and use the extension.</p>"


@app.get("/api/candidates")
async def candidates(account: Account, page: int | None = None) -> list[dict]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM candidates WHERE account_id=? ORDER BY name", (account,)).fetchall()
    items = [parse_candidate(row) for row in rows]
    if page is not None:
        items = [item for item in items if json.loads(item.get("source_json", "{}") or "{}").get("page") == page]
    return items


def locally_handled(account_id: str, slugs: list[str]) -> set[str]:
    """Members of this list that already have a message that is sent, waiting or skipped, or that the bot gave up on."""
    if not slugs:
        return set()
    with connection() as conn:
        rows = conn.execute(
            "SELECT c.external_id FROM candidates c WHERE c.account_id=? AND c.external_id IN ({}) AND EXISTS (SELECT 1 FROM messages m WHERE m.candidate_id=c.id AND m.direction='outbound' "
            "AND (m.status IN ('sent', 'scheduled', 'approved', 'skipped') OR (m.status='failed' AND m.error LIKE 'Gave up:%')))".format(",".join("?" * len(slugs))),
            (account_id, *slugs),
        ).fetchall()
    return {row["external_id"] for row in rows}


async def import_candidates(account_id: str, page: int) -> dict:
    """Read one page of members. Members who are already contacted or claimed (local database, or sent/queued
    in the shared ledger) are recognised first and are not fetched again, so a page of people you already
    messaged costs one search and one ledger lookup, not one request for every member."""
    try:
        client = HimalayasMCP(account_id)
        listed = await client.list_candidates(page)
        slugs = [item["talent_slug"] for item in listed if item.get("talent_slug")]
        handled = locally_handled(account_id, slugs)
        rest = [slug for slug in slugs if slug not in handled]
        if rest and settings.supabase_url and settings.supabase_key:
            try:
                handled |= await SupabaseLedger().contacted_among(rest)
            except Exception:
                pass
        imported = [item for item in listed if item.get("talent_slug") not in handled]
        semaphore = asyncio.Semaphore(settings.profile_fetch_concurrency)

        async def enrich(item: dict) -> dict:
            from .profile_parse import extract_country

            async with semaphore:
                profile = await client.get_talent_profile(item["talent_slug"])
            item["profile"] = profile
            item["summary"] = f"{item.get('summary', '')}\n{profile}"[:12000]
            country = extract_country(item.get("country") or "", item.get("location") or "", profile)
            if country:
                item["country"] = country
            return item

        imported = await asyncio.gather(*(enrich(item) for item in imported))
    except (MCPError, Exception) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    with connection() as conn:
        for item in imported:
            stack = item.get("stack", item.get("skills", []))
            summary = item.get("summary", item.get("bio", ""))
            category = classify(stack, summary)
            country = (item.get("country") or "").strip()
            now = utc_now()
            candidate_slug = item.get("talent_slug", item.get("slug", item.get("id", "")))
            if not candidate_slug:
                continue
            conn.execute(
                """INSERT INTO candidates (account_id, external_id, name, profile_url, summary, stack_json, category, source_json, country, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, external_id) DO UPDATE SET name=excluded.name, profile_url=excluded.profile_url,
                summary=excluded.summary, stack_json=excluded.stack_json,
                category=CASE WHEN candidates.suggested_role IS NOT NULL THEN candidates.category ELSE excluded.category END,
                source_json=excluded.source_json,
                country=CASE WHEN excluded.country != '' THEN excluded.country ELSE candidates.country END,
                updated_at=excluded.updated_at""",
                (account_id, str(candidate_slug), item.get("name", "Candidate"), item.get("profile_url", ""), summary,
                 json.dumps(stack), category, json.dumps(item), country, now, now),
            )
    publish_dashboard_update("candidates_synced", account_id)
    return {"imported": len(listed), "fresh": len(imported), "already_contacted": len(listed) - len(imported)}


@app.post("/api/candidates/sync")
async def sync_candidates(account: Account, page: int = 1) -> dict:
    return await import_candidates(account, page)


async def page_has_pending_messages(account_id: str, page: int) -> bool:
    with connection() as conn:
        row = conn.execute(
            """SELECT 1 FROM messages m JOIN candidates c ON c.id=m.candidate_id
            WHERE m.account_id=? AND json_extract(c.source_json, '$.page') = ?
            AND m.direction='outbound' AND m.status IN ('scheduled', 'approved') LIMIT 1""",
            (account_id, page),
        ).fetchone()
    return row is not None


AUTOMATION_RETRY_SECONDS = (30, 60, 120, 300)  # pause after an error. Automation never gives up. Only the Stop button stops it.


def automation_key(account_id: str) -> str:
    return f"automation:{account_id}"


def remember_automation(account_id: str, after_id: int | None) -> None:
    """Automation that is running is written to the database, so a restart of the server continues it. None = it is not running."""
    set_state(automation_key(account_id), json.dumps({"after_id": after_id}) if after_id is not None else "")


def candidate_from_profile(row: dict) -> dict:
    """Build the candidate dict used by message drafting from a talent_profiles row (no local DB)."""
    stack = row.get("stack") or []
    if isinstance(stack, str):
        try:
            stack = json.loads(stack)
        except Exception:
            stack = []
    if not isinstance(stack, list):
        stack = []
    slug = row.get("talent_slug") or ""
    category = (row.get("category") or "").strip()
    summary = (row.get("summary") or "").strip()
    if not summary:
        # Live talent_profiles may omit summary; draft from category + stack instead.
        bits = [category] if category else []
        if stack:
            bits.append(", ".join(str(s) for s in stack[:12]))
        summary = ". ".join(bits)
    return {
        "external_id": slug,
        "name": (row.get("candidate_name") or slug or "Candidate").strip() or "Candidate",
        "profile_url": row.get("profile_url") or (f"https://himalayas.app/@{slug}" if slug else ""),
        "summary": summary,
        "category": category or "unknown",
        "stack": stack,
        "country": (row.get("country") or "").strip(),
        "suggested_role": category if category and category.lower() not in {"developer", "non_developer", "unknown"} else "",
    }


async def wait_for_send_window(account_id: str, state: dict) -> bool:
    """Wait through pause / login hold / first-message cool-down / daily limit. Returns False if automation should stop."""
    while True:
        if not automation_state(account_id).get("running") and get_state(automation_key(account_id)) == "":
            return False
        if is_paused(account_id):
            state["status"] = "paused · waiting to resume"
            await asyncio.sleep(5)
            continue
        if hold_active(account_id):
            state["status"] = hold_hint(holds[account_id]["reason"])
            state["detail"] = holds[account_id]["reason"]
            await asyncio.sleep(5)
            continue
        if hold_active("first:" + account_id):
            state["status"] = hold_hint(holds["first:" + account_id]["reason"])
            state["detail"] = holds["first:" + account_id]["reason"]
            await asyncio.sleep(5)
            continue
        until = next_first_message_at.get(account_id, 0)
        now = time.monotonic()
        if until > now:
            wait = int(until - now)
            state["status"] = f"spacing · next send in {wait}s"
            await asyncio.sleep(min(5, max(1, wait)))
            continue
        today = await profiles_daily_status(account_id)
        if today["reached"]:
            state["status"] = daily_limit_reason(today)
            await asyncio.sleep(60)
            continue
        return True


async def profiles_sent_today(account_id: str) -> int:
    """First messages marked sent today on talent_profiles for this account (UTC day)."""
    if not settings.supabase_url or not settings.supabase_key:
        return first_messages_today(account_id)
    try:
        profiles = TalentProfiles()
        day = day_start().isoformat()
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                profiles.endpoint,
                headers={**profiles.headers, "Prefer": "count=exact"},
                params={
                    "select": "id",
                    "status": f"eq.{PROFILE_SENT}",
                    "account_id": f"eq.{account_id}",
                    "sent_at": f"gte.{day}",
                    "limit": "1",
                },
            )
        if response.is_error:
            cached = _cached_profiles_daily(account_id)
            return cached if cached is not None else first_messages_today(account_id)
        content_range = response.headers.get("content-range") or ""
        count = None
        if "/" in content_range:
            try:
                count = int(content_range.rsplit("/", 1)[-1])
            except ValueError:
                count = None
        if count is None:
            count = len(response.json() or [])
        _cache_profiles_daily(account_id, count)
        return count
    except Exception:
        cached = _cached_profiles_daily(account_id)
        return cached if cached is not None else first_messages_today(account_id)


# Lifetime talent_profiles outreach totals per account (status column).
_profiles_totals_cache: dict[str, dict[str, int]] = {}


def _cache_profiles_totals(account_id: str, totals: dict[str, int]) -> None:
    _profiles_totals_cache[account_id] = {
        "sent": int(totals.get("sent", 0)),
        "failed": int(totals.get("failed", 0)),
        "skip": int(totals.get("skip", 0)),
        "queued": int(totals.get("queued", 0)),
    }


def _cached_profiles_totals(account_id: str) -> dict[str, int]:
    return dict(_profiles_totals_cache.get(account_id) or {"sent": 0, "failed": 0, "skip": 0, "queued": 0})


def _bump_profiles_total(account_id: str, key: str, delta: int = 1) -> None:
    totals = _cached_profiles_totals(account_id)
    totals[key] = max(0, totals.get(key, 0) + delta)
    _cache_profiles_totals(account_id, totals)


async def profiles_outreach_totals(account_id: str) -> dict[str, int]:
    """All-time counts on talent_profiles for this bot: sent / failed / skip / queued."""
    empty = {"sent": 0, "failed": 0, "skip": 0, "queued": 0}
    if not settings.supabase_url or not settings.supabase_key:
        return empty
    try:
        profiles = TalentProfiles()
        totals = dict(empty)
        async with httpx.AsyncClient(timeout=25) as client:
            for key, status in (
                ("sent", PROFILE_SENT),
                ("failed", PROFILE_FAILED),
                ("skip", PROFILE_SKIP),
                ("queued", PROFILE_QUEUED),
            ):
                response = await client.get(
                    profiles.endpoint,
                    headers={**profiles.headers, "Prefer": "count=exact"},
                    params={
                        "select": "id",
                        "status": f"eq.{status}",
                        "account_id": f"eq.{account_id}",
                        "limit": "1",
                    },
                )
                if response.is_error:
                    continue
                content_range = response.headers.get("content-range") or ""
                if "/" in content_range:
                    try:
                        totals[key] = int(content_range.rsplit("/", 1)[-1])
                    except ValueError:
                        pass
        _cache_profiles_totals(account_id, totals)
        return totals
    except Exception:
        return _cached_profiles_totals(account_id)


async def profiles_daily_status(account_id: str) -> dict:
    limit = settings.daily_dm_limit
    sent = await profiles_sent_today(account_id)
    reset = day_start() + timedelta(days=1)
    return {
        "limit": limit,
        "sent_today": sent,
        "remaining": max(0, limit - sent) if limit else None,
        "reached": bool(limit) and sent >= limit,
        "resets_at": reset.isoformat(),
        "resets_in_seconds": int((reset - datetime.now(timezone.utc)).total_seconds()),
    }


async def process_talent_profile(account_id: str, row: dict, state: dict) -> str:
    """Claim one talent_profiles row, send or skip, write status. Returns outcome label."""
    slug = row.get("talent_slug") or ""
    if not slug:
        return "skipped"
    label = account_label(account_id)
    profiles = TalentProfiles()
    status = (row.get("status") or "").strip()
    if status == PROFILE_SENT:
        return "skipped_sent"
    if status == "skip":
        return "skipped"
    if status == "queued" and row.get("account_id") and row.get("account_id") != account_id:
        return "skipped_claimed"

    claimed = await profiles.claim(slug, account_id, label)
    if not claimed:
        return "skipped_claimed"

    candidate = candidate_from_profile(row)
    blocked = contact_blocked_reason({"country": candidate["country"], "summary": candidate["summary"]})
    if blocked:
        await profiles.mark_skip(slug, account_id=account_id, account_label=label, error=blocked)
        state["skipped"] = state.get("skipped", 0) + 1
        _bump_profiles_total(account_id, "skip", 1)
        return "skipped_blocked"

    if not await wait_for_send_window(account_id, state):
        await profiles.release_claim(slug, account_id)
        return "stopped"

    from .ai import sparse_first_message, resolve_client

    state["status"] = f"drafting · {candidate['name']}"
    state["current_name"] = candidate["name"]
    delivery_state(account_id).update({"status": "sending", "current_name": candidate["name"], "current_id": None, "detail": ""})
    recent = list(recent_first_messages(account_id))
    refused_bodies: list[str] = []
    role = ""
    body = ""
    last_exc: Exception | None = None

    for attempt in range(1, MAX_REFUSALS_PER_MEMBER + 1):
        if not await wait_for_send_window(account_id, state):
            await profiles.release_claim(slug, account_id)
            return "stopped"
        try:
            if attempt > 1 and attempt - 1 >= SAFER_AFTER_REFUSALS:
                company = resolve_client(account_id=account_id)["name"]
                first = sparse_first_message(candidate, company)
                if role:
                    first["role"] = role
            else:
                first = await write_first_message(
                    candidate,
                    fixed_role=role or None,
                    recent=recent + refused_bodies,
                    account_id=account_id,
                )
            role = first["role"]
            body = first["message"]
        except Exception as exc:
            await profiles.mark_failed(slug, account_id=account_id, account_label=label, error=f"Draft failed: {exc}")
            _bump_profiles_total(account_id, "failed", 1)
            delivery_state(account_id).update({"status": "failed", "current_name": None, "current_id": None})
            return "failed_draft"

        state["status"] = f"sending · {candidate['name']}" + (f" · try {attempt}" if attempt > 1 else "")
        try:
            await HimalayasMCP(account_id).send_message(slug, body, first_contact=True)
            break
        except LoginExpired as exc:
            set_hold(account_id, str(exc))
            await profiles.release_claim(slug, account_id)
            delivery_state(account_id).update({"status": hold_hint(str(exc)), "detail": str(exc), "current_name": None})
            return "held_login"
        except AlreadyMessaged:
            await profiles.mark_skip(
                slug, account_id=account_id, account_label=label,
                error="Already messaged on Himalayas (contacted before, for example by hand)",
            )
            state["skipped"] = state.get("skipped", 0) + 1
            _bump_profiles_total(account_id, "skip", 1)
            delivery_state(account_id).update({"status": "skipped", "current_name": None, "current_id": None})
            return "skipped_already"
        except ConversationUnavailable as exc:
            await profiles.mark_skip(slug, account_id=account_id, account_label=label, error=str(exc))
            state["skipped"] = state.get("skipped", 0) + 1
            _bump_profiles_total(account_id, "skip", 1)
            delivery_state(account_id).update({"status": "failed", "current_name": None, "current_id": None})
            note_ops(account_id, "warn", "cannot reopen", str(exc))
            return "skipped_unreachable"
        except HimalayasUnavailable as exc:
            # 401 / outage — release claim and hold briefly so reconnect or retry can happen.
            await profiles.release_claim(slug, account_id)
            detail = str(exc)
            if "401" in detail or "login" in detail.lower() or "unauthorized" in detail.lower():
                set_hold(account_id, f"Himalayas login problem: {detail[:160]}. Press Reconnect Himalayas.")
                delivery_state(account_id).update({"status": "on hold · login", "detail": detail, "current_name": None})
                note_ops(account_id, "warn", "on hold · login", detail)
                return "held_login"
            pause = 60
            reason = f"Himalayas unavailable ({detail[:90]}). Retrying shortly."
            holds["first:" + account_id] = {"reason": reason, "since": utc_now(), "until": time.monotonic() + pause}
            delivery_state(account_id).update({"status": hold_hint(reason), "detail": reason, "current_name": None})
            note_ops(account_id, "warn", hold_hint(reason), reason)
            return "held_outage"
        except HimalayasRejected as exc:
            last_exc = exc
            if is_rate_limit_error(exc):
                await profiles.release_claim(slug, account_id)
                reason = f"Rate limit reached (10 messages/minute). Please wait. Retrying shortly. {str(exc)[:120]}"
                until = time.monotonic() + RATE_LIMIT_HOLD_SECONDS
                next_first_message_at[account_id] = until
                holds["first:" + account_id] = {"reason": reason, "since": utc_now(), "until": until}
                delivery_state(account_id).update({"status": hold_hint(reason), "detail": reason, "current_name": None})
                note_ops(account_id, "warn", hold_hint(reason), reason)
                return "held_rate"
            spam_refusal = any(word in str(exc).lower() for word in ("filter", "spam"))
            hint = "refused · spam filter" if spam_refusal else "send refused"
            note_ops(account_id, "warn", hint, str(exc))
            delivery_state(account_id).update({"status": hint, "detail": str(exc), "current_name": candidate["name"]})
            if spam_refusal:
                note_first_refused(body)
                refused_bodies.append(body)
                recent.append(body)
                # Streak of different members refused → short hold (same as local queue).
                streak = rejection_streak.setdefault(account_id, [])
                if slug not in streak:
                    streak.append(slug)
                if len(streak) >= REFUSED_MEMBERS_BEFORE_HOLD:
                    hold_first_messages(account_id, str(exc))
                now = time.monotonic()
                window = [t for t in refusal_times.get(account_id, []) if now - t < REFUSAL_WINDOW_SECONDS] + [now]
                refusal_times[account_id] = window
                if len(window) >= REFUSALS_PER_WINDOW and not hold_active("first:" + account_id):
                    detail = (
                        f"Himalayas refused {len(window)} messages in {REFUSAL_WINDOW_SECONDS // 60} minutes, "
                        f"so first messages wait {FIRST_HOLD_SECONDS // 60} minutes. {str(exc)[:120]}"
                    )
                    holds["first:" + account_id] = {
                        "reason": detail, "since": utc_now(), "until": time.monotonic() + FIRST_HOLD_SECONDS,
                    }
                    note_ops(account_id, "warn", hold_hint(detail), detail)
                if attempt < MAX_REFUSALS_PER_MEMBER:
                    note_ops(
                        account_id, "info", "retry · rewrite",
                        f"Rewriting first message for {candidate['name']} (try {attempt + 1}/{MAX_REFUSALS_PER_MEMBER})",
                    )
                    await asyncio.sleep(random.uniform(*RETRY_PAUSE_SECONDS))
                    continue
            # Non-spam rejection, or out of rewrite tries.
            await profiles.mark_failed(slug, account_id=account_id, account_label=label, error=str(exc)[:300])
            _bump_profiles_total(account_id, "failed", 1)
            delivery_state(account_id).update({
                "status": hint, "detail": str(exc), "current_name": None,
                "latest_recipient": candidate["name"], "latest_status": "failed",
                "latest_error": str(exc)[:200], "latest_at": utc_now(),
            })
            return "failed_reject"
        except Exception as exc:
            await profiles.mark_failed(slug, account_id=account_id, account_label=label, error=str(exc)[:300])
            _bump_profiles_total(account_id, "failed", 1)
            delivery_state(account_id).update({"status": "failed", "current_name": None, "current_id": None})
            return "failed"
    else:
        # Loop exhausted without break (should be covered by failed_reject).
        err = str(last_exc or "Gave up after repeated refusals")[:300]
        await profiles.mark_failed(slug, account_id=account_id, account_label=label, error=err)
        _bump_profiles_total(account_id, "failed", 1)
        delivery_state(account_id).update({"status": "send refused", "detail": err, "current_name": None})
        return "failed_reject"

    sent_at = utc_now()
    await profiles.mark_sent(
        slug, account_id=account_id, account_label=label, message_body=body, sent_at=sent_at,
    )
    rejection_streak.pop(account_id, None)
    note_first_message_sent(account_id)
    note_first_accepted(body)
    note_ops(account_id, "info", "sent · talent_profiles", f"Accepted first message to {candidate['name']}")
    state["queued"] = state.get("queued", 0)
    state["sent"] = state.get("sent", 0) + 1
    cached = _cached_profiles_daily(account_id)
    _cache_profiles_daily(account_id, (cached or 0) + 1)
    _bump_profiles_total(account_id, "sent", 1)
    delivery_state(account_id).update({
        "status": "sent",
        "current_name": None,
        "current_id": None,
        "detail": "",
        "latest_recipient": candidate["name"],
        "latest_status": "sent",
        "latest_error": None,
        "latest_at": sent_at,
    })
    publish_dashboard_update("message_sent", account_id)
    return "sent"


async def automatic_campaign(account_id: str, after_id: int = 0) -> None:
    """Drive first-message outreach from Supabase talent_profiles (no Himalayas page import / local queue)."""
    state = automation_state(account_id)
    state.update({
        "running": True, "page": None, "after_id": after_id, "status": "starting · talent_profiles",
        "queued": 0, "sent": 0, "skipped": 0,
    })
    remember_automation(account_id, after_id)
    errors_in_a_row = 0
    cursor = after_id
    try:
        if not settings.supabase_url or not settings.supabase_key:
            state["status"] = "failed: SUPABASE_URL / SUPABASE_KEY not configured"
            remember_automation(account_id, None)
            return
        profiles = TalentProfiles()
        empty_batches = 0
        while True:
            try:
                if get_state(automation_key(account_id)) == "":
                    state["status"] = "stopped"
                    break
                state["after_id"] = cursor
                state["status"] = "fetching · talent_profiles"
                remember_automation(account_id, cursor)
                batch = await profiles.list_pending(limit=settings.automation_batch_size, after_id=cursor)
                if not batch:
                    empty_batches += 1
                    if empty_batches >= 2:
                        state["status"] = (
                            f"complete · talent_profiles ({state.get('sent', 0)} sent, "
                            f"{state.get('skipped', 0)} skipped)"
                        )
                        remember_automation(account_id, None)
                        break
                    # Re-scan from the start for newly pending / released rows.
                    cursor = 0
                    state["status"] = "rescanning · talent_profiles"
                    await asyncio.sleep(2)
                    continue
                empty_batches = 0
                for row in batch:
                    if get_state(automation_key(account_id)) == "":
                        state["status"] = "stopped"
                        return
                    row_id = int(row.get("id") or 0)
                    if row_id > cursor:
                        cursor = row_id
                    state["after_id"] = cursor
                    remember_automation(account_id, cursor)
                    outcome = await process_talent_profile(account_id, row, state)
                    if outcome == "stopped":
                        state["status"] = "stopped"
                        return
                    errors_in_a_row = 0
                    state.pop("last_error", None)
                    # Small yield so Stop / dashboard stay responsive between members.
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                pause = AUTOMATION_RETRY_SECONDS[min(errors_in_a_row, len(AUTOMATION_RETRY_SECONDS) - 1)]
                errors_in_a_row += 1
                state["last_error"] = {"message": str(exc)[:200], "at": utc_now()}
                state["status"] = f"retrying in {pause}s: {str(exc)[:120]}"
                print(f"Automation for {account_id} (talent_profiles) error, retrying in {pause}s: {exc!r}")
                publish_dashboard_update("automation_error", account_id)
                await asyncio.sleep(pause)
    except asyncio.CancelledError:
        state["status"] = "stopped"
        raise
    finally:
        state["running"] = False


def resume_automation() -> None:
    """After a restart of the server: continue the automation that was running."""
    with connection() as conn:
        rows = conn.execute("SELECT key, value FROM app_state WHERE key LIKE 'automation:%' AND value != ''").fetchall()
    for row in rows:
        account_id = row["key"].split(":", 1)[1]
        after_id = 0
        try:
            data = json.loads(row["value"])
            if isinstance(data, dict):
                after_id = max(0, int(data.get("after_id") or data.get("page") or 0))
                # Legacy page-based resume: start from the beginning of talent_profiles.
                if "page" in data and "after_id" not in data:
                    after_id = 0
        except (ValueError, TypeError, AttributeError):
            after_id = 0
        task = automation_tasks.get(account_id)
        if not task or task.done():
            automation_tasks[account_id] = asyncio.create_task(automatic_campaign(account_id, after_id))
            print(f"Automation for {account_id} continues from talent_profiles after_id={after_id}.")


@app.post("/api/automation/start")
async def start_automation(account: Account) -> dict:
    task = automation_tasks.get(account)
    if task and not task.done():
        return automation_state(account)
    automation_tasks[account] = asyncio.create_task(automatic_campaign(account, 0))
    publish_dashboard_update("automation_started", account)
    return {**automation_state(account), "status": "starting · talent_profiles"}


@app.post("/api/automation/stop")
async def stop_automation(account: Account) -> dict:
    task = automation_tasks.get(account)
    remember_automation(account, None)  # a stop from the person is the only thing that ends the automation
    if task and not task.done():
        task.cancel()
        automation_state(account)["status"] = "stopping"
        publish_dashboard_update("automation_stopping", account)
    return automation_state(account)


def skip_korea_queued_messages(account_id: str | None = None) -> int:
    """Cancel waiting first messages to Korea-based members. Returns how many were skipped."""
    with connection() as conn:
        if account_id:
            rows = conn.execute(
                """SELECT m.id, c.country, c.summary FROM messages m
                JOIN candidates c ON c.id=m.candidate_id
                WHERE m.account_id=? AND m.direction='outbound' AND m.status IN ('scheduled', 'approved', 'queued')
                AND NOT EXISTS (SELECT 1 FROM messages s WHERE s.candidate_id=m.candidate_id AND s.direction='outbound' AND s.status='sent')""",
                (account_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT m.id, c.country, c.summary FROM messages m
                JOIN candidates c ON c.id=m.candidate_id
                WHERE m.direction='outbound' AND m.status IN ('scheduled', 'approved', 'queued')
                AND NOT EXISTS (SELECT 1 FROM messages s WHERE s.candidate_id=m.candidate_id AND s.direction='outbound' AND s.status='sent')"""
            ).fetchall()
        skipped = 0
        for row in rows:
            if contact_blocked_reason({"country": row["country"] or "", "summary": row["summary"] or ""}):
                conn.execute("UPDATE messages SET status='skipped', error=? WHERE id=?", (KOREA_SKIP_REASON, row["id"]))
                skipped += 1
    return skipped


async def queue_campaign(account_id: str, request: CampaignRequest) -> dict:
    created = 0
    skipped = 0
    skipped += skip_korea_queued_messages(account_id)
    # Every new first message is due now. The gap between first messages is enforced when they are sent, in the order they were queued.
    next_send_at = datetime.now(timezone.utc)
    if request.page is not None:
        with connection() as conn:
            rows = conn.execute("SELECT * FROM candidates WHERE account_id=?", (account_id,)).fetchall()
        rows = [row for row in rows if json.loads(row["source_json"] or "{}").get("page") == request.page]
    elif request.candidate_ids:
        with connection() as conn:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE account_id=? AND id IN ({})".format(",".join("?" * len(request.candidate_ids))),
                [account_id, *request.candidate_ids],
            ).fetchall()
    else:
        raise HTTPException(status_code=400, detail="Provide page or candidate_ids")
    eligible_rows = []
    for row in rows:
        candidate = parse_candidate(row)
        with connection() as conn:
            already_contacted = conn.execute("SELECT 1 FROM messages WHERE candidate_id=? AND direction='outbound' AND status != 'failed' LIMIT 1", (candidate["id"],)).fetchone()
            # Himalayas cannot reopen this member's conversation. Trying again only adds another failure, so wait for Himalayas to fix it.
            unreachable = conn.execute("SELECT 1 FROM messages WHERE candidate_id=? AND direction='outbound' AND status='failed' AND (error LIKE 'Himalayas cannot reopen%' OR error LIKE 'Gave up:%') LIMIT 1", (candidate["id"],)).fetchone()
        if already_contacted or unreachable:
            skipped += 1
            continue
        if contact_blocked_reason(candidate):
            skipped += 1
            continue
        eligible_rows.append(candidate)

    # Shared ledger: members already sent or claimed (queued) by another profile must not get a second first message.
    if eligible_rows and settings.supabase_url and settings.supabase_key:
        try:
            claimed = await SupabaseLedger().contacted_among([c["external_id"] for c in eligible_rows])
        except Exception:
            claimed = set()
        if claimed:
            before = len(eligible_rows)
            eligible_rows = [c for c in eligible_rows if c["external_id"] not in claimed]
            skipped += before - len(eligible_rows)

    semaphore = asyncio.Semaphore(settings.message_generation_concurrency)

    recent = recent_first_messages(account_id)

    async def generate(candidate: dict) -> tuple[dict, dict]:
        async with semaphore:
            first = await write_first_message(candidate, recent=list(recent), account_id=account_id)
            recent.insert(0, first["message"])  # the messages written after this one must differ from it too
            return candidate, first

    try:
        generated = await asyncio.gather(*(generate(candidate) for candidate in eligible_rows))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Message generation failed: {exc}") from exc

    queued_notes = []
    for candidate, first in generated:
        candidate["suggested_role"] = first["role"]
        candidate["category"] = chat.category_for(first["role"])
        with connection() as conn:
            conn.execute("UPDATE candidates SET suggested_role=?, category=? WHERE id=?", (first["role"], candidate["category"], candidate["id"]))
            message_id = conn.execute("INSERT INTO messages (account_id, candidate_id, direction, body, status, send_after, stage, created_at) VALUES (?, ?, 'outbound', ?, 'scheduled', ?, ?, ?)",
                                      (account_id, candidate["id"], first["message"], next_send_at.isoformat(), chat.FIRST, utc_now())).lastrowid
        queued_notes.append((candidate, message_id, first["message"]))
    limit = asyncio.Semaphore(5)

    async def note(candidate: dict, message_id: int, body: str) -> bool:
        async with limit:
            claimed = await note_ledger(account_id, candidate, "queued", message_id, body)
            if claimed is False:
                # Another profile claimed this member while we were writing. Do not leave a sendable local copy.
                with connection() as conn:
                    conn.execute(
                        "UPDATE messages SET status='skipped', error=? WHERE id=? AND status='scheduled'",
                        ("Already claimed by another profile (shared contact ledger)", message_id),
                    )
                return False
            return True

    claimed_ok = await asyncio.gather(*(note(*item) for item in queued_notes))
    created = sum(1 for ok in claimed_ok if ok)
    skipped += sum(1 for ok in claimed_ok if not ok)
    publish_dashboard_update("messages_scheduled", account_id)
    return {"queued": created, "skipped": skipped, "first_send_at": next_send_at.isoformat() if created else None}


@app.post("/api/campaigns")
async def create_campaign(request: CampaignRequest, account: Account) -> dict:
    return await queue_campaign(account, request)


@app.get("/api/messages")
async def messages(account: Account, status: str | None = None) -> list[dict]:
    query = "SELECT m.*, c.name FROM messages m JOIN candidates c ON c.id=m.candidate_id WHERE m.account_id = ?"
    params: list[str] = [account]
    if status:
        query += " AND m.status = ?"
        params.append(status)
    query += " ORDER BY m.created_at DESC"
    with connection() as conn:
        return [dict(row) for row in conn.execute(query, params)]


@app.get("/api/progress")
async def progress(account: Account) -> dict:
    with connection() as conn:
        counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM messages WHERE account_id=? AND status != 'superseded' GROUP BY status", (account,))
        }
        next_message = conn.execute(
            """SELECT c.name, m.send_after FROM messages m JOIN candidates c ON c.id=m.candidate_id
            WHERE m.account_id=? AND m.direction='outbound' AND m.status IN ('scheduled', 'approved')
            ORDER BY m.send_after, m.id LIMIT 1""",
            (account,),
        ).fetchone()
        latest = conn.execute(
            """SELECT c.name, m.status, m.sent_at, m.error FROM messages m JOIN candidates c ON c.id=m.candidate_id
            WHERE m.account_id=? AND m.direction='outbound' ORDER BY COALESCE(m.sent_at, m.created_at) DESC, m.id DESC LIMIT 1""",
            (account,),
        ).fetchone()
    delivery = delivery_state(account)
    automation = automation_state(account)
    today = await profiles_daily_status(account) if settings.supabase_url and settings.supabase_key else daily_status(account)
    profiles_totals = (
        await profiles_outreach_totals(account)
        if settings.supabase_url and settings.supabase_key
        else {"sent": 0, "failed": 0, "skip": 0, "queued": 0}
    )
    next_name = delivery.get("current_name") or automation.get("current_name") or (next_message["name"] if next_message else None)
    latest_name = delivery.get("latest_recipient") or (latest["name"] if latest else None)
    latest_status = delivery.get("latest_status") or (latest["status"] if latest else None)
    latest_error = delivery.get("latest_error") if delivery.get("latest_recipient") else (latest["error"] if latest else None)
    return {
        "total": sum(counts.values()),
        "sent": profiles_totals.get("sent", 0),
        "failed": profiles_totals.get("failed", 0),
        "skipped": profiles_totals.get("skip", 0),
        "queued": profiles_totals.get("queued", 0),
        "profiles_totals": profiles_totals,
        "next_recipient": next_name,
        "next_send_at": next_message["send_after"] if next_message else None,
        "latest_recipient": latest_name,
        "latest_status": latest_status,
        "latest_error": latest_error,
        "daily": today,
    }


@app.get("/api/activity")
async def activity(account: Account, limit: int = Query(default=40, ge=1, le=100)) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT m.id, c.name, m.direction, m.status, m.created_at, m.sent_at,
            m.send_after, m.error FROM messages m JOIN candidates c ON c.id=m.candidate_id
            WHERE m.account_id=? AND m.status != 'superseded'
            ORDER BY COALESCE(m.sent_at, m.created_at) DESC, m.id DESC LIMIT ?""",
            (account, limit),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/conversations")
async def conversations(account: Account) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, c.category,
            COUNT(m.id) AS message_count,
            MAX(COALESCE(m.sent_at, m.created_at)) AS last_activity,
            (SELECT direction FROM messages WHERE candidate_id=c.id ORDER BY created_at DESC, id DESC LIMIT 1) AS last_direction,
            (SELECT status FROM messages WHERE candidate_id=c.id ORDER BY created_at DESC, id DESC LIMIT 1) AS last_status,
            (SELECT COUNT(*) FROM messages WHERE candidate_id=c.id AND direction='inbound' AND read_at IS NULL) AS unread_count,
            EXISTS(SELECT 1 FROM messages WHERE candidate_id=c.id AND direction='inbound') AS has_reply,
            MAX(CASE WHEN m.direction='inbound' THEN m.created_at END) AS last_reply_at,
            (SELECT body FROM messages WHERE candidate_id=c.id AND direction='inbound' ORDER BY created_at DESC, id DESC LIMIT 1) AS last_reply,
            MAX(CASE WHEN m.direction='inbound' OR m.status='sent' THEN COALESCE(m.sent_at, m.created_at) END) AS real_activity,
            EXISTS(SELECT 1 FROM messages WHERE candidate_id=c.id AND direction='outbound' AND status IN ('scheduled', 'approved')) AS response_scheduled
            FROM candidates c JOIN messages m ON m.candidate_id=c.id
            WHERE c.account_id=?
            GROUP BY c.id ORDER BY last_activity DESC""",
            (account,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["last_reply"] = (item["last_reply"] or "")[:160] or None
        item["conversation_status"] = "response scheduled" if item["response_scheduled"] else "new reply" if item["has_reply"] and item["last_direction"] == "inbound" else "awaiting reply" if item["last_direction"] == "outbound" and item["last_status"] == "sent" else item["last_status"] or "no activity"
        result.append(item)
    # Members who wrote to us come first: an unread reply on top (newest first), then everyone who ever replied, then the rest.
    # Queued or failed messages are not activity, so a batch of queued messages never pushes a reply down the list.
    def recency(item: dict) -> tuple[str, str]:
        return (item["last_reply_at"] if item["unread_count"] else item["real_activity"] or "", item["last_activity"] or "")
    result.sort(key=recency, reverse=True)
    result.sort(key=lambda item: 0 if item["unread_count"] else 1 if item["has_reply"] else 2)
    for item in result:
        item.pop("real_activity", None)
    return result


@app.get("/api/conversations/{candidate_id}")
async def conversation(candidate_id: int, account: Account) -> dict:
    with connection() as conn:
        candidate = conn.execute("SELECT id, name, category, summary, profile_url, suggested_role, github_username, github_email, github_invited_at FROM candidates WHERE id=? AND account_id=?", (candidate_id, account)).fetchone()
        messages = conn.execute(
            "SELECT id, direction, body, status, created_at, sent_at, send_after, error FROM messages WHERE candidate_id=? AND status != 'superseded' ORDER BY created_at, id",
            (candidate_id,),
        ).fetchall()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return {"candidate": {**dict(candidate), "stage": chat.current_stage(candidate_id)}, "messages": [dict(message) for message in messages]}


@app.post("/api/conversations/{candidate_id}/read")
async def mark_conversation_read(candidate_id: int, account: Account) -> dict:
    with connection() as conn:
        owned = conn.execute("SELECT 1 FROM candidates WHERE id=? AND account_id=?", (candidate_id, account)).fetchone()
        if not owned:
            raise HTTPException(status_code=404, detail="Candidate not found")
        # Count unread by candidate (same as the inbox), not messages.account_id, so older rows with an empty account_id still clear.
        updated = conn.execute(
            "UPDATE messages SET read_at=? WHERE candidate_id=? AND direction='inbound' AND read_at IS NULL",
            (utc_now(), candidate_id),
        ).rowcount
    if updated:
        publish_dashboard_update("conversation_read", account)
    return {"marked_read": updated}


# ---- Per-account members (Control center) ----


def member_rows(account_id: str) -> list[dict]:
    """One row for every member who was reached (has at least one outbound message), with the outreach status."""
    with connection() as conn:
        checks = {row["candidate_id"]: row for row in conn.execute("SELECT * FROM himalayas_check WHERE account_id=?", (account_id,))}
        candidates = conn.execute(
            "SELECT id, name, external_id, profile_url, suggested_role, category, country, github_username, github_invited_at FROM candidates WHERE account_id=?",
            (account_id,),
        ).fetchall()
        messages = conn.execute(
            """SELECT m.id, m.candidate_id, m.direction, m.status, m.stage, m.error, m.sent_at, m.created_at, m.read_at, m.body
            FROM messages m JOIN candidates c ON c.id=m.candidate_id
            WHERE c.account_id=? AND m.status != 'superseded' ORDER BY m.created_at, m.id""",
            (account_id,),
        ).fetchall()
    by_candidate: dict[int, list] = {}
    for message in messages:
        by_candidate.setdefault(message["candidate_id"], []).append(message)
    members = []
    for candidate in candidates:
        history = by_candidate.get(candidate["id"], [])
        outbound = [m for m in history if m["direction"] == "outbound"]
        if not outbound:
            continue
        inbound = [m for m in history if m["direction"] == "inbound"]
        unread_count = sum(1 for m in inbound if not m["read_at"])
        last_reply = (inbound[-1]["body"] or "")[:160] if inbound else None
        last_reply_at = inbound[-1]["created_at"] if inbound else None
        # Outreach = the first-contact message. A sent one wins, otherwise the latest attempt.
        first_contact = [m for m in outbound if m["stage"] in (None, chat.FIRST)] or outbound
        outreach = next((m for m in first_contact if m["status"] == "sent"), first_contact[-1])
        sent = sorted((m for m in outbound if m["status"] == "sent"), key=lambda m: (m["sent_at"] or m["created_at"], m["id"]))
        stage = (sent[-1]["stage"] or chat.FIRST) if sent else None
        failed = next((m for m in reversed(outbound) if m["status"] == "failed"), None)
        members.append({
            "id": candidate["id"], "name": candidate["name"], "role": candidate["suggested_role"], "category": candidate["category"],
            "country": candidate["country"] or "",
            "outreach_status": outreach["status"], "outreach_error": outreach["error"], "outreach_message_id": outreach["id"],
            "outreach_sent_at": outreach["sent_at"], "last_status": outbound[-1]["status"], "stage": stage,
            "replies": len(inbound), "unread_count": unread_count,
            "last_reply": last_reply, "last_reply_at": last_reply_at,
            "last_activity": max((m["sent_at"] or m["created_at"]) for m in history),
            "github_username": candidate["github_username"], "github_invited_at": candidate["github_invited_at"],
            "profile_url": candidate["profile_url"], "slug": candidate["external_id"],
            "himalayas": ({"checked_at": checks[candidate["id"]]["checked_at"], "room": checks[candidate["id"]]["room"], "total": checks[candidate["id"]]["total"], "ours": checks[candidate["id"]]["ours"],
                           "theirs": checks[candidate["id"]]["theirs"], "read": checks[candidate["id"]]["ours_read"], "ok": bool(checks[candidate["id"]]["matches"]), "note": checks[candidate["id"]]["note"]}
                          if candidate["id"] in checks else None),
            "failed_message_id": failed["id"] if failed and outbound[-1]["status"] == "failed" else None,
            "failed_error": failed["error"] if failed and outbound[-1]["status"] == "failed" else None,
        })
    # Unread replies first (newest unread on top), then the rest by last activity.
    def recency(item: dict) -> str:
        return item["last_reply_at"] if item["unread_count"] else item["last_activity"] or ""
    members.sort(key=recency, reverse=True)
    members.sort(key=lambda item: 0 if item["unread_count"] else 1)
    return members


@app.get("/api/db/members")
async def db_members(account: Account, status: str | None = None, q: str | None = None, limit: int = Query(default=200, ge=1, le=1000)) -> dict:
    members = member_rows(account)
    summary: dict[str, int] = {}
    unread_total = 0
    for member in members:
        summary[member["outreach_status"]] = summary.get(member["outreach_status"], 0) + 1
        unread_total += int(member.get("unread_count") or 0)
    if status == "unread":
        members = [m for m in members if m.get("unread_count")]
    elif status:
        members = [m for m in members if m["outreach_status"] == status]
    if q:
        needle = q.lower()
        members = [m for m in members if needle in m["name"].lower() or needle in (m.get("country") or "").lower() or needle in (m.get("role") or "").lower()]
    return {
        "summary": {**summary, "total": sum(summary.values()), "unread": unread_total},
        "members": members[:limit],
        "matched": len(members),
    }


# ---- Admin dashboard: every account (one per Chrome profile / extension) in one place ----

ADMIN_DIR = Path(os.environ.get("HIM_ADMIN_DIR") or Path(__file__).parent / "admin")


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


@app.get("/login")
async def login_page() -> HTMLResponse:
    return HTMLResponse((ADMIN_DIR / "login.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/admin")
@app.get("/admin/")
async def admin_index() -> HTMLResponse:
    """Serve the dashboard shell with no-cache so JS/CSS updates are picked up after deploy."""
    return HTMLResponse(
        (ADMIN_DIR / "index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@app.post("/api/login")
async def login(body: LoginRequest, request: Request) -> Response:
    if not settings.admin_password:
        raise HTTPException(status_code=403, detail="Remote access is off. Set ADMIN_PASSWORD in settings.env on the server.")
    headers = {key.lower(): value for key, value in request.headers.items()}
    address = login_address(headers, request.client.host if request.client else "")
    if login_blocked(address):
        raise HTTPException(status_code=429, detail=f"Too many wrong passwords. Try again in {LOGIN_BLOCK_SECONDS // 60} minutes.")
    if not credentials_ok(body.username.strip(), body.password):
        failed_logins[address] = failed_logins.get(address, []) + [time.monotonic()]
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    failed_logins.pop(address, None)
    response = Response(json.dumps({"ok": True}), media_type="application/json")
    response.set_cookie(SESSION_COOKIE, make_session(), max_age=SESSION_DAYS * 86400, httponly=True, samesite="strict", path="/")
    return response


@app.post("/api/logout")
async def logout() -> Response:
    response = Response(json.dumps({"ok": True}), media_type="application/json")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def ago(value: str | None) -> str:
    seconds = max(0, int((datetime.now(timezone.utc) - stored_time(value)).total_seconds())) if value else 0
    return "just now" if seconds < 90 else f"{seconds // 60} min ago" if seconds < 5400 else f"{seconds // 3600} h ago"


def account_overview(account_id: str, label: str, paused: bool, last_seen: str | None) -> dict:
    members = member_rows(account_id)
    by_status: dict[str, int] = {}
    for member in members:
        by_status[member["outreach_status"]] = by_status.get(member["outreach_status"], 0) + 1
    with connection() as conn:
        imported = conn.execute("SELECT COUNT(*) FROM candidates WHERE account_id=?", (account_id,)).fetchone()[0]
        next_message = conn.execute(
            """SELECT c.name, m.send_after FROM messages m JOIN candidates c ON c.id=m.candidate_id
            WHERE m.account_id=? AND m.direction='outbound' AND m.status IN ('scheduled', 'approved') ORDER BY m.send_after, m.id LIMIT 1""",
            (account_id,),
        ).fetchone()
        unread = conn.execute(
            """SELECT COUNT(*) FROM messages m JOIN candidates c ON c.id=m.candidate_id
            WHERE c.account_id=? AND m.direction='inbound' AND m.read_at IS NULL""",
            (account_id,),
        ).fetchone()[0]
    login = login_state(account_id)
    authorized = login == "connected"
    automation = automation_state(account_id)
    delivery = delivery_state(account_id)
    monitor = reply_monitor_state(account_id)
    failed_followups = sum(1 for m in members if m["failed_message_id"] and m["outreach_status"] != "failed")
    alerts = []
    if unread:
        alerts.append(f"{unread} unread {'reply' if unread == 1 else 'replies'}")
    if login == "expired":
        alerts.append("Himalayas login expired: press Reconnect Himalayas")
    elif login == "none":
        alerts.append("Himalayas is not connected")
    if paused:
        alerts.append("Sending is paused")
    if hold_reason(account_id):
        alerts.append(f"Sending on hold: {hold_reason(account_id)}")
    today = daily_status(account_id)
    if today["reached"]:
        alerts.append(daily_limit_reason(today))
    unreachable = sum(1 for m in members if m["outreach_status"] == "failed" and (m.get("outreach_error") or "").startswith("Himalayas cannot reopen"))
    if by_status.get("failed", 0) - unreachable > 0:
        alerts.append(f"{by_status['failed'] - unreachable} failed first message(s)")
    if unreachable:  # start_conversation could not reopen the room and no usable room was found another way
        alerts.append(f"{unreachable} member(s) cannot be reached: Himalayas could not open their conversation")
    if failed_followups:
        alerts.append(f"{failed_followups} failed reply(ies)")
    # Errors are shown with their age, because the last error stays in memory until the next run. Login problems are
    # reported above from the real login state, so they are not repeated here.
    if str(automation["status"]).startswith("failed") and authorized:
        alerts.append(f"Automation failed {ago(automation.get('failed_at'))}: {str(automation['status']).splitlines()[0][8:140]}")
    if str(monitor["status"]).startswith("waiting for Himalayas") and authorized:
        alerts.append(f"Waiting for Himalayas to answer ({str(monitor["status"]).splitlines()[0][22:120].strip()}). Replies are checked again every {settings.reply_poll_interval_seconds} seconds")
    elif str(monitor["status"]).startswith("failed") and authorized:
        alerts.append(f"Reply monitor failed: {str(monitor['status']).splitlines()[0][8:140]}")
    online = extension_online(last_seen)
    if not online:
        alerts.insert(0, "Chrome extension offline or removed")
    # Next / latest: prefer live talent_profiles automation over local scheduled queue (often empty).
    next_name = delivery.get("current_name") or automation.get("current_name") or (next_message["name"] if next_message else None)
    next_at = next_message["send_after"] if next_message else None
    latest_recipient = delivery.get("latest_recipient")
    latest_status = delivery.get("latest_status")
    latest_error = delivery.get("latest_error")
    latest_at = delivery.get("latest_at")
    from .hiring_client import get_hiring_client
    return {
        "id": account_id, "label": label, "paused": paused, "last_seen": last_seen, "extension_online": online,
        "hiring_client_name": get_hiring_client(account_id)["name"],
        "himalayas_authorized": authorized, "himalayas_login": login,
        "automation": automation, "delivery": delivery, "reply_monitor": monitor, "daily": today,
        "imported": imported, "members": len(members), "by_status": by_status,
        "replied": sum(1 for m in members if m["replies"]), "failed_followups": failed_followups, "unread": unread,
        "next_send_at": next_at, "next_recipient": next_name,
        "next_send_in": max(0.0, next_first_message_at.get(account_id, 0) - time.monotonic()),
        "latest_recipient": latest_recipient,
        "latest_status": latest_status,
        "latest_error": latest_error,
        "latest_at": latest_at,
        "profiles_totals": _cached_profiles_totals(account_id),
        "alerts": alerts,
    }


# ---- Playbook: the .md file that holds the company, roles, pay and message wording ----

class PlaybookFile(BaseModel):
    md: str
    filename: str = "playbook.md"


def check_playbook(md: str) -> tuple[dict, dict]:
    if len(md.encode()) > playbook.MAX_BYTES:
        raise HTTPException(status_code=413, detail="The file is too large. A playbook is a few pages of text.")
    pb, errors, warnings, notes = playbook.parse(md)
    report = {"ok": not errors, "errors": errors, "warnings": warnings, "notes": notes}
    if not errors:
        report["summary"] = playbook.summary(pb)
        report["samples"] = playbook.samples(pb)
        wants_github = "github" in pb["prompts"]["chat"].lower() if pb.get("mode") == "prompt" else any(role["type"] == "developer" for role in pb["roles"])
        if wants_github and not settings_ready():
            report["warnings"].append("This file uses GitHub invitations, but the GitHub token, owner and repository are not set in Settings. The AI is told that invitations are not available until they are.")
    return pb, report


def playbook_status() -> dict:
    meta = playbook.meta()
    with connection() as conn:
        waiting = conn.execute("SELECT COUNT(*) FROM messages WHERE direction='outbound' AND status IN ('scheduled', 'approved')").fetchone()[0]
    return {"company": "", "website": "", "careers_url": "", **playbook.summary(playbook.active()), "is_default": not playbook.get_state(playbook.STATE_MD), "filename": meta.get("filename"), "imported_at": meta.get("imported_at"), "queued_messages": waiting}


@app.get("/api/admin/playbook")
async def get_playbook() -> dict:
    return playbook_status()


@app.get("/api/admin/playbook/download")
async def download_playbook(builtin: bool = False, kind: str = "") -> Response:
    """The active playbook as a .md file. It is also the template: edit it and import it again. kind=prompts gives the prompt template."""
    pb = playbook.prompt_template() if kind == "prompts" else playbook.default_playbook() if builtin else playbook.active()
    return Response(playbook.to_markdown(pb), media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="playbook-{playbook.slug(pb["name"]) or "template"}.md"'})


@app.post("/api/admin/playbook/check")
async def check_playbook_file(body: PlaybookFile) -> dict:
    """Read the file and report problems and sample messages. Nothing changes."""
    return check_playbook(body.md)[1]


class TryPlaybook(BaseModel):
    md: str
    profile: str = "Senior software engineer with 8 years of experience in Python, FastAPI, PostgreSQL and AWS. Built payment APIs and led a small team."
    reply: str = ""


@app.post("/api/admin/playbook/try")
async def try_playbook(body: TryPlaybook) -> dict:
    """Write message 1 (and a reply to a sample member answer) from a prompt playbook, without applying it and without sending anything."""
    pb, report = check_playbook(body.md)
    if not report["ok"]:
        raise HTTPException(status_code=422, detail="The file has errors: " + " ".join(report["errors"][:3]))
    if pb.get("mode") != "prompt":
        raise HTTPException(status_code=400, detail="Try it works for prompt playbooks (with '## First message prompt' and '## Chat logic prompt'). The other format has fixed texts: see the sample messages.")
    candidate = {"id": 0, "name": "Sample Member", "summary": body.profile[:3000], "stack": []}
    try:
        first = await prompted.first_message(candidate, prompts=pb["prompts"])
        result = {"role": first["role"], "first_message": first["message"], "reply": None}
        if body.reply.strip():
            history = [{"direction": "outbound", "body": first["message"]}, {"direction": "inbound", "body": body.reply.strip()}]
            decision = await prompted.decide(candidate, body.reply.strip(), history, 1, prompts=pb["prompts"], github_ready=True)
            result["reply"] = decision
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"The AI could not write it: {exc}") from exc
    return result


@app.put("/api/admin/playbook")
async def import_playbook(body: PlaybookFile) -> dict:
    pb, report = check_playbook(body.md)
    if not report["ok"]:
        raise HTTPException(status_code=422, detail="The file has errors: " + " ".join(report["errors"][:3]))
    playbook.save_active(body.md, body.filename[:120], pb)
    return {**report, "status": playbook_status()}


@app.delete("/api/admin/playbook")
async def reset_playbook() -> dict:
    playbook.reset_active()
    return playbook_status()


@app.get("/api/admin/ledger")
async def admin_ledger(
    status: str | None = None,
    q: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    account_id: str | None = None,
) -> dict:
    """Browse every member in Supabase talent_profiles (the shared outreach store)."""
    if not settings.supabase_url or not settings.supabase_key:
        raise HTTPException(status_code=503, detail="Supabase is not configured. Add the URL and key under Settings.")
    allowed = {"not_sent", "pending", "queued", "sent", "failed", "skip"}
    if status and status not in allowed:
        raise HTTPException(status_code=400, detail=f"status must be one of: not_sent, queued, sent, failed, skip")
    try:
        return await TalentProfiles().list_all(status=status, q=q, offset=offset, limit=limit, account_id=account_id)
    except SupabaseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/admin/ledger/{talent_slug}")
async def admin_ledger_member(talent_slug: str) -> dict:
    """One talent_profiles row, plus any matching local profile rows."""
    if not settings.supabase_url or not settings.supabase_key:
        raise HTTPException(status_code=503, detail="Supabase is not configured. Add the URL and key under Settings.")
    try:
        contact = await TalentProfiles().get(talent_slug)
    except SupabaseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not contact:
        raise HTTPException(status_code=404, detail="No talent_profiles row for that member")
    with connection() as conn:
        local = [
            dict(row)
            for row in conn.execute(
                """SELECT c.id, c.account_id, a.label AS account_label, c.name, c.country, c.suggested_role, c.category,
                c.profile_url, c.github_username, c.github_invited_at, c.created_at, c.updated_at,
                (SELECT COUNT(*) FROM messages m WHERE m.candidate_id=c.id AND m.direction='outbound' AND m.status='sent') AS sent,
                (SELECT COUNT(*) FROM messages m WHERE m.candidate_id=c.id AND m.direction='inbound') AS replies
                FROM candidates c LEFT JOIN accounts a ON a.id=c.account_id
                WHERE c.external_id=? ORDER BY c.updated_at DESC""",
                (talent_slug,),
            )
        ]
    return {"contact": contact, "local": local, "source": "talent_profiles"}



@app.get("/api/admin/ops-log")
async def admin_ops_log(limit: int = Query(80, ge=1, le=300)) -> dict:
    """Recent hold / refusal / skip details for the Control center log panel (cards show short hints only)."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT id, account_id, account_label, level, hint, detail, created_at FROM ops_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.get("/api/admin/overview")
async def admin_overview() -> dict:
    prune_empty_orphan_accounts()
    with connection() as conn:
        accounts = conn.execute("SELECT id, label, paused, last_seen, created_at FROM accounts ORDER BY created_at").fetchall()
    # Refresh talent_profiles daily + lifetime totals (async — does not block the event loop).
    for a in accounts:
        if settings.supabase_url and settings.supabase_key:
            try:
                await profiles_sent_today(a["id"])
                await profiles_outreach_totals(a["id"])
            except Exception:
                pass
    rows = [account_overview(a["id"], a["label"], bool(a["paused"]), a["last_seen"]) for a in accounts]
    # Mirror: Control center only lists extensions that are currently alive (heartbeating).
    online_rows = [r for r in rows if r["extension_online"]]
    ledger_error = None
    try:
        await TalentProfiles().columns()
        ledger_ready = True
    except Exception as exc:
        ledger_ready, ledger_error = None, ledger_problem(exc) if settings.supabase_url and settings.supabase_key else None
    totals = {"accounts": len(online_rows), "offline": 0, "connected": sum(r["himalayas_authorized"] for r in online_rows),
              "paused": sum(r["paused"] for r in online_rows),
              "automation_running": sum(1 for r in online_rows if r["automation"]["running"]), "members": sum(r["members"] for r in online_rows),
              "unread": sum(r.get("unread", 0) for r in online_rows)}
    for status in ("sent", "failed", "skipped"):
        key = "skip" if status == "skipped" else status
        totals[status] = sum((r.get("profiles_totals") or {}).get(key, 0) for r in online_rows)
    return {
        "server": {"ok": True, "time": utc_now()}, "totals": totals, "accounts": online_rows,
        "holds": [{"key": key, "reason": hold["reason"], "since": hold["since"]} for key, hold in holds.items()],
        "checks": {"openrouter_key": bool(settings.openrouter_api_key), "supabase": bool(settings.supabase_url and settings.supabase_key) and not ledger_error, "supabase_error": ledger_error,
                   "ledger_status_ready": ledger_ready, "github": settings_ready()},
    }


class PauseRequest(BaseModel):
    paused: bool


@app.post("/api/admin/accounts/{account_id}/pause")
async def admin_pause(account_id: str, request: PauseRequest) -> dict:
    with connection() as conn:
        updated = conn.execute("UPDATE accounts SET paused=? WHERE id=?", (1 if request.paused else 0, account_id)).rowcount
    if not updated:
        raise HTTPException(status_code=404, detail="Account not found")
    if not request.paused:
        holds.pop(account_id, None)  # the person looked at the problem and chose to continue
        holds.pop("first:" + account_id, None)
        rejection_streak.pop(account_id, None)
        next_first_message_at.pop(account_id, None)
        next_reply_at.pop(account_id, None)
    publish_dashboard_update("paused" if request.paused else "resumed", account_id)
    return {"id": account_id, "paused": request.paused}


def clear_account_runtime(account_id: str) -> None:
    """Drop in-memory state for a profile that is being removed from the Control center."""
    task = automation_tasks.pop(account_id, None)
    if task and not task.done():
        task.cancel()
    for store in (automation_states, reply_monitor_states, delivery_states, last_seen_written, loop_crashes,
                  next_first_message_at, refusal_times, rejection_streak, next_reply_at, outage_step, verify_locks):
        store.pop(account_id, None)
    holds.pop(account_id, None)
    holds.pop("first:" + account_id, None)
    known_accounts.discard(account_id)


def delete_account_data(account_id: str) -> bool:
    """Stop automation and erase one profile's local rows. Returns False if the account was already gone."""
    remember_automation(account_id, None)
    clear_account_runtime(account_id)
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
            return False
        candidate_ids = [row[0] for row in conn.execute("SELECT id FROM candidates WHERE account_id=?", (account_id,))]
        if candidate_ids:
            placeholders = ",".join("?" * len(candidate_ids))
            conn.execute(f"DELETE FROM himalayas_check WHERE candidate_id IN ({placeholders})", candidate_ids)
            conn.execute(f"DELETE FROM messages WHERE candidate_id IN ({placeholders})", candidate_ids)
        conn.execute("DELETE FROM messages WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM candidates WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM oauth_tokens WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM oauth_state WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM processed_inbound WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM app_state WHERE key LIKE ?", (f"%{account_id}%",))
        conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    return True


def prune_empty_orphan_accounts() -> list[str]:
    """Drop unused offline profiles (no members, never logged in). Typical cause: popup and service worker
    each minted an account id on first install before storage settled."""
    removed: list[str] = []
    with connection() as conn:
        rows = conn.execute("SELECT id, last_seen FROM accounts").fetchall()
        for row in rows:
            if extension_online(row["last_seen"]):
                continue
            if conn.execute("SELECT 1 FROM candidates WHERE account_id=? LIMIT 1", (row["id"],)).fetchone():
                continue
            token = conn.execute("SELECT access_token FROM oauth_tokens WHERE account_id=?", (row["id"],)).fetchone()
            if token and token["access_token"]:
                continue
            removed.append(row["id"])
    for account_id in removed:
        if delete_account_data(account_id):
            publish_dashboard_update("account_removed", account_id)
    return removed


@app.delete("/api/admin/accounts/{account_id}")
async def admin_delete_account(account_id: str) -> dict:
    """Remove a Chrome profile from the Control center. Stops its automation and deletes its local data."""
    if not ACCOUNT_ID_PATTERN.match(account_id):
        raise HTTPException(status_code=400, detail="Invalid account id")
    if not delete_account_data(account_id):
        raise HTTPException(status_code=404, detail="Account not found")
    publish_dashboard_update("account_removed", account_id)
    return {"ok": True, "id": account_id}


class BulkRequest(BaseModel):
    action: Literal["start_automation", "stop_automation", "pause", "resume"]
    account_ids: list[str] | None = None  # every account when omitted


@app.post("/api/admin/bulk")
async def admin_bulk(request: BulkRequest) -> dict:
    with connection() as conn:
        known = [row["id"] for row in conn.execute("SELECT id FROM accounts ORDER BY created_at")]
    targets = [a for a in (request.account_ids or known) if a in known]
    results: dict[str, str] = {}
    for account_id in targets:
        if request.action == "start_automation":
            if not oauth_status(account_id):
                results[account_id] = "skipped: Himalayas login expired" if login_state(account_id) == "expired" else "skipped: Himalayas is not connected"
            elif is_paused(account_id):
                results[account_id] = "skipped: sending is paused"
            else:
                await start_automation(account_id)
                results[account_id] = "started"
        elif request.action == "stop_automation":
            await stop_automation(account_id)
            results[account_id] = "stopped"
        else:
            await admin_pause(account_id, PauseRequest(paused=request.action == "pause"))
            results[account_id] = "paused" if request.action == "pause" else "resumed"
    return {"action": request.action, "results": results}


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/admin/")


# ---- Check the bot's chat histories against what Himalayas really holds ----

verify_locks: dict[str, asyncio.Lock] = {}
VERIFY_INTERVAL_SECONDS = 1200


async def verify_with_himalayas(account_id: str, import_replies: bool = True) -> dict:
    """Read every conversation straight from Himalayas and compare it with the bot's records.
    It saves the result per member (message counts, read receipts), and imports a reply the bot has not recorded yet.
    Nothing is sent and nothing is deleted."""
    async with verify_locks.setdefault(account_id, asyncio.Lock()):
        client = HimalayasMCP(account_id)
        rooms = await client.rooms_by_slug()
        with connection() as conn:
            candidates = conn.execute("SELECT * FROM candidates WHERE account_id=?", (account_id,)).fetchall()
            messages = conn.execute("SELECT candidate_id, direction, status, body, COALESCE(sent_at, created_at) AS at FROM messages WHERE account_id=? AND status NOT IN ('failed', 'superseded', 'skipped', 'scheduled', 'approved', 'queued')", (account_id,)).fetchall()
        # Messages from before the login switched to another Himalayas company live in that company's inbox, not in this one.
        # They are not expected here, and their earlier check results stay as they were.
        cutoff = company_switch_time(account_id)
        if cutoff:
            messages = [m for m in messages if (m["at"] or "") >= cutoff]
        by_candidate: dict[int, list] = {}
        for message in messages:
            by_candidate.setdefault(message["candidate_id"], []).append(message)
        targets = [c for c in candidates if c["external_id"] in rooms or by_candidate.get(c["id"])]
        gate = asyncio.Semaphore(4)
        summary = {"conversations_on_himalayas": len(rooms), "checked": 0, "match": 0, "mismatch": [], "missing_on_himalayas": [], "replies_imported": 0, "not_in_bot": [r["name"] for slug, r in rooms.items() if slug not in {c["external_id"] for c in candidates}]}

        async def check(candidate) -> None:
            slug = candidate["external_id"]
            mine = by_candidate.get(candidate["id"], [])
            room = rooms.get(slug, {}).get("room")
            note, thread = "", {"count": 0, "messages": []}
            if room:
                async with gate:
                    for attempt in range(2):
                        try:
                            thread = await client.get_thread(room)
                            note = ""
                            break
                        except Exception as exc:
                            note = f"could not read: {type(exc).__name__} {str(exc)[:80]}"
                            await asyncio.sleep(2)
            elif any(m["direction"] == "outbound" and m["status"] == "sent" for m in mine):
                note = "no conversation on Himalayas"
            company_text = " ".join(letters_only(m["body"]) for m in thread["messages"] if m["who"] == "company")
            missing = [m for m in mine if m["direction"] == "outbound" and m["status"] == "sent" and letters_only(m["body"])[:40] not in company_text]
            known_replies = {letters_only(m["body"])[:40] for m in mine if m["direction"] == "inbound"}
            new_replies = [m for m in thread["messages"] if m["who"] == "member" and letters_only(m["body"])[:40] not in known_replies]
            if import_replies:
                for reply in reversed(new_replies):  # oldest first
                    if await respond_to_inbound(account_id, candidate, reply["body"], hashlib.sha256(f"{room}\n{reply['body']}".encode()).hexdigest(), utc_now()):
                        summary["replies_imported"] += 1
            matches = bool(room) and not missing and (not new_replies or import_replies) and not note
            with connection() as conn:
                conn.execute(
                    "INSERT INTO himalayas_check (candidate_id, account_id, checked_at, room, total, ours, theirs, ours_read, last_when, matches, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(candidate_id) DO UPDATE SET checked_at=excluded.checked_at, room=excluded.room, total=excluded.total, ours=excluded.ours, theirs=excluded.theirs, "
                    "ours_read=excluded.ours_read, last_when=excluded.last_when, matches=excluded.matches, note=excluded.note",
                    (candidate["id"], account_id, utc_now(), room, len(thread["messages"]), sum(m["who"] == "company" for m in thread["messages"]), sum(m["who"] == "member" for m in thread["messages"]),
                     sum(m["read"] for m in thread["messages"] if m["who"] == "company"), thread["messages"][0]["when"] if thread["messages"] else None, 1 if matches else 0, note or ("; ".join(f"{len(x)} not on Himalayas" for x in (missing,) if x)) or None),
                )
            summary["checked"] += 1
            if matches:
                summary["match"] += 1
            else:
                summary["mismatch"].append(candidate["name"])
            if missing:
                summary["missing_on_himalayas"].append(candidate["name"])

        await asyncio.gather(*(check(candidate) for candidate in targets))
    publish_dashboard_update("verified", account_id)
    return summary


@app.post("/api/db/verify-himalayas")
async def verify_himalayas(account: Account, import_replies: bool = True) -> dict:
    if not oauth_status(account):
        raise HTTPException(status_code=409, detail="Himalayas is not connected for this account")
    try:
        return await verify_with_himalayas(account, import_replies)
    except (MCPError, LoginExpired) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/conversations/{candidate_id}/himalayas")
async def conversation_on_himalayas(candidate_id: int, account: Account) -> dict:
    """The member's conversation exactly as Himalayas holds it right now (newest message first)."""
    with connection() as conn:
        candidate = conn.execute("SELECT id, name, external_id, profile_url FROM candidates WHERE id=? AND account_id=?", (candidate_id, account)).fetchone()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    client = HimalayasMCP(account)
    try:
        room = (await client.rooms_by_slug()).get(candidate["external_id"], {}).get("room")
        thread = await client.get_thread(room) if room else {"count": 0, "room": None, "messages": []}
    except (MCPError, LoginExpired) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {**thread, "profile_url": candidate["profile_url"], "name": candidate["name"]}


async def himalayas_check_loop() -> None:
    """A safety net: every 20 minutes, compare with Himalayas and import a reply that the normal monitor did not see."""
    await asyncio.sleep(120)
    while True:
        for account_id in authorized_accounts():
            try:
                await verify_with_himalayas(account_id, import_replies=True)
            except Exception as exc:
                print(f"Himalayas check failed for {account_id}: {exc}")
        await asyncio.sleep(VERIFY_INTERVAL_SECONDS)


# ---- Database cleanup (this account only) ----

class CleanRequest(BaseModel):
    mode: Literal["range", "all"]
    from_time: str | None = None      # inclusive, ISO time
    before_time: str | None = None    # exclusive, ISO time
    dry_run: bool = True
    confirm: str | None = None


def parse_time(value: str | None, fallback: datetime | None = None) -> datetime | None:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date: {value}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def stored_time(value: str | None) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except ValueError:
        parsed = None
    if parsed is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def select_for_cleanup(account_id: str, request: CleanRequest) -> dict:
    """Members whose last activity is in the range (or all members), with their message counts."""
    start, end = parse_time(request.from_time), parse_time(request.before_time)
    if request.mode == "range" and not (start or end):
        raise HTTPException(status_code=400, detail="Choose at least one date")
    if start and end and start >= end:
        raise HTTPException(status_code=400, detail="The From date must be before the To date")
    with connection() as conn:
        candidates = conn.execute("SELECT id, created_at FROM candidates WHERE account_id=?", (account_id,)).fetchall()
        messages = conn.execute("SELECT id, candidate_id, direction, status, external_id, sent_at, created_at FROM messages WHERE account_id=?", (account_id,)).fetchall()
    by_candidate: dict[int, list] = {}
    for message in messages:
        by_candidate.setdefault(message["candidate_id"], []).append(message)
    ids, message_rows = [], []
    for candidate in candidates:
        history = by_candidate.get(candidate["id"], [])
        activity = max([stored_time(candidate["created_at"])] + [stored_time(m["sent_at"] or m["created_at"]) for m in history])
        if request.mode == "all" or ((not start or activity >= start) and (not end or activity < end)):
            ids.append(candidate["id"])
            message_rows.extend(history)
    by_status: dict[str, int] = {}
    for message in message_rows:
        if message["direction"] == "outbound":
            by_status[message["status"]] = by_status.get(message["status"], 0) + 1
    return {
        "ids": ids, "messages": message_rows, "members": len(ids), "message_count": len(message_rows),
        "cancelled": sum(by_status.get(status, 0) for status in ("scheduled", "approved", "queued")), "outbound_by_status": by_status,
    }


@app.post("/api/db/clean")
async def db_clean(request: CleanRequest, account: Account) -> dict:
    """Delete members and their messages. Preview first (dry_run), then confirm.
    Login, settings, and the shared Supabase ledger are never touched, so nobody who was already contacted is messaged again."""
    selection = select_for_cleanup(account, request)
    summary = {"members": selection["members"], "messages": selection["message_count"], "queued_cancelled": selection["cancelled"], "outbound_by_status": selection["outbound_by_status"]}
    if request.dry_run:
        return {"dry_run": True, **summary}
    if request.mode == "all" and request.confirm != "DELETE":
        raise HTTPException(status_code=400, detail="Type DELETE to confirm deleting all data")
    task = automation_tasks.get(account)
    if task and not task.done():
        raise HTTPException(status_code=409, detail="Stop the automatic campaign first")
    if delivery_state(account)["current_id"]:
        raise HTTPException(status_code=409, detail="A message is being sent right now. Try again in a few seconds")
    if not selection["ids"]:
        return {"dry_run": False, **summary, "backup": None}
    backup = backup_database()
    ids = selection["ids"]
    seen_inbound = [(m["external_id"], account, utc_now()) for m in selection["messages"] if m["direction"] == "inbound" and m["external_id"]]
    with connection() as conn:
        # Remember replies that were already handled, so a re-imported member is not answered a second time.
        conn.executemany("INSERT OR IGNORE INTO processed_inbound (external_id, account_id, deleted_at) VALUES (?, ?, ?)", seen_inbound)
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM messages WHERE account_id=? AND candidate_id IN ({marks})", [account, *chunk])
            conn.execute(f"DELETE FROM candidates WHERE account_id=? AND id IN ({marks})", [account, *chunk])
    for candidate_id in ids:
        candidate_locks.pop(candidate_id, None)
        last_invitation_try.pop(candidate_id, None)
    vacuum()
    publish_dashboard_update("database_cleaned", account)
    return {"dry_run": False, **summary, "backup": backup}


# Errors that happen BEFORE anything is sent (ledger check, login). Retrying these cannot send a message twice.
NOT_SENT_ERRORS = (
    "Ledger schema check failed", "Contact lookup failed", "SUPABASE_URL and SUPABASE_KEY are not configured",
    "Himalayas login expired", "Himalayas authorization required", "Himalayas access token expired",
    "Himalayas rejected", "Himalayas did not return a conversation", "Himalayas cannot reopen", "Not delivered:",
)


class RetryFailedRequest(BaseModel):
    dry_run: bool = True
    include_unavailable: bool = False  # also retry members whose conversation Himalayas cannot reopen (pointless until that is fixed)
    rewrite: bool = False  # write the first messages again in the current short, neutral style before queueing them


@app.post("/api/db/retry-failed")
async def retry_failed(request: RetryFailedRequest, account: Account) -> dict:
    """Queue again every failed message that never left because of a ledger, login, or Himalayas refusal.
    With rewrite, first messages that were not delivered (failed or still queued) get new text first, because a message that
    Himalayas refused as spam would be refused again. Messages that failed after a send was attempted are left alone."""
    with connection() as conn:
        failed = conn.execute("SELECT id, candidate_id, stage, error FROM messages WHERE account_id=? AND direction='outbound' AND status='failed' ORDER BY created_at, id", (account,)).fetchall()
        waiting = conn.execute("SELECT id, candidate_id, stage FROM messages WHERE account_id=? AND direction='outbound' AND status IN ('scheduled', 'approved') AND stage='first_sent' AND " + TRUE_FIRST_SQL + " ORDER BY created_at, id", (account,)).fetchall() if request.rewrite else []
        answered = {row["candidate_id"] for row in conn.execute("SELECT DISTINCT candidate_id FROM messages WHERE account_id=? AND direction='outbound' AND status='sent'", (account,))}
    with connection() as conn:
        # A failed message that already has a newer message for the same member (queued, sent, or skipped) must not be queued again.
        replaced = {row["candidate_id"] for row in conn.execute("SELECT DISTINCT candidate_id FROM messages WHERE account_id=? AND direction='outbound' AND stage='first_sent' AND status IN ('scheduled', 'approved', 'sent', 'skipped')", (account,))}
    unavailable = [row for row in failed if "Himalayas cannot reopen" in (row["error"] or "")]
    safe = [row for row in failed if row["candidate_id"] not in replaced and any(pattern in (row["error"] or "") for pattern in NOT_SENT_ERRORS)
            and (request.include_unavailable or "Himalayas cannot reopen" not in (row["error"] or ""))]
    # Only a member's real first message is rewritten. A message to a member who already got one (an answer or introduction) is not.
    to_rewrite = [row for row in [*safe, *waiting] if row["stage"] == "first_sent" and row["candidate_id"] not in answered] if request.rewrite else []
    result = {"dry_run": request.dry_run, "safe": len(safe), "other": sum(1 for row in failed if row["candidate_id"] not in replaced and row not in safe and row not in unavailable), "unavailable": len(unavailable), "waiting": len(waiting), "rewrite": len(to_rewrite), "retried": 0, "rewritten": 0}
    if request.dry_run or not (safe or waiting):
        return result
    bodies: dict[int, str] = {}
    if to_rewrite:
        gate = asyncio.Semaphore(settings.message_generation_concurrency)
        recent = recent_first_messages(account)

        async def rewrite(row) -> None:
            with connection() as conn:
                candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (row["candidate_id"],)).fetchone()
            item = parse_candidate(candidate)
            async with gate:
                fresh = await write_first_message(item, fixed_role=candidate["suggested_role"] or None, recent=list(recent), account_id=account)
                recent.insert(0, fresh["message"])
            bodies[row["id"]] = fresh["message"]

        try:
            await asyncio.gather(*(rewrite(row) for row in to_rewrite))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not rewrite the messages: {exc}") from exc
    queue = [*safe, *waiting]
    with connection() as conn:
        when = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()  # due now. The sender keeps the normal gap between messages.
        for row in queue:
            if row["id"] in bodies:
                conn.execute("UPDATE messages SET body=?, status='scheduled', error=NULL, send_after=? WHERE id=?", (bodies[row["id"]], when, row["id"]))
            else:
                conn.execute("UPDATE messages SET status='scheduled', error=NULL, send_after=? WHERE id=?", (when, row["id"]))
    publish_dashboard_update("messages_scheduled", account)
    return {**result, "retried": len(safe), "rewritten": len(bodies)}


@app.post("/api/messages/{message_id}/retry")
async def retry_message(message_id: int, account: Account) -> dict:
    send_after = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    with connection() as conn:
        updated = conn.execute(
            "UPDATE messages SET status='scheduled', error=NULL, send_after=? WHERE id=? AND account_id=? AND status='failed'",
            (send_after, message_id, account),
        ).rowcount
    if not updated:
        raise HTTPException(status_code=404, detail="Failed message not found")
    with connection() as conn:
        row = conn.execute(
            """SELECT m.id, m.candidate_id, m.body, c.external_id AS candidate_external_id, c.name AS candidate_name, c.profile_url AS candidate_profile_url,
            c.summary AS candidate_summary, c.category AS candidate_category, c.stack_json AS candidate_stack_json, c.country AS candidate_country,
            c.suggested_role AS candidate_suggested_role
            FROM messages m JOIN candidates c ON c.id=m.candidate_id WHERE m.id=?""",
            (message_id,),
        ).fetchone()
    if row and message_id == await first_outbound_message_id(row["candidate_id"]):
        if await note_ledger(account, ledger_candidate(row), "queued", message_id, row["body"]) is False:
            with connection() as conn:
                conn.execute(
                    "UPDATE messages SET status='skipped', error=? WHERE id=?",
                    ("Already claimed by another profile (shared contact ledger)", message_id),
                )
            return {"retried": False, "skipped": True, "reason": "Already claimed by another profile"}
    publish_dashboard_update("messages_scheduled", account)
    return {"retried": True}


@app.get("/api/admin/diagnose")
async def diagnose(account: Account, probe: str = "") -> dict:
    """Read-only checks of what Himalayas answers right now. Nothing is created and nothing is sent."""
    client = HimalayasMCP(account)
    report: dict = {}
    async def attempt(name: str, action) -> None:
        try:
            report[name] = await action()
        except Exception as exc:
            report[name] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    async def tools():
        reply = await client.rpc("tools/list")
        listed = (reply.get("result") or {}).get("tools", [])
        return {"http": reply.get("http"), "tools": {t["name"]: sorted((t.get("inputSchema") or {}).get("properties", {})) for t in listed}, "error": reply.get("error")}
    await attempt("tools", tools)
    async def search():
        result = await client.call("search_talent", {"page": 1, "sort": "recent"})
        return {"ok": True, "chars": len(result_text(result))}
    await attempt("search_talent", search)
    async def conversations():
        result = await client.call("list_conversations", {})
        return {"ok": True, "text": result_text(result)[:200]}
    await attempt("list_conversations", conversations)
    async def my_profile():
        return {"ok": True, "text": result_text(await client.call("get_my_profile", {}))[:500]}
    if probe:  # opens an EMPTY conversation with this member (no message is sent). The room name shows which company the login acts as.
        async def open_probe():
            room = await client.open_room(probe)
            return {"ok": True, "room": room}
        await attempt("start_conversation", open_probe)
    await attempt("get_my_profile", my_profile)
    async def company_profile():
        return {"ok": True, "text": result_text(await client.call("get_company_profile", {}))[:500]}
    await attempt("get_company_profile", company_profile)
    return report


@app.post("/api/db/retry-replies")
async def retry_replies(account: Account) -> dict:
    """Write again, in different words, the follow-up messages that Himalayas refused, and queue them. Only members whose last message
    is such a refused reply, and who have nothing waiting. A message that was refused is never sent again as it was."""
    with connection() as conn:
        rows = conn.execute(
            """SELECT m.* FROM messages m WHERE m.account_id=? AND m.direction='outbound' AND m.status='failed' AND m.stage IS NOT NULL AND m.stage != 'closed'
            AND EXISTS (SELECT 1 FROM messages s WHERE s.candidate_id=m.candidate_id AND s.direction='outbound' AND s.status='sent' AND s.id<m.id)
            AND m.id=(SELECT MAX(x.id) FROM messages x WHERE x.candidate_id=m.candidate_id AND x.direction='outbound' AND x.status!='superseded')""",
            (account,)).fetchall()
    queued = 0
    for row in rows:
        if already_sent_this_step(row["candidate_id"], row["stage"], row["id"]):
            continue  # a message for this step was delivered since, so this failed one is not owed any more
        new_id = await (requeue_intro(row, account) if row["stage"] == "intro_sent" else requeue_rephrased(row, account))
        queued += 1 if new_id else 0
    if queued:
        publish_dashboard_update("messages_scheduled", account)
    return {"failed_replies": len(rows), "queued": queued}


@app.post("/api/db/retry-unreachable")
async def retry_unreachable(account: Account) -> dict:
    """Queue again the first messages of members whose conversation Himalayas could not reopen.

    That often happens when the conversation already exists for the company (another profile opened it,
    or start_conversation created it but failed to return the room). After a fix that recovers existing
    rooms, or after the login changes to another company, retrying is useful.
    """
    with connection() as conn:
        ids = [row["id"] for row in conn.execute(
            """SELECT m.id FROM messages m WHERE m.account_id=? AND m.direction='outbound' AND m.status='failed' AND m.error LIKE 'Himalayas cannot reopen%'
            AND NOT EXISTS (SELECT 1 FROM messages x WHERE x.candidate_id=m.candidate_id AND x.direction='outbound' AND x.id>m.id AND x.status!='failed')""",
            (account,))]
    for message_id in ids:
        await retry_message(message_id, account)
    return {"requeued": len(ids)}


@app.post("/api/db/retry-outage")
async def retry_outage(account: Account) -> dict:
    """Queue again the messages that failed only because Himalayas could not open the conversation (for example error 406).
    Nothing was sent for them, and no filter refused them, so the same text is fine."""
    with connection() as conn:
        ids = [row["id"] for row in conn.execute(
            """SELECT m.id FROM messages m WHERE m.account_id=? AND m.direction='outbound' AND m.status='failed' AND m.error LIKE '%Failed to start conversation%'
            AND NOT EXISTS (SELECT 1 FROM messages x WHERE x.candidate_id=m.candidate_id AND x.direction='outbound' AND x.id>m.id AND x.status!='failed')""",
            (account,))]
    for message_id in ids:
        await retry_message(message_id, account)
    return {"requeued": len(ids)}


@app.get("/api/events")
async def events(account: StreamAccount) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    dashboard_subscribers[queue] = account

    async def stream():
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"event: dashboard-update\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            dashboard_subscribers.pop(queue, None)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/admin/events")
async def admin_events() -> StreamingResponse:
    """Live updates for the Control center across every profile (extension uses /api/events for one account)."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    dashboard_subscribers[queue] = "*"

    async def stream():
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"event: dashboard-update\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            dashboard_subscribers.pop(queue, None)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# Gap between first messages, enforced when they are sent (not only when they are scheduled). After a pause, a restart, or a long
# outage every queued message is overdue, and sending them back to back is what a spam filter looks for.
next_first_message_at: dict[str, float] = {}


def note_first_message_sent(account_id: str) -> None:
    next_first_message_at[account_id] = time.monotonic() + first_message_gap()


def cooling_accounts() -> list[str]:
    now = time.monotonic()
    return [account for account, until in next_first_message_at.items() if until > now]


# Himalayas' spam filter refuses some messages at random: most refused members were accepted a moment later with different wording.
# So a refusal never changes the pace. That member gets a different message and the bot carries on.
# Only refusals of several DIFFERENT members in a row (no first message accepted in between) suggest a real block. Then FIRST messages
# wait a few minutes and start again by themselves. Replies to members who already answered are never held back by this.
REFUSED_MEMBERS_BEFORE_HOLD = 4
FIRST_HOLD_SECONDS = 600
# A refused message is rewritten in different wording and sent again AT ONCE, and again, until it is delivered. Nothing was sent, so no
# gap is needed. The cap is a guard against an endless loop when the problem is the member and not the wording.
MAX_REFUSALS_PER_MEMBER = 6
RETRY_PAUSE_SECONDS = (1.0, 3.0)  # a moment between tries, only so that the tries are not simultaneous
# If Himalayas has really blocked the account, retrying would burn through refusals within minutes. Many refusals in a short time
# put FIRST messages on the same self-releasing hold. Ordinary retrying stays far below this.
REFUSALS_PER_WINDOW = 15
REFUSAL_WINDOW_SECONDS = 600
refusal_times: dict[str, list[float]] = {}
REPLY_GAP_SECONDS = (20, 60)  # replies to different members are also spaced a little
rejection_streak: dict[str, list[int]] = {}  # the different members refused in a row, per account. A first message that is accepted resets it.
next_reply_at: dict[str, float] = {}
# A member's first message is one that has no sent message before it. (A member's answer can carry the stage "first_sent" too.)
TRUE_FIRST_SQL = "NOT EXISTS (SELECT 1 FROM messages s WHERE s.candidate_id=messages.candidate_id AND s.direction='outbound' AND s.status='sent')"


def day_start() -> datetime:
    """The day for the daily limit is the UTC day. It starts at 00:00 UTC."""
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def first_messages_today(account_id: str) -> int:
    """New members messaged today by this profile: first messages that were sent (replies are not counted)."""
    with connection() as conn:
        return conn.execute(
            """SELECT COUNT(*) FROM messages m WHERE m.account_id=? AND m.direction='outbound' AND m.status='sent' AND m.sent_at >= ?
            AND m.id=(SELECT MIN(x.id) FROM messages x WHERE x.candidate_id=m.candidate_id AND x.direction='outbound' AND x.status='sent')""",
            (account_id, max(day_start().isoformat(), company_switch_time(account_id))),  # a company that has just been connected starts from zero
        ).fetchone()[0]


# talent_profiles daily send counts (UTC day). Updated on send and by async probes — never sync HTTP in request handlers.
_profiles_daily_cache: dict[str, tuple[str, int]] = {}  # account_id → (utc_day_iso_date, count)


def _cache_profiles_daily(account_id: str, sent: int) -> None:
    _profiles_daily_cache[account_id] = (day_start().date().isoformat(), max(0, int(sent)))


def _cached_profiles_daily(account_id: str) -> int | None:
    row = _profiles_daily_cache.get(account_id)
    if not row:
        return None
    day, count = row
    if day != day_start().date().isoformat():
        return None
    return count


def daily_status(account_id: str) -> dict:
    """Daily first-message count. Prefer cached talent_profiles count (outreach source of truth)."""
    limit = settings.daily_dm_limit
    local = first_messages_today(account_id)
    cached = _cached_profiles_daily(account_id)
    sent = max(local, cached if cached is not None else 0)
    reset = day_start() + timedelta(days=1)
    return {
        "limit": limit,
        "sent_today": sent,
        "remaining": max(0, limit - sent) if limit else None,
        "reached": bool(limit) and sent >= limit,
        "resets_at": reset.isoformat(),
        "resets_in_seconds": int((reset - datetime.now(timezone.utc)).total_seconds()),
    }


def daily_limit_reason(status: dict) -> str:
    return f"Daily limit reached: {status['sent_today']} of {status['limit']} new members messaged today. Sending resumes at 00:00 UTC"


def capped_accounts() -> list[str]:
    """Profiles that have reached the daily limit. Their first messages wait until tomorrow. Replies are not affected."""
    if not settings.daily_dm_limit:
        return []
    with connection() as conn:
        accounts = [row["id"] for row in conn.execute("SELECT id FROM accounts")]
    return [account for account in accounts if daily_status(account)["reached"]]


def already_sent_this_step(candidate_id: int, stage: str | None, exclude_id: int | None = None) -> bool:
    """True when a message for this step was already DELIVERED to the member, and the member has not written since.
    The conversation goes one message at a time: the member answers, then we answer. A second message for the same step is a duplicate.
    It happens when several rewritten copies of one message are waiting, and one of them was accepted."""
    with connection() as conn:
        return conn.execute(
            """SELECT 1 FROM messages x WHERE x.candidate_id=? AND x.direction='outbound' AND x.status='sent' AND COALESCE(x.stage, 'first_sent')=? AND x.id!=?
            AND COALESCE(x.sent_at, x.created_at) > COALESCE((SELECT MAX(i.created_at) FROM messages i WHERE i.candidate_id=? AND i.direction='inbound'), '') LIMIT 1""",
            (candidate_id, stage or "first_sent", exclude_id or 0, candidate_id),
        ).fetchone() is not None


def company_switch_time(account_id: str) -> str:
    """When the Himalayas login last changed to another company (ISO time), or an empty string."""
    try:
        return json.loads(get_state(f"himalayas_company_change:{account_id}") or "{}").get("at", "")
    except ValueError:
        return ""


def first_message_gap() -> float:
    """Seconds to wait between first messages: a random value between the configured minimum and maximum."""
    low = settings.min_message_delay_seconds
    return random.uniform(low, max(low, settings.max_message_delay_seconds))


def recent_first_messages(account_id: str, limit: int = 15) -> list[str]:
    """Newest first messages used as anti-clone context. Accepted winners come first so new drafts match what works."""
    winners = winning_first_messages(min(8, limit))
    with connection() as conn:
        rows = conn.execute(
            "SELECT body FROM messages WHERE account_id=? AND direction='outbound' AND stage='first_sent' AND status IN ('sent', 'scheduled', 'approved') ORDER BY id DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
    local = [row["body"] for row in rows if row["body"]]
    merged: list[str] = []
    seen: set[str] = set()
    for text in [*winners, *local]:
        if text and text not in seen:
            seen.add(text)
            merged.append(text)
        if len(merged) >= limit:
            break
    return merged


HOLD_SECONDS = 60
# Himalayas caps send_message at 10/minute. Wait just over a minute, then retry.
# Must also cool the whole account: delaying only one message lets the rest of the overdue
# queue fire immediately and hit the same cap again, so nothing appears to send.
RATE_LIMIT_HOLD_SECONDS = 75
# After this many refusals for one member, switch to the neutral/safer template before giving up at MAX_REFUSALS_PER_MEMBER.
SAFER_AFTER_REFUSALS = 2
holds: dict[str, dict] = {}  # "ledger" for the shared contact ledger, or an account id for its Himalayas login


def set_hold(key: str, reason: str) -> None:
    since = holds.get(key, {}).get("since") or utc_now()
    holds[key] = {"reason": reason, "since": since, "until": time.monotonic() + HOLD_SECONDS}


def hold_active(key: str) -> bool:
    hold = holds.get(key)
    if not hold:
        return False
    if time.monotonic() < hold["until"]:
        return True
    if key.startswith("first:"):  # a hold on first messages ends by itself, and the count of refusals starts again
        holds.pop(key, None)
        rejection_streak.pop(key[len("first:"):], None)
    return False


def hold_reason(account_id: str) -> str | None:
    hold_active("first:" + account_id)  # clears the hold if it has run out
    hold = holds.get("ledger") or holds.get(account_id) or holds.get("first:" + account_id)
    return hold["reason"] if hold else None


def hold_first_messages(account_id: str, reason: str) -> None:
    """Hold FIRST messages for a few minutes. Replies to members who answered keep going. It ends by itself."""
    detail = f"Himalayas refused {REFUSED_MEMBERS_BEFORE_HOLD} different members in a row, so first messages wait {FIRST_HOLD_SECONDS // 60} minutes. Replies keep going. {reason[:160]}"
    holds["first:" + account_id] = {"reason": detail, "since": utc_now(), "until": time.monotonic() + FIRST_HOLD_SECONDS}
    delivery_state(account_id).update({"status": hold_hint(detail), "detail": detail, "current_id": None, "current_name": None})
    note_ops(account_id, "warn", hold_hint(detail), detail)
    publish_dashboard_update("paused", account_id)


def refusals_for_member(candidate_id: int) -> int:
    """Refusals in the current run of tries: those after the member's last accepted message. An accepted message starts the count again."""
    with connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM messages WHERE candidate_id=? AND direction='outbound' AND status='failed' AND error LIKE 'Himalayas rejected%' "
            "AND id > COALESCE((SELECT MAX(id) FROM messages WHERE candidate_id=? AND direction='outbound' AND status='sent'), 0)",
            (candidate_id, candidate_id),
        ).fetchone()[0]


INTRO_REFUSALS_BEFORE_NEXT_LEVEL = 3


def intro_level() -> int:
    return int(get_state("intro_level", "1"))


def note_intro_result(level: int, refused: bool) -> None:
    """Learn which wording of the company introduction Himalayas accepts. Three refusals in a row at a level move everyone up a level.
    An accepted introduction starts the count again. (Refusals are partly random, so two in a row is not enough to give up the website.)"""
    key = f"intro_refusals_level_{level}"
    if not refused:
        set_state(key, "0")
        return
    count = int(get_state(key, "0")) + 1
    set_state(key, str(count))
    if count >= INTRO_REFUSALS_BEFORE_NEXT_LEVEL and level < chat.ai.INTRO_LEVELS and intro_level() <= level:
        set_state("intro_level", str(level + 1))
        set_state(key, "0")
        upgrade_pending_intros()


def upgrade_pending_intros() -> int:
    """Introductions that are still waiting are written again at the current level, so none goes out in wording that is known to fail."""
    level = intro_level()
    changed = 0
    with connection() as conn:
        rows = conn.execute("SELECT m.id, m.candidate_id FROM messages m WHERE m.direction='outbound' AND m.stage='intro_sent' AND m.status IN ('scheduled', 'approved') AND COALESCE(m.variant, 0) < ?", (level,)).fetchall()
        for row in rows:
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (row["candidate_id"],)).fetchone()
            text = chat.ai.intro_message(parse_candidate(candidate), candidate["suggested_role"] or chat.ai.default_role(True), level)
            conn.execute("UPDATE messages SET body=?, variant=? WHERE id=?", (text, level, row["id"]))
            changed += 1
    return changed


async def requeue_intro(row, account_id: str) -> int | None:
    """A member answered, and Himalayas refused our company introduction. Write it again and queue it. Best effort.
    The first retry keeps the level (one refusal can be chance). After that it moves up a level."""
    try:
        with connection() as conn:
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (row["candidate_id"],)).fetchone()
        used = row["variant"] or 0
        level = max(intro_level(), used if refusals_for_member(row["candidate_id"]) <= 1 else min(used + 1, chat.ai.INTRO_LEVELS))
        if already_sent_this_step(row["candidate_id"], "intro_sent"):
            return None
        text = await chat.ai.write_intro(
            parse_candidate(candidate), candidate["suggested_role"] or chat.ai.default_role(True), level, account_id=account_id,
        )
        with connection() as conn:
            return conn.execute(
                "INSERT INTO messages (account_id, candidate_id, direction, body, status, send_after, stage, variant, created_at) VALUES (?, ?, 'outbound', ?, 'scheduled', ?, 'intro_sent', ?, ?)",
                (account_id, row["candidate_id"], text, utc_now(), level, utc_now()),
            ).lastrowid
    except Exception as exc:
        print(f"Could not write the introduction again after a refusal: {exc}")
        return None


async def requeue_rephrased(row, account_id: str) -> int | None:
    """Any other reply (process overview, assessment, answer, invitation) that Himalayas refused: the same facts in different words."""
    try:
        with connection() as conn:
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (row["candidate_id"],)).fetchone()
        if already_sent_this_step(row["candidate_id"], row["stage"]):
            return None
        text = await chat.ai.rephrase_reply(row["body"], chat.ai.greeting_name(candidate["name"]))
        if not text:
            return None
        with connection() as conn:
            return conn.execute(
                "INSERT INTO messages (account_id, candidate_id, direction, body, status, send_after, stage, variant, created_at) VALUES (?, ?, 'outbound', ?, 'scheduled', ?, ?, ?, ?)",
                (account_id, row["candidate_id"], text, utc_now(), row["stage"], row["variant"], utc_now()),
            ).lastrowid
    except Exception as exc:
        print(f"Could not rephrase a refused reply: {exc}")
        return None


async def requeue_rewritten(row, account_id: str, safer: bool = False) -> int | None:
    """After a spam refusal, write that member a different first message. It is due at once. Best effort.
    With safer=True the wording is shorter and more neutral (used after repeated refusals)."""
    try:
        with connection() as conn:
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (row["candidate_id"],)).fetchone()
        if already_sent_this_step(row["candidate_id"], "first_sent"):
            return None
        item = parse_candidate(candidate)
        if safer:
            # Still use the writer so accepted-style length/tone apply; pass refused bodies so wording changes.
            fresh = await write_first_message(
                item,
                fixed_role=candidate["suggested_role"] or chat.ai.default_role(True),
                recent=[row["body"], *recent_first_messages(account_id), *chat.ai.refused_first_messages(6)],
                account_id=account_id,
            )
        else:
            fresh = await write_first_message(
                item,
                fixed_role=candidate["suggested_role"] or None,
                recent=[row["body"], *recent_first_messages(account_id)],
                account_id=account_id,
            )
        with connection() as conn:
            new_id = conn.execute(
                "INSERT INTO messages (account_id, candidate_id, direction, body, status, send_after, stage, created_at) VALUES (?, ?, 'outbound', ?, 'scheduled', ?, 'first_sent', ?)",
                (account_id, row["candidate_id"], fresh["message"], utc_now(), utc_now()),
            ).lastrowid
        if await note_ledger(account_id, ledger_candidate(row), "queued", new_id, fresh["message"]) is False:
            with connection() as conn:
                conn.execute(
                    "UPDATE messages SET status='skipped', error=? WHERE id=?",
                    ("Already claimed by another profile (shared contact ledger)", new_id),
                )
            return None
        return new_id
    except Exception as exc:
        print(f"Could not write a new message after a refusal: {exc}")
        return None


def ledger_problem(exc: Exception) -> str:
    text = str(exc)
    if "401" in text or "Invalid API key" in text or "403" in text:
        return "Supabase rejected the key. Check the Supabase URL and key in Settings"
    if "not configured" in text:
        return "The Supabase URL and key are not set in Settings"
    return f"The contact ledger is not available ({text.splitlines()[0][:100]})"


OUTAGE_PAUSES = (60, 120, 240, 480, 900)  # seconds between tries while Himalayas does not open conversations. It grows, up to 15 minutes.
OUTAGE_TRIES_BEFORE_MEMBER_FAILS = 6
outage_step: dict[str, int] = {}  # account -> how many tries in a row failed this way
held_by_outage: dict[int, int] = {}  # message id -> how many times it was held this way
worked_since_held: set[int] = set()  # held messages that have seen Himalayas work for other members since. Only these can be a problem of their own


def hold_message(message_id: int, account_id: str, reason: str, seconds: int = HOLD_SECONDS) -> str:
    """Keep the message queued and try it again later. Nothing was sent, so nothing is lost and nothing is marked failed."""
    later = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    with connection() as conn:
        conn.execute("UPDATE messages SET send_after=? WHERE id=?", (later, message_id))
    hint = hold_hint(reason)
    delivery_state(account_id).update({"status": hint, "detail": reason, "current_id": None, "current_name": None})
    note_ops(account_id, "warn", hint, reason)
    publish_dashboard_update("delivery_held", account_id)
    return "held"


async def deliver(message_id: int) -> str:
    with connection() as conn:
        row = conn.execute(
            """SELECT m.*, c.external_id AS candidate_external_id, c.name AS candidate_name,
            c.profile_url AS candidate_profile_url, c.summary AS candidate_summary,
            c.category AS candidate_category, c.stack_json AS candidate_stack_json, c.country AS candidate_country,
            c.suggested_role AS candidate_suggested_role
            FROM messages m JOIN candidates c ON c.id=m.candidate_id WHERE m.id=?""",
            (message_id,),
        ).fetchone()
    if not row:
        return "failed"
    account_id = row["account_id"]
    state = delivery_state(account_id)
    if already_sent_this_step(row["candidate_id"], row["stage"], message_id):
        # Another copy of this message was already delivered to this member. Sending this one too would be a duplicate.
        with connection() as conn:
            conn.execute("UPDATE messages SET status='superseded', error='Not sent: this step was already delivered to the member' WHERE id=?", (message_id,))
        state.update({"status": "idle", "current_id": None, "current_name": None})
        publish_dashboard_update("message_skipped", account_id)
        return "skipped"
    blocked = contact_blocked_reason({
        "country": row["candidate_country"] or "",
        "summary": row["candidate_summary"] or "",
    })
    if blocked:
        with connection() as conn:
            sent_before = conn.execute(
                "SELECT 1 FROM messages WHERE candidate_id=? AND direction='outbound' AND status='sent' AND id!=? LIMIT 1",
                (row["candidate_id"], message_id),
            ).fetchone()
        if not sent_before:
            with connection() as conn:
                conn.execute("UPDATE messages SET status='skipped', error=? WHERE id=?", (blocked, message_id))
            state.update({"status": "skipped", "current_id": None, "current_name": None})
            publish_dashboard_update("message_skipped", account_id)
            return "skipped"
    if hold_active(account_id):  # this account's Himalayas login needs attention. Do not even try.
        return hold_message(message_id, account_id, holds[account_id]["reason"])
    # Know whether this is a first contact before marking "sending", so a rate-limit / first-message
    # hold does not flash "Sending to …" for a message that will only wait.
    first_contact = row["id"] == (await first_outbound_message_id(row["candidate_id"]))
    if first_contact:
        if hold_active("first:" + account_id):
            return hold_message(message_id, account_id, holds["first:" + account_id]["reason"])
        if hold_active("ledger"):
            return hold_message(message_id, account_id, holds["ledger"]["reason"])
        today = daily_status(account_id)
        if today["reached"]:
            return hold_message(message_id, account_id, daily_limit_reason(today), min(3600, max(60, today["resets_in_seconds"])))
    state["current_id"] = message_id
    state["status"] = "sending"
    state["detail"] = ""
    publish_dashboard_update("message_sending", account_id)
    state["current_name"] = row["candidate_name"]
    delivered = False  # becomes True once Himalayas has accepted the message
    try:
        ledger = None
        if first_contact:
            try:
                ledger = SupabaseLedger()
                already_contacted = await ledger.was_contacted(row["candidate_external_id"], account_id=account_id)
            except SupabaseError as exc:
                # The check that protects against messaging someone twice cannot run. Nothing was sent yet, so keep the
                # message queued (not failed) and try again shortly. This also stops a bad key from failing the whole queue.
                set_hold("ledger", ledger_problem(exc))
                return hold_message(message_id, account_id, holds["ledger"]["reason"])
            holds.pop("ledger", None)
            if already_contacted:
                with connection() as conn:
                    conn.execute(
                        "UPDATE messages SET status='skipped', error=? WHERE id=?",
                        ("Already contacted or claimed by another profile (shared contact ledger)", message_id),
                    )
                state.update({"status": "skipped", "current_id": None, "current_name": None})
                publish_dashboard_update("message_skipped", account_id)
                return "skipped"
        try:
            await HimalayasMCP(account_id).send_message(row["candidate_external_id"], row["body"], first_contact=first_contact)
        except LoginExpired as exc:
            set_hold(account_id, str(exc))
            return hold_message(message_id, account_id, str(exc))
        except AlreadyMessaged:
            # Someone already wrote to this member on Himalayas (for example by hand). Do not write again.
            with connection() as conn:
                conn.execute("UPDATE messages SET status='skipped', error='Already messaged on Himalayas (contacted before, for example by hand)' WHERE id=?", (message_id,))
            if first_contact and ledger is not None:
                try:
                    await ledger.record_contact({**ledger_candidate(row), "sent_at": utc_now(), "account_id": account_id, "account_label": account_label(account_id)}, message_id, row["body"])
                except SupabaseError as exc:
                    print(f"Could not record the existing contact in the ledger: {exc}")
            state.update({"status": "skipped", "current_id": None, "current_name": None})
            publish_dashboard_update("message_skipped", account_id)
            return "skipped"
        except ConversationUnavailable as exc:
            # A problem with this one member on Himalayas' side. It is not a spam refusal, so it does not count as a strike and
            # does not pause the account. The other members go on.
            with connection() as conn:
                conn.execute("UPDATE messages SET status='failed', error=? WHERE id=?", (str(exc), message_id))
            state.update({"status": "failed", "current_id": None, "current_name": None})
            if first_contact:
                await note_ledger(account_id, ledger_candidate(row), "failed", message_id, row["body"], str(exc))
            return "failed"
        except HimalayasUnavailable as exc:
            # Himalayas could not open the conversation and blames no message. If it happens again and again it is Himalayas or the
            # account, not the member, so the message is NOT failed. It stays queued and is tried again after a pause that grows.
            tries = held_by_outage[message_id] = held_by_outage.get(message_id, 0) + 1
            if tries > OUTAGE_TRIES_BEFORE_MEMBER_FAILS and message_id in worked_since_held:  # other members went through in between, so this member really cannot be opened
                with connection() as conn:
                    conn.execute("UPDATE messages SET status='failed', error=? WHERE id=?", (str(exc), message_id))
                state.update({"status": "failed", "current_id": None, "current_name": None})
                held_by_outage.pop(message_id, None)
                worked_since_held.discard(message_id)
                if first_contact:
                    await note_ledger(account_id, ledger_candidate(row), "failed", message_id, row["body"], str(exc))
                return "failed"
            step = outage_step.get(account_id, 0)
            outage_step[account_id] = step + 1
            pause = OUTAGE_PAUSES[min(step, len(OUTAGE_PAUSES) - 1)]
            reason = (f"Himalayas is not opening conversations right now ({str(exc)[:90]}). Messages stay queued and nothing is marked failed. "
                      f"The next try is in {pause // 60} min. If this lasts, reconnect Himalayas or check messaging for the company on Himalayas.")
            if first_contact:
                holds["first:" + account_id] = {"reason": reason, "since": holds.get("first:" + account_id, {}).get("since") or utc_now(), "until": time.monotonic() + pause}
            publish_dashboard_update("paused", account_id)
            return hold_message(message_id, account_id, reason, pause)
        except HimalayasRejected as exc:
            # Rate limits are temporary pacing (10/min), not spam. Keep the message queued and cool THIS
            # account's first-message queue so overdue siblings do not stampede the same cap.
            if is_rate_limit_error(exc):
                reason = (
                    f"Rate limit reached (10 messages/minute). Please wait. "
                    f"Retrying shortly. {str(exc)[:120]}"
                )
                until = time.monotonic() + RATE_LIMIT_HOLD_SECONDS
                next_first_message_at[account_id] = until
                holds["first:" + account_id] = {"reason": reason, "since": utc_now(), "until": until}
                return hold_message(message_id, account_id, reason, RATE_LIMIT_HOLD_SECONDS)

            # Himalayas refused it, so it was not delivered. Mark it failed with the real reason. The bot keeps working:
            # rewrite (safer after a few tries), skip this member after MAX_REFUSALS_PER_MEMBER, move to the next.
            with connection() as conn:
                conn.execute("UPDATE messages SET status='failed', error=? WHERE id=?", (str(exc), message_id))
            spam_refusal = any(word in str(exc).lower() for word in ("filter", "spam"))
            hint = "refused · spam filter" if spam_refusal else "send refused"
            state.update({"status": hint, "detail": str(exc), "current_id": None, "current_name": None})
            note_ops(account_id, "warn", hint, str(exc))
            if spam_refusal and first_contact:
                note_first_refused(row["body"])
            if first_contact:
                await note_ledger(account_id, ledger_candidate(row), "failed", message_id, row["body"], str(exc))
            refused = refusals_for_member(row["candidate_id"])
            if first_contact and spam_refusal:
                # Count DIFFERENT members refused in a row. The same member refused again is not a sign of a block.
                streak = rejection_streak.setdefault(account_id, [])
                if row["candidate_id"] not in streak:
                    streak.append(row["candidate_id"])
                if len(streak) >= REFUSED_MEMBERS_BEFORE_HOLD:
                    hold_first_messages(account_id, str(exc))
            if spam_refusal:
                now = time.monotonic()
                recent = [t for t in refusal_times.get(account_id, []) if now - t < REFUSAL_WINDOW_SECONDS] + [now]
                refusal_times[account_id] = recent
                if first_contact and len(recent) >= REFUSALS_PER_WINDOW and not hold_active("first:" + account_id):
                    detail = (
                        f"Himalayas refused {len(recent)} messages in {REFUSAL_WINDOW_SECONDS // 60} minutes, "
                        f"so first messages wait {FIRST_HOLD_SECONDS // 60} minutes. Replies keep going. {str(exc)[:120]}"
                    )
                    holds["first:" + account_id] = {
                        "reason": detail, "since": utc_now(), "until": time.monotonic() + FIRST_HOLD_SECONDS,
                    }
                    delivery_state(account_id).update({
                        "status": hold_hint(detail), "detail": detail, "current_id": None, "current_name": None,
                    })
                    note_ops(account_id, "warn", hold_hint(detail), detail)
                    publish_dashboard_update("paused", account_id)
            on_hold = hold_active("first:" + account_id) if first_contact else False
            if spam_refusal:
                if row["stage"] == "intro_sent":
                    note_intro_result(row["variant"] or 0, True)
                if refused >= MAX_REFUSALS_PER_MEMBER:
                    give_up = f"Gave up after {refused} refusals for this member. Skipping to the next. {exc}"
                    with connection() as conn:
                        conn.execute("UPDATE messages SET error=? WHERE id=?", (give_up, message_id))
                        conn.execute(
                            "UPDATE messages SET status='skipped', error=? "
                            "WHERE candidate_id=? AND direction='outbound' AND status IN ('scheduled', 'approved') "
                            "AND COALESCE(stage, 'first_sent')=? AND id!=?",
                            (f"Skipped: gave up after {refused} refusals for this member", row["candidate_id"], row["stage"] or "first_sent", message_id),
                        )
                    state.update({"status": "skipped · too many refusals", "detail": give_up, "current_id": None, "current_name": None})
                    note_ops(account_id, "warn", "skipped · too many refusals", give_up)
                    publish_dashboard_update("message_skipped", account_id)
                else:
                    # Rewrite in a different (safer after a few tries) style and send AT ONCE. Cap above stops endless loops.
                    safer = refused >= SAFER_AFTER_REFUSALS
                    if first_contact and row["stage"] in (None, "first_sent"):
                        new_id = await requeue_rewritten(row, account_id, safer=safer)
                    elif row["stage"] == "intro_sent":
                        new_id = await requeue_intro(row, account_id)  # the member answered, so this conversation matters most
                    else:
                        new_id = await requeue_rephrased(row, account_id)
                    if new_id and safer:
                        note_ops(account_id, "info", "retry · safer wording", f"Rewrote message for {row['candidate_name']} with a safer style after {refused} refusals.")
                    if new_id and not on_hold:  # during a hold the new message waits in the queue and goes when the hold ends
                        await asyncio.sleep(random.uniform(*RETRY_PAUSE_SECONDS))
                        return await deliver(new_id)
            else:
                # Non-spam rejection: still keep going — the delivery loop picks the next due message.
                pass
            return "failed"
        delivered = True
        outage_step.pop(account_id, None)  # Himalayas opens conversations again
        worked_since_held.update(held_by_outage)
        held_by_outage.pop(message_id, None)
        worked_since_held.discard(message_id)
        if first_contact:
            rejection_streak.pop(account_id, None)  # a first message was accepted, so refusals in a row start again from zero
            note_first_message_sent(account_id)
            note_first_accepted(row["body"])  # learn this wording for every future first message
            note_ops(account_id, "info", "sent · learned style", f"Accepted first message to {row['candidate_name']} added to style memory.")
        else:
            next_reply_at[account_id] = time.monotonic() + random.uniform(*REPLY_GAP_SECONDS)
            if row["stage"] == "intro_sent":
                note_intro_result(row["variant"] or 0, False)
        holds.pop(account_id, None)
        candidate = {
            "external_id": row["candidate_external_id"],
            "name": row["candidate_name"],
            "profile_url": row["candidate_profile_url"],
            "summary": row["candidate_summary"],
            "category": row["candidate_category"],
            "stack": json.loads(row["candidate_stack_json"]),
            "country": row["candidate_country"] or "",
            "suggested_role": row["candidate_suggested_role"] or "",
        }
        candidate["sent_at"] = utc_now()
        with connection() as conn:
            conn.execute("UPDATE messages SET status='sent', sent_at=? WHERE id=?", (candidate["sent_at"], message_id))
            # The step is done. Other copies of it that are still waiting (rewrites made while this one was refused) must never go out.
            conn.execute(
                "UPDATE messages SET status='superseded', error='Not sent: this step was already delivered to the member' "
                "WHERE candidate_id=? AND direction='outbound' AND status IN ('scheduled', 'approved') AND COALESCE(stage, 'first_sent')=? AND id!=?",
                (row["candidate_id"], row["stage"] or "first_sent", message_id),
            )
        if first_contact:
            try:
                await ledger.record_contact(candidate, message_id, row["body"])
            except SupabaseError as exc:
                with connection() as conn:
                    conn.execute("UPDATE messages SET error=? WHERE id=?", (f"Sent, but contact ledger failed: {exc}", message_id))
    except (SupabaseError, MCPError, Exception) as exc:
        if delivered:
            # The message left. Something after it went wrong (bookkeeping). It must never be shown as failed,
            # because someone could retry it and send it twice.
            with connection() as conn:
                conn.execute("UPDATE messages SET status='sent', sent_at=COALESCE(sent_at, ?), error=? WHERE id=?", (utc_now(), f"Sent, but a follow-up step failed: {exc}", message_id))
            state.update({"status": "sent", "current_id": None, "current_name": None})
            return "sent"
        with connection() as conn:
            conn.execute("UPDATE messages SET status='failed', error=? WHERE id=?", (str(exc), message_id))
        state.update({"status": "failed", "current_id": None, "current_name": None})
        if first_contact:
            await note_ledger(account_id, ledger_candidate(row), "failed", message_id, row["body"], str(exc))
        return "failed"
    state.update({"status": "sent", "current_id": None, "current_name": None})
    publish_dashboard_update("message_sent", account_id)
    return "sent"


def ledger_candidate(row) -> dict:
    """Candidate fields for the ledger, from a joined message row (see deliver)."""
    keys = row.keys()
    return {
        "external_id": row["candidate_external_id"], "name": row["candidate_name"], "profile_url": row["candidate_profile_url"],
        "summary": row["candidate_summary"], "category": row["candidate_category"], "stack": json.loads(row["candidate_stack_json"]),
        "country": (row["candidate_country"] if "candidate_country" in keys else "") or "",
        "suggested_role": (row["candidate_suggested_role"] if "candidate_suggested_role" in keys else "") or "",
    }


async def first_outbound_message_id(candidate_id: int) -> int | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE candidate_id=? AND direction='outbound' AND status != 'failed' ORDER BY created_at, id LIMIT 1",
            (candidate_id,),
        ).fetchone()
    return row["id"] if row else None


def owns_message(account_id: str, message_id: int) -> bool:
    with connection() as conn:
        return conn.execute("SELECT 1 FROM messages WHERE id=? AND account_id=?", (message_id, account_id)).fetchone() is not None


@app.post("/api/messages/{message_id}/approve")
async def approve(message_id: int, account: Account) -> dict:
    with connection() as conn:
        updated = conn.execute("UPDATE messages SET status='approved' WHERE id=? AND account_id=? AND status='queued'", (message_id, account)).rowcount
    if not updated:
        raise HTTPException(status_code=404, detail="Queued message not found")
    return {"approved": True}


@app.post("/api/messages/{message_id}/send")
async def send(message_id: int, account: Account) -> dict:
    if not owns_message(account, message_id):
        raise HTTPException(status_code=404, detail="Message not found")
    delivery_status = await deliver(message_id)
    if delivery_status != "sent":
        raise HTTPException(status_code=502, detail=f"Message delivery status: {delivery_status}")
    return {"sent": True}


candidate_locks: dict[int, asyncio.Lock] = {}


async def respond_to_inbound(account_id: str, candidate_row, body: str, external_id: str | None = None, received_at: str | None = None) -> bool:
    """Record one candidate message and schedule the next step of the hiring conversation.

    Returns False when the message is a duplicate or needs no reply. One lock per candidate keeps two quick
    messages from producing two replies.
    """
    candidate_id = candidate_row["id"]
    async with candidate_locks.setdefault(candidate_id, asyncio.Lock()):
        with connection() as conn:
            if external_id and (
                conn.execute("SELECT 1 FROM messages WHERE external_id=?", (external_id,)).fetchone()
                or conn.execute("SELECT 1 FROM processed_inbound WHERE external_id=?", (external_id,)).fetchone()
            ):
                return False
            history = [dict(item) for item in conn.execute(
                "SELECT direction, body FROM messages WHERE candidate_id=? AND status NOT IN ('superseded', 'failed', 'skipped') ORDER BY created_at, id", (candidate_id,))]
            conn.execute(
                "INSERT INTO messages (account_id, candidate_id, direction, body, status, external_id, created_at) VALUES (?, ?, 'inbound', ?, 'received', ?, ?)",
                (account_id, candidate_id, body, external_id, received_at or utc_now()),
            )
        publish_dashboard_update("reply_received", account_id)
        candidate = parse_candidate(candidate_row)
        try:
            chat.note_contact_details(candidate_id, body)
            in_flight = {state["current_id"] for state in delivery_states.values() if state["current_id"]}
            chat.supersede_pending_replies(candidate_id, in_flight)
            plan = await chat.plan_reply(candidate, body, history + [{"direction": "inbound", "body": body}])
        except Exception:
            if external_id:  # forget the message so the next poll tries again
                with connection() as conn:
                    conn.execute("DELETE FROM messages WHERE external_id=?", (external_id,))
            raise
        if plan is None:
            return True
        with connection() as conn:
            conn.execute(
                "INSERT INTO messages (account_id, candidate_id, direction, body, status, send_after, stage, variant, created_at) VALUES (?, ?, 'outbound', ?, 'scheduled', ?, ?, ?, ?)",
                (account_id, candidate_id, plan["body"], chat.reply_time(), plan["stage"], plan.get("variant"), utc_now()),
            )
        if plan.get("invited"):
            publish_dashboard_update("github_invited", account_id)
        publish_dashboard_update("messages_scheduled", account_id)
        return True


@app.post("/api/candidates/{candidate_id}/replies")
async def reply(candidate_id: int, request: ReplyRequest, account: Account) -> dict:
    with connection() as conn:
        candidate_row = conn.execute("SELECT * FROM candidates WHERE id=? AND account_id=?", (candidate_id, account)).fetchone()
    if not candidate_row:
        raise HTTPException(status_code=404, detail="Candidate not found")
    try:
        await respond_to_inbound(account, candidate_row, request.body)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not create reply: {exc}") from exc
    return {"queued": True}


async def process_inbound_message(account_id: str, item: dict) -> bool:
    if item.get("direction") not in {"inbound", "reply", "received"}:
        return False
    external_id = item.get("external_id") or hashlib.sha256(
        f"{item['talent_slug']}\n{item['body']}".encode()
    ).hexdigest()
    with connection() as conn:
        candidate_row = conn.execute(
            "SELECT * FROM candidates WHERE account_id=? AND (external_id=? OR lower(name)=lower(?)) LIMIT 1",
            (account_id, item["talent_slug"], item.get("candidate_name", "")),
        ).fetchone()
    if not candidate_row:
        return False
    return await respond_to_inbound(account_id, candidate_row, item["body"], external_id, item.get("created_at"))


async def poll_replies(account_id: str, semaphore: asyncio.Semaphore) -> None:
    state = reply_monitor_state(account_id)

    async def process_with_limit(item: dict) -> bool:
        async with semaphore:
            return await process_inbound_message(account_id, item)

    state["running"] = True
    try:
        inbound_messages = await HimalayasMCP(account_id).list_messages()
        results = await asyncio.gather(*(process_with_limit(item) for item in inbound_messages), return_exceptions=True)
        detected = sum(result is True for result in results)
        failures = sum(isinstance(result, Exception) for result in results)
        state["detected"] += detected
        state["status"] = f"monitoring · {detected} new" if not failures else f"monitoring · {failures} failed"
        if detected:
            publish_dashboard_update("reply_received", account_id)
    except HimalayasRejected as exc:
        # Himalayas itself is not answering (for example a 404 while it blocks the account). It is not a fault of the bot, and the
        # next poll tries again. It is shown as waiting, not as an error.
        state["status"] = f"waiting for Himalayas: {str(exc).replace('Himalayas rejected ', '')[:110]}"
    except Exception as exc:
        state["status"] = f"failed: {exc}"


INVITATION_RETRY_SECONDS = 300
last_invitation_try: dict[int, float] = {}


async def retry_invitations() -> None:
    """Retry GitHub invitations that failed because of our settings. Waits until the GitHub settings are complete."""
    if not settings_ready():
        return
    now = asyncio.get_running_loop().time()
    for row in chat.candidates_waiting_for_invitation():
        if now - last_invitation_try.get(row["id"], -INVITATION_RETRY_SECONDS) < INVITATION_RETRY_SECONDS:
            continue
        last_invitation_try[row["id"]] = now
        async with candidate_locks.setdefault(row["id"], asyncio.Lock()):
            plan = await chat.retry_invitation(parse_candidate(row))
            if plan is None:
                continue
            with connection() as conn:
                conn.execute(
                    "INSERT INTO messages (account_id, candidate_id, direction, body, status, send_after, stage, created_at) VALUES (?, ?, 'outbound', ?, 'scheduled', ?, ?, ?)",
                    (row["account_id"], row["id"], plan["body"], chat.reply_time(), plan["stage"], utc_now()),
                )
        if plan.get("invited"):
            publish_dashboard_update("github_invited", row["account_id"])
        publish_dashboard_update("messages_scheduled", row["account_id"])


async def reply_monitor_loop() -> None:
    semaphore = asyncio.Semaphore(settings.reply_processing_concurrency)
    while True:
        try:
            await retry_invitations()
        except Exception as exc:
            print(f"Invitation retry failed: {exc}")
        polled = set(authorized_accounts())
        with connection() as conn:
            everyone = [row["id"] for row in conn.execute("SELECT id FROM accounts")]
        for account_id in everyone:
            if account_id not in polled:  # no working Himalayas login, so there is nothing to read
                reply_monitor_state(account_id).update(running=False, status="waiting for a Himalayas login")
        # One failing account never blocks the others.
        await asyncio.gather(*(poll_replies(account_id, semaphore) for account_id in authorized_accounts()))
        await asyncio.sleep(settings.reply_poll_interval_seconds)


async def delivery_loop() -> None:
    # A single sequential loop serves every account, so the shared "already contacted" check
    # and the send that follows it can never interleave between two accounts.
    while True:
        # Accounts that are paused in the admin dashboard send nothing. Their queue waits.
        query = "SELECT id FROM messages WHERE account_id NOT IN (SELECT id FROM accounts WHERE paused=1) AND ((status='scheduled' AND send_after <= ?)"
        params: list[str] = [utc_now()]
        if settings.auto_send:
            query += " OR (status='approved' AND send_after <= ?)"
            params.append(utc_now())
        query += ")"
        capped = capped_accounts()
        if capped:  # the daily limit is reached: first messages wait until tomorrow. Replies to members who answered still go.
            query += " AND NOT (account_id IN ({}) AND {})".format(",".join("?" * len(capped)), TRUE_FIRST_SQL)
            params.extend(capped)
            for account in capped:
                current = delivery_state(account)
                if current["status"] != "sending":
                    current["status"] = daily_limit_reason(daily_status(account))
        cooling = cooling_accounts()
        if cooling:  # an account that just sent a first message waits before the next first message. Replies are not held back by this.
            query += " AND NOT (account_id IN ({}) AND {})".format(",".join("?" * len(cooling)), TRUE_FIRST_SQL)
            params.extend(cooling)
        now = time.monotonic()
        reply_cooling = [account for account, until in next_reply_at.items() if until > now]
        if reply_cooling:  # replies to different members are spaced a little too, but they never wait for first messages
            query += " AND NOT (account_id IN ({}) AND NOT {})".format(",".join("?" * len(reply_cooling)), TRUE_FIRST_SQL)
            params.extend(reply_cooling)
        query += " ORDER BY send_after, id LIMIT 1"
        with connection() as conn:
            row = conn.execute(query, params).fetchone()
        if row:
            await deliver(row["id"])
        await asyncio.sleep(settings.delivery_poll_interval_seconds)


# Serves the admin dashboard at /admin/. Mounted last so it never shadows an API route.
app.mount("/admin", StaticFiles(directory=ADMIN_DIR, html=True), name="admin")

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import chat
from .ai import classify, write_first_message
from .config import settings
from .github import settings_ready
from .db import ACCOUNT_ID_PATTERN, backup_database, connection, ensure_account, init_db, utc_now, vacuum
from .mcp_client import HimalayasMCP, MCPError, authorization_url, authorized_accounts, exchange_code, oauth_status
from .supabase_client import SupabaseError, SupabaseLedger

app = FastAPI(title="Himalayas Hiring Assistant", version="0.1.0")
reply_monitor_task: asyncio.Task | None = None
# One backend serves every extension (one per Chrome profile / Himalayas account). All runtime state is kept per account.
automation_tasks: dict[str, asyncio.Task] = {}
automation_states: dict[str, dict] = {}
reply_monitor_states: dict[str, dict] = {}
delivery_states: dict[str, dict] = {}
dashboard_subscribers: dict[asyncio.Queue, str] = {}


def automation_state(account_id: str) -> dict:
    return automation_states.setdefault(account_id, {"running": False, "page": None, "status": "idle", "queued": 0})


def reply_monitor_state(account_id: str) -> dict:
    return reply_monitor_states.setdefault(account_id, {"running": False, "status": "idle", "detected": 0})


def delivery_state(account_id: str) -> dict:
    return delivery_states.setdefault(account_id, {"running": True, "status": "idle", "current_id": None, "current_name": None})


def optional_account(x_account_id: Annotated[str | None, Header()] = None, account_id: str | None = Query(default=None)) -> str | None:
    value = x_account_id or account_id
    if value is None:
        return None
    if not ACCOUNT_ID_PATTERN.match(value):
        raise HTTPException(status_code=400, detail="Invalid account id")
    ensure_account(value)
    return value


def get_account(account_id: Annotated[str | None, Depends(optional_account)]) -> str:
    if account_id is None:
        raise HTTPException(status_code=400, detail="X-Account-Id header is required")
    return account_id


Account = Annotated[str, Depends(get_account)]


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
    auto_send: bool | None = None
    min_message_delay_seconds: int | None = Field(default=None, ge=5, le=86400)
    max_message_delay_seconds: int | None = Field(default=None, ge=5, le=86400)
    reply_poll_interval_seconds: int | None = Field(default=None, ge=10, le=3600)
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


async def note_ledger(account_id: str, candidate: dict, status: str, message_id: int | None = None, body: str = "", error: str | None = None) -> None:
    """Show a queued or failed first message in the shared ledger. Best effort: it must never stop queueing or sending."""
    try:
        details = {**candidate, "account_id": account_id, "account_label": account_label(account_id)}
        await SupabaseLedger().record_status(details, status, message_id, body, error)
    except Exception as exc:
        print(f"Ledger status note failed for {candidate.get('external_id')}: {exc}")


def parse_candidate(row) -> dict:
    item = dict(row)
    item["stack"] = json.loads(item.pop("stack_json"))
    return item


def publish_dashboard_update(reason: str, account_id: str) -> None:
    event = {"reason": reason, "at": utc_now()}
    for subscriber, subscriber_account in tuple(dashboard_subscribers.items()):
        if subscriber_account != account_id:
            continue
        try:
            subscriber.put_nowait(event)
        except asyncio.QueueFull:
            pass


def compact_scheduled_queue() -> None:
    # Each account has its own send pacing, so queues are re-spaced per account.
    with connection() as conn:
        for account in conn.execute("SELECT DISTINCT account_id FROM messages WHERE direction='outbound' AND status='scheduled'").fetchall():
            rows = conn.execute(
                "SELECT id FROM messages WHERE account_id=? AND direction='outbound' AND status='scheduled' ORDER BY send_after, id",
                (account["account_id"],),
            ).fetchall()
            next_send_at = datetime.now(timezone.utc) + timedelta(seconds=5)
            for row in rows:
                conn.execute("UPDATE messages SET send_after=? WHERE id=?", (next_send_at.isoformat(), row["id"]))
                next_send_at += timedelta(seconds=settings.min_message_delay_seconds)


@app.on_event("startup")
async def startup() -> None:
    global reply_monitor_task
    init_db()
    compact_scheduled_queue()
    asyncio.create_task(delivery_loop())
    reply_monitor_task = asyncio.create_task(reply_monitor_loop())


@app.get("/api/health")
async def health(account_id: Annotated[str | None, Depends(optional_account)]) -> dict:
    if account_id is None:
        return {"ok": True}
    return {"ok": True, "auto_send": settings.auto_send, "himalayas_authorized": oauth_status(account_id), "automation": automation_state(account_id), "reply_monitor": reply_monitor_state(account_id), "delivery": delivery_state(account_id)}


class AccountUpdate(BaseModel):
    label: str = Field(max_length=60)


@app.get("/api/account")
async def get_account_info(account: Account) -> dict:
    with connection() as conn:
        row = conn.execute("SELECT id, label FROM accounts WHERE id=?", (account,)).fetchone()
    return {**dict(row), "himalayas_authorized": oauth_status(account)}


@app.put("/api/account")
async def rename_account(update: AccountUpdate, account: Account) -> dict:
    with connection() as conn:
        conn.execute("UPDATE accounts SET label=? WHERE id=?", (update.label.strip(), account))
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


@app.put("/api/settings")
async def save_settings(update: SettingsUpdate) -> dict:
    values = update.model_dump(exclude_none=True)
    for name, value in list(values.items()):
        if name in SECRET_SETTINGS and (not value or set(value) == {"•"} or value == masked(getattr(settings, name))):
            values.pop(name, None)
            continue
        setattr(settings, name, value)
    if values:
        update_env_file(values)
        for subscriber_account in set(dashboard_subscribers.values()):
            publish_dashboard_update("settings_updated", subscriber_account)
    return public_settings()


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


async def import_candidates(account_id: str, page: int) -> dict:
    try:
        client = HimalayasMCP(account_id)
        imported = await client.list_candidates(page)
        semaphore = asyncio.Semaphore(settings.profile_fetch_concurrency)

        async def enrich(item: dict) -> dict:
            async with semaphore:
                profile = await client.get_talent_profile(item["talent_slug"])
            item["profile"] = profile
            item["summary"] = f"{item.get('summary', '')}\n{profile}"[:12000]
            return item

        imported = await asyncio.gather(*(enrich(item) for item in imported))
    except (MCPError, Exception) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    with connection() as conn:
        for item in imported:
            stack = item.get("stack", item.get("skills", []))
            summary = item.get("summary", item.get("bio", ""))
            category = classify(stack, summary)
            now = utc_now()
            candidate_slug = item.get("talent_slug", item.get("slug", item.get("id", "")))
            if not candidate_slug:
                continue
            conn.execute(
                """INSERT INTO candidates (account_id, external_id, name, profile_url, summary, stack_json, category, source_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, external_id) DO UPDATE SET name=excluded.name, profile_url=excluded.profile_url,
                summary=excluded.summary, stack_json=excluded.stack_json,
                category=CASE WHEN candidates.suggested_role IS NOT NULL THEN candidates.category ELSE excluded.category END,
                source_json=excluded.source_json, updated_at=excluded.updated_at""",
                (account_id, str(candidate_slug), item.get("name", "Candidate"), item.get("profile_url", ""), summary,
                 json.dumps(stack), category, json.dumps(item), now, now),
            )
    publish_dashboard_update("candidates_synced", account_id)
    return {"imported": len(imported)}


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


async def automatic_campaign(account_id: str) -> None:
    page = 1
    state = automation_state(account_id)
    state.update({"running": True, "page": page, "status": "syncing", "queued": 0})
    try:
        while True:
            state["page"] = page
            state["status"] = "syncing"
            sync_result = await import_candidates(account_id, page)
            if not sync_result["imported"]:
                state["status"] = "complete"
                break
            state["status"] = "queueing"
            campaign_result = await queue_campaign(account_id, CampaignRequest(page=page))
            state["queued"] += campaign_result["queued"]
            state["status"] = "sending"
            while await page_has_pending_messages(account_id, page):
                await asyncio.sleep(5)
            page += 1
    except asyncio.CancelledError:
        state["status"] = "stopped"
        raise
    except Exception as exc:
        state["status"] = f"failed: {exc}"
    finally:
        state["running"] = False
        state["page"] = page


@app.post("/api/automation/start")
async def start_automation(account: Account) -> dict:
    task = automation_tasks.get(account)
    if task and not task.done():
        return automation_state(account)
    automation_tasks[account] = asyncio.create_task(automatic_campaign(account))
    publish_dashboard_update("automation_started", account)
    return {**automation_state(account), "status": "starting"}


@app.post("/api/automation/stop")
async def stop_automation(account: Account) -> dict:
    task = automation_tasks.get(account)
    if task and not task.done():
        task.cancel()
        automation_state(account)["status"] = "stopping"
        publish_dashboard_update("automation_stopping", account)
    return automation_state(account)


async def queue_campaign(account_id: str, request: CampaignRequest) -> dict:
    created = 0
    skipped = 0
    next_send_at = datetime.now(timezone.utc)
    with connection() as conn:
        latest = conn.execute(
            "SELECT send_after FROM messages WHERE account_id=? AND direction='outbound' AND status IN ('scheduled', 'approved') AND send_after IS NOT NULL ORDER BY send_after DESC LIMIT 1",
            (account_id,),
        ).fetchone()
    if latest:
        latest_send_at = datetime.fromisoformat(latest["send_after"])
        next_send_at = max(next_send_at, latest_send_at + timedelta(seconds=settings.min_message_delay_seconds))
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
        if already_contacted:
            skipped += 1
            continue
        eligible_rows.append(candidate)

    semaphore = asyncio.Semaphore(settings.message_generation_concurrency)

    async def generate(candidate: dict) -> tuple[dict, dict]:
        async with semaphore:
            return candidate, await write_first_message(candidate)

    try:
        generated = await asyncio.gather(*(generate(candidate) for candidate in eligible_rows))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Message generation failed: {exc}") from exc

    queued_notes = []
    for candidate, first in generated:
        with connection() as conn:
            conn.execute("UPDATE candidates SET suggested_role=?, category=? WHERE id=?", (first["role"], chat.category_for(first["role"]), candidate["id"]))
            message_id = conn.execute("INSERT INTO messages (account_id, candidate_id, direction, body, status, send_after, stage, created_at) VALUES (?, ?, 'outbound', ?, 'scheduled', ?, ?, ?)",
                                      (account_id, candidate["id"], first["message"], next_send_at.isoformat(), chat.FIRST, utc_now())).lastrowid
        queued_notes.append((candidate, message_id, first["message"]))
        next_send_at += timedelta(seconds=settings.min_message_delay_seconds)
        created += 1
    limit = asyncio.Semaphore(5)

    async def note(candidate: dict, message_id: int, body: str) -> None:
        async with limit:
            await note_ledger(account_id, candidate, "queued", message_id, body)

    await asyncio.gather(*(note(*item) for item in queued_notes))
    publish_dashboard_update("messages_scheduled", account_id)
    return {"queued": created, "skipped": skipped, "first_send_at": (next_send_at - timedelta(seconds=settings.min_message_delay_seconds)).isoformat() if created else None}


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
    return {
        "total": sum(counts.values()),
        "sent": counts.get("sent", 0),
        "queued": counts.get("scheduled", 0) + counts.get("approved", 0) + counts.get("queued", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped", 0),
        "next_recipient": next_message["name"] if next_message else None,
        "next_send_at": next_message["send_after"] if next_message else None,
        "latest_recipient": latest["name"] if latest else None,
        "latest_status": latest["status"] if latest else None,
        "latest_error": latest["error"] if latest else None,
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
            EXISTS(SELECT 1 FROM messages WHERE candidate_id=c.id AND direction='outbound' AND status IN ('scheduled', 'approved')) AS response_scheduled
            FROM candidates c JOIN messages m ON m.candidate_id=c.id
            WHERE c.account_id=?
            GROUP BY c.id ORDER BY last_activity DESC""",
            (account,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["conversation_status"] = "response scheduled" if item["response_scheduled"] else "new reply" if item["has_reply"] and item["last_direction"] == "inbound" else "awaiting reply" if item["last_direction"] == "outbound" and item["last_status"] == "sent" else item["last_status"] or "no activity"
        result.append(item)
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
        updated = conn.execute(
            "UPDATE messages SET read_at=? WHERE candidate_id=? AND account_id=? AND direction='inbound' AND read_at IS NULL",
            (utc_now(), candidate_id, account),
        ).rowcount
    if updated:
        publish_dashboard_update("conversation_read", account)
    return {"marked_read": updated}


# ---- Database view (per account) ----

LEDGER_COLUMNS = [
    ("id", "bigint", True), ("talent_slug", "text", False), ("candidate_name", "text", False), ("profile_url", "text", False),
    ("summary", "text", False), ("category", "text", False), ("stack", "jsonb", False), ("message_id", "bigint", False),
    ("message_body", "text", False), ("status", "text", False), ("error", "text", False), ("account_id", "text", False),
    ("account_label", "text", False), ("sent_at", "timestamptz", False), ("created_at", "timestamptz", False), ("updated_at", "timestamptz", False),
]
TABLE_INFO = [
    ("accounts", "Chrome profiles that use this server"),
    ("candidates", "Members imported from Himalayas, with the role suggested for each"),
    ("messages", "Every message sent to or received from a member"),
]


@app.get("/api/db/structure")
async def db_structure(account: Account) -> dict:
    tables = []
    with connection() as conn:
        for name, description in TABLE_INFO:
            columns = [
                {"name": row["name"], "type": (row["type"] or "TEXT").upper(), "primary_key": bool(row["pk"]), "required": bool(row["notnull"])}
                for row in conn.execute(f"PRAGMA table_info({name})")
                if row["name"] != "account_id" or name != "accounts"
            ]
            if name == "accounts":
                rows = 1
            else:
                rows = conn.execute(f"SELECT COUNT(*) FROM {name} WHERE account_id=?", (account,)).fetchone()[0]
            tables.append({"name": name, "description": description, "rows": rows, "columns": columns})
    try:
        ledger_ready = await SupabaseLedger().status_ready()
    except Exception:
        ledger_ready = None  # Supabase is not configured or not reachable
    return {
        "tables": tables,
        "ledger_status_ready": ledger_ready,
        "links": ["messages.candidate_id → candidates.id", "candidates.account_id → accounts.id", "messages.account_id → accounts.id"],
        "external": {
            "name": settings.supabase_table,
            "description": "Shared contact ledger on Supabase. One row per member who was contacted. Every account checks it before a first message.",
            "columns": [{"name": name, "type": kind.upper(), "primary_key": pk, "required": False} for name, kind, pk in LEDGER_COLUMNS],
        },
        "note": "Row counts are for this Chrome profile only. Login tokens are stored in the database but never shown here.",
    }


def member_rows(account_id: str) -> list[dict]:
    """One row for every member who was reached (has at least one outbound message), with the outreach status."""
    with connection() as conn:
        candidates = conn.execute("SELECT id, name, suggested_role, category, github_username, github_invited_at FROM candidates WHERE account_id=?", (account_id,)).fetchall()
        messages = conn.execute(
            "SELECT id, candidate_id, direction, status, stage, error, sent_at, created_at FROM messages WHERE account_id=? AND status != 'superseded' ORDER BY created_at, id",
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
        # Outreach = the first-contact message. A sent one wins, otherwise the latest attempt.
        first_contact = [m for m in outbound if m["stage"] in (None, chat.FIRST)] or outbound
        outreach = next((m for m in first_contact if m["status"] == "sent"), first_contact[-1])
        sent = sorted((m for m in outbound if m["status"] == "sent"), key=lambda m: (m["sent_at"] or m["created_at"], m["id"]))
        stage = (sent[-1]["stage"] or chat.FIRST) if sent else None
        failed = next((m for m in reversed(outbound) if m["status"] == "failed"), None)
        members.append({
            "id": candidate["id"], "name": candidate["name"], "role": candidate["suggested_role"], "category": candidate["category"],
            "outreach_status": outreach["status"], "outreach_error": outreach["error"], "outreach_message_id": outreach["id"],
            "outreach_sent_at": outreach["sent_at"], "last_status": outbound[-1]["status"], "stage": stage,
            "replies": sum(1 for m in history if m["direction"] == "inbound"),
            "last_activity": max((m["sent_at"] or m["created_at"]) for m in history),
            "github_username": candidate["github_username"], "github_invited_at": candidate["github_invited_at"],
            "failed_message_id": failed["id"] if failed and outbound[-1]["status"] == "failed" else None,
            "failed_error": failed["error"] if failed and outbound[-1]["status"] == "failed" else None,
        })
    members.sort(key=lambda item: item["last_activity"], reverse=True)
    return members


@app.get("/api/db/members")
async def db_members(account: Account, status: str | None = None, q: str | None = None, limit: int = Query(default=200, ge=1, le=1000)) -> dict:
    members = member_rows(account)
    summary: dict[str, int] = {}
    for member in members:
        summary[member["outreach_status"]] = summary.get(member["outreach_status"], 0) + 1
    if status:
        members = [m for m in members if m["outreach_status"] == status]
    if q:
        members = [m for m in members if q.lower() in m["name"].lower()]
    return {"summary": {**summary, "total": sum(summary.values())}, "members": members[:limit], "matched": len(members)}


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
            c.summary AS candidate_summary, c.category AS candidate_category, c.stack_json AS candidate_stack_json
            FROM messages m JOIN candidates c ON c.id=m.candidate_id WHERE m.id=?""",
            (message_id,),
        ).fetchone()
    if row and message_id == await first_outbound_message_id(row["candidate_id"]):
        await note_ledger(account, ledger_candidate(row), "queued", message_id, row["body"])
    publish_dashboard_update("messages_scheduled", account)
    return {"retried": True}


@app.get("/api/events")
async def events(account: Account) -> StreamingResponse:
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


async def deliver(message_id: int) -> str:
    with connection() as conn:
        row = conn.execute(
            """SELECT m.*, c.external_id AS candidate_external_id, c.name AS candidate_name,
            c.profile_url AS candidate_profile_url, c.summary AS candidate_summary,
            c.category AS candidate_category, c.stack_json AS candidate_stack_json
            FROM messages m JOIN candidates c ON c.id=m.candidate_id WHERE m.id=?""",
            (message_id,),
        ).fetchone()
    if not row:
        return "failed"
    account_id = row["account_id"]
    state = delivery_state(account_id)
    state["current_id"] = message_id
    state["status"] = "sending"
    publish_dashboard_update("message_sending", account_id)
    state["current_name"] = row["candidate_name"]
    first_contact = False
    try:
        first_contact = row["id"] == (await first_outbound_message_id(row["candidate_id"]))
        ledger = SupabaseLedger()
        if first_contact and await ledger.was_contacted(row["candidate_external_id"]):
            with connection() as conn:
                conn.execute("UPDATE messages SET status='skipped', error='Already contacted (shared contact ledger)' WHERE id=?", (message_id,))
            state["status"] = "skipped"
            publish_dashboard_update("message_skipped", account_id)
            return "skipped"
        await HimalayasMCP(account_id).send_message(row["candidate_external_id"], row["body"], first_contact=first_contact)
        candidate = {
            "external_id": row["candidate_external_id"],
            "name": row["candidate_name"],
            "profile_url": row["candidate_profile_url"],
            "summary": row["candidate_summary"],
            "category": row["candidate_category"],
            "stack": json.loads(row["candidate_stack_json"]),
        }
        candidate["sent_at"] = utc_now()
        with connection() as conn:
            conn.execute("UPDATE messages SET status='sent', sent_at=? WHERE id=?", (candidate["sent_at"], message_id))
        if first_contact:
            try:
                await ledger.record_contact(candidate, message_id, row["body"])
            except SupabaseError as exc:
                with connection() as conn:
                    conn.execute("UPDATE messages SET error=? WHERE id=?", (f"Sent, but contact ledger failed: {exc}", message_id))
    except (SupabaseError, MCPError, Exception) as exc:
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
    return {
        "external_id": row["candidate_external_id"], "name": row["candidate_name"], "profile_url": row["candidate_profile_url"],
        "summary": row["candidate_summary"], "category": row["candidate_category"], "stack": json.loads(row["candidate_stack_json"]),
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
                "INSERT INTO messages (account_id, candidate_id, direction, body, status, send_after, stage, created_at) VALUES (?, ?, 'outbound', ?, 'scheduled', ?, ?, ?)",
                (account_id, candidate_id, plan["body"], chat.reply_time(), plan["stage"], utc_now()),
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
        # One failing account never blocks the others.
        await asyncio.gather(*(poll_replies(account_id, semaphore) for account_id in authorized_accounts()))
        await asyncio.sleep(settings.reply_poll_interval_seconds)


async def delivery_loop() -> None:
    # A single sequential loop serves every account, so the shared "already contacted" check
    # and the send that follows it can never interleave between two accounts.
    while True:
        query = "SELECT id FROM messages WHERE status='scheduled' AND send_after <= ?"
        params: list[str] = [utc_now()]
        if settings.auto_send:
            query += " OR (status='approved' AND send_after <= ?)"
            params.append(utc_now())
        query += " ORDER BY send_after LIMIT 1"
        with connection() as conn:
            row = conn.execute(query, params).fetchone()
        if row:
            await deliver(row["id"])
        await asyncio.sleep(settings.delivery_poll_interval_seconds)

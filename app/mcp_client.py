import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlencode

import httpx

from .config import settings
from .db import LEGACY_ACCOUNT, connection, get_state, set_state, utc_now


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def authorization_url(account_id: str) -> str:
    client_id = settings.himalayas_oauth_client_id
    client_secret = settings.himalayas_oauth_client_secret or None
    if not client_id:
        payload = {
            "client_name": "Himalayas Hiring Assistant",
            "redirect_uris": [settings.himalayas_oauth_redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(settings.himalayas_oauth_registration_endpoint, json=payload)
        response.raise_for_status()
        registration = response.json()
        client_id = registration["client_id"]
        client_secret = registration.get("client_secret")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    with connection() as conn:
        conn.execute("DELETE FROM oauth_state WHERE account_id=?", (account_id,))
        conn.execute("INSERT INTO oauth_state (state, account_id, code_verifier, created_at) VALUES (?, ?, ?, ?)", (state, account_id, verifier, utc_now()))
        conn.execute(
            "INSERT INTO oauth_tokens (account_id, access_token, refresh_token, expires_at, client_id, client_secret) VALUES (?, '', NULL, NULL, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET client_id=excluded.client_id, client_secret=excluded.client_secret",
            (account_id, client_id, client_secret),
        )
    query = urlencode({
        "response_type": "code", "client_id": client_id,
        "redirect_uri": settings.himalayas_oauth_redirect_uri,
        "scope": "read_messages write_messages", "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256", "state": state,
    })
    return f"{settings.himalayas_oauth_authorization_endpoint}?{query}"


async def exchange_code(code: str, state: str) -> None:
    with connection() as conn:
        oauth_state = conn.execute("SELECT account_id, code_verifier FROM oauth_state WHERE state=?", (state,)).fetchone()
        client = oauth_state and conn.execute("SELECT client_id, client_secret FROM oauth_tokens WHERE account_id=?", (oauth_state["account_id"],)).fetchone()
    if not oauth_state or not client:
        raise RuntimeError("OAuth state is missing or expired")
    payload = {"grant_type": "authorization_code", "code": code, "redirect_uri": settings.himalayas_oauth_redirect_uri, "client_id": client["client_id"], "code_verifier": oauth_state["code_verifier"]}
    if client["client_secret"]:
        payload["client_secret"] = client["client_secret"]
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(settings.himalayas_oauth_token_endpoint, data=payload)
    response.raise_for_status()
    account_id = oauth_state["account_id"]
    save_token(account_id, response.json())
    with connection() as conn:
        conn.execute("DELETE FROM oauth_state WHERE account_id=?", (account_id,))


def save_token(account_id: str, token: dict) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE oauth_tokens SET access_token=?, refresh_token=COALESCE(?, refresh_token), expires_at=?, needs_reconnect=0 WHERE account_id=?",
            (token["access_token"], token.get("refresh_token"), time.time() + float(token.get("expires_in", 3600)), account_id),
        )


def static_token_applies(account_id: str) -> bool:
    # A fixed token belongs to one Himalayas account, so it must never be shared across accounts.
    return bool(settings.himalayas_mcp_token) and account_id == LEGACY_ACCOUNT


class LoginExpired(RuntimeError):
    """The saved Himalayas login can no longer be refreshed. The person must connect again."""


LOGIN_EXPIRED_MESSAGE = "Himalayas login expired. Press Reconnect Himalayas."
refresh_locks: dict[str, asyncio.Lock] = {}


def mark_login_expired(account_id: str) -> None:
    with connection() as conn:
        conn.execute("UPDATE oauth_tokens SET needs_reconnect=1 WHERE account_id=?", (account_id,))


def read_token_row(account_id: str):
    with connection() as conn:
        return conn.execute("SELECT * FROM oauth_tokens WHERE account_id=?", (account_id,)).fetchone()


def token_is_fresh(row) -> bool:
    return not row["expires_at"] or row["expires_at"] > time.time() + 60


async def access_token(account_id: str) -> str:
    if static_token_applies(account_id):
        return settings.himalayas_mcp_token
    row = read_token_row(account_id)
    if not row or not row["access_token"]:
        raise RuntimeError("Himalayas authorization required. Open /api/auth/start first.")
    if row["needs_reconnect"]:
        raise LoginExpired(LOGIN_EXPIRED_MESSAGE)
    if token_is_fresh(row):
        return row["access_token"]
    # Several tasks (reply monitor, sender, campaign) can need a new token at the same moment. Refresh tokens are usually
    # single use, so only one may refresh. The others wait, then use the token that the first one saved.
    async with refresh_locks.setdefault(account_id, asyncio.Lock()):
        row = read_token_row(account_id)
        if row["needs_reconnect"]:
            raise LoginExpired(LOGIN_EXPIRED_MESSAGE)
        if token_is_fresh(row):
            return row["access_token"]
        if not row["refresh_token"]:
            mark_login_expired(account_id)
            raise LoginExpired(LOGIN_EXPIRED_MESSAGE)
        payload = {"grant_type": "refresh_token", "refresh_token": row["refresh_token"], "client_id": row["client_id"]}
        if row["client_secret"]:
            payload["client_secret"] = row["client_secret"]
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.post(settings.himalayas_oauth_token_endpoint, data=payload)
        if response.status_code in (400, 401):
            # The provider rejected the refresh token itself, so retrying cannot help. A new login is needed.
            mark_login_expired(account_id)
            raise LoginExpired(LOGIN_EXPIRED_MESSAGE)
        response.raise_for_status()  # a server error or timeout is temporary and does not mark the login as expired
        token = response.json()
        save_token(account_id, token)
        return token["access_token"]


def login_state(account_id: str) -> str:
    """'connected', 'expired' (saved login no longer works), or 'none' (never connected)."""
    if static_token_applies(account_id):
        return "connected"
    row = read_token_row(account_id)
    if not row or not row["access_token"]:
        return "none"
    return "expired" if row["needs_reconnect"] else "connected"


def oauth_status(account_id: str) -> bool:
    return login_state(account_id) == "connected"


def authorized_accounts() -> list[str]:
    with connection() as conn:
        rows = conn.execute("SELECT account_id FROM oauth_tokens WHERE access_token != '' AND needs_reconnect = 0").fetchall()
    return [row["account_id"] for row in rows]


class MCPError(RuntimeError):
    pass


class HimalayasRejected(MCPError):
    """Himalayas refused the request, or accepted it but the message is not in the conversation. Nothing was delivered."""


class HimalayasUnavailable(HimalayasRejected):
    """Himalayas could not open a conversation and gave no reason about this member or this message (for example error 406).
    When it happens for many members in a row it is a problem with the account or with Himalayas, not with the members."""


class ConversationUnavailable(MCPError):
    """Himalayas reports a conversation for this member that start_conversation cannot reopen, and no usable room
    could be found another way. Nothing was sent, and it is not a spam refusal."""


class AlreadyMessaged(MCPError):
    """Himalayas already shows a message from the company in this conversation, for example one sent by hand."""


def has_company_message(conversation_text: str) -> bool:
    return bool(re.search(r"(?m)^🏢\s+\*\*", conversation_text))  # 🏢 marks the company's side, 👤 the member's


def parse_conversation(text: str) -> dict:
    """Read a get_conversation reply. Himalayas lists the newest message first. 🏢 is the company, 👤 the member, ✓ means read."""
    count = re.search(r"\((\d+) messages?\)", text.split("\n", 1)[0])
    room = re.search(r"Room: `([^`]+)`", text)
    messages = []
    for block in re.split(r"\n\s*---\s*\n", text.split("\n\n", 1)[1] if "\n\n" in text else ""):
        header = re.match(r"\s*(🏢|👤)\s+\*\*(.+?)\*\*(?:\s+—\s+_(.+?)_)?(\s*✓)?\s*\n(.*)", block, re.S)
        if header:
            messages.append({"who": "company" if header.group(1) == "🏢" else "member", "sender": header.group(2).strip(), "when": header.group(3) or "", "read": bool(header.group(4)), "body": header.group(5).strip()})
    return {"count": int(count.group(1)) if count else None, "room": room.group(1) if room else None, "messages": messages}


def result_text(result: dict) -> str:
    return "\n".join(item.get("text", "") for item in result.get("content", []) if item.get("type") == "text")


def letters_only(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


ROOM_PREFIX = "private_company_candidate_"


def company_of_room(room: str, talent_slug: str) -> str | None:
    """The company a conversation belongs to, from its room name: private_company_candidate_<company>_<member>."""
    if room.startswith(ROOM_PREFIX) and room.endswith("_" + talent_slug) and len(room) > len(ROOM_PREFIX) + len(talent_slug) + 1:
        return room[len(ROOM_PREFIX):-(len(talent_slug) + 1)]
    return None


def note_company(account_id: str, room: str, talent_slug: str) -> None:
    """Remember which Himalayas company this login acts as. If it changes (for example after reconnecting with another Himalayas login),
    the change is recorded, because messages then go out under another company."""
    company = company_of_room(room, talent_slug)
    if not company:
        return
    key = f"himalayas_company:{account_id}"
    stored = get_state(key)
    previous = stored
    if previous is None:  # first time seen: learn the company used so far from an earlier conversation
        with connection() as conn:
            row = conn.execute("SELECT h.room, c.external_id FROM himalayas_check h JOIN candidates c ON c.id=h.candidate_id WHERE h.account_id=? AND h.room IS NOT NULL LIMIT 1", (account_id,)).fetchone()
        previous = company_of_room(row["room"], row["external_id"]) if row else None
    if stored != company:
        set_state(key, company)
    if previous and previous != company:
        set_state(f"himalayas_company_change:{account_id}", json.dumps({"from": previous, "to": company, "at": utc_now()}))


class HimalayasMCP:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        if not settings.himalayas_mcp_url:
            raise MCPError("HIMALAYAS_MCP_URL is not configured")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.url = settings.himalayas_mcp_url
        self.headers = headers

    async def call(self, tool_name: str, arguments: dict) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        try:
            token = await access_token(self.account_id)
        except LoginExpired:
            raise  # nothing was sent yet. The caller keeps the message queued until the person reconnects.
        except Exception as exc:
            raise MCPError(str(exc)) from exc
        headers = {**self.headers, "Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(self.url, headers=headers, json=payload)
        response.raise_for_status()
        data_line = next(
            (line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")),
            response.text,
        )
        data = json.loads(data_line)
        if data.get("error"):
            raise MCPError(str(data["error"]))
        result = data.get("result", {})
        if result.get("isError"):
            # A tool reports its own failure inside a normal reply. Treating that as success is how a refused message
            # used to be recorded as sent.
            reason = result_text(result)[:300] or "no reason given"
            hint = " Himalayas' spam filter blocked this text. Rewrite it. Repeated spam messages can get the account banned." if "filter" in reason.lower() or "spam" in reason.lower() else ""
            raise HimalayasRejected(f"Himalayas rejected {tool_name}: {reason}{hint}")
        return result

    async def rpc(self, method: str, params: dict | None = None) -> dict:
        """One raw MCP request (for example tools/list). Read-only checks use it. Returns the parsed JSON-RPC reply."""
        token = await access_token(self.account_id)
        headers = {**self.headers, "Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(self.url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        line = next((l[5:].strip() for l in response.text.splitlines() if l.startswith("data:")), response.text)
        try:
            return {"http": response.status_code, **json.loads(line)}
        except ValueError:
            return {"http": response.status_code, "raw": response.text[:300]}

    async def list_candidates(self, page: int = 1) -> list[dict]:
        result = await self.call("search_talent", {"page": page, "sort": "recent"})
        text = "\n".join(
            item.get("text", "") for item in result.get("content", []) if item.get("type") == "text"
        )
        candidates = []
        for block in text.split("\n---\n"):
            slug_match = re.search(r"himalayas\.app/@([^?\s]+)", block)
            name_match = re.search(r"\*\*([^*]+)\*\*", block)
            if not slug_match or not name_match:
                continue
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            summary = next((line for line in lines if "💡" in line), "").replace("💡", "").strip()
            role = next((line for line in lines if line.startswith("💼")), "").replace("💼", "").strip()
            candidates.append({
                "talent_slug": slug_match.group(1),
                "name": name_match.group(1).strip(),
                "summary": f"{role}. {summary}".strip(". "),
                "profile_url": f"https://himalayas.app/@{slug_match.group(1)}",
                "stack": [],
                "page": page,
            })
        return candidates

    async def get_talent_profile(self, talent_slug: str) -> str:
        result = await self.call("get_talent_profile", {"talent_slug": talent_slug})
        return "\n".join(
            item.get("text", "") for item in result.get("content", []) if item.get("type") == "text"
        )

    async def find_existing_room(self, candidate_slug: str) -> str | None:
        """Find a conversation Himalayas already has for this member when start_conversation cannot return it.

        Happens often when another login of the same company already opened the room (or start_conversation
        created it but failed to return the name). list_conversations and a guessed room name are tried.
        """
        try:
            rooms = await self.rooms_by_slug()
            if candidate_slug in rooms:
                return rooms[candidate_slug]["room"]
        except Exception as exc:
            print(f"Could not list Himalayas conversations while recovering a room: {exc}")
        company = get_state(f"himalayas_company:{self.account_id}")
        if not company:
            return None
        guessed = f"{ROOM_PREFIX}{company}_{candidate_slug}"
        try:
            result = await self.call("get_conversation", {"room_name": guessed})
            text = result_text(result)
            if re.search(r"Room: `" + re.escape(guessed) + r"`", text) or "messages" in text.lower() or "🏢" in text or "👤" in text:
                return guessed
            # Some replies omit the room line but still return the thread for a valid room.
            if text.strip() and "not found" not in text.lower() and "does not exist" not in text.lower():
                return guessed
        except HimalayasRejected:
            return None
        except Exception as exc:
            print(f"Could not open guessed Himalayas room {guessed}: {exc}")
        return None

    async def open_room(self, candidate_slug: str) -> str:
        """Create the conversation, or return the existing one. It does not reliably post a message, so none is passed."""
        try:
            result = await self.call("start_conversation", {"talent_slug": candidate_slug})
        except HimalayasRejected as exc:
            detail = str(exc)
            if "already exists but could not be fetched" in detail:
                # The room is often still usable (same company, another profile, or a fetch glitch). Recover it
                # instead of treating every case as a permanently deleted conversation.
                room = await self.find_existing_room(candidate_slug)
                if room:
                    try:
                        note_company(self.account_id, room, candidate_slug)
                    except Exception as note_exc:
                        print(f"Could not note the Himalayas company: {note_exc}")
                    return room
                raise ConversationUnavailable(
                    "Himalayas cannot reopen this member's conversation. It already exists but could not be fetched, "
                    "and it was not found in the conversation list. Nothing was sent."
                ) from exc
            if "failed to start conversation" in detail.lower():
                raise HimalayasUnavailable(str(exc)) from exc
            raise
        match = re.search(r"Room: `([^`]+)`", result_text(result))
        if not match:
            # start_conversation sometimes succeeds without a room line when the conversation already exists.
            room = await self.find_existing_room(candidate_slug)
            if room:
                return room
            raise HimalayasRejected("Himalayas did not return a conversation for this member")
        try:
            note_company(self.account_id, match.group(1), candidate_slug)
        except Exception as exc:  # bookkeeping only. It must never stop a send
            print(f"Could not note the Himalayas company: {exc}")
        return match.group(1)

    async def send_message(self, candidate_slug: str, body: str, first_contact: bool = False) -> dict:
        """Post the message, then read the conversation back. It counts as sent only if the text is really there."""
        room = await self.open_room(candidate_slug)
        if first_contact:
            # The contact ledger only knows what this app sent. Himalayas knows everything, including messages sent by hand.
            # Never send a first message into a conversation where the company has already written.
            before = await self.call("get_conversation", {"room_name": room})
            if has_company_message(result_text(before)):
                raise AlreadyMessaged("Already messaged on Himalayas")
        result = await self.call(settings.mcp_send_message_tool, {"room_name": room, "message": body})
        conversation = await self.call("get_conversation", {"room_name": room})
        if letters_only(body)[:40] not in letters_only(result_text(conversation)):
            raise HimalayasRejected("Himalayas accepted the request, but the message is not in the conversation")
        return result

    async def rooms_by_slug(self) -> dict[str, dict]:
        """Every conversation Himalayas holds, keyed by the member's slug, with its state line."""
        result = await self.call("list_conversations", {})
        rooms = {}
        for match in re.finditer(r"👤 \*\*(.+?)\*\*(.*?)🔑 Room: `([^`]+)`", result_text(result), re.S):
            room = match.group(3)
            slug = room.split("_", 4)[-1] if room.count("_") >= 4 else room.rsplit("_", 1)[-1]
            rooms[slug] = {"name": match.group(1), "room": room, "state": " ".join(match.group(2).split())}
        return rooms

    async def get_thread(self, room: str) -> dict:
        return parse_conversation(result_text(await self.call("get_conversation", {"room_name": room})))

    async def list_messages(self) -> list[dict]:
        result = await self.call("list_conversations", {})
        text = "\n".join(item.get("text", "") for item in result.get("content", []) if item.get("type") == "text")
        messages = []
        for block in text.split("\n---\n"):
            if "New reply" not in block:
                continue
            room_match = re.search(r"Room: `([^`]+)`", block)
            if not room_match:
                continue
            history = await self.call("get_conversation", {"room_name": room_match.group(1)})
            history_text = "\n".join(item.get("text", "") for item in history.get("content", []) if item.get("type") == "text")
            slug_match = re.search(r"himalayas\.app/@([^\s]+)", history_text)
            if not slug_match:
                continue
            headers = list(re.finditer(r"(?m)^(🏢|👤)\s+\*\*(.+?)\*\*.*$", history_text))
            for index, header in enumerate(headers):
                if header.group(1) != "👤":
                    continue
                body_start = header.end()
                body_end = headers[index + 1].start() if index + 1 < len(headers) else len(history_text)
                body = history_text[body_start:body_end].strip()
                body = re.sub(r"\n🔑 Room:.*", "", body, flags=re.DOTALL).strip()
                if body:
                    messages.append({
                        "external_id": hashlib.sha256(f"{room_match.group(1)}\n{body}".encode()).hexdigest(),
                        "talent_slug": slug_match.group(1),
                        "candidate_name": header.group(2).strip(),
                        "body": body,
                        "direction": "inbound",
                        "created_at": utc_now(),
                    })
        return messages
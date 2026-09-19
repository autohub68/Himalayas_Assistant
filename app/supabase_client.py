import asyncio
import time

import httpx

from .config import settings


class SupabaseError(RuntimeError):
    pass


RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
SCHEMA_RECHECK_SECONDS = 60

# Status of a member in the shared ledger. Only "sent" means the member was contacted.
QUEUED, SENT, FAILED = "queued", "sent", "failed"


class SupabaseLedger:
    """Shared contact ledger: one row per member (talent_slug), with the outreach status.

    Rows with status "sent" block a second first-message to the same member from any account.
    "queued" and "failed" rows only make the status visible. They never block anyone.
    Before the status columns exist in Supabase (see supabase_schema.sql), the ledger works as it did
    before: it stores sent contacts only.
    """

    _status_ready: bool | None = None
    _status_checked_at = 0.0

    def __init__(self) -> None:
        if not settings.supabase_url or not settings.supabase_key:
            raise SupabaseError("SUPABASE_URL and SUPABASE_KEY are not configured")
        self.endpoint = f"{settings.supabase_url.rstrip('/')}/rest/v1/{settings.supabase_table}"
        self.headers = {
            "apikey": settings.supabase_key,
            "Authorization": f"Bearer {settings.supabase_key}",
            "Content-Type": "application/json",
        }

    async def status_ready(self) -> bool:
        """True when the table has the status columns. Checked again every minute, so running the SQL takes effect without a restart."""
        cls = SupabaseLedger
        if cls._status_ready is not None and time.monotonic() - cls._status_checked_at < SCHEMA_RECHECK_SECONDS:
            return cls._status_ready
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(self.endpoint, headers=self.headers, params={"select": "status", "limit": 1})
        if response.is_error:
            if "42703" in response.text or "does not exist" in response.text:
                ready = False
            else:
                raise SupabaseError(f"Ledger schema check failed ({response.status_code}): {response.text}")
        else:
            ready = True
        cls._status_ready, cls._status_checked_at = ready, time.monotonic()
        return ready

    async def was_contacted(self, talent_slug: str) -> bool:
        last_error: SupabaseError | None = None
        for attempt in range(RETRY_ATTEMPTS):
            if attempt:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            try:
                params = {"talent_slug": f"eq.{talent_slug}", "select": "id", "limit": 1}
                if await self.status_ready():
                    params["status"] = f"eq.{SENT}"  # queued and failed rows do not count as contacted
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.get(self.endpoint, headers=self.headers, params=params)
            except SupabaseError as exc:
                last_error = exc
                continue
            if not response.is_error:
                return bool(response.json())
            last_error = SupabaseError(f"Contact lookup failed ({response.status_code}): {response.text}")
        raise last_error

    @staticmethod
    def base_payload(candidate: dict, message_id: int | None, body: str) -> dict:
        return {
            "talent_slug": candidate["external_id"],
            "candidate_name": candidate["name"],
            "profile_url": candidate.get("profile_url", ""),
            "summary": candidate.get("summary", ""),
            "category": candidate.get("category", "unknown"),
            "stack": candidate.get("stack", []),
            "message_id": message_id,
            "message_body": body,
        }

    async def record_contact(self, candidate: dict, message_id: int, body: str) -> None:
        """The first message was sent. Overwrites a queued or failed row for the same member."""
        payload = self.base_payload(candidate, message_id, body)
        payload["sent_at"] = candidate.get("sent_at")
        if await self.status_ready():
            payload.update(status=SENT, error=None, account_id=candidate.get("account_id"), account_label=candidate.get("account_label"), updated_at=candidate.get("sent_at"))
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                self.endpoint,
                headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                params={"on_conflict": "talent_slug"},
                json=payload,
            )
        if response.is_error:
            raise SupabaseError(f"Contact ledger write failed ({response.status_code}): {response.text}")

    async def record_status(self, candidate: dict, status: str, message_id: int | None = None, body: str = "", error: str | None = None) -> None:
        """Show that a first message is queued or failed. A row that is already "sent" is never changed.
        Does nothing until the status columns exist in Supabase."""
        if status not in {QUEUED, FAILED}:
            raise ValueError(f"record_status handles queued and failed only, not {status}")
        if not await self.status_ready():
            return
        payload = self.base_payload(candidate, message_id, body)
        payload.update(status=status, error=error, account_id=candidate.get("account_id"), account_label=candidate.get("account_label"))
        # A queued message can be replaced by a failed one, and a failed one can be queued again. Nothing replaces "sent".
        replaces = "failed" if status == QUEUED else "queued,failed"
        async with httpx.AsyncClient(timeout=20) as client:
            created = await client.post(
                self.endpoint,
                headers={**self.headers, "Prefer": "resolution=ignore-duplicates,return=minimal"},
                params={"on_conflict": "talent_slug"},
                json=payload,
            )
            if created.is_error:
                raise SupabaseError(f"Ledger status write failed ({created.status_code}): {created.text}")
            changed = await client.patch(
                self.endpoint,
                headers={**self.headers, "Prefer": "return=minimal"},
                params={"talent_slug": f"eq.{candidate['external_id']}", "status": f"in.({replaces})"},
                json={key: value for key, value in payload.items() if key != "talent_slug"},
            )
        if changed.is_error:
            raise SupabaseError(f"Ledger status update failed ({changed.status_code}): {changed.text}")

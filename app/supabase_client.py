import asyncio
import time

import httpx

from .config import settings


class SupabaseError(RuntimeError):
    pass


RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
SCHEMA_RECHECK_SECONDS = 60

# Status of a member in the shared ledger.
# "sent" = already messaged. "queued" = one account has claimed the member and will send.
# Both block every other account from a first message. "failed" does not block.
QUEUED, SENT, FAILED = "queued", "sent", "failed"
CLAIMED = (QUEUED, SENT)


class SupabaseLedger:
    """Shared contact ledger: one row per member (talent_slug), with the outreach status.

    Rows with status "sent" or "queued" block a second first-message to the same member from any
    other account. The account that wrote the "queued" row may still deliver that message.
    "failed" rows only make the status visible. They never block anyone.
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
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(self.endpoint, headers=self.headers, params={"select": "status", "limit": 1})
        except httpx.HTTPError as exc:
            raise SupabaseError(f"Could not reach Supabase: {type(exc).__name__}") from exc
        if response.is_error:
            if "42703" in response.text or "does not exist" in response.text:
                ready = False
            else:
                raise SupabaseError(f"Ledger schema check failed ({response.status_code}): {response.text}")
        else:
            ready = True
        cls._status_ready, cls._status_checked_at = ready, time.monotonic()
        return ready

    async def claim_info(self, talent_slug: str) -> dict | None:
        """Current ledger claim for this member: {status, account_id} or None when there is no blocking row."""
        last_error: SupabaseError | None = None
        for attempt in range(RETRY_ATTEMPTS):
            if attempt:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            try:
                if not await self.status_ready():
                    # Legacy table: any row means sent.
                    params = {"talent_slug": f"eq.{talent_slug}", "select": "id", "limit": 1}
                    async with httpx.AsyncClient(timeout=20) as client:
                        response = await client.get(self.endpoint, headers=self.headers, params=params)
                    if response.is_error:
                        last_error = SupabaseError(f"Contact lookup failed ({response.status_code}): {response.text}")
                        continue
                    return {"status": SENT, "account_id": None} if response.json() else None
                params = {
                    "talent_slug": f"eq.{talent_slug}",
                    "status": f"in.({QUEUED},{SENT})",
                    "select": "status,account_id",
                    "limit": 1,
                }
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.get(self.endpoint, headers=self.headers, params=params)
            except SupabaseError as exc:
                last_error = exc
                continue
            except httpx.HTTPError as exc:
                last_error = SupabaseError(f"Could not reach Supabase: {type(exc).__name__}")
                continue
            if response.is_error:
                last_error = SupabaseError(f"Contact lookup failed ({response.status_code}): {response.text}")
                continue
            rows = response.json()
            if not rows:
                return None
            row = rows[0]
            return {"status": row.get("status") or SENT, "account_id": row.get("account_id") or None}
        raise last_error

    async def was_contacted(self, talent_slug: str, account_id: str | None = None) -> bool:
        """True when this member must not get a first message from this account.

        Blocks on "sent" always. Blocks on "queued" when another account holds the claim.
        The account that queued the member may still deliver (was_contacted returns False for them).
        """
        claim = await self.claim_info(talent_slug)
        if not claim:
            return False
        if claim["status"] == SENT:
            return True
        if claim["status"] == QUEUED:
            owner = claim.get("account_id")
            if not account_id:
                return True  # import / bulk skip: any queue claim means leave this member alone
            if not owner:
                return False  # old row with no owner: let a waiting sender deliver; Himalayas still blocks a true duplicate
            return owner != account_id
        return False

    async def contacted_among(self, slugs: list[str]) -> set[str]:
        """Which of these members are already sent or claimed (queued)? One request for the whole page.

        On any problem it returns an empty set, because the check before every send still protects
        against a second message.
        """
        if not slugs:
            return set()
        try:
            quoted = ",".join('"' + slug.replace('"', '\\"') + '"' for slug in slugs)
            params = {"talent_slug": f"in.({quoted})", "select": "talent_slug", "limit": len(slugs)}
            if await self.status_ready():
                params["status"] = f"in.({QUEUED},{SENT})"
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(self.endpoint, headers=self.headers, params=params)
            return {row["talent_slug"] for row in response.json()} if not response.is_error else set()
        except Exception:
            return set()

    @staticmethod
    def base_payload(candidate: dict, message_id: int | None, body: str) -> dict:
        payload = {
            "talent_slug": candidate["external_id"],
            "candidate_name": candidate["name"],
            "profile_url": candidate.get("profile_url", ""),
            "summary": candidate.get("summary", ""),
            "category": candidate.get("category", "unknown"),
            "stack": candidate.get("stack", []),
            "message_id": message_id,
            "message_body": body,
        }
        country = (candidate.get("country") or "").strip()
        if country:
            payload["country"] = country
        return payload

    async def list_contacts(
        self,
        *,
        status: str | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = 50,
        account_id: str | None = None,
    ) -> dict:
        """Browse the shared ledger. Returns {total, offset, limit, contacts}."""
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        needle = (q or "").replace(",", " ").replace(".", " ").strip()

        def params(with_country: bool) -> dict[str, str]:
            select = "id,talent_slug,candidate_name,profile_url,summary,category,stack,status,error,account_id,account_label,message_id,message_body,sent_at,created_at,updated_at"
            if with_country:
                select += ",country"
            out: dict[str, str] = {
                "select": select,
                "order": "updated_at.desc.nullslast,sent_at.desc.nullslast,id.desc",
                "limit": str(limit),
                "offset": str(offset),
            }
            if status:
                out["status"] = f"eq.{status}"
            if account_id:
                out["account_id"] = f"eq.{account_id}"
            if needle:
                fields = ["candidate_name", "talent_slug", "account_label"]
                if with_country:
                    fields.append("country")
                out["or"] = "(" + ",".join(f"{field}.ilike.*{needle}*" for field in fields) + ")"
            return out

        headers = {**self.headers, "Prefer": "count=exact"}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(self.endpoint, headers=headers, params=params(True))
                if response.is_error and "country" in response.text:
                    response = await client.get(self.endpoint, headers=headers, params=params(False))
        except httpx.HTTPError as exc:
            raise SupabaseError(f"Could not reach Supabase: {type(exc).__name__}") from exc
        if response.is_error:
            raise SupabaseError(f"Ledger list failed ({response.status_code}): {response.text}")
        total = None
        content_range = response.headers.get("content-range") or ""
        if "/" in content_range:
            try:
                total = int(content_range.rsplit("/", 1)[-1])
            except ValueError:
                total = None
        rows = response.json()
        return {"total": total if total is not None else len(rows), "offset": offset, "limit": limit, "contacts": rows}

    async def get_contact(self, talent_slug: str) -> dict | None:
        """One ledger row by talent slug, or None."""
        select_full = "id,talent_slug,candidate_name,profile_url,summary,category,stack,status,error,account_id,account_label,message_id,message_body,sent_at,created_at,updated_at,country"
        select_basic = select_full.replace(",country", "")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    self.endpoint,
                    headers=self.headers,
                    params={"talent_slug": f"eq.{talent_slug}", "select": select_full, "limit": "1"},
                )
                if response.is_error and "country" in response.text:
                    response = await client.get(
                        self.endpoint,
                        headers=self.headers,
                        params={"talent_slug": f"eq.{talent_slug}", "select": select_basic, "limit": "1"},
                    )
        except httpx.HTTPError as exc:
            raise SupabaseError(f"Could not reach Supabase: {type(exc).__name__}") from exc
        if response.is_error:
            raise SupabaseError(f"Ledger lookup failed ({response.status_code}): {response.text}")
        rows = response.json()
        return rows[0] if rows else None

    async def record_contact(self, candidate: dict, message_id: int, body: str) -> None:
        """The first message was sent. Overwrites a queued or failed row for the same member."""
        payload = self.base_payload(candidate, message_id, body)
        payload["sent_at"] = candidate.get("sent_at")
        if await self.status_ready():
            payload.update(status=SENT, error=None, account_id=candidate.get("account_id"), account_label=candidate.get("account_label"), updated_at=candidate.get("sent_at"))
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    self.endpoint,
                    headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                    params={"on_conflict": "talent_slug"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise SupabaseError(f"Contact ledger write failed: could not reach Supabase ({type(exc).__name__})") from exc
        if response.is_error:
            raise SupabaseError(f"Contact ledger write failed ({response.status_code}): {response.text}")

    async def record_status(self, candidate: dict, status: str, message_id: int | None = None, body: str = "", error: str | None = None) -> bool:
        """Claim a first message as queued, or mark it failed. A row that is already "sent" is never changed.

        For "queued": does not steal a queued claim from another account. Returns False when the member
        is already sent or queued by someone else (caller should skip the local message).
        Does nothing (returns True) until the status columns exist in Supabase.
        """
        if status not in {QUEUED, FAILED}:
            raise ValueError(f"record_status handles queued and failed only, not {status}")
        if not await self.status_ready():
            return True
        account_id = candidate.get("account_id")
        if status == QUEUED:
            claim = await self.claim_info(candidate["external_id"])
            if claim and claim["status"] == SENT:
                return False
            if claim and claim["status"] == QUEUED:
                owner = claim.get("account_id")
                # Another account holds the claim, or an ownerless queued row exists: do not take over.
                if not account_id or not owner or owner != account_id:
                    return False
        payload = self.base_payload(candidate, message_id, body)
        payload.update(status=status, error=error, account_id=account_id, account_label=candidate.get("account_label"))
        # failed → queued/failed freely. Own queued rows refresh in a second patch. Never replaces "sent".
        replaces = "failed" if status == QUEUED else "queued,failed"
        try:
            return await self._write_status(payload, candidate, replaces, status=status)
        except httpx.HTTPError as exc:
            raise SupabaseError(f"Ledger status write failed: could not reach Supabase ({type(exc).__name__})") from exc

    async def _write_status(self, payload: dict, candidate: dict, replaces: str, status: str = FAILED) -> bool:
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
            if status == QUEUED and payload.get("account_id"):
                own = await client.patch(
                    self.endpoint,
                    headers={**self.headers, "Prefer": "return=minimal"},
                    params={
                        "talent_slug": f"eq.{candidate['external_id']}",
                        "status": f"eq.{QUEUED}",
                        "account_id": f"eq.{payload['account_id']}",
                    },
                    json={key: value for key, value in payload.items() if key != "talent_slug"},
                )
                if own.is_error:
                    raise SupabaseError(f"Ledger status update failed ({own.status_code}): {own.text}")
            if status == QUEUED:
                claim = await self.claim_info(candidate["external_id"])
                if not claim or claim["status"] == SENT:
                    return False
                owner = claim.get("account_id")
                if owner and payload.get("account_id") and owner != payload["account_id"]:
                    return False
        return True

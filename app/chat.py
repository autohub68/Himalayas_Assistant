"""Hiring conversation. Steps 1 to 3 are the same for every candidate. Step 4 depends on the suggested role.

Stages (the stage is the state a conversation is left in by our last SENT message):
  first_sent      step 1 sent: short offer for one suggested role
  intro_sent      step 2 sent: company introduction, asked about interest and confidence
  process_sent    step 3 sent: hiring process overview, asked if it works
  assessment_sent step 4, developer roles: assessment overview, asked for the GitHub username
  invite_pending  developer roles: username received, but the invitation failed on our side. It is retried automatically
  invited         developer roles: GitHub invitation sent, assessment requirements are in the project
  apply_sent      step 4, business roles: careers-page link for the suggested position sent
  closed          candidate declined
A candidate reply moves the conversation one step forward, or gets a short answer that repeats the open question.
"""
import random
from datetime import datetime, timedelta, timezone

from . import ai, prompted
from .config import settings
from .db import connection, get_state, utc_now
from .github import GitHubError, GitHubUserNotFound, detect_email, detect_username, invite_to_repository, repository_name, settings_ready

FIRST, INTRO, PROCESS, ASSESSMENT, INVITE_PENDING, INVITED, APPLY, CLOSED = "first_sent", "intro_sent", "process_sent", "assessment_sent", "invite_pending", "invited", "apply_sent", "closed"
FINAL_STAGES = {INVITED, APPLY}
# Reply time counts from when the reply is detected. Polling adds up to REPLY_POLL_INTERVAL_SECONDS, so the total stays near 1 to 2 minutes.
REPLY_DELAY_SECONDS = (40, 85)


def category_for(role: str) -> str:
    """Candidate category from the suggested role."""
    if ai.is_developer_role(role):
        return "developer"
    if role in ai.NON_DEV_ROLES:
        return "non_developer"
    return "developer" if ai.DEVELOPER_PATTERN.search(role or "") else "non_developer"  # a role written in a prompt, not in a list


def current_stage(candidate_id: int) -> str | None:
    """Stage of the last SENT outbound message. None if nothing was sent yet. Older messages without a stage count as step 1."""
    with connection() as conn:
        row = conn.execute(
            "SELECT stage FROM messages WHERE candidate_id=? AND direction='outbound' AND status='sent' "
            "ORDER BY COALESCE(sent_at, created_at) DESC, id DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
    if not row:
        return None
    return row["stage"] or FIRST


def supersede_pending_replies(candidate_id: int, in_flight_ids: set[int]) -> None:
    """A newer candidate message replaces any reply that is still waiting to be sent."""
    with connection() as conn:
        pending = conn.execute(
            "SELECT id FROM messages WHERE candidate_id=? AND direction='outbound' AND status='scheduled'", (candidate_id,)
        ).fetchall()
        for row in pending:
            if row["id"] not in in_flight_ids:
                conn.execute("UPDATE messages SET status='superseded' WHERE id=?", (row["id"],))


def reply_time() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=random.randint(*REPLY_DELAY_SECONDS))).isoformat()


async def plan_reply(candidate: dict, body: str, history: list[dict]) -> dict | None:
    """Decide the reply to one candidate message. Returns {"body", "stage"} or None when no reply is needed."""
    stage = current_stage(candidate["id"])
    if stage is None or stage == CLOSED:
        return None
    if ai.PROMPTS.get("mode") == "prompt":
        return await prompted.plan_reply(candidate, body, history, stage)
    name = candidate["name"]
    last_outbound = next((item["body"] for item in reversed(history) if item["direction"] == "outbound"), "")
    reading = await ai.read_reply(candidate, stage, last_outbound, body)
    intent = reading["intent"]
    role = candidate.get("suggested_role")
    if not role or role not in ai.ALL_ROLES:
        # Contacted before roles were stored, or the role is not in the current playbook.
        role = await ai.choose_role(candidate)
        with connection() as conn:
            conn.execute("UPDATE candidates SET suggested_role=?, category=? WHERE id=?", (role, category_for(role), candidate["id"]))

    if intent == "negative":
        return {"body": ai.closing_message(name), "stage": CLOSED}

    if stage in FINAL_STAGES:
        # The last step is done (assessment running, or careers link sent). Answer real questions only. Thanks and short notes get no reply.
        if intent == "question":
            return {"body": await ai.write_answer(candidate, stage, history, body, role), "stage": stage}
        return None

    if stage in {ASSESSMENT, INVITE_PENDING}:
        username = detect_username(body, reading["github_username"])
        if username:
            return await invite(candidate, username, stage)
        if stage == INVITE_PENDING and intent != "question":
            return None  # the invitation is being retried. Nothing new to say.
        return {"body": await ai.write_answer(candidate, stage, history, body, role), "stage": stage}

    if intent == "positive":
        if stage == FIRST:
            level = int(get_state("intro_level", "1"))  # learned: the wording of the introduction that Himalayas accepts
            return {"body": await ai.write_intro(candidate, role, level), "stage": INTRO, "variant": level}
        if stage == INTRO:
            return {"body": ai.process_message(name, role), "stage": PROCESS}
        # Process agreed. Developers take an assessment. Business roles apply on their careers page.
        if ai.is_developer_role(role):
            return {"body": await ai.write_assessment(candidate, role), "stage": ASSESSMENT}
        return {"body": ai.application_message(name, role), "stage": APPLY}

    return {"body": await ai.write_answer(candidate, stage, history, body, role), "stage": stage}


async def invite(candidate: dict, username: str, stage: str = ASSESSMENT) -> dict | None:
    name = candidate["name"]
    with connection() as conn:
        conn.execute("UPDATE candidates SET github_username=? WHERE id=?", (username, candidate["id"]))
    written = ai.PROMPTS.get("mode") == "prompt"
    try:
        await invite_to_repository(username)
    except GitHubUserNotFound:
        return {"body": await prompted.invitation_message(candidate, username, "not_found") if written else ai.username_not_found_message(name, username), "stage": ASSESSMENT}
    except GitHubError as exc:
        # Our own setup problem (token, repository). The error shows in the chat details and the invitation is retried
        # automatically. The candidate gets one short holding message, not the error.
        with connection() as conn:
            conn.execute("UPDATE candidates SET github_invited_at=? WHERE id=?", (f"failed: {exc}", candidate["id"]))
        if stage == INVITE_PENDING:
            return None  # the holding message was already sent
        return {"body": await prompted.invitation_message(candidate, username, "pending") if written else ai.invite_pending_message(name), "stage": INVITE_PENDING}
    with connection() as conn:
        conn.execute("UPDATE candidates SET github_invited_at=? WHERE id=?", (utc_now(), candidate["id"]))
    return {"body": await prompted.invitation_message(candidate, username, "invited") if written else ai.invited_message(name, username, repository_name()), "stage": INVITED, "invited": True}


async def retry_invitation(candidate: dict) -> dict | None:
    """Retry an invitation that failed on our side. Returns the message to send, or None to try again later."""
    written = ai.PROMPTS.get("mode") == "prompt"
    try:
        await invite_to_repository(candidate["github_username"])
    except GitHubUserNotFound:
        return {"body": await prompted.invitation_message(candidate, candidate["github_username"], "not_found") if written else ai.username_not_found_message(candidate["name"], candidate["github_username"]), "stage": ASSESSMENT}
    except GitHubError as exc:
        with connection() as conn:
            conn.execute("UPDATE candidates SET github_invited_at=? WHERE id=?", (f"failed: {exc}", candidate["id"]))
        return None
    with connection() as conn:
        conn.execute("UPDATE candidates SET github_invited_at=? WHERE id=?", (utc_now(), candidate["id"]))
    return {"body": await prompted.invitation_message(candidate, candidate["github_username"], "invited") if written else ai.invited_message(candidate["name"], candidate["github_username"], repository_name()), "stage": INVITED, "invited": True}


def candidates_waiting_for_invitation() -> list:
    """Candidates whose last sent message was the holding message and who have nothing else queued."""
    with connection() as conn:
        return conn.execute(
            """SELECT c.* FROM candidates c WHERE c.github_username IS NOT NULL
            AND (SELECT stage FROM messages WHERE candidate_id=c.id AND direction='outbound' AND status='sent'
                 ORDER BY COALESCE(sent_at, created_at) DESC, id DESC LIMIT 1) = ?
            AND NOT EXISTS (SELECT 1 FROM messages WHERE candidate_id=c.id AND direction='outbound' AND status='scheduled')""",
            (INVITE_PENDING,),
        ).fetchall()


def note_contact_details(candidate_id: int, body: str) -> None:
    email = detect_email(body)
    if email:
        with connection() as conn:
            conn.execute("UPDATE candidates SET github_email=? WHERE id=?", (email, candidate_id))

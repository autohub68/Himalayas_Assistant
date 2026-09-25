"""Per-account client company the bot hires FOR (agency / recruiting stance).

Real agencies: lightly identify as recruiters, put the *client* and role front-and-center,
never pitch the agency. Each bot stores its own client name + brief via the dashboard.
"""

from __future__ import annotations

import json

from .db import get_state, set_state

DEFAULT_NAME = "Ocean Park Asset"
DEFAULT_ABOUT = "Ocean Park Asset builds AI technology for digital asset markets."


def _key(account_id: str) -> str:
    return f"hiring_client:{account_id}"


def parse_description(description: str) -> dict:
    """First non-empty line = company name; remaining lines = brief introduction."""
    lines = [line.strip() for line in (description or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return {"name": DEFAULT_NAME, "about": DEFAULT_ABOUT, "description": f"{DEFAULT_NAME}\n{DEFAULT_ABOUT}"}
    name = lines[0]
    if name.lower().startswith("name:"):
        name = name.split(":", 1)[1].strip() or name
    about = "\n".join(lines[1:]).strip()
    if about.lower().startswith("about:"):
        about = about.split(":", 1)[1].strip()
    if not about:
        about = DEFAULT_ABOUT if name == DEFAULT_NAME else f"{name} is hiring."
    text = f"{name}\n{about}".strip()
    return {"name": name, "about": about, "description": text}


def get_hiring_client(account_id: str | None) -> dict:
    """Client company this account hires for. Falls back to Ocean Park Asset defaults."""
    if not account_id:
        return parse_description(f"{DEFAULT_NAME}\n{DEFAULT_ABOUT}")
    raw = get_state(_key(account_id))
    if not raw:
        return parse_description(f"{DEFAULT_NAME}\n{DEFAULT_ABOUT}")
    try:
        data = json.loads(raw)
    except ValueError:
        return parse_description(raw if isinstance(raw, str) else "")
    if isinstance(data, dict):
        description = (data.get("description") or "").strip()
        if description:
            return parse_description(description)
        name = (data.get("name") or "").strip() or DEFAULT_NAME
        about = (data.get("about") or "").strip() or DEFAULT_ABOUT
        return {"name": name, "about": about, "description": f"{name}\n{about}".strip()}
    return parse_description(str(data))


def set_hiring_client(account_id: str, description: str) -> dict:
    client = parse_description(description)
    set_state(_key(account_id), json.dumps(client))
    return client


def client_prompt_block(client: dict, *, first_message: bool = False) -> str:
    """How a professional recruiting agency talks when placing talent with a client."""
    name = client.get("name") or DEFAULT_NAME
    about = client.get("about") or DEFAULT_ABOUT
    if first_message:
        return f"""WHO YOU ARE (recruiting agency — be realistic)
You are a human recruiter at an independent recruiting / talent firm.
You are sourcing candidates FOR a client. You do not work for the client as staff.
Client (the hiring company): {name}
Client brief (saved for the NEXT message after they show interest — do NOT paste it here): {about}

WHEN THE CLIENT IS INTRODUCED
- First touch: only NAME {name} once as the hiring company. Do not explain what they do yet.
- Full "about {name}" introduction happens in the follow-up, after the member replies positively.

HOW REAL AGENCIES WRITE A FIRST TOUCH
- Sound like one person writing to another: calm, specific, no sales pitch about your firm.
- Identify as a recruiter in at most a short phrase ("I'm a recruiter…", "reaching out on a search…").
- Do not name or describe your recruiting company. Never say "our recruiting firm", "we are an agency filling…", or pitch services.
- Lead with why this person fits a live search at {name}, then name the role once.
- Name {name} once. Do not invent client facts beyond the brief.
- Soft close: one open question (interest / short chat). No pressure, no process dump, no pay, no links.
- Never write crypto, trading, platform, invest, profit, or token."""
    return f"""WHO YOU ARE (recruiting agency — be realistic)
You are a human recruiter at an independent recruiting / talent firm hiring FOR a client.
Client (the hiring company): {name}
What {name} does (this is the client introduction — stay faithful to this brief): {about}

WHEN THE CLIENT IS INTRODUCED (important)
- This follow-up is the moment you properly introduce {name}: what they do, then the role.
- The first message only named them; now explain the client clearly so the candidate understands who they would work with.
- Focus on {name}, the role, and the candidate — not on your agency.
- If you mention your role, keep it to a light "I'm the recruiter on this search" at most.
- Share client context only from the brief above. Do not invent team size, funding, or location.
- Stay professional, concise, and human. No marketing hype."""

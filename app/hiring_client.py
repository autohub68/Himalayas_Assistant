"""Client employer vs recruiting agency.

Client company is always Ocean Park Asset (roles, intros, pay, every chat step).
Each account may store its own recruiting-agency profile for a brief “I’m a recruiter…”
mention only — never as the company the candidate would work for.
"""

from __future__ import annotations

import json

from .db import get_state, set_state

# Fixed client employer for every bot / every step.
CLIENT_NAME = "Ocean Park Asset"
CLIENT_ABOUT = "Ocean Park Asset builds AI technology for digital asset markets."
CLIENT_WEBSITE = "oceanparkasset.com"

# Legacy key wrongly stored the agency as "client". Kept for one-time migration.
_LEGACY_KEY = "hiring_client:{account_id}"
_AGENCY_KEY = "recruiting_agency:{account_id}"


def ocean_park_client() -> dict:
    """The only client company bots hire for."""
    return {
        "name": CLIENT_NAME,
        "about": CLIENT_ABOUT,
        "website": CLIENT_WEBSITE,
        "description": f"{CLIENT_NAME}\n{CLIENT_ABOUT}",
    }


def _agency_key(account_id: str) -> str:
    return _AGENCY_KEY.format(account_id=account_id)


def _legacy_key(account_id: str) -> str:
    return _LEGACY_KEY.format(account_id=account_id)


def parse_description(description: str) -> dict:
    """First non-empty line = company name; remaining lines = brief."""
    lines = [line.strip() for line in (description or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return {"name": "", "about": "", "description": ""}
    name = lines[0]
    if name.lower().startswith("name:"):
        name = name.split(":", 1)[1].strip() or name
    about = "\n".join(lines[1:]).strip()
    if about.lower().startswith("about:"):
        about = about.split(":", 1)[1].strip()
    text = f"{name}\n{about}".strip() if about else name
    return {"name": name, "about": about, "description": text}


def _looks_like_ocean_park(data: dict) -> bool:
    name = (data.get("name") or "").strip().lower()
    return "ocean park" in name


def _load_raw_agency(account_id: str) -> dict | None:
    raw = get_state(_agency_key(account_id))
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            return parse_description(raw if isinstance(raw, str) else "")
        if isinstance(data, dict):
            description = (data.get("description") or "").strip()
            if description:
                return parse_description(description)
            name = (data.get("name") or "").strip()
            about = (data.get("about") or "").strip()
            if name or about:
                return {"name": name, "about": about, "description": f"{name}\n{about}".strip()}
        return parse_description(str(data))
    # One-time: old "hiring_client" often held the agency (e.g. Techhavenlabs).
    legacy = get_state(_legacy_key(account_id))
    if not legacy:
        return None
    try:
        data = json.loads(legacy)
    except ValueError:
        data = parse_description(legacy if isinstance(legacy, str) else "")
    if isinstance(data, dict) and not _looks_like_ocean_park(data):
        agency = parse_description(data.get("description") or f"{data.get('name', '')}\n{data.get('about', '')}")
        if agency.get("name"):
            set_state(_agency_key(account_id), json.dumps(agency))
            return agency
    return None


def get_recruiting_agency(account_id: str | None) -> dict:
    """Per-account recruiting firm profile (brief mention only). Empty if unset."""
    empty = {"name": "", "about": "", "description": ""}
    if not account_id:
        return empty
    return _load_raw_agency(account_id) or empty


def set_recruiting_agency(account_id: str, description: str) -> dict:
    agency = parse_description(description)
    set_state(_agency_key(account_id), json.dumps(agency))
    return agency


# Back-compat names used by older call sites / API field mapping.
def get_hiring_client(account_id: str | None) -> dict:
    """Deprecated name: returns the recruiting agency profile for the dashboard textarea."""
    return get_recruiting_agency(account_id)


def set_hiring_client(account_id: str, description: str) -> dict:
    """Deprecated name: saves the recruiting agency profile."""
    return set_recruiting_agency(account_id, description)


def client_prompt_block(
    client: dict | None = None,
    *,
    first_message: bool = False,
    agency: dict | None = None,
) -> str:
    """Prompts: client is always Ocean Park Asset; agency helps candidates get hired there."""
    client = client or ocean_park_client()
    name = client.get("name") or CLIENT_NAME
    about = client.get("about") or CLIENT_ABOUT
    agency = agency or {}
    agency_name = (agency.get("name") or "").strip() or "our recruiting firm"
    agency_about = (agency.get("about") or "").strip()

    if first_message:
        return f"""WHO YOU ARE
You work at a recruitment company{f' named {agency_name}' if agency.get('name') else ''}.
Your firm helps candidates get hired by client companies. You do not employ them yourself.
CLIENT company for this search (always): {name}
Client brief (for message 2 only — do NOT paste in message 1): {about}
{f'Agency about (tone only; do not paste the whole thing): {agency_about}' if agency_about else ''}

FIRST-MESSAGE FRAMING (required)
1. Greet the member by name.
2. In one short sentence, say you are with a recruitment company that helps candidates get hired by companies
   (name {agency_name if agency.get('name') else 'your firm'} once if known). Do NOT pitch agency services or describe staffing in detail.
3. Name one real profile detail if available.
4. Name the role once at {name} once (the hiring company).
5. Soft question (open to a short chat?).

CRITICAL
- {name} is the employer. Your firm only helps them get hired there.
- Do not introduce what {name} does yet (that is message 2).
- In-app chat DM, not email. No Subject:, no [Your Name], no Best/Regards.
- No crypto, trading, platform, invest, profit, or token. No links, pay, or process dump."""

    return f"""WHO YOU ARE
You work at a recruitment company{f' ({agency_name})' if agency.get('name') else ''} that helps candidates get hired by client companies.
CLIENT (always — introduce THIS company now): {name}
What {name} does (stay faithful; do not invent): {about}
{f'Agency context (do not paste): {agency_about}' if agency_about else ''}

CRITICAL
- This message introduces {name}, not your recruiting firm.
- At most one short reminder that you are the recruiter helping them explore this hire.
- All substance is about {name} and the role. No marketing hype about your agency."""

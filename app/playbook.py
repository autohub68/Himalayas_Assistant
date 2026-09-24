"""Playbook: one Markdown file that holds everything specific to a company and a freelance platform.

The bot's structure does not change. The playbook supplies the company, the roles and pay, the wording of each step,
and the prompts. Import it in the Control center. Export the active one to get a template to edit.
Sections that a file leaves out keep the built-in text, so a short file works.
"""

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone

from . import ai
from .db import get_state, set_state

STATE_MD, STATE_META = "playbook_md", "playbook_meta"
MAX_BYTES = 200_000
TYPE_ALIASES = {"developer": "developer", "dev": "developer", "assessment": "developer", "technical": "developer",
                "business": "business", "non-developer": "business", "nondeveloper": "business", "application": "business"}
PLACEHOLDERS = {"role", "company", "website", "careers", "url", "interview", "username", "repository", "a_role"}
MESSAGE_SECTIONS = {  # heading in the file -> key in ai.MESSAGES, and the placeholders that make sense there
    "hiring process (developer)": "process_developer",
    "hiring process (business)": "process_business",
    "application": "application",
    "invitation sent": "invited",
    "invitation pending": "invite_pending",
    "not interested": "closing",
    "github username not found": "username_not_found",
    "fallback answer": "fallback_answer",
}
QUESTION_LABELS = {
    "after first message": "first_sent",
    "after introduction": "intro_sent",
    "after experience": "experience_sent",
    "after process": "process_sent",
    "after assessment": "assessment_sent",
}


# ---------- reading a file ----------

def heading(line: str) -> tuple[int, str] | None:
    match = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
    return (len(match.group(1)), re.sub(r"[*_`]", "", match.group(2)).strip()) if match else None


def split_sections(text: str) -> tuple[dict, list]:
    """A tree: {level-2 name(lower): {"body": [...], "subs": {level-3 name(lower): {"title", "body": [...], "subs": {...}}}}}."""
    root, current, sub, subsub = {}, None, None, None
    preamble: list[str] = []
    for line in text.splitlines():
        found = heading(line)
        if found and found[0] == 1 and current is None:
            preamble.append(line)
        elif found and found[0] == 2:
            current, sub, subsub = {"title": found[1], "body": [], "subs": {}}, None, None
            root[found[1].lower()] = current
        elif found and found[0] == 3 and current is not None:
            sub, subsub = {"title": found[1], "body": [], "subs": {}}, None
            if found[1].lower() in current["subs"]:
                current.setdefault("duplicates", []).append(found[1])
            current["subs"][found[1].lower()] = sub
        elif found and found[0] == 4 and sub is not None:
            subsub = {"title": found[1], "body": []}
            sub["subs"][found[1].lower()] = subsub
        elif subsub is not None:
            subsub["body"].append(line)
        elif sub is not None:
            sub["body"].append(line)
        elif current is not None:
            current["body"].append(line)
        else:
            preamble.append(line)
    return root, preamble


def text_of(lines: list[str]) -> str:
    return "\n".join(lines).strip()


def bullets(lines: list[str]) -> list[str]:
    """List items. A line that starts with - or * (or 1.) is one item. An indented line continues the item before it."""
    items: list[str] = []
    for line in lines:
        match = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$", line)
        if match:
            items.append(match.group(1).strip())
        elif line.strip() and items and line.startswith((" ", "\t")):
            items[-1] += " " + line.strip()
    return items


def fields(lines: list[str]) -> tuple[dict, list[str]]:
    """`Key: value` lines (also `- Key: value` and `**Key:** value`). Returns the fields and the lines that are not fields."""
    found, rest = {}, []
    for line in lines:
        match = re.match(r"^\s*(?:[-*]\s+)?\**([A-Za-z][A-Za-z ()/'-]{1,40}?)\**\s*:\**\s*(.*\S)\s*$", line)
        if match:
            found[match.group(1).strip().lower()] = match.group(2).strip()
        else:
            rest.append(line)
    return found, rest


# ---------- prompt mode: the file is a few prompts ----------

PROMPT_HEADINGS = {"first message prompt", "chat logic prompt", "chat logic"}
KNOWLEDGE_HEADINGS = ("company and roles", "company and role knowledge", "knowledge", "company", "roles")
STYLE_HEADINGS = ("style rules", "style", "writing rules")


def raw_sections(md: str) -> dict[str, str]:
    """Text under each `## Heading`, everything included (sub-headings too). Keys are lower case."""
    sections: dict[str, list[str]] = {}
    current = None
    fenced = False
    for line in md.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
        found = None if fenced else heading(line)
        if found and found[0] == 2:
            current = found[1].lower()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {name: text_of(lines) for name, lines in sections.items()}


def parse_prompts(md: str, raw: dict[str, str]) -> tuple[dict, list[str], list[str], list[str]]:
    """A prompt playbook: knowledge, first message prompt, chat logic prompt, style. Returns (playbook, errors, warnings, notes)."""
    errors: list[str] = []
    warnings: list[str] = []
    notes: list[str] = ["Prompt mode: the AI writes message 1 and every reply from your prompts. The fixed steps and role lists are not used."]
    _, preamble = split_sections(md)
    title = next((heading(line) for line in preamble if heading(line)), None)
    name = re.sub(r"^playbook\s*[:\-–]\s*", "", title[1], flags=re.IGNORECASE).strip() if title else ""
    head_fields, _ = fields(preamble)
    first = raw.get("first message prompt") or ""
    chat = raw.get("chat logic prompt") or raw.get("chat logic") or ""
    knowledge = "\n\n".join(f"{raw[key]}" for key in KNOWLEDGE_HEADINGS if raw.get(key))
    style = next((raw[key] for key in STYLE_HEADINGS if raw.get(key)), "")
    if not first:
        errors.append('Section "## First message prompt" is missing. Describe how message 1 must look.')
    elif len(first) < 30:
        warnings.append('"## First message prompt" is very short. A longer description gives better messages.')
    if not chat:
        errors.append('Section "## Chat logic prompt" is missing. Describe the steps of the conversation and what to do when the member answers.')
    elif len(chat) < 30:
        warnings.append('"## Chat logic prompt" is very short. A longer description gives better replies.')
    if not knowledge:
        warnings.append('There is no "## Company and roles" section. The AI then has no facts to use and will say very little about the company, the roles and pay.')
    if len(knowledge) + len(first) + len(chat) + len(style) > 60_000:
        errors.append("The prompts are too long (over 60,000 characters). Shorten them.")
    pb = {"mode": "prompt", "name": name or "Prompt playbook", "platform": head_fields.get("platform", ""),
          "prompts": {"knowledge": knowledge, "first": first, "chat": chat, "style": style}}
    return pb, errors, warnings, notes


def prompt_template() -> dict:
    """A prompt playbook that gives the same behavior as the built-in one, written as prompts. Edit it for another company."""
    d = default_playbook()
    c = d["company"]
    about = c["about"].replace("{company}", c["name"])
    knowledge = [f'COMPANY\nName: {c["name"]}\nWebsite: {c["website"]}\n{about}', "ROLES (pay text is exact. Copy it word for word)"]
    for r in d["roles"]:
        if r["type"] == "developer":
            lines = [f'{r["name"]} (developer role. Assessment on GitHub)', r["rate"], "No job description is on file. Do not describe duties, tools, or requirements for this role.", f'Assessment: {d["assessments"].get(r["name"], "")}']
        else:
            lines = [f'{r["name"]} (business role. Application link)', r["rate"], f'Application link: {r["url"]}', f'Interview: {r["interview"] or "an interview"}', f'What the role does: {r["summary"]}', "Facts about the role (the only details you may give):", r["facts"]]
        knowledge.append("\n".join(line for line in lines if line))
    first = """Write a short, personal first message (2 to 4 short sentences, at most 60 words).
- Start with a greeting that uses the member's name.
- Name one real detail from the member's profile, in plain everyday words. If the profile is thin, claim nothing about it.
- Say that Ocean Park Asset is hiring, and name the ONE role that fits the member best, exactly as written in the roles list.
- End with one short friendly question, for example asking if the member is open to a short chat.
- This is a cold message on a platform whose spam filter refuses pitches. Never write the words crypto, trading, platform, invest, profit, returns, token, earnings, or income. No links, prices, or percentages. No marketing words such as exciting or amazing.
- Vary the opening, the sentence order and the closing every time, so no two messages look the same."""
    chat = """The first message is sent. Each time the member answers, send the next step. Do not skip a step. Do not repeat a step. Sound like a professional recruiter: calm, clear, and human. Do not sound like a short bot template.

Step 2, company introduction (message 2): thank the member in one short sentence for their interest. Explain in two short sentences that Ocean Park Asset builds AI technology for digital asset markets. Do not write the words crypto, trading or platform. Say in one sentence what the role does, using only the role facts (developer roles have none: say nothing about duties). Give the website as plain text: oceanparkasset.com. Give the pay text of the role word for word. End with this question: How do you feel about this role, and how confident are you in this work?

Step 3, experience check (message 3): thank the member for sharing how they feel about the role. Mention one or two real details from their profile (skills, tools, or past work) in plain words. If the profile is thin, invent nothing. Ask about their recent experience in a way that fits the suggested role (for example a project, stack, client work, or responsibility). End with this question: Could you tell me a little more about your recent experience with this kind of work?

Step 4, hiring process (message 4): thank the member for sharing their experience. Developer roles: first a technical assessment, second an interview about real project challenges, third a check of technical fit and teamwork, then an offer if all goes well. Business roles: first a short application form with a lightweight assessment, second the interview named in the role facts with the leadership team, third a final interview and a contract for selected candidates. End with this question: Does this process work for you?

Step 5, next action (message 5):
- Developer roles: explain the assessment for the role in two short sentences, linked to one or two skills from the member's profile. Say that we invite the member to a GitHub project for the assessment. End with this question: What is your GitHub username? When the member sends a username, use the action invite_github. After the system confirms the invitation, tell the member to accept it, open the project folder, read the requirements with care, complete the work and send the result.
- Business roles: give the application link of the role exactly as written, and say that our team reviews the application after it is submitted.

After step 5, answer real questions only. A thank-you or short note needs no reply.

Always:
- If the member asks a question, answer it briefly using only the knowledge text, then repeat the question that is still open. If the answer is not in the knowledge text (for example location or team size), say that the team will discuss it in a later step. Never change, round, negotiate or promise pay. If asked about anything beyond the pay text, say that the team will discuss it later.
- If the member declines or asks to stop, use the action close with a short, polite closing.
- Every message is professional, clear, and follows the style rules."""
    return {"mode": "prompt", "name": "Ocean Park Asset (prompts)", "platform": "Himalayas",
            "prompts": {"knowledge": "\n\n".join(knowledge), "first": first, "chat": chat, "style": "\n".join(f"- {line}" for line in d["writing_rules"])}}


def prompt_markdown(pb: dict) -> str:
    p = pb["prompts"]
    return (
        f'# Playbook: {pb["name"]}\n\nPlatform: {pb["platform"]}\n\n'
        "<!-- Prompt playbook. You write four prompts in plain language. The AI writes message 1 and every reply from them.\n"
        "     Fixed by the system, whatever you write: the member's name is used in message 1 only, pay figures and links must be written in\n"
        "     the Company and roles section, and messages are short plain text. -->\n\n"
        "## Company and roles\n\n<!-- Everything the AI may say: the company, the roles, pay, application links, the assessment. It may state nothing that is not here. -->\n\n" + (p["knowledge"] or "") + "\n\n"
        "## First message prompt\n\n" + p["first"] + "\n\n"
        "## Chat logic prompt\n\n" + p["chat"] + "\n\n"
        "## Style rules\n\n" + (p["style"] or "") + "\n"
    )


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def parse(md: str) -> tuple[dict, list[str], list[str], list[str]]:
    """Returns (playbook, errors, warnings, notes). The playbook is complete: what the file leaves out keeps the built-in text."""
    md = re.sub(r"<!--.*?-->", "", md, flags=re.DOTALL)  # comments are for the person who writes the file
    errors: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    raw = raw_sections(md)
    if raw.keys() & PROMPT_HEADINGS:
        return parse_prompts(md, raw)
    default = default_playbook()
    pb = json.loads(json.dumps(default))  # a copy
    pb["roles"] = []
    root, preamble = split_sections(md)

    title = next((heading(line) for line in preamble if heading(line)), None)
    if title:
        pb["name"] = re.sub(r"^playbook\s*[:\-–]\s*", "", title[1], flags=re.IGNORECASE).strip() or default["name"]
    head_fields, _ = fields(preamble)
    pb["platform"] = head_fields.get("platform", pb["platform"])

    # -- company
    company = root.get("company")
    if not company:
        errors.append('Section "## Company" is missing. It needs the company name and the website.')
    else:
        found, _ = fields(company["body"])
        pb["company"]["name"] = found.get("name", "").strip()
        pb["company"]["website"] = found.get("website", "").strip()
        pb["company"]["careers_url"] = (found.get("careers page") or found.get("careers url") or found.get("careers") or "").strip()
        if not pb["company"]["name"]:
            errors.append('"## Company" needs a line "Name: ...". This is the company name as the platform shows it.')
        if not re.match(r"^https?://\S+$", pb["company"]["website"]):
            errors.append('"## Company" needs a line "Website: https://...".')
        about = company["subs"].get("about")
        if about and text_of(about["body"]):
            pb["company"]["about"] = text_of(about["body"])
        else:
            notes.append("No About text: the built-in company description is used. Add ### About under ## Company.")
            if pb["company"]["about"] == default["company"]["about"] and pb["company"]["name"] != default["company"]["name"]:
                errors.append('"### About" is missing under "## Company". Without it the bot would describe the wrong company. Write what the company does, in plain words.')
        if not pb["company"]["careers_url"]:
            pb["company"]["careers_url"] = pb["company"]["website"].rstrip("/") + "/careers" if pb["company"]["website"] else ""
            notes.append(f'No "Careers page:" line. Business role links use {pb["company"]["careers_url"]}/<role-name>.')

    # -- first message
    first = root.get("first message")
    if first:
        for key, names in (("hiring", ("hiring sentences",)), ("closers", ("closing lines",))):
            sub = next((first["subs"][n] for n in names if n in first["subs"]), None)
            items = bullets(sub["body"]) if sub else []
            if items:
                pb["first_message"][key] = items
        instructions = first["subs"].get("instructions")
        kept = [line for line in (instructions["body"] if instructions else []) if not line.strip().startswith("(")]
        if text_of(kept):
            pb["first_message"]["instructions"] = text_of(kept)
    # -- introduction lines (message 2)
    intro = root.get("introduction lines")
    if intro and bullets(intro["body"]):
        pb["intro_lines"] = bullets(intro["body"])
    # -- questions
    questions = root.get("questions")
    if questions:
        found, _ = fields(questions["body"])
        for label, key in QUESTION_LABELS.items():
            if found.get(label):
                pb["questions"][key] = found[label]
    # -- messages
    messages = root.get("messages")
    if messages:
        for name, sub in messages["subs"].items():
            key = MESSAGE_SECTIONS.get(name)
            if key is None:
                warnings.append(f'Unknown message section "{sub["title"]}" was ignored. Known: ' + ", ".join(f'"{n.title()}"' for n in MESSAGE_SECTIONS) + ".")
            elif text_of(sub["body"]):
                pb["messages"][key] = text_of(sub["body"])
    # -- writing rules and role choice
    rules = root.get("writing rules")
    if rules and bullets(rules["body"]):
        pb["writing_rules"] = bullets(rules["body"])
    choice = root.get("role choice rules")
    if choice and text_of(choice["body"]):
        pb["role_choice_rules"] = text_of(choice["body"])
    # -- assessments
    pb["assessments"] = {}
    assessments = root.get("assessments")
    if assessments:
        for sub in assessments["subs"].values():
            if text_of(sub["body"]):
                pb["assessments"][sub["title"]] = text_of(sub["body"])
    # -- roles
    roles = root.get("roles")
    if not roles or not roles["subs"]:
        errors.append('Section "## Roles" needs at least one role, written as "### Role name".')
    else:
        seen = set()
        for name in roles.get("duplicates", []):
            errors.append(f'Role "{name}" is listed twice.')
        for sub in roles["subs"].values():
            found, rest = fields(sub["body"])
            details = sub["subs"].get("details")
            role = {
                "name": sub["title"], "type": TYPE_ALIASES.get(found.get("type", "").lower(), ""), "rate": found.get("rate", ""),
                "url": found.get("careers link") or found.get("link") or found.get("apply link") or "",
                "interview": found.get("interview", ""), "summary": found.get("summary", ""),
                "facts": text_of(details["body"]) if details else text_of(rest),
            }
            if not role["type"]:
                errors.append(f'Role "{sub["title"]}" needs "Type: developer" (assessment on GitHub) or "Type: business" (application link).')
            if role["type"] == "business":
                if not role["url"]:
                    role["url"] = f'{pb["company"]["careers_url"].rstrip("/")}/{slug(sub["title"])}'
                    notes.append(f'{sub["title"]}: no "Careers link:", so {role["url"]} is used.')
                if not role["summary"]:
                    errors.append(f'Business role "{sub["title"]}" needs a "Summary:" line (one sentence that starts with a verb, for example "Owns brand and growth."). It is used in the introduction.')
            if not role["rate"]:
                warnings.append(f'{sub["title"]}: no "Rate:" line. The bot will say nothing about pay for this role.')
            pb["roles"].append(role)
        if not any(r["type"] == "developer" for r in pb["roles"]):
            notes.append("No developer roles: the GitHub assessment step is not used.")
    developer_names = [r["name"] for r in pb["roles"] if r["type"] == "developer"]
    pb["assessments"] = {k: v for k, v in pb["assessments"].items() if k in developer_names}
    for name in developer_names:
        if name not in pb["assessments"]:
            warnings.append(f'{name}: no assessment text under "## Assessments". A general text is used.')
    role_defaults = fields(roles["body"])[0] if roles else {}
    for kind in ("developer", "business"):
        chosen = role_defaults.get(f"default {kind} role")
        pb["default_roles"][kind] = chosen if chosen in [r["name"] for r in pb["roles"] if r["type"] == kind] else ""
        if chosen and not pb["default_roles"][kind]:
            warnings.append(f'"Default {kind} role: {chosen}" is not a {kind} role in this file. The first one is used.')

    validate(pb, errors, warnings)
    return pb, errors, warnings, notes


def validate(pb: dict, errors: list[str], warnings: list[str]) -> None:
    """Rules that protect the bot: no member name after message 1, known placeholders, and words the platform's spam filter refused."""
    def scan(label: str, text: str, allowed: set[str], spam: bool = False) -> None:
        for name in set(re.findall(r"\{(\w+)\}", text)):
            if name == "name":
                errors.append(f'{label}: "{{name}}" is not allowed. The bot uses the member name in the first message only.')
            elif name not in allowed:
                errors.append(f'{label}: unknown placeholder "{{{name}}}". Allowed here: ' + ", ".join("{" + n + "}" for n in sorted(allowed)) + ".")
        if spam and (found := ai.SPAM_TERMS.search(re.sub(r"\{\w+\}", "", text))):
            warnings.append(f'{label}: "{found.group(0)}" was refused by Himalayas\' spam filter in a first message. Use other words.')
    base = {"company", "website", "careers"}
    for key, text in pb["messages"].items():
        allowed = base | {"role", "interview", "url", "username", "repository"}
        scan(f'Message "{key}"', text, allowed)
    if "{url}" not in pb["messages"]["application"]:
        errors.append('Message "Application" must contain {url}, the link to the application page.')
    if "{username}" not in pb["messages"]["invited"]:
        warnings.append('Message "Invitation sent" has no {username}.')
    for key, text in pb["questions"].items():
        scan(f"Question ({key})", text, base)
    for text in pb["first_message"]["hiring"]:
        scan("Hiring sentence", text, base | {"role", "a_role"}, spam=True)
        if not re.search(r"\{(?:role|a_role)\}", text):
            errors.append(f'Hiring sentence "{text[:50]}" must contain {{role}} or {{a_role}}.')
    for text in pb["first_message"]["closers"]:
        scan("Closing line", text, base, spam=True)
    for text in pb["intro_lines"]:
        scan("Introduction line", text, base, spam=True)
    scan("About", pb["company"]["about"], base)
    for role in pb["roles"]:
        for label, text in (("rate", role["rate"]), ("interview", role["interview"]), ("details", role["facts"])):
            scan(f'Role "{role["name"]}" {label}', text, base)
    if any(not (re.search(r"\{company\}", line)) for line in pb["intro_lines"]):
        warnings.append('An introduction line has no {company}. That is allowed, but the message reads better with the company name.')
    if not pb["first_message"]["hiring"] or not pb["first_message"]["closers"] or not pb["intro_lines"]:
        errors.append("The first message needs hiring sentences, closing lines and introduction lines.")


# ---------- the built-in playbook ----------

_default: dict | None = None
_current: dict | None = None  # the playbook the bot is using now


def default_playbook() -> dict:
    """The built-in content (Ocean Park Asset on Himalayas), read from the defaults in ai.py at import time."""
    global _default
    if _default is None:
        _default = {
            "mode": "structured", "name": "Ocean Park Asset on Himalayas", "platform": "Himalayas",
            "company": {"name": ai.COMPANY_NAME, "website": ai.COMPANY_WEBSITE, "careers_url": ai.CAREERS_URL, "about": ai.DEFAULT_ABOUT},
            "writing_rules": [line[2:].strip() for line in ai.DEFAULT_WRITING_RULES.splitlines() if line.startswith("- ")],
            "role_choice_rules": ai.DEFAULT_ROLE_CHOICE_RULES.split("\n", 1)[1].strip(),
            "questions": dict(ai.PENDING_QUESTIONS),
            "first_message": {"hiring": list(ai.HIRING_PHRASES), "closers": list(ai.CLOSERS), "instructions": ""},
            "intro_lines": list(ai.BUSINESS_LINES),
            "messages": dict(ai.DEFAULT_MESSAGES),
            "assessments": dict(ai.ASSESSMENT_OVERVIEWS),
            "default_roles": {"developer": "Full Stack Developer", "business": "Operations Manager"},
            "roles": (
                [{"name": r, "type": "developer", "rate": ai.DEV_RATES.get(r, ""), "url": "", "interview": "", "summary": "", "facts": ""} for r in ai.ROLES]
                + [{"name": n, "type": "business", "rate": d["rate"], "url": d["url"], "interview": d["interview"] or "", "summary": d["summary"], "facts": d["facts"]} for n, d in ai.NON_DEV_ROLES.items()]
            ),
        }
    return _default


def to_markdown(pb: dict) -> str:
    """The playbook as a file to edit. Reading this file back gives the same playbook."""
    if pb.get("mode") == "prompt":
        return prompt_markdown(pb)
    c, out = pb["company"], []
    add = out.append
    add(f'# Playbook: {pb["name"]}\n\nPlatform: {pb["platform"]}\n')
    add("<!-- How this file works: every section is optional except Company and Roles. A section that is left out keeps the built-in text.")
    add("     The member's name is used in the first message only, so {name} is not allowed in any other text.")
    add("     Placeholders: {company} {website} {careers} {role} {a_role} {url} {interview} {username} {repository} -->\n")
    add(f'## Company\n\nName: {c["name"]}\nWebsite: {c["website"]}\nCareers page: {c["careers_url"]}\n\n### About\n\n{c["about"]}\n')
    add("## First message\n\nMessage 1 is put together in code from these lists, and the AI writes one personal sentence. Do not use words the platform's spam filter refuses (crypto, trading, invest, profit, links, prices).\n")
    add("### Hiring sentences\n\n" + "\n".join(f"- {t}" for t in pb["first_message"]["hiring"]) + "\n")
    add("### Closing lines\n\n" + "\n".join(f"- {t}" for t in pb["first_message"]["closers"]) + "\n")
    add("### Instructions\n\n" + (pb["first_message"]["instructions"] or "(Optional. Extra instructions for the one personal sentence, one per line, for example: - Mention only their most recent job.)") + "\n")
    add("## Introduction lines\n\nMessage 2 uses one of these lines to describe the company. Keep them free of the words the spam filter refuses.\n\n" + "\n".join(f"- {t}" for t in pb["intro_lines"]) + "\n")
    q = pb["questions"]
    add("## Questions\n\nThe question at the end of each step. It is repeated when the member does not answer it.\n\n" + "\n".join(f"{label.capitalize()}: {q[key]}" for label, key in QUESTION_LABELS.items()) + "\n")
    add("## Messages\n")
    for name, key in MESSAGE_SECTIONS.items():
        add(f"### {name.title().replace('Github', 'GitHub')}\n\n{pb['messages'][key]}\n")
    add("## Writing rules\n\nRules for every text the AI writes.\n\n" + "\n".join(f"- {t}" for t in pb["writing_rules"]) + "\n")
    add(f'## Role choice rules\n\n{pb["role_choice_rules"]}\n')
    add("## Assessments\n\nFor developer roles: what the GitHub assessment is. (Business roles use an application link instead.)\n")
    for name, text in pb["assessments"].items():
        add(f"### {name}\n\n{text}\n")
    add("## Roles\n\nType: developer = assessment on GitHub. Type: business = application link. Rate is inserted word for word in message 2.\n")
    for kind in ("developer", "business"):
        chosen = pb["default_roles"].get(kind)
        if chosen:
            add(f"Default {kind} role: {chosen}")
    add("")
    for r in pb["roles"]:
        block = [f'### {r["name"]}', "", f'Type: {r["type"]}']
        if r["rate"]:
            block.append(f'Rate: {r["rate"]}')
        if r["type"] == "business":
            block += [f'Careers link: {r["url"]}', f'Interview: {r["interview"]}' if r["interview"] else "", f'Summary: {r["summary"]}']
        if r["facts"]:
            block += ["", "#### Details", "", r["facts"]]
        add("\n".join(line for line in block if line is not None).replace("\n\n\n", "\n\n") + "\n")
    return "\n".join(out).rstrip() + "\n"


# ---------- applying it ----------

def apply(pb: dict) -> None:
    """Make the bot use this playbook. Everything reads these values when it runs, so no restart is needed."""
    global _current
    _current = pb
    if pb.get("mode") == "prompt":
        ai.PROMPTS.clear(); ai.PROMPTS.update({"mode": "prompt", **pb["prompts"]})
        return
    ai.PROMPTS.clear(); ai.PROMPTS.update({"mode": "structured"})
    c = pb["company"]
    ai.COMPANY_NAME, ai.COMPANY_WEBSITE, ai.CAREERS_URL = c["name"], c["website"], c["careers_url"].rstrip("/")
    ai.OCEANPARKASSET_CONTEXT = ai.build_context(c["name"], c["website"], c["about"])
    ai.WRITING_RULES = "WRITING RULES\n" + "\n".join(f"- {t}" for t in pb["writing_rules"]) + "\n" + ai.NO_NAME_RULE
    ai.ROLE_CHOICE_RULES = "ROLE CHOICE RULES\n" + pb["role_choice_rules"]
    ai.PENDING_QUESTIONS.clear(); ai.PENDING_QUESTIONS.update({k: v.replace("{company}", c["name"]) for k, v in pb["questions"].items()})
    ai.HIRING_PHRASES = tuple(pb["first_message"]["hiring"])
    ai.CLOSERS = tuple(pb["first_message"]["closers"])
    ai.BUSINESS_LINES = tuple(pb["intro_lines"])
    extra = pb["first_message"]["instructions"].strip()
    ai.FIRST_MESSAGE_INSTRUCTIONS = "".join("\n   - " + line.lstrip("-* ").strip() for line in extra.splitlines() if line.strip() and not line.strip().startswith("(")) if extra else ""
    ai.MESSAGES.clear(); ai.MESSAGES.update(pb["messages"])
    developers = [r for r in pb["roles"] if r["type"] == "developer"]
    ai.ROLES = tuple(r["name"] for r in developers)
    ai.DEV_RATES.clear(); ai.DEV_RATES.update({r["name"]: r["rate"] for r in developers if r["rate"]})
    ai.ASSESSMENT_OVERVIEWS.clear(); ai.ASSESSMENT_OVERVIEWS.update(pb["assessments"])
    ai.NON_DEV_ROLES.clear()
    for r in pb["roles"]:
        if r["type"] == "business":
            ai.NON_DEV_ROLES[r["name"]] = {"rate": r["rate"], "url": r["url"], "interview": r["interview"] or None, "summary": r["summary"], "facts": r["facts"]}
    ai.ALL_ROLES = ai.ROLES + tuple(ai.NON_DEV_ROLES)
    ai.DEFAULT_ROLES.clear(); ai.DEFAULT_ROLES.update(pb["default_roles"])
    ai.reset_cycles()


@contextmanager
def applied(pb: dict):
    """Use a playbook for a moment (a preview), then put the active one back. It has no await inside, so nothing else runs meanwhile."""
    previous = _current or default_playbook()
    apply(pb)
    try:
        yield
    finally:
        apply(previous)


def active() -> dict:
    md = get_state(STATE_MD)
    if md:
        pb, errors, _, _ = parse(md)
        if not errors:
            return pb
    return default_playbook()


def meta() -> dict:
    try:
        return json.loads(get_state(STATE_META) or "{}")
    except ValueError:
        return {}


def load_active() -> None:
    """At start: use the imported playbook. A stored file that no longer reads correctly is ignored, and the built-in one is used."""
    md = get_state(STATE_MD)
    if md:
        pb, errors, _, _ = parse(md)
        if errors:
            print("Stored playbook has errors, using the built-in playbook:", "; ".join(errors[:3]))
            pb = default_playbook()
    else:
        pb = default_playbook()
    apply(pb)


def save_active(md: str, filename: str, pb: dict) -> None:
    set_state(STATE_MD, md)
    set_state(STATE_META, json.dumps({"filename": filename, "imported_at": datetime.now(timezone.utc).isoformat(), "name": pb["name"]}))
    apply(pb)


def reset_active() -> None:
    set_state(STATE_MD, "")
    set_state(STATE_META, "")
    apply(default_playbook())


def summary(pb: dict) -> dict:
    if pb.get("mode") == "prompt":
        p = pb["prompts"]
        return {"mode": "prompt", "name": pb["name"], "platform": pb["platform"], "prompts": {key: len(value) for key, value in p.items()}, "roles": []}
    return {"mode": "structured", 
        "name": pb["name"], "platform": pb["platform"],
        "company": pb["company"]["name"], "website": pb["company"]["website"], "careers_url": pb["company"]["careers_url"],
        "roles": [{"name": r["name"], "type": r["type"], "rate": r["rate"], "url": r["url"]} for r in pb["roles"]],
    }


def samples(pb: dict) -> list[dict]:
    """What the fixed messages look like with this playbook, for each role. The AI-written parts are not shown."""
    if pb.get("mode") == "prompt":
        return []  # written by the AI. The "Try it" box shows real output
    out = []
    with applied(pb):
        for role in pb["roles"]:
            name = role["name"]
            steps = [("Message 2 (introduction)", ai.intro_message({"name": "Member"}, name, 1)), ("Message 3 (hiring process)", ai.process_message("Member", name))]
            if role["type"] == "business":
                steps.append(("Message 4 (application link)", ai.application_message("Member", name)))
            else:
                steps.append(("Message 4 (after the GitHub invitation)", ai.invited_message("Member", "github-user", "project-repo")))
            out.append({"role": name, "type": role["type"], "steps": [{"step": label, "text": text} for label, text in steps]})
    return out


default_playbook()  # read the built-in text now, before any playbook changes it

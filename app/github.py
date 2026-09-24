import re

import httpx

from .config import settings

USERNAME = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
USERNAME_ONLY = re.compile(rf"^{USERNAME}$")
URL_PATTERN = re.compile(rf"github\.com/@?({USERNAME})(?![A-Za-z0-9_-])", re.IGNORECASE)
LABEL_PATTERN = re.compile(rf"github(?:\s+(?:user)?name|\s+handle|\s+account|\s+id)?\s*(?:is|:|=|-)\s*@?({USERNAME})(?![A-Za-z0-9_-])", re.IGNORECASE)
BARE_PATTERN = re.compile(rf"^@?({USERNAME})[.!]?$")
NOT_USERNAMES = {"yes", "ok", "okay", "sure", "thanks", "thank", "no", "yeah", "done", "hello", "hi", "please", "agree", "great", "good", "fine", "yep"}
EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")

AI_DEVELOPER_ROLE = "AI Developer"
TECH_ASSESSMENT_REPO = "Tech_Assessment"
AI_ASSESSMENT_REPO = "AI_Assessment"


class GitHubError(RuntimeError):
    pass


class GitHubUserNotFound(GitHubError):
    pass


def detect_username(body: str, model_guess: str | None = None) -> str | None:
    """Find a GitHub username in a reply.

    A link or a "github: name" label is explicit and trusted. A bare word such as "Interested" could be
    a normal reply, so it counts only when the model also names it as the username.
    """
    text = body.strip()
    for pattern in (URL_PATTERN, LABEL_PATTERN):
        match = pattern.search(text)
        if match and match.group(1).lower() not in NOT_USERNAMES:
            return match.group(1)
    guess = model_guess.lstrip("@") if isinstance(model_guess, str) else ""
    if guess and USERNAME_ONLY.match(guess) and guess.lower() not in NOT_USERNAMES:
        bare = BARE_PATTERN.match(text)
        if bare and bare.group(1).lower() == guess.lower():
            return guess
        if guess.lower() in text.lower():
            return guess
    return None


def detect_email(body: str) -> str | None:
    match = EMAIL_PATTERN.search(body)
    return match.group(0) if match else None


def api_headers() -> dict:
    return {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {settings.github_token}", "X-GitHub-Api-Version": "2022-11-28"}


def repo_for_role(role: str | None = None) -> str:
    """Assessment repository for the role. AI Developer uses AI_Assessment; every other developer uses Tech_Assessment."""
    if (role or "").strip() == AI_DEVELOPER_ROLE:
        return (settings.github_ai_repo or AI_ASSESSMENT_REPO).strip() or AI_ASSESSMENT_REPO
    return (settings.github_repo or TECH_ASSESSMENT_REPO).strip() or TECH_ASSESSMENT_REPO


def settings_ready() -> bool:
    return bool(settings.github_token and settings.github_owner and repo_for_role("Full Stack Developer") and repo_for_role(AI_DEVELOPER_ROLE))


def repository_name(role: str | None = None) -> str:
    return f"{settings.github_owner}/{repo_for_role(role)}"


async def invite_to_repository(username: str, role: str | None = None) -> None:
    """Check that the account exists, then invite it. Safe to repeat: GitHub ignores a second invitation."""
    repo = repo_for_role(role)
    if not settings.github_token or not settings.github_owner or not repo:
        raise GitHubError("GitHub token, owner, and repository are required in Settings")
    base = settings.github_api_url.rstrip("/")
    async with httpx.AsyncClient(timeout=30) as client:
        lookup = await client.get(f"{base}/users/{username}", headers=api_headers())
        if lookup.status_code == 404:
            raise GitHubUserNotFound(f"GitHub user {username} does not exist")
        if lookup.is_error:
            raise GitHubError(f"GitHub user lookup failed ({lookup.status_code}): {lookup.text}")
        response = await client.put(
            f"{base}/repos/{settings.github_owner}/{repo}/collaborators/{username}",
            headers=api_headers(),
            json={"permission": "pull"},
        )
    if response.status_code not in {201, 204, 422}:
        raise GitHubError(f"GitHub invitation failed ({response.status_code}): {response.text}")

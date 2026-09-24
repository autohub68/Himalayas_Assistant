import re
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import settings

# Data created before multi-account support belongs to this account until the first extension claims it.
LEGACY_ACCOUNT = "default"
ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection():
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def candidates_ddl(table: str) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            external_id TEXT NOT NULL,
            name TEXT NOT NULL,
            profile_url TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            stack_json TEXT DEFAULT '[]',
            category TEXT DEFAULT 'unknown',
            source_json TEXT DEFAULT '{{}}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            github_username TEXT,
            github_email TEXT,
            github_invited_at TEXT,
            suggested_role TEXT,
            country TEXT DEFAULT '',
            UNIQUE(account_id, external_id)
        );"""


def oauth_tokens_ddl(table: str) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {table} (
            account_id TEXT PRIMARY KEY,
            access_token TEXT NOT NULL,
            refresh_token TEXT,
            expires_at REAL,
            client_id TEXT NOT NULL,
            client_secret TEXT,
            needs_reconnect INTEGER NOT NULL DEFAULT 0
        );"""


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db() -> None:
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                paused INTEGER NOT NULL DEFAULT 0,
                last_seen TEXT
            );
            """
            + candidates_ddl("candidates")
            + """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL DEFAULT '',
                candidate_id INTEGER NOT NULL REFERENCES candidates(id),
                direction TEXT NOT NULL CHECK(direction IN ('outbound', 'inbound')),
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                send_after TEXT,
                sent_at TEXT,
                error TEXT,
                external_id TEXT,
                read_at TEXT,
                stage TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS himalayas_check (
                candidate_id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                room TEXT,
                total INTEGER NOT NULL DEFAULT 0,
                ours INTEGER NOT NULL DEFAULT 0,
                theirs INTEGER NOT NULL DEFAULT 0,
                ours_read INTEGER NOT NULL DEFAULT 0,
                last_when TEXT,
                matches INTEGER NOT NULL DEFAULT 0,
                note TEXT
            );
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_inbound (
                external_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                deleted_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_state (
                state TEXT PRIMARY KEY,
                account_id TEXT NOT NULL DEFAULT '',
                code_verifier TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
            + oauth_tokens_ddl("oauth_tokens")
        )
        migrate(conn)
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_ready ON messages(status, send_after);
            CREATE INDEX IF NOT EXISTS idx_messages_account ON messages(account_id, status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_external_id ON messages(external_id) WHERE external_id IS NOT NULL;
            """
        )


def migrate(conn: sqlite3.Connection) -> None:
    """Bring databases created by older versions up to the multi-account layout."""
    message_columns = columns(conn, "messages")
    for name in ("error", "external_id", "read_at", "stage", "variant"):
        if name not in message_columns:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {'INTEGER' if name == 'variant' else 'TEXT'}")
    if "account_id" not in message_columns:
        conn.execute("ALTER TABLE messages ADD COLUMN account_id TEXT NOT NULL DEFAULT ''")
    if "account_id" not in columns(conn, "oauth_state"):
        conn.execute("ALTER TABLE oauth_state ADD COLUMN account_id TEXT NOT NULL DEFAULT ''")

    account_columns = columns(conn, "accounts")
    if "paused" not in account_columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN paused INTEGER NOT NULL DEFAULT 0")
    if "last_seen" not in account_columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN last_seen TEXT")

    candidate_columns = columns(conn, "candidates")
    if "account_id" not in candidate_columns:
        # external_id used to be globally unique; it must now be unique per account, which needs a table rebuild.
        for name in ("github_username", "github_email", "github_invited_at"):
            if name not in candidate_columns:
                conn.execute(f"ALTER TABLE candidates ADD COLUMN {name} TEXT")
        conn.execute(candidates_ddl("candidates_new").replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE"))
        conn.execute(
            """INSERT INTO candidates_new (id, account_id, external_id, name, profile_url, summary, stack_json, category,
            source_json, created_at, updated_at, github_username, github_email, github_invited_at)
            SELECT id, ?, external_id, name, profile_url, summary, stack_json, category,
            source_json, created_at, updated_at, github_username, github_email, github_invited_at FROM candidates""",
            (LEGACY_ACCOUNT,),
        )
        conn.execute("DROP TABLE candidates")
        conn.execute("ALTER TABLE candidates_new RENAME TO candidates")

    if "suggested_role" not in columns(conn, "candidates"):
        conn.execute("ALTER TABLE candidates ADD COLUMN suggested_role TEXT")
    if "country" not in columns(conn, "candidates"):
        conn.execute("ALTER TABLE candidates ADD COLUMN country TEXT DEFAULT ''")

    if "needs_reconnect" not in columns(conn, "oauth_tokens") and "id" not in columns(conn, "oauth_tokens"):
        conn.execute("ALTER TABLE oauth_tokens ADD COLUMN needs_reconnect INTEGER NOT NULL DEFAULT 0")

    if "id" in columns(conn, "oauth_tokens"):
        conn.execute(oauth_tokens_ddl("oauth_tokens_new").replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE"))
        conn.execute(
            "INSERT INTO oauth_tokens_new (account_id, access_token, refresh_token, expires_at, client_id, client_secret) SELECT ?, access_token, refresh_token, expires_at, client_id, client_secret FROM oauth_tokens",
            (LEGACY_ACCOUNT,),
        )
        conn.execute("DROP TABLE oauth_tokens")
        conn.execute("ALTER TABLE oauth_tokens_new RENAME TO oauth_tokens")

    conn.execute("UPDATE messages SET account_id=(SELECT account_id FROM candidates WHERE candidates.id=messages.candidate_id) WHERE account_id=''")
    conn.execute("UPDATE oauth_state SET account_id=? WHERE account_id=''", (LEGACY_ACCOUNT,))
    has_legacy_data = any(
        conn.execute(f"SELECT 1 FROM {table} WHERE account_id=? LIMIT 1", (LEGACY_ACCOUNT,)).fetchone()
        for table in ("candidates", "oauth_tokens")
    )
    if has_legacy_data:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (id, label, created_at) VALUES (?, 'Existing data', ?)",
            (LEGACY_ACCOUNT, utc_now()),
        )


BACKUPS_TO_KEEP = 5


def backup_database() -> str:
    """Copy the database next to itself before a cleanup, keep the newest few copies, and return the file name."""
    path = Path(settings.database_path).resolve()
    target = path.with_name(f"{path.name}.bak-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}")
    source = sqlite3.connect(path)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    for old in sorted(path.parent.glob(f"{path.name}.bak-*"))[:-BACKUPS_TO_KEEP]:
        old.unlink(missing_ok=True)
    return target.name


def vacuum() -> None:
    """Give the freed space back to the file. Skipped quietly if the database is busy."""
    conn = sqlite3.connect(settings.database_path, isolation_level=None)
    try:
        conn.execute("VACUUM")
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


def get_state(key: str, default: str | None = None) -> str | None:
    """Small values the app learns and remembers, for example which wording of a message Himalayas accepts."""
    with connection() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(key: str, value: str) -> None:
    with connection() as conn:
        conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


ACCOUNT_TABLES = ("candidates", "messages", "oauth_tokens", "oauth_state", "himalayas_check")
known_accounts: set[str] = set()


def ensure_account(account_id: str) -> None:
    """Register an account on first sight. The first new account adopts data left over from the single-account version."""
    if account_id in known_accounts:
        return
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
            legacy = conn.execute("SELECT label FROM accounts WHERE id=?", (LEGACY_ACCOUNT,)).fetchone()
            if legacy and account_id != LEGACY_ACCOUNT:
                for table in ACCOUNT_TABLES:
                    conn.execute(f"UPDATE {table} SET account_id=? WHERE account_id=?", (account_id, LEGACY_ACCOUNT))
                conn.execute("UPDATE accounts SET id=? WHERE id=?", (account_id, LEGACY_ACCOUNT))
            else:
                conn.execute("INSERT INTO accounts (id, label, created_at) VALUES (?, '', ?)", (account_id, utc_now()))
    known_accounts.add(account_id)

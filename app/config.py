from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    openrouter_api_key: str = ""
    openrouter_model: str = "deepseek/deepseek-v4-flash-0731"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    himalayas_mcp_url: str = "https://mcp.himalayas.app/mcp"
    himalayas_mcp_token: str = ""
    himalayas_oauth_authorization_endpoint: str = "https://mcp.himalayas.app/authorize"
    himalayas_oauth_token_endpoint: str = "https://mcp.himalayas.app/oauth/token"
    himalayas_oauth_registration_endpoint: str = "https://mcp.himalayas.app/oauth/register"
    himalayas_oauth_redirect_uri: str = "http://127.0.0.1:8765/api/auth/callback"
    himalayas_oauth_client_id: str = ""
    himalayas_oauth_client_secret: str = ""
    mcp_list_candidates_tool: str = "list_candidates"
    mcp_send_message_tool: str = "send_message"
    mcp_get_profile_tool: str = "get_candidate_profile"
    mcp_list_messages_tool: str = "list_messages"
    database_path: str = "./hiring.db"
    auto_send: bool = False
    min_message_delay_seconds: int = 30
    max_message_delay_seconds: int = 120
    daily_dm_limit: int = 0  # new members messaged per day, for each profile. 0 = no limit
    delivery_poll_interval_seconds: int = 5
    profile_fetch_concurrency: int = 4
    message_generation_concurrency: int = 3
    reply_processing_concurrency: int = 4
    reply_poll_interval_seconds: int = 30
    supabase_url: str = ""
    supabase_key: str = ""
    supabase_table: str = "outreach_contacts"
    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    github_owner: str = ""
    github_repo: str = ""
    # Remote access to the Control center. Requests from this machine need no password. Any other request needs this login.
    # With no password set, remote requests are refused. SERVER_HOST=0.0.0.0 makes the server reachable from other machines.
    server_host: str = "127.0.0.1"
    admin_username: str = "admin"
    admin_password: str = ""

    model_config = SettingsConfigDict(
        env_file=(".env", "/home/star/.local/share/him/settings.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
# Himalayas Hiring Assistant

A local-first recruiting assistant with a Chromium extension UI and a Python backend.

## What it does

- Imports candidate profiles from the Himalayas MCP server.
- Classifies profiles as developer or non-developer using the candidate's visible stack and expertise.
- Generates concise first outreach and replies through OpenRouter.
- Grounds every DeepSeek recruiting message in the Oceanparkasset company brief and website context, including its AI crypto-trading platform, execution workflow, and risk controls.
- Schedules first outreach when Start page outreach is clicked, then sends one message at a time using the configured interval and reports queue, sent, skipped, and failed states.
- Supports automatic campaigns: sync page 1, queue and finish its outreach, then continue through later pages until MCP returns no more candidates.
- Uses bounded parallel MCP profile enrichment and AI message generation so sync and queue creation do not wait on every candidate serially.
- Schedules follow-up messages one at a time per candidate with a content-aware delay.
- Records inbound replies and generates the next recruiting message.
- Polls the configured MCP message tool, detects candidate replies, and automatically queues the next hiring-logic response without duplicating already processed messages.
- Guides engaged chats through profile-specific questions, a satisfaction and technical-assessment step, then requests a GitHub username for project access after the assessment stage.
- Detects GitHub usernames or emails in replies and sends a repository invitation when GitHub token, owner, and repository settings are configured.
- Processes Himalayas talent pages in order. Sync page 1, start that page's outreach, then use Next page to import and queue page 2, page 3, and later pages.
- Checks the Supabase contact ledger before every first message and skips any member whose `talent_slug` is already saved.
- Keeps campaign state in SQLite so the extension can be closed without losing work.

The first-outreach action creates a paced delivery queue when started. The scheduler sends one due campaign message at a time using `MIN_MESSAGE_DELAY_SECONDS`; follow-up messages remain controlled by `AUTO_SEND`. Use it only where the platform, candidate consent, and applicable law permit automated outreach.

## Conversation flow

Every candidate is contacted. Nobody is skipped because of their profile. The model reads the whole profile and picks the ONE closest role of the ten. If no role matches exactly, it uses transferable strengths (for example a designer becomes Marketing Manager, a lawyer becomes Compliance Officer). If a profile has almost no text, the first message makes no claim about the candidate. The only skips are duplicate protection: a candidate already messaged, or one the shared contact ledger shows another account already contacted. Each candidate reply moves the chat one step. The bot sends its reply 1 to 2 minutes after it sees the message. All messages follow ASD STE100.

Developer roles: Full Stack, Backend, Frontend, AI Developer. Business roles: Business Development Manager, Client Relations Manager, Marketing Manager, Operations Manager, Financial Analyst, Compliance Officer.

1. **First message** (about 40 words): names one or two real skills or results, suggests the role, and asks if the candidate wants to hear more.
2. **Company introduction**, after an interested reply: short business summary, the website, one line about the role (business roles), the **pay rate**, and one question about interest and confidence. The rate text is fixed per role (`rate` in `NON_DEV_ROLES`, and `DEV_RATES` for the developer roles) and inserted by code word for word, so the model can never change a number. Developer rates are USD per hour.
3. **Hiring process overview**, after a positive reply. Fixed text in `process_message()` in `app/ai.py`. Developers: technical assessment, project discussion, technical fit. Business roles: the process from the job description (application form with a lightweight assessment, interview with the leadership team, final interview and contract). Asks if the process works.
4. **After the candidate agrees, the step depends on the role:**
   - *Developer roles:* an assessment overview for the role and skills (`ASSESSMENT_OVERVIEWS` in `app/ai.py`), then a request for a GitHub username. The bot reads the username, checks that the account exists, invites it to the repository set in Settings, and says the requirements are in the project folder.
   - *Business roles:* the careers-page link for the suggested position (`NON_DEV_ROLES` in `app/ai.py`) and a request to submit the application there.

Other cases: a question gets a short answer and the open question is asked again. A decline gets a polite close and the chat stops. After the last step, only questions get replies. An unknown GitHub name gets a request to check the spelling. If a candidate sends a second message before the first reply is sent, one reply answers both. If the GitHub token, owner, or repository is empty or wrong, the candidate gets one short holding message ("We will send your invitation soon") and the error shows in the chat details. The bot retries the invitation automatically every 5 minutes once the GitHub settings are complete, then sends the normal invitation message.

Business roles: the bot states only the facts written in `NON_DEV_ROLES` (duties, requirements, engagement, why people join). Pay is stated only as the official rate text, in step 2 and when a candidate asks about it again. A code check rejects any model text that shows another money figure or refuses, promises, or negotiates pay, and regenerates it (or sends a safe fixed reply). Anything beyond the rate (a higher rate, benefits, contract terms) gets a reply that the team will discuss it in a later step. For any other missing detail it also says the team will discuss it later. To make it answer more about a role, add facts there. Financial Analyst and Compliance Officer are complete except that the Compliance Officer interview name is not on file, so step 3 says "an interview" for it.

## Oceanparkasset recruiting context

DeepSeek receives a dedicated Oceanparkasset recruiting brief for every first message and reply. Messages must connect verified candidate experience to relevant platform work, follow ASD STE100, and avoid investment solicitation, profit claims, unsupported company claims, or invented role details.

## Setup

1. Rotate the OpenRouter key that was pasted into chat, then copy `.env.example` to `.env` and provide the replacement key.
2. Set `HIMALAYAS_MCP_URL` to the official Himalayas MCP endpoint and add `HIMALAYAS_MCP_TOKEN` if that server requires authentication. The MCP tool names are configurable because deployments can expose different names.
3. Install the backend as a background service that starts automatically on login, so you never run `uvicorn` by hand. Pick the script for your OS (see [Running on another machine](#running-on-another-machine) below for Windows/macOS):

   ```bash
   ./scripts/install-service.sh   # Linux (systemd)
   ```

   This creates a virtual environment, installs dependencies, and registers a `systemd --user` service (`him-hiring-assistant.service`) that starts on login and restarts itself if it ever crashes. To remove it later, run `./scripts/uninstall-service.sh`. Useful commands:

   ```bash
   systemctl --user status him-hiring-assistant.service   # check it's running
   journalctl --user -u him-hiring-assistant.service -f   # follow logs
   systemctl --user restart him-hiring-assistant.service  # restart after a code or .env change
   ```

   (If you'd rather run it manually for development, `uvicorn app.main:app --reload --port 8765` still works — but avoid `--reload` for anything long-running: an open extension connection can make it hang mid-reload.)

### Start the server from the extension (optional)

Instead of a login service, the extension can start and stop the backend with its **Start server** / **Stop server** button. Chrome extensions cannot launch programs directly, so this uses a small native messaging helper ([native/him_host.py](native/him_host.py)):

```bash
python3 -m venv ~/.venvs/him && ~/.venvs/him/bin/pip install -r requirements.txt   # once, if you skipped the service install
./scripts/install-native-host.sh   # once per machine (Linux/macOS); registers the helper for Chrome, Chromium, Brave and Edge
```

Then reload the extension at `chrome://extensions`. If the systemd service from `install-service.sh` is installed, the button controls it; otherwise the helper starts uvicorn in the background (log: `~/.local/share/him/backend.log`). The extension ID is fixed by the `key` in `extension/manifest.json`, so the helper trusts only this extension. Windows is not covered by the installer.

Before first use, run `supabase_schema.sql` in the Supabase SQL Editor. The send path fails closed until Supabase is reachable and the table exists. Run the file again after an update: it is safe to repeat, and its last part adds the outreach status columns.

   **Ledger statuses.** The shared ledger has one row per member with a `status`: `queued` (first message scheduled), `sent`, or `failed` (with the error, the account, and when). Only `sent` counts as contacted, so a queued or failed row never blocks a member, and nothing ever overwrites a `sent` row. Until the status columns exist, the ledger works as before and stores sent contacts only, and the Database tab says so. The columns are detected again every minute, so no restart is needed after running the SQL.

4. Load `extension/` in Chrome or Chromium at `chrome://extensions` using **Load unpacked** — select the `extension` folder itself, not the project root. As long as the background service is running (step 3), the extension works immediately — no separate server process to start each time.

## Database dashboard and settings

The **Database ↗** button in the extension popup opens a full-size dashboard in its own browser tab (`database.html`). Pressing it again reuses that tab. It shows this Chrome profile's data:

- **Summary cards**: members reached, sent, failed, queued, skipped, and replied.
- **Members table**: every member who was reached, with a status tag from their first message (**sent**, **failed**, **queued**, **skipped**), the suggested role, the chat step, replies, GitHub state, and last activity. A failed reply to a member who was reached shows a second **Reply failed** tag. Failed messages have a **Retry** button. Filter by status, search by name, sort by any column, and export the current list to CSV. The list refreshes every 15 seconds.
- **Member details**: click a row to open a side panel with the member's profile link, suggested role, GitHub invitation state, profile text, and the whole conversation with a status tag on every message.
- **Structure** (left side): the tables (accounts, candidates, messages) with columns, types, keys, and row counts for this profile, the shared Supabase contact ledger, and how the tables link. Login tokens are never shown.
- **Clean up** (left side): delete members and all of their messages from this Chrome profile, either **by date** (from and to, by each member's last activity, or open-ended on one side) or **everything**. Press **Preview** first: it shows exactly how many members and messages will go and how many queued messages will be cancelled. Deleting everything needs the word `DELETE` typed exactly. Before anything is removed, a backup copy of the database is saved next to it as `hiring.db.bak-<date>` (the newest 5 are kept), and the file is compacted afterward.

The popup's Settings tab keeps only what is needed: account name, OpenRouter API key, Supabase URL and key, GitHub token, owner and repository, and the seconds between first messages. Advanced values (AI model, MCP URL, polling intervals, concurrency) keep good defaults and can still be set in `.env`.

What a cleanup keeps and protects:

- Your Himalayas login, your account, and all settings stay.
- Other Chrome profiles' data is never touched.
- The shared Supabase ledger is not changed, so a cleaned member who was already contacted is still skipped and never messaged twice, even if imported again.
- Replies that were already handled are remembered, so an old reply is not answered a second time after a member is re-imported.
- A cleanup is refused while the automatic campaign is running or a message is being sent. Stop the campaign first.

To undo a cleanup, stop the server and copy a `hiring.db.bak-...` file over `hiring.db`.

Endpoints: `GET /api/db/structure`, `GET /api/db/members?status=&q=`, `POST /api/messages/{id}/retry`, `POST /api/db/clean` (`dry_run` previews).

## Several Himalayas accounts (one Chrome profile each)

One backend serves any number of extensions. Load the extension in each Chrome profile and click **Connect Himalayas** in that profile with that profile's Himalayas login.

- Each extension creates an account id in its own profile storage and sends it as `X-Account-Id` on every request. The backend keeps a separate Himalayas login, candidate list, message queue, automation, reply monitor and progress per account. One profile cannot see or send another profile's data.
- Everything shares one database and one Supabase contact ledger. One sequential delivery loop sends for all accounts, so the "already contacted" check and the send never interleave. A member contacted by one account is skipped by every other account.
- Only one server runs at a time (port 8765). Extra **Start server** clicks in other profiles find it already running.
- Name each profile in **Settings > This browser profile** to tell them apart. `GET /api/accounts` lists all accounts with their queue and login state.
- Data from the single-account version is adopted by the first extension that connects. Connect the profile that owns your existing Himalayas login first.
- Settings such as API keys, delays and GitHub are shared by all profiles.

## Running on another machine

Each machine needs its own copy of the backend running as a background service — the extension alone has no server logic. On a new machine:

1. Copy the project folder over (everything except `.venv`, `__pycache__`, and `hiring.db` — leave those out and let the install script and app recreate them).
2. Create `.env` from `.env.example` and fill in the same credentials (OpenRouter key, `HIMALAYAS_MCP_URL`, Supabase URL/key, GitHub token if used). Reuse the **same Supabase project** so the "already contacted" ledger stays shared and accurate across machines.
3. Run the install script for that OS:

   | OS | Script |
   |---|---|
   | Linux | `./scripts/install-service.sh` |
   | macOS | `./scripts/install-service-macos.sh` |
   | Windows | `powershell -ExecutionPolicy Bypass -File .\scripts\install-service-windows.ps1` |

   Each script creates a virtual environment, installs dependencies, and registers the backend to start automatically at login (`systemd --user` on Linux, `launchd` on macOS, Task Scheduler on Windows).
4. Load `extension/` in Chrome on that machine via **Load unpacked**.
5. Open the extension and click **Connect Himalayas** to authorize that machine's backend — the OAuth token is stored locally per machine, so this step is needed again even if you copied `.env` over.

Only run the backend on **one machine at a time** (all your Chrome profiles use that one server). Two live instances polling and sending concurrently can race on the same candidates/messages — this is the exact kind of duplicate-delivery bug already found and fixed earlier in this project (an orphaned duplicate process double-processing the same queue). Stop the service on one machine before starting it on another.

## API shape

- `GET /api/health`
- `GET /api/candidates`
- `POST /api/candidates/sync` imports candidates through MCP
- `POST /api/candidates/sync?page=2` imports a specific Himalayas talent page
- `POST /api/campaigns` creates a paced first-message queue for the selected page or candidates
- `POST /api/automation/start` starts the automatic page-by-page campaign runner
- `POST /api/automation/stop` stops the automatic campaign runner
- `GET /api/messages?status=queued` lists outbound messages
- `POST /api/messages/{id}/approve` approves a queued message
- `POST /api/messages/{id}/send` sends immediately through MCP
- `POST /api/candidates/{id}/replies` stores a reply and queues an AI response

The scheduler processes one due campaign or approved follow-up message at a time. The automatic runner waits for the current page's scheduled/approved messages to finish before moving to the next page. Because candidate profiles and messages are handled through MCP, the extension does not visit profile pages or navigate back through the talent list, so website scroll position is not changed by this workflow. Keep `AUTO_SEND=false` while validating your MCP tool names and message copy.

## ASD STE100

Generated prompts require short, direct, plain-English messages. They avoid idioms, unnecessary adjectives, unexplained abbreviations, and multiple questions in one message. The prompt is a guardrail, not a substitute for reviewing messages before use.

## MCP adapter

The adapter uses the official Himalayas MCP endpoint at `https://mcp.himalayas.app/mcp`. Candidate search uses `search_talent`; first outreach uses `start_conversation`; replies use `send_message`. These calls happen directly through MCP. The extension does not open a candidate profile, click a message button, scroll, or navigate back to a list page.

Himalayas employer messaging requires OAuth 2.1 with PKCE. Configure these tool names in `.env`:

- `MCP_LIST_CANDIDATES_TOOL`
- `MCP_SEND_MESSAGE_TOOL`
- `MCP_GET_PROFILE_TOOL`
- `MCP_LIST_MESSAGES_TOOL`

Performance settings:

- `PROFILE_FETCH_CONCURRENCY` controls parallel profile requests and defaults to `4`.
- `MESSAGE_GENERATION_CONCURRENCY` controls parallel AI drafts and defaults to `3`.
- `REPLY_PROCESSING_CONCURRENCY` controls how many independent candidate replies can be processed at once and defaults to `4`.
- `REPLY_POLL_INTERVAL_SECONDS` controls inbound reply polling and defaults to `30`.
- `DELIVERY_POLL_INTERVAL_SECONDS` controls how often due messages are checked and defaults to `5`.

The payload mapping is isolated in `app/mcp_client.py` so it can be adjusted to the exact official Himalayas MCP schema without changing scheduling or recruiting logic.
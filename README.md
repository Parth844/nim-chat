# Nemotron Chat

A Claude Code–style terminal chat running on NVIDIA NIM models
(Nemotron 3 Super by default). The model can search the frontend/backend code graph, read
source, and check Sentry, Freshdesk and backend logs, all read-only. It can also build HTML
pages with skills.

## Quick start

```bash
cd ~/Documents/nim-chat
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # first time only
.venv/bin/python main.py
```

On first run without a key it asks for one; or run `/key` any time.

### Getting an NVIDIA API key

1. Go to https://build.nvidia.com and sign in (or create a free NVIDIA account).
2. Open any model page, for example search for **Nemotron 3 Super**.
3. Click **Get API Key** (top right of the model page, or your profile menu → **API Keys**), then **Generate Key**.
4. Copy the key. It starts with `nvapi-` and is shown only once, so paste it straight in.
5. In the chat, run `/key` and paste it. The chat checks it with NVIDIA, saves it to `.env`, and starts using it.

The free tier has rate limits and request credits, so heavy use can hit `429` errors (the chat
retries those). Treat the key like a password: don't commit it or paste it into chat messages.
To replace a leaked key, revoke it on the same **API Keys** page and run `/key` with a new one.

## Using it

- Type a question; `/` opens the command menu, Tab completes.
- **Shift+Tab** cycles modes, shown in the status line:

  | Mode | Behaviour |
  |---|---|
  | default | Paid log queries follow `/logs` |
  | ⏵⏵ auto | Paid log queries run without asking (capped per question) |
  | ⏸ plan | Read-only research, no paid logs or pages; ends with a numbered plan |

- Ctrl-C interrupts a reply, Ctrl-D quits. Sessions save after every reply; resume with `/resume`.

## Commands

| Command | What it does |
|---|---|
| `/model`, `/models` | Pick a model / list NIM models (optional filter) |
| `/effort`, `/think` | Thinking effort off · low · medium · high / toggle thinking |
| `/logs` | Backend log queries (paid): ask · auto · off |
| `/tools`, `/graph` | Toggle code-graph tools / show or rebuild (`/graph reload`) the graph |
| `/clear [name]` | New conversation, old one saved (alias `/reset`) |
| `/resume [id\|name]` | Resume a saved session |
| `/rename [name]` | Name the session (auto-titles if empty) |
| `/branch [name]` | Copy the conversation into a new session |
| `/rewind` | Drop the conversation back to an earlier message |
| `/compact [focus]` | Summarize the history to free context |
| `/context` | Context used, by part |
| `/usage` | Tokens used this session (aliases `/cost`, `/stats`) |
| `/status` | Model, effort, mode, session, memory |
| `/btw <question>` | Side question, not added to the conversation |
| `/recap` | One-line session summary |
| `/copy [N]` | Copy the last (or Nth-latest) reply |
| `/export [file]` | Save the conversation as text |
| `/memory` | Edit notes added to every conversation |
| `/output-style` | default · concise · explanatory |
| `/doctor` | Check NVIDIA, Sentry, Freshdesk and the code graph |
| `/key` | Set or change the NVIDIA key (hidden input, verified, saved to `.env`) |
| `/skills`, `/<skill> <request>` | List skills / run one |
| `/artifacts [N]` | List HTML pages / open one |
| `/help`, `/exit` | Help / quit |

## Skills and HTML pages

Skills are `~/.nim-chat/skills/<name>/SKILL.md` files (frontmatter `name`, `description`,
then instructions). The model sees each description and loads a skill when a request matches;
`/<name> <request>` runs one directly. Add a folder to add a skill.

Installed:

- `html-artifact` (built-in): one self-contained HTML page with light and dark mode,
  saved to `~/.nim-chat/artifacts/` and opened in the browser. Pages without dark mode are refused.
- From [taste-skill](https://github.com/leonxlnx/taste-skill) (MIT): `design-taste-frontend`
  (large, ~22K tokens), `minimalist-ui`, `high-end-visual-design`, `industrial-brutalist-ui`,
  `gpt-taste`, `redesign-existing-projects`, `full-output-enforcement`. They run through
  `html-artifact`, so output is always one HTML file.

Try:

```
/design-taste-frontend A one-page report of Sentry errors per day: Sep30 10, Oct1 66, Oct2 39, Oct3 89. Chart + table.
```

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `NVIDIA_API_KEY` | NVIDIA NIM key (required; set with `/key`) |
| `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, `SENTRY_PROJECT` | Sentry access; `SENTRY_PROJECTS` lists projects searched |
| `FRESHDESK_API_KEY` | Freshdesk access (`FRESHDESK_GROUP_IDS` limits which groups are visible) |
| `OPENSEARCH_DASHBOARDS_URL`, `OPENSEARCH_USER`, `OPENSEARCH_PASSWORD` | Backend logs (paid per query) |
| `LOGS_MODE`, `LOGS_MAX_PER_TURN` | Default log mode (ask) and per-question cap (3) |
| `FRONTEND_REPO`, `BACKEND_REPO`, `ADMIN_REPO` | Repo paths (paths to your local repos) |
| `CHAT_SESSIONS_DIR`, `CHAT_SKILLS_DIR`, `CHAT_ARTIFACTS_DIR`, `CHAT_MEMORY_FILE` | Where data lives (default `~/.nim-chat/…`) |
| `CHAT_CONTEXT_TOKENS` | Context size used by `/context` (default 131072) |

`.env` is git-ignored and kept at mode 600. Never print it unmasked.

## Safety

- Freshdesk, Sentry and logs are read-only; the guards live in code (`freshdesk._read_only`,
  the OpenSearch proxy guard), not only in prompts. This chat cannot post Freshdesk notes.
- Ticket text and Sentry events are redacted (emails, phones, tokens, query strings) before the
  model sees them.
- Log queries are shaped in code: one service, short window, few hits, cached, budgeted.
- Verdicts should be confirmed in source code; the graph is for navigation, and an empty
  Sentry/log search is not evidence.

## Files

| File | Role |
|---|---|
| `main.py` | The chat: UI, commands, modes, sessions |
| `skills.py` | Skills, `load_skill` and `create_artifact` tools |
| `models.py`, `picker.py` | Model catalog, effort levels, arrow-key picker |
| `integrations/` | Optional private integrations (code graph, Sentry, Freshdesk, logs). Kept local, not in this repo |

Without the `integrations/` folder (for example in a fresh clone), the chat still runs as a plain
NVIDIA NIM chat with skills and HTML pages; the code graph, Sentry, Freshdesk and logs are off.
To enable them, add an `integrations/` folder with those modules and fill in `.env`.


# Matrix Chat Integration for Agent Zero

Chat with Agent Zero via the Matrix protocol. Supports a persistent chat bridge with restricted and elevated modes.

## Features

- **Chat Bridge**: Persistent Matrix bot that routes messages through Agent Zero's LLM
- **Restricted Mode** (default): Conversational AI only — no tools, no code execution, no file access
- **Elevated Mode** (opt-in): Authenticated users get full Agent Zero agent loop access
- **Rate Limiting**: Per-user rate limiting to prevent abuse
- **Prompt Injection Defense**: Comprehensive sanitization of all external content
- **Room Management**: Designate specific rooms for the bridge via tools or WebUI
- **Auto-Start**: Optionally start the bridge automatically when Agent Zero launches

## Setup

### 1. Create a Matrix Bot Account

1. Register a new account on your Matrix homeserver (e.g., matrix.org)
2. Obtain either:
   - **Access Token** (preferred): From Element → Settings → Help & About → Access Token
   - **Password**: The account password for login-based auth

### 2. Configure the Plugin

Go to Agent Zero → Settings → External → Matrix Chat and configure:

- **Homeserver URL**: e.g., `https://matrix.org`
- **User ID**: e.g., `@mybot:matrix.org`
- **Access Token** or **Password**

### 3. Install Dependencies

Click the "Init" button in the Plugin List, or run:

```bash
python /a0/usr/plugins/matrix_chat/initialize.py
```

This installs `matrix-nio`, `aiohttp`, `pyyaml`, and `aiofiles`.

### 4. Start the Bridge

Use the `matrix_chat` tool:

```json
{"action": "start"}
```

Or enable **Auto-start** in settings to connect automatically.

### 5. Add Bridge Rooms

Invite the bot to a Matrix room, then register it:

```json
{"action": "add_room", "room_id": "!abc123:matrix.org", "label": "My Chat"}
```

## Tools

### matrix_chat
Manage the chat bridge: `start`, `stop`, `restart`, `status`, `add_room`, `remove_room`, `list`.

### matrix_send
Send messages or reactions to Matrix rooms: `send`, `reply`, `react`.

### matrix_read
Read messages from rooms, list joined rooms, or get room info: `messages`, `rooms`, `room_info`.

## Security

### Modes

- **Restricted** (default): Uses `call_utility_model()` — the LLM has NO access to tools, files, or system resources
- **Elevated** (opt-in): Full Agent Zero access. Requires `allow_elevated: true` in config + `!auth <key>` in Matrix

### Chat Commands

| Command | Description |
|---------|-------------|
| `!auth <key>` | Authenticate for elevated mode |
| `!deauth` | End elevated session |
| `!status` | Check current mode and session info |

### Protection Features

- User allowlist (restrict by Matrix user ID)
- Rate limiting (10 messages/60 seconds per user)
- Auth failure lockout (5 attempts/5 minutes)
- Content sanitization (prompt injection defense)
- Session timeout for elevated mode

## Configuration

### Environment Variables

| Variable | Description |
|----------|-------------|
| `MATRIX_HOMESERVER` | Homeserver URL |
| `MATRIX_USER_ID` | Bot user ID |
| `MATRIX_ACCESS_TOKEN` | Access token |
| `MATRIX_PASSWORD` | Password (fallback) |

Environment variables override config file settings.

### Config File

Settings are stored in `/a0/usr/plugins/matrix_chat/config.json` (created automatically).
Edit via the WebUI Settings panel or modify the file directly.

## Architecture

```
matrix_chat/
├── plugin.yaml              # Plugin manifest
├── default_config.yaml      # Default configuration
├── initialize.py            # Dependency installer
├── hooks.py                 # Lifecycle hooks
├── README.md                # This file
├── helpers/
│   ├── sanitize.py          # Input sanitization & security
│   ├── matrix_client.py     # Lightweight Matrix client wrapper
│   ├── matrix_bridge.py     # Persistent chat bridge bot
│   └── message_store.py     # Message persistence
├── tools/
│   ├── matrix_chat.py       # Bridge management tool
│   ├── matrix_send.py       # Message sending tool
│   └── matrix_read.py       # Message reading tool
├── prompts/                 # Tool documentation for LLM
├── extensions/
│   └── python/agent_init/   # Auto-start extension
├── webui/
│   └── config.html          # Settings UI
├── api/
│   ├── matrix_bridge_api.py # Bridge REST API
│   └── matrix_config_api.py # Config REST API
└── data/                    # Runtime data (state, messages)
```

## Dependencies

- `matrix-nio` >= 0.24 — Async Matrix client library
- `aiohttp` >= 3.9 — HTTP client
- `pyyaml` >= 6.0 — YAML parsing
- `aiofiles` >= 23.0 — Async file I/O

## License

Part of Agent Zero. See the main project license.

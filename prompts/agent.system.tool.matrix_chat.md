## matrix_chat
Manage the Matrix chat bridge — a persistent bot that lets users chat with Agent Zero through Matrix.

**Arguments:**
- **action** (string): `start`, `stop`, `restart`, `status`, `add_room`, `remove_room`, or `list`
- **room_id** (string): Room ID (for `add_room`/`remove_room`), e.g. `!abc123:matrix.org`
- **label** (string): Friendly name for the room (for `add_room`)

**start** — Launch the chat bridge bot:
~~~json
{"action": "start"}
~~~

**stop** — Stop the bot:
~~~json
{"action": "stop"}
~~~

**restart** — Restart the bot:
~~~json
{"action": "restart"}
~~~

**status** — Check bot status:
~~~json
{"action": "status"}
~~~

**add_room** — Designate a room for LLM bridging:
~~~json
{"action": "add_room", "room_id": "!abc123:matrix.org", "label": "llm-chat"}
~~~

**remove_room** — Remove a room from the bridge:
~~~json
{"action": "remove_room", "room_id": "!abc123:matrix.org"}
~~~

**list** — List all bridge rooms:
~~~json
{"action": "list"}
~~~

**Notes:**
- Uses matrix-nio sync loop (not webhooks) — no public URL needed
- Default mode is restricted (chat only, no tools)
- Elevated mode requires `!auth <key>` from allowed users
- Enable auto_start in config to launch on agent startup
- Room IDs look like `!opaque_id:server.name`

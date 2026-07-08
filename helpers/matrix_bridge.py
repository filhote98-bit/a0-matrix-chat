"""Persistent Matrix bot for the chat bridge.
Uses matrix-nio's AsyncClient with a sync loop to receive messages
and routes them through Agent Zero's LLM.

SECURITY MODEL:
  - Restricted mode (default): Uses context.communicate() with a restricted system
    prompt. The agent loop runs (making chats visible in the UI) but the system prompt
    guides the agent to be conversational only.
  - Elevated mode (opt-in): Authenticated users get full agent loop access via
    context.communicate(). Requires: allow_elevated=true in config + runtime auth
    via !auth <key> in Matrix. Sessions expire after a configurable timeout.
"""

# Context data keys for Matrix integration
CTX_MX_ROOM = "matrix_room_id"
CTX_MX_SENDER = "matrix_sender"
CTX_MX_DISPLAY_NAME = "matrix_display_name"
CTX_MX_RESTRICTED = "matrix_restricted"

import asyncio
import base64
import collections
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional


logger = logging.getLogger("matrix_chat_bridge")

# Singleton bot instance and its dedicated event loop thread
_bot_instance: Optional["ChatBridgeBot"] = None
_bot_thread: Optional[threading.Thread] = None
_bot_loop: Optional[asyncio.AbstractEventLoop] = None
_auto_start_attempted: bool = False

# Watchdog: stores connection params and monitors bot health
_watchdog_thread: Optional[threading.Thread] = None
_watchdog_stop = threading.Event()
_bridge_params: dict = {}  # homeserver, user_id, access_token, password, device_name

ROOM_STATE_FILE = "chat_bridge_state.json"


def _get_state_path() -> Path:
    candidates = [
        Path(__file__).parent.parent / "data" / ROOM_STATE_FILE,
        Path("/a0/usr/plugins/matrix_chat/data") / ROOM_STATE_FILE,
    ]
    for p in candidates:
        if p.exists():
            return p
    path = candidates[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_room_state() -> dict:
    path = _get_state_path()
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {"rooms": {}, "contexts": {}}


def save_room_state(state: dict):
    from usr.plugins.matrix_chat.helpers.sanitize import secure_write_json
    secure_write_json(_get_state_path(), state)


def add_room(room_id: str, label: str = ""):
    state = load_room_state()
    state.setdefault("rooms", {})[room_id] = {
        "label": label or room_id,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_room_state(state)


def remove_room(room_id: str):
    state = load_room_state()
    state.get("rooms", {}).pop(room_id, None)
    state.get("contexts", {}).pop(room_id, None)
    save_room_state(state)


def get_room_list() -> dict:
    return load_room_state().get("rooms", {})


def get_context_id(room_id: str) -> Optional[str]:
    return load_room_state().get("contexts", {}).get(room_id)


def set_context_id(room_id: str, context_id: str):
    state = load_room_state()
    state.setdefault("contexts", {})[room_id] = context_id
    save_room_state(state)


class ChatBridgeBot:
    """Matrix bot that bridges messages to Agent Zero's LLM.

    SECURITY: By default, uses direct LLM calls (call_utility_model) with NO
    tool access. Authenticated users can optionally elevate to full agent loop.
    """

    MAX_CHAT_MESSAGE_LENGTH = 4096
    MAX_HISTORY_MESSAGES = 20
    RATE_LIMIT_MAX = 10
    RATE_LIMIT_WINDOW = 60  # seconds
    AUTH_MAX_FAILURES = 5
    AUTH_FAILURE_WINDOW = 300  # 5 minute lockout

    CHAT_SYSTEM_PROMPT = (
        "You are a friendly, helpful AI assistant chatting with users on Matrix.\n\n"
        "IMPORTANT CONSTRAINTS:\n"
        "- You are a conversational chat bot ONLY. You have NO access to tools, files, "
        "commands, terminals, or any system resources.\n"
        "- If users ask you to run commands, access files, list directories, execute code, "
        "or perform any system operations, explain that you don't have those capabilities.\n"
        "- NEVER fabricate or make up file listings, directory contents, command outputs, "
        "or system information. You genuinely do not have access to any of these.\n"
        "- Be helpful, friendly, and conversational within these constraints.\n"
        "- You can help with general knowledge, answer questions, have discussions, "
        "write text, brainstorm ideas, and more — just not anything involving system access.\n"
        "- Each message shows the Matrix username prefix. Respond naturally to the "
        "conversation.\n"
    )

    def __init__(self, homeserver: str, user_id: str,
                 access_token: str = "", password: str = "",
                 device_name: str = "AgentZero"):
        if not homeserver or not homeserver.strip():
            raise ValueError("Homeserver URL must be provided to ChatBridgeBot.")
        self.homeserver = homeserver.rstrip("/")
        self.user_id = user_id
        self.access_token = access_token
        self.password = password
        self.device_name = device_name
        self._running = False
        self._client = None
        self._bot_user_id = user_id
        # Per-user rate limiting: user_id -> deque of timestamps
        self._rate_limits: dict[str, collections.deque] = {}
        # Per-room conversation history (in-memory)
        self._conversations: dict[str, list[dict]] = {}
        # Elevated session tracking: "{user_id}:{room_id}" -> {"at": float, "name": str}
        self._elevated_sessions: dict[str, dict] = {}
        # Failed auth attempt tracking: user_id -> deque of timestamps
        self._auth_failures: dict[str, collections.deque] = {}
        self._ready_event: Optional[threading.Event] = None
        # Track the sync token so we only process new messages
        self._since_token: Optional[str] = None
        # Flag to skip initial sync backlog
        self._initial_sync_done = False

    # ------------------------------------------------------------------
    # Config access
    # ------------------------------------------------------------------

    def _get_api_token(self) -> str:
        """Generate the Agent Zero API token matching create_auth_token() logic."""
        try:
            env_path = Path('/a0/usr/.env')
            env_vals = {}
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        k, v = line.split('=', 1)
                        env_vals[k.strip()] = v.strip()
            runtime_id = env_vals.get('A0_PERSISTENT_RUNTIME_ID', '')
            username = env_vals.get('AUTH_LOGIN', '')
            password = env_vals.get('AUTH_PASSWORD', '')
            hash_bytes = hashlib.sha256(f"{runtime_id}:{username}:{password}".encode()).digest()
            b64_token = base64.urlsafe_b64encode(hash_bytes).decode().replace('=', '')
            return b64_token[:16]
        except Exception as e:
            logger.warning("Could not generate API token: %s", e)
            return ''

    def _mxc_to_http(self, mxc_url: str) -> str:
        """Convert mxc://server/media_id to HTTP download URL."""
        if not mxc_url.startswith('mxc://'):
            return mxc_url
        parts = mxc_url[6:].split('/', 1)  # server_name/media_id
        if len(parts) != 2:
            return mxc_url
        server_name, media_id = parts
        return f"{self.homeserver}/_matrix/media/v3/download/{server_name}/{media_id}"

    async def _download_media(self, mxc_url: str, filename: str) -> dict:
        """Download media from Matrix and return as {filename, base64} dict."""
        import aiohttp
        http_url = self._mxc_to_http(mxc_url)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                http_url,
                timeout=aiohttp.ClientTimeout(total=60),
                headers={'Authorization': f'Bearer {self.access_token}'} if self.access_token else {},
            ) as resp:
                if resp.status != 200:
                    logger.warning("Media download failed: %s -> %d", mxc_url, resp.status)
                    return None
                data = await resp.read()
                # Limit file size to 20MB
                if len(data) > 20 * 1024 * 1024:
                    logger.warning("Media too large: %s (%d bytes)", filename, len(data))
                    return None
                import base64 as b64mod
                return {
                    "filename": filename,
                    "base64": b64mod.b64encode(data).decode('ascii'),
                }


    def _get_config(self) -> dict:
        try:
            from usr.plugins.matrix_chat.helpers.matrix_client import get_matrix_config
            return get_matrix_config()
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _session_key(self, user_id: str, room_id: str) -> str:
        return f"{user_id}:{room_id}"

    def _is_elevated(self, user_id: str, room_id: str) -> bool:
        config = self._get_config()
        if not config.get("chat_bridge", {}).get("allow_elevated", False):
            return False
        key = self._session_key(user_id, room_id)
        session = self._elevated_sessions.get(key)
        if not session:
            return False
        timeout = config.get("chat_bridge", {}).get("session_timeout", 300)
        if timeout > 0 and time.monotonic() - session["at"] > timeout:
            del self._elevated_sessions[key]
            return False
        return True

    def _get_auth_key(self, config: dict) -> str:
        bridge_config = config.get("chat_bridge", {})
        auth_key = bridge_config.get("auth_key", "")
        if not auth_key and bridge_config.get("allow_elevated", False):
            from usr.plugins.matrix_chat.helpers.sanitize import generate_auth_key
            auth_key = generate_auth_key()
            bridge_config["auth_key"] = auth_key
            config["chat_bridge"] = bridge_config
            try:
                from usr.plugins.matrix_chat.helpers.sanitize import secure_write_json
                config_path = Path(__file__).parent.parent / "config.json"
                if config_path.exists():
                    existing = json.loads(config_path.read_text())
                    existing.setdefault("chat_bridge", {})["auth_key"] = auth_key
                    secure_write_json(config_path, existing)
                    logger.info("Auto-generated auth key for elevated mode")
            except Exception as e:
                logger.warning("Could not persist auto-generated auth key: %s", type(e).__name__)
        return auth_key

    # ------------------------------------------------------------------
    # Auth command handling
    # ------------------------------------------------------------------

    async def _handle_auth_command(self, room_id: str, sender: str,
                                    display_name: str, text: str) -> Optional[str]:
        """Handle !auth, !deauth, and !status commands.
        Returns response text if command was handled, None otherwise.
        """
        text_stripped = text.strip()

        # --- !deauth ---
        if text_stripped.lower() in ("!deauth", "!dauth", "!unauth", "!logout", "!logoff"):
            key = self._session_key(sender, room_id)
            if key in self._elevated_sessions:
                del self._elevated_sessions[key]
                self._conversations.pop(room_id, None)
                logger.info("Elevated session ended: user=%s room=%s", sender, room_id)
                return "Session ended. Back to restricted mode."
            return "No active elevated session."

        # --- !status ---
        if text_stripped.lower() in ("!bridge-status", "!status"):
            if self._is_elevated(sender, room_id):
                session = self._elevated_sessions[self._session_key(sender, room_id)]
                elapsed = int(time.monotonic() - session["at"])
                config = self._get_config()
                timeout = config.get("chat_bridge", {}).get("session_timeout", 300)
                if timeout > 0:
                    remaining = max(0, timeout - elapsed)
                    expire_info = f"Session expires in {remaining // 60}m {remaining % 60}s"
                else:
                    expire_info = "Session does not expire"
                return f"Mode: **Elevated** (full agent access)\n{expire_info}. Use `!deauth` to end."
            else:
                config = self._get_config()
                elevated_available = config.get("chat_bridge", {}).get("allow_elevated", False)
                if elevated_available:
                    return "Mode: **Restricted** (chat only). Use `!auth <key>` to elevate."
                return "Mode: **Restricted** (chat only). Elevated mode is not enabled."

        # --- !auth <key> ---
        if text_stripped.lower().startswith("!auth"):
            config = self._get_config()
            if not config.get("chat_bridge", {}).get("allow_elevated", False):
                return "Elevated mode is not enabled in the configuration."

            auth_key = self._get_auth_key(config)
            if not auth_key:
                return ("Elevated mode is enabled but no auth key could be generated. "
                        "Check plugin configuration.")

            # Rate limit auth failures
            now = time.monotonic()
            if sender not in self._auth_failures:
                self._auth_failures[sender] = collections.deque()
            failures = self._auth_failures[sender]
            while failures and now - failures[0] > self.AUTH_FAILURE_WINDOW:
                failures.popleft()
            if len(failures) >= self.AUTH_MAX_FAILURES:
                return "Too many failed attempts. Please wait before trying again."

            parts = text_stripped.split(maxsplit=1)
            provided_key = parts[1].strip() if len(parts) > 1 else ""

            if provided_key and hmac.compare_digest(provided_key, auth_key):
                session_key = self._session_key(sender, room_id)
                self._elevated_sessions[session_key] = {
                    "at": now,
                    "name": display_name or sender,
                }
                timeout = config.get("chat_bridge", {}).get("session_timeout", 300)
                if timeout > 0:
                    mins = timeout // 60
                    secs = timeout % 60
                    duration = f"{mins}m" if not secs else f"{mins}m {secs}s"
                    expire_msg = f"Session expires in {duration}."
                else:
                    expire_msg = "Session does not expire."
                logger.info("Elevated session granted: user=%s room=%s", sender, room_id)
                return (f"Elevated session active. {expire_msg} "
                        f"You now have full Agent Zero access in this room. "
                        f"Use `!deauth` to end the session.")
            else:
                failures.append(now)
                remaining = self.AUTH_MAX_FAILURES - len(failures)
                logger.warning("Failed auth attempt: user=%s room=%s", sender, room_id)
                return f"Authentication failed. {remaining} attempt(s) remaining."

        # Unknown ! command
        return "Unknown command. Available: `!auth <key>`, `!deauth`, `!status`"

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _on_room_message(self, room_id: str, event):
        """Handle incoming m.room.message events (text, images, files, audio, video)."""
        from nio import (RoomMessageText, RoomMessageImage, RoomMessageFile,
                         RoomMessageAudio, RoomMessageVideo)

        is_text = isinstance(event, RoomMessageText)
        is_media = isinstance(event, (RoomMessageImage, RoomMessageFile,
                                       RoomMessageAudio, RoomMessageVideo))
        if not is_text and not is_media:
            return

        # Ignore own messages
        if event.sender == self._bot_user_id:
            return

        sender = event.sender
        body = event.body or ""
        
        # For media events, construct a description if body is just a filename
        media_attachments = []  # list of {"filename": str, "base64": str}
        if is_media:
            media_url = getattr(event, 'url', '') or ''  # mxc:// URL
            media_body = getattr(event, 'body', '') or 'file'
            if not body.strip():
                body = f"[Datei gesendet: {media_body}]"
            else:
                body = f"[Datei: {media_body}] {body}"
            # Download media
            if media_url:
                try:
                    dl_result = await self._download_media(media_url, media_body)
                    if dl_result:
                        media_attachments.append(dl_result)
                        logger.info("Downloaded media: %s (%d bytes)", media_body, len(dl_result.get('base64', '')))
                except Exception as e:
                    logger.warning("Failed to download media %s: %s", media_url, e)
        elif not body.strip():
            return

        room_list = get_room_list()
        # Only respond in designated rooms
        if room_list and room_id not in room_list:
            return

        # User allowlist
        config = self._get_config()
        allowed_users = config.get("chat_bridge", {}).get("allowed_users", [])
        if allowed_users and sender not in [str(u) for u in allowed_users]:
            return

        # Store message for matrix_read tool
        if not body.startswith("!"):
            try:
                from usr.plugins.matrix_chat.helpers.message_store import store_message
                store_message(room_id, {
                    "event_id": event.event_id,
                    "sender": sender,
                    "timestamp": event.server_timestamp,
                    "body": body,
                    "type": "text",
                })
            except Exception as e:
                logger.debug("Could not store message: %s", e)

        # Get display name for sender
        display_name = sender
        try:
            if self._client and hasattr(self._client, "rooms") and room_id in self._client.rooms:
                room = self._client.rooms[room_id]
                if hasattr(room, "user_name") and callable(room.user_name):
                    dn = room.user_name(sender)
                    if dn:
                        display_name = dn
                elif hasattr(room, "users") and sender in room.users:
                    user = room.users[sender]
                    if hasattr(user, "display_name") and user.display_name:
                        display_name = user.display_name
        except Exception:
            pass

        # Handle auth commands
        if body.strip().startswith("!"):
            response_text = await self._handle_auth_command(
                room_id, sender, display_name, body
            )
            if response_text:
                await self._send_response(room_id, response_text)
            return

        # Enforce content length limit
        if len(body) > self.MAX_CHAT_MESSAGE_LENGTH:
            await self._send_response(
                room_id,
                f"Message too long ({len(body)} chars). Max: {self.MAX_CHAT_MESSAGE_LENGTH}."
            )
            return

        # Per-user rate limiting
        now = time.monotonic()
        if sender not in self._rate_limits:
            self._rate_limits[sender] = collections.deque()
        timestamps = self._rate_limits[sender]
        while timestamps and now - timestamps[0] > self.RATE_LIMIT_WINDOW:
            timestamps.popleft()
        if len(timestamps) >= self.RATE_LIMIT_MAX:
            await self._send_response(
                room_id,
                f"Rate limit: max {self.RATE_LIMIT_MAX} messages per "
                f"{self.RATE_LIMIT_WINDOW}s. Please wait."
            )
            return
        timestamps.append(now)

        # Route based on elevation status
        is_elevated = self._is_elevated(sender, room_id)

        # Send typing indicator
        try:
            await self._client.room_typing(room_id, typing_state=True, timeout=30000)
        except Exception:
            pass

        try:
            if is_elevated:
                response_text = await self._get_elevated_response(
                    room_id, body, sender, display_name,
                    attachments=media_attachments
                )
            else:
                response_text = await self._get_agent_response(
                    room_id, body, sender, display_name,
                    attachments=media_attachments
                )
        except Exception as e:
            logger.error("Agent error: %s", type(e).__name__, exc_info=True)
            response_text = "An error occurred while processing your message."
        finally:
            try:
                await self._client.room_typing(room_id, typing_state=False)
            except Exception:
                pass

        await self._send_response(room_id, response_text)

    # ------------------------------------------------------------------
    # Restricted mode: direct LLM call, NO tools
    # ------------------------------------------------------------------

    async def _get_agent_response(self, room_id: str, text: str,
                                   sender: str, display_name: str,
                                   attachments: list = None) -> str:
        try:
            from agent import AgentContext, AgentContextType, UserMessage
            from initialize import initialize_agent

            context_id = get_context_id(room_id)
            context = None
            if context_id:
                context = AgentContext.get(context_id)
            if context is None:
                config = initialize_agent()
                context = AgentContext(config=config, type=AgentContextType.USER)
                set_context_id(room_id, context.id)
                logger.info("Created new context %s for room %s", context.id, room_id)

            # Set Matrix context data so extensions can detect this context
            context.data[CTX_MX_ROOM] = room_id
            context.data[CTX_MX_SENDER] = sender
            context.data[CTX_MX_DISPLAY_NAME] = display_name or sender
            context.data[CTX_MX_RESTRICTED] = True

            # Set a friendly context name for the UI
            friendly_name = f"Matrix: {display_name or sender}"
            if not getattr(context, 'name', None):
                context.name = friendly_name

            agent = context.agent0

            from usr.plugins.matrix_chat.helpers.sanitize import sanitize_content, sanitize_username
            author_name = sanitize_username(display_name or sender)
            safe_text = sanitize_content(text)

            # Build user message using the prompt template
            user_msg = agent.read_prompt(
                "fw.matrix.user_message.md",
                sender=author_name,
                body=safe_text,
            )

            # Use context.communicate() to run the full agent loop.
            # This makes the chat visible in the Agent Zero UI.
            # The restricted system prompt is injected via the system_prompt extension.
            user_message = UserMessage(message=user_msg, attachments=[])
            task = context.communicate(user_message)
            result = await task.result()

            return result if isinstance(result, str) else str(result)

        except ImportError:
            return await self._get_agent_response_http(room_id, text, sender, display_name, attachments=attachments)

    # ------------------------------------------------------------------
    # HTTP fallback: route through Agent Zero's HTTP API
    # ------------------------------------------------------------------

    async def _get_agent_response_http(self, room_id: str, text: str,
                                        sender: str, display_name: str,
                                        attachments: list = None) -> str:
        """Fallback: route through Agent Zero's HTTP API when framework imports fail."""
        import aiohttp

        context_id = get_context_id(room_id) or ""

        # Sanitize input
        try:
            from usr.plugins.matrix_chat.helpers.sanitize import sanitize_content, sanitize_username
            author_name = sanitize_username(display_name or sender)
            safe_text = sanitize_content(text)
        except Exception:
            author_name = display_name or sender
            safe_text = text

        prefixed_text = f"[Matrix message from {author_name}]: {safe_text}"

        try:
            async with aiohttp.ClientSession() as session:
                payload = {"message": prefixed_text, "context_id": context_id}
                if attachments:
                    payload["attachments"] = attachments
                headers = {"Content-Type": "application/json", "X-API-KEY": self._get_api_token()}

                async with session.post(
                    "http://localhost:80/api/api_message",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return f"Agent API error ({resp.status}): {body[:200]}"
                    data = await resp.json()

                    if data.get("context_id"):
                        set_context_id(room_id, data["context_id"])

                    return data.get("response", "No response from agent.")
        except Exception as e:
            logger.error("HTTP fallback error: %s", type(e).__name__, exc_info=True)
            return f"Could not reach Agent Zero API: {type(e).__name__}"

    # ------------------------------------------------------------------
    # Elevated mode: full agent loop with tools
    # ------------------------------------------------------------------

    async def _get_elevated_response(self, room_id: str, text: str,
                                      sender: str, display_name: str,
                                      attachments: list = None) -> str:
        try:
            from agent import AgentContext, AgentContextType, UserMessage
            from initialize import initialize_agent

            context_id = get_context_id(room_id)
            context = None
            if context_id:
                context = AgentContext.get(context_id)
            if context is None:
                config = initialize_agent()
                context = AgentContext(config=config, type=AgentContextType.USER)
                set_context_id(room_id, context.id)
                logger.info("Created new elevated context %s for room %s", context.id, room_id)

            # Set Matrix context data so extensions can detect this context
            context.data[CTX_MX_ROOM] = room_id
            context.data[CTX_MX_SENDER] = sender
            context.data[CTX_MX_DISPLAY_NAME] = display_name or sender
            context.data[CTX_MX_RESTRICTED] = False

            # Set a friendly context name for the UI
            friendly_name = f"Matrix: {display_name or sender}"
            if not getattr(context, 'name', None):
                context.name = friendly_name

            from usr.plugins.matrix_chat.helpers.sanitize import sanitize_content
            safe_text = sanitize_content(text)

            user_msg = UserMessage(message=safe_text, attachments=[])
            task = context.communicate(user_msg)
            result = await task.result()

            return result if isinstance(result, str) else str(result)

        except ImportError:
            return await self._get_agent_response_http(room_id, text, sender, display_name, attachments=attachments)
        except Exception as e:
            logger.error("Elevated mode error: %s", type(e).__name__, exc_info=True)
            set_context_id(room_id, "")
            raise

    # ------------------------------------------------------------------
    # Response sending
    # ------------------------------------------------------------------

    @staticmethod
    def _markdown_to_html(md: str) -> str:
        """Convert Markdown to safe HTML for Matrix formatted_body."""
        try:
            import markdown as md_lib
            html = md_lib.markdown(
                md,
                extensions=["nl2br", "fenced_code", "tables"],
                output_format="html",
            )
            # Sanitize: remove script/style/tags Matrix doesn't allow
            import re
            # Strip dangerous tags
            html = re.sub(r'<(script|style|iframe|object|embed)[^>]*>.*?</\1>', '', html, flags=re.DOTALL|re.IGNORECASE)
            return html
        except Exception:
            # Fallback: escape HTML and convert newlines
            import html as html_mod
            return html_mod.escape(md).replace('\n', '<br>')

    async def _send_response(self, room_id: str, text: str):
        if not text:
            text = "(No response)"

        chunks = _split_message(text)
        for chunk in chunks:
            try:
                from nio import RoomSendResponse
                html_body = self._markdown_to_html(chunk)
                resp = await self._client.room_send(
                    room_id=room_id,
                    message_type="m.room.message",
                    content={
                        "msgtype": "m.text",
                        "body": chunk,
                        "format": "org.matrix.custom.html",
                        "formatted_body": html_body,
                    },
                )
                # Store bot response for matrix_read tool
                if isinstance(resp, RoomSendResponse):
                    try:
                        from usr.plugins.matrix_chat.helpers.message_store import store_message
                        store_message(room_id, {
                            "event_id": resp.event_id,
                            "sender": self._bot_user_id,
                            "timestamp": int(time.time() * 1000),
                            "body": chunk,
                            "type": "text",
                        })
                    except Exception:
                        pass
            except Exception as e:
                logger.error("Failed to send response to %s: %s", room_id, e)


def _split_message(content: str, max_length: int = 4096) -> list[str]:
    if len(content) <= max_length:
        return [content]
    chunks = []
    while content:
        if len(content) <= max_length:
            chunks.append(content)
            break
        split_at = content.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = content.rfind(" ", 0, max_length)
        if split_at == -1:
            split_at = max_length
        chunks.append(content[:split_at])
        content = content[split_at:].lstrip("\n")
    return chunks


def _is_bot_alive() -> bool:
    if _bot_instance is None:
        return False
    if not _bot_instance._running:
        return False
    if _bot_thread is None or not _bot_thread.is_alive():
        return False
    return True


def _cleanup_dead_bot():
    global _bot_instance, _bot_thread, _bot_loop
    if not _is_bot_alive():
        _bot_instance = None
        _bot_thread = None
        _bot_loop = None


def _kill_all_bot_threads(timeout: float = 20.0):
    """Find and stop ALL matrix bridge threads by name, even orphaned ones.
    Uses ctypes as nuclear option to interrupt threads blocked in sync()."""
    import threading as _th
    import time as _time
    import ctypes
    import inspect

    def _async_raise(tid, exctype=SystemExit):
        if not inspect.isclass(exctype):
            exctype = type(exctype)
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_long(tid), ctypes.py_object(exctype))
        if res == 0:
            return False
        if res != 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(tid), None)
            return False
        return True

    # Phase 1: graceful shutdown — set _running=False and try to close client
    for t in _th.enumerate():
        if not t.is_alive():
            continue
        if t.name.startswith("matrix-chat-bridge"):
            try:
                args = getattr(t, "_args", ())
                if args and hasattr(args[0], "_running"):
                    bot = args[0]
                    bot._running = False
                    client = getattr(bot, "_client", None)
                    loop = getattr(bot, "_bot_loop", None)
                    if client and loop and loop.is_running():
                        try:
                            import asyncio
                            asyncio.run_coroutine_threadsafe(
                                client.close(), loop
                            ).result(timeout=3)
                        except Exception:
                            pass
            except Exception:
                pass

    # Phase 2: wait briefly for graceful shutdown
    _time.sleep(2)

    # Phase 3: nuclear — inject exception into surviving threads
    for attempt in range(3):
        zombies = [t for t in _th.enumerate()
                   if t.is_alive() and t.name.startswith("matrix-chat-bridge")]
        if not zombies:
            break
        for t in zombies:
            logger.warning("Nuking zombie thread: %s (attempt %d)", t.name, attempt + 1)
            _async_raise(t.ident)
        _time.sleep(1)

    # Phase 4: final wait
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        alive = [t for t in _th.enumerate()
                 if t.is_alive() and (t.name.startswith("matrix-chat-bridge") or t.name == "matrix-watchdog")]
        if not alive:
            break
        _time.sleep(0.5)

    still_alive = [t for t in _th.enumerate()
                   if t.is_alive() and (t.name.startswith("matrix-chat-bridge") or t.name == "matrix-watchdog")]
    if still_alive:
        logger.warning("%d matrix threads still alive after kill_all", len(still_alive))
    else:
        logger.info("All matrix threads killed successfully")

def _watchdog_loop():
    """Background watchdog: restarts the bot if it crashes or dies."""
    global _bot_instance, _bot_thread, _bot_loop, _bridge_params
    logger.info("Matrix bridge watchdog started")
    while not _watchdog_stop.wait(timeout=60):
        if not _bridge_params:
            continue
        if _is_bot_alive():
            continue
        logger.warning("Watchdog: bot is dead, attempting restart...")
        _cleanup_dead_bot()
        try:
            bot = ChatBridgeBot(
                homeserver=_bridge_params["homeserver"],
                user_id=_bridge_params["user_id"],
                access_token=_bridge_params.get("access_token", ""),
                password=_bridge_params.get("password", ""),
                device_name=_bridge_params.get("device_name", "AgentZero"),
            )
            _bot_instance = bot
            ready_event = threading.Event()
            thread = threading.Thread(
                target=_run_bot_in_thread,
                args=(bot, ready_event),
                daemon=True,
                name="matrix-chat-bridge-watchdog",
            )
            _bot_thread = thread
            thread.start()
            ready_event.wait(timeout=35)
            if _is_bot_alive():
                logger.info("Watchdog: bot restarted successfully")
            else:
                logger.error("Watchdog: bot restart failed, will retry in 60s")
        except Exception as e:
            logger.error("Watchdog: restart error: %s", type(e).__name__, exc_info=True)
    logger.info("Matrix bridge watchdog stopped")

def _start_watchdog():
    """Start the watchdog thread if not already running."""
    global _watchdog_thread, _watchdog_stop
    if _watchdog_thread and _watchdog_thread.is_alive():
        return
    _watchdog_stop.clear()
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop, daemon=True, name="matrix-watchdog"
    )
    _watchdog_thread.start()

def _stop_watchdog():
    """Stop the watchdog thread."""
    global _watchdog_thread, _watchdog_stop
    _watchdog_stop.set()
    if _watchdog_thread:
        _watchdog_thread.join(timeout=5)
    _watchdog_thread = None

def _run_bot_in_thread(bot: ChatBridgeBot, ready_event: threading.Event):
    """Run the bot in a dedicated thread with its own event loop."""
    global _bot_instance, _bot_thread, _bot_loop

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _bot_loop = loop
    bot._bot_loop = loop  # Store on instance for _kill_all_bot_threads

    bot._ready_event = ready_event

    try:
        from nio import (AsyncClient, LoginResponse, RoomMessageText, RoomMessageImage,
                         RoomMessageFile, RoomMessageAudio, RoomMessageVideo, SyncResponse)
        _media_types = (RoomMessageText, RoomMessageImage, RoomMessageFile,
                        RoomMessageAudio, RoomMessageVideo)

        async def _start():
            client = AsyncClient(bot.homeserver, bot.user_id)
            bot._client = client

            # Authenticate
            if bot.access_token:
                client.access_token = bot.access_token
                client.user_id = bot.user_id
            else:
                resp = await client.login(
                    password=bot.password,
                    device_name=bot.device_name,
                )
                if not isinstance(resp, LoginResponse):
                    error_msg = getattr(resp, "message", str(resp))
                    raise ValueError(f"Matrix login failed: {error_msg}")
                bot.access_token = resp.access_token
                logger.info("Logged in as %s, device %s", resp.user_id, resp.device_id)

            bot._bot_user_id = bot.user_id
            bot._running = True

            # Do initial sync to get current state (skip old messages)
            logger.info("Performing initial sync...")
            initial_resp = await client.sync(timeout=10000)
            if isinstance(initial_resp, SyncResponse):
                bot._since_token = initial_resp.next_batch
                logger.info("Initial sync complete, token: %s", bot._since_token[:20] if bot._since_token else "None")
            bot._initial_sync_done = True

            logger.info("Chat bridge connected as %s", bot.user_id)
            ready_event.set()

            # Main sync loop - only process NEW messages after initial sync
            while bot._running:
                try:
                    resp = await client.sync(
                        timeout=30000,
                        since=bot._since_token,
                    )
                    if isinstance(resp, SyncResponse):
                        bot._since_token = resp.next_batch
                        # Process room events
                        for room_id, room_info in resp.rooms.join.items():
                            for event in room_info.timeline.events:
                                if isinstance(event, _media_types):
                                    try:
                                        await bot._on_room_message(room_id, event)
                                    except Exception as e:
                                        logger.error("Error handling message in %s: %s",
                                                     room_id, type(e).__name__, exc_info=True)
                except Exception as e:
                    if bot._running:
                        logger.error("Sync error: %s", type(e).__name__, exc_info=True)
                        await asyncio.sleep(5)

            await client.close()

        loop.run_until_complete(_start())
    except Exception as e:
        logger.error("Chat bridge bot exited with error: %s", type(e).__name__, exc_info=True)
    finally:
        logger.info("Chat bridge bot thread ending, cleaning up singleton")
        bot._running = False
        ready_event.set()
        _bot_instance = None
        _bot_thread = None
        _bot_loop = None
        try:
            loop.close()
        except Exception:
            pass


async def start_chat_bridge(homeserver: str, user_id: str,
                             access_token: str = "", password: str = "",
                             device_name: str = "AgentZero") -> ChatBridgeBot:
    """Start the chat bridge bot in a dedicated background thread."""
    global _bot_instance, _bot_thread, _bot_loop

    if not homeserver or not homeserver.strip():
        raise ValueError("Cannot start chat bridge: homeserver URL is empty.")

    _cleanup_dead_bot()
    _kill_all_bot_threads(timeout=20)

    if _bot_instance and _is_bot_alive():
        return _bot_instance

    # Save connection params for watchdog auto-restart
    global _bridge_params
    _bridge_params = {
        "homeserver": homeserver,
        "user_id": user_id,
        "access_token": access_token,
        "password": password,
        "device_name": device_name,
    }

    if _bot_instance:
        _bot_instance._running = False
        _bot_instance = None
        _bot_thread = None
        _bot_loop = None

    bot = ChatBridgeBot(
        homeserver=homeserver,
        user_id=user_id,
        access_token=access_token,
        password=password,
        device_name=device_name,
    )
    _bot_instance = bot

    ready_event = threading.Event()
    thread = threading.Thread(
        target=_run_bot_in_thread,
        args=(bot, ready_event),
        daemon=True,
        name="matrix-chat-bridge",
    )
    _bot_thread = thread
    thread.start()

    ready_event.wait(timeout=35)

    if not bot._running:
        logger.warning("Bot started but may not be fully ready yet")

    _start_watchdog()

    return bot


async def stop_chat_bridge():
    """Stop the chat bridge bot."""
    global _bot_instance, _bot_thread, _bot_loop

    _stop_watchdog()
    _bridge_params.clear()

    if _bot_instance:
        _bot_instance._running = False
        # Force-close the client to interrupt any pending sync() call
        if _bot_instance._client:
            try:
                # Schedule close on the bot's event loop
                if _bot_loop and _bot_loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        _bot_instance._client.close(), _bot_loop
                    ).result(timeout=5)
            except Exception as e:
                logger.warning("Error closing client during stop: %s", e)

    if _bot_thread and _bot_thread.is_alive():
        _bot_thread.join(timeout=15)
        if _bot_thread.is_alive():
            logger.warning("Bot thread did not stop within 15s, it may still be running")

    _bot_instance = None
    _bot_thread = None
    _bot_loop = None


def is_bridge_running() -> bool:
    """Check if the bridge is actively syncing."""
    return _is_bot_alive()


def get_bot_status() -> dict:
    """Get current bot status."""
    _cleanup_dead_bot()

    if _bot_instance is None:
        return {"running": False, "status": "stopped"}
    if not _bot_instance._running:
        return {"running": False, "status": "stopped"}
    if _bot_thread and not _bot_thread.is_alive():
        return {"running": False, "status": "crashed"}
    return {
        "running": True,
        "status": "connected",
        "user": _bot_instance._bot_user_id,
    }

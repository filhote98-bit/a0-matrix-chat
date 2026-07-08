"""Matrix client wrapper using matrix-nio.

Provides a lightweight async client for direct Matrix operations
(tools, API handlers). The chat bridge uses its own long-running
client instance.
"""

import asyncio
import datetime
import logging
import os
from typing import Optional

logger = logging.getLogger("matrix_chat_client")


def get_matrix_config(agent=None) -> dict:
    """Load Matrix config through the plugin framework with env var overrides."""
    try:
        from helpers import plugins
        config = plugins.get_plugin_config("matrix_chat", agent=agent) or {}
    except Exception:
        config = {}

    # Environment variable overrides
    server = config.setdefault("server", {})
    if os.environ.get("MATRIX_HOMESERVER"):
        server["homeserver"] = os.environ["MATRIX_HOMESERVER"]
    if os.environ.get("MATRIX_USER_ID"):
        server["user_id"] = os.environ["MATRIX_USER_ID"]
    if os.environ.get("MATRIX_ACCESS_TOKEN"):
        server["access_token"] = os.environ["MATRIX_ACCESS_TOKEN"]
    if os.environ.get("MATRIX_PASSWORD"):
        server["password"] = os.environ["MATRIX_PASSWORD"]
    return config


class MatrixChatClient:
    """Lightweight Matrix client for one-shot operations."""

    def __init__(self, homeserver: str, user_id: str,
                 access_token: str = "", password: str = "",
                 device_name: str = "AgentZero"):
        self.homeserver = homeserver.rstrip("/")
        self.user_id = user_id
        self.access_token = access_token
        self.password = password
        self.device_name = device_name
        self._client = None

    @classmethod
    def from_config(cls, agent=None) -> "MatrixChatClient":
        config = get_matrix_config(agent)
        server = config.get("server", {})
        homeserver = (server.get("homeserver", "") or "").strip()
        user_id = (server.get("user_id", "") or "").strip()
        access_token = (server.get("access_token", "") or "").strip()
        password = (server.get("password", "") or "").strip()
        device_name = (server.get("device_name", "") or "AgentZero").strip()

        if not homeserver:
            raise ValueError(
                "Homeserver not configured. Set MATRIX_HOMESERVER env var "
                "or configure in Matrix Chat plugin settings."
            )
        if not user_id:
            raise ValueError(
                "User ID not configured. Set MATRIX_USER_ID env var "
                "or configure in Matrix Chat plugin settings."
            )
        if not access_token and not password:
            raise ValueError(
                "No credentials configured. Set MATRIX_ACCESS_TOKEN or "
                "MATRIX_PASSWORD, or configure in plugin settings."
            )
        return cls(
            homeserver=homeserver,
            user_id=user_id,
            access_token=access_token,
            password=password,
            device_name=device_name,
        )

    async def _ensure_client(self):
        """Create and authenticate the nio client if not already done."""
        if self._client is not None:
            return

        from nio import AsyncClient, LoginResponse

        self._client = AsyncClient(self.homeserver, self.user_id)

        if self.access_token:
            self._client.access_token = self.access_token
            self._client.user_id = self.user_id
            # device_id is optional when using access_token directly
        else:
            resp = await self._client.login(
                password=self.password,
                device_name=self.device_name,
            )
            if not isinstance(resp, LoginResponse):
                error_msg = getattr(resp, "message", str(resp))
                raise MatrixAPIError(0, f"Login failed: {error_msg}", "login")
            self.access_token = resp.access_token
            logger.info("Logged in as %s, device %s", resp.user_id, resp.device_id)

    async def close(self):
        """Close the client session."""
        if self._client:
            await self._client.close()
            self._client = None

    # --- Bot info ---

    async def whoami(self) -> dict:
        """Get authenticated user info."""
        await self._ensure_client()
        from nio import WhoamiResponse
        resp = await self._client.whoami()
        if isinstance(resp, WhoamiResponse):
            return {
                "user_id": resp.user_id,
                "device_id": resp.device_id if hasattr(resp, "device_id") else None,
            }
        error_msg = getattr(resp, "message", str(resp))
        raise MatrixAPIError(0, f"Whoami failed: {error_msg}", "whoami")

    # --- Messages ---

    async def send_message(
        self, room_id: str, text: str,
        msgtype: str = "m.text",
        reply_to_event_id: Optional[str] = None,
    ) -> dict:
        """Send a text message to a room."""
        await self._ensure_client()
        from nio import RoomSendResponse

        # Convert Markdown to HTML for rich rendering in Matrix clients
        try:
            import markdown as md_lib
            import re
            html_body = md_lib.markdown(
                text,
                extensions=["nl2br", "fenced_code", "tables"],
                output_format="html",
            )
            # Strip dangerous tags Matrix doesn't allow
            html_body = re.sub(r'<(script|style|iframe|object|embed)[^>]*>.*?</\1>', '', html_body, flags=re.DOTALL|re.IGNORECASE)
        except Exception:
            import html as html_mod
            html_body = html_mod.escape(text).replace('\n', '<br>')

        content = {
            "msgtype": msgtype,
            "body": text,
            "format": "org.matrix.custom.html",
            "formatted_body": html_body,
        }

        if reply_to_event_id:
            content["m.relates_to"] = {
                "m.in_reply_to": {
                    "event_id": reply_to_event_id,
                }
            }

        resp = await self._client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )
        if isinstance(resp, RoomSendResponse):
            return {"event_id": resp.event_id}
        error_msg = getattr(resp, "message", str(resp))
        raise MatrixAPIError(0, f"Send failed: {error_msg}", "room_send")

    async def send_reaction(
        self, room_id: str, event_id: str, emoji: str,
    ) -> dict:
        """Send an emoji reaction to an event."""
        await self._ensure_client()
        from nio import RoomSendResponse

        content = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event_id,
                "key": emoji,
            }
        }
        resp = await self._client.room_send(
            room_id=room_id,
            message_type="m.reaction",
            content=content,
        )
        if isinstance(resp, RoomSendResponse):
            return {"event_id": resp.event_id}
        error_msg = getattr(resp, "message", str(resp))
        raise MatrixAPIError(0, f"Reaction failed: {error_msg}", "room_send")

    # --- Room info ---

    async def get_joined_rooms(self) -> list:
        """Get list of joined room IDs."""
        await self._ensure_client()
        from nio import JoinedRoomsResponse
        resp = await self._client.joined_rooms()
        if isinstance(resp, JoinedRoomsResponse):
            return resp.rooms
        return []

    async def get_room_messages(
        self, room_id: str, limit: int = 50,
    ) -> list:
        """Fetch recent messages from a room."""
        await self._ensure_client()
        from nio import RoomMessagesResponse

        resp = await self._client.room_messages(
            room_id=room_id,
            start="",
            limit=limit,
        )
        if isinstance(resp, RoomMessagesResponse):
            messages = []
            for event in reversed(resp.chunk):
                msg = _event_to_message(event)
                if msg:
                    messages.append(msg)
            return messages
        return []

    async def get_room_info(self, room_id: str) -> dict:
        """Get room display name and topic via state events."""
        await self._ensure_client()
        info = {"room_id": room_id, "name": "", "topic": "", "members": 0}

        try:
            # Try to get room name
            from nio import RoomGetStateEventResponse
            resp = await self._client.room_get_state_event(
                room_id, "m.room.name", ""
            )
            if isinstance(resp, RoomGetStateEventResponse):
                info["name"] = resp.content.get("name", "")
        except Exception:
            pass

        try:
            resp = await self._client.room_get_state_event(
                room_id, "m.room.topic", ""
            )
            if isinstance(resp, RoomGetStateEventResponse):
                info["topic"] = resp.content.get("topic", "")
        except Exception:
            pass

        try:
            from nio import JoinedMembersResponse
            resp = await self._client.joined_members(room_id)
            if isinstance(resp, JoinedMembersResponse):
                info["members"] = len(resp.members)
        except Exception:
            pass

        return info

    async def sync_once(self, timeout: int = 0) -> dict:
        """Perform a single sync to get latest state."""
        await self._ensure_client()
        resp = await self._client.sync(timeout=timeout)
        return resp


class MatrixAPIError(Exception):
    def __init__(self, error_code: int, description: str, method: str):
        self.error_code = error_code
        self.description = description
        self.method = method
        super().__init__(f"Matrix API error on {method}: {description}")


def _event_to_message(event) -> Optional[dict]:
    """Convert a nio event to a simple message dict."""
    from nio import RoomMessageText, RoomMessageImage, RoomMessageFile

    if isinstance(event, RoomMessageText):
        return {
            "event_id": event.event_id,
            "sender": event.sender,
            "timestamp": event.server_timestamp,
            "body": event.body,
            "type": "text",
        }
    elif isinstance(event, RoomMessageImage):
        return {
            "event_id": event.event_id,
            "sender": event.sender,
            "timestamp": event.server_timestamp,
            "body": event.body or "[Image]",
            "type": "image",
            "url": getattr(event, "url", ""),
        }
    elif isinstance(event, RoomMessageFile):
        return {
            "event_id": event.event_id,
            "sender": event.sender,
            "timestamp": event.server_timestamp,
            "body": event.body or "[File]",
            "type": "file",
            "url": getattr(event, "url", ""),
        }
    return None


def format_messages(messages: list, include_ids: bool = False) -> str:
    """Format Matrix messages into readable text for LLM consumption.

    All external content is sanitized to neutralise prompt injection.
    """
    from usr.plugins.matrix_chat.helpers.sanitize import (
        sanitize_content, sanitize_username,
    )

    lines = []
    for msg in messages:
        sender = sanitize_username(msg.get("sender", "Unknown"))
        timestamp = ""
        ts = msg.get("timestamp", 0)
        if ts:
            # Matrix timestamps are in milliseconds
            dt = datetime.datetime.fromtimestamp(
                ts / 1000 if ts > 1e12 else ts,
                tz=datetime.timezone.utc,
            )
            timestamp = dt.strftime("%Y-%m-%d %H:%M")
        content = sanitize_content(msg.get("body", ""))
        msg_type = msg.get("type", "text")

        media_text = ""
        if msg_type == "image":
            media_text = " [Image]"
        elif msg_type == "file":
            media_text = " [File]"

        prefix = f"[{msg.get('event_id', '?')}] " if include_ids else ""
        lines.append(f"{prefix}[{timestamp}] {sender}: {content}{media_text}")

    return "\n".join(lines)

from helpers.tool import Tool, Response
from usr.plugins.matrix_chat.helpers.matrix_client import get_matrix_config
from usr.plugins.matrix_chat.helpers.matrix_bridge import (
    start_chat_bridge,
    stop_chat_bridge,
    get_bot_status,
    add_room,
    remove_room,
    get_room_list,
)
from usr.plugins.matrix_chat.helpers.sanitize import require_auth, validate_room_id


class MatrixChat(Tool):
    """Manage the Matrix chat bridge — a persistent bot that lets users
    chat with Agent Zero through Matrix."""

    async def execute(self, **kwargs) -> Response:
        config = get_matrix_config(self.agent)
        try:
            require_auth(config)
        except ValueError as e:
            return Response(message=f"Auth error: {e}", break_loop=False)

        action = self.args.get("action", "status")

        if action == "start":
            return await self._start()
        elif action == "stop":
            return await self._stop()
        elif action == "restart":
            return await self._restart()
        elif action == "add_room":
            return self._add_room()
        elif action == "remove_room":
            return self._remove_room()
        elif action == "list":
            return self._list_rooms()
        elif action == "status":
            return self._status()
        else:
            return Response(
                message=f"Unknown action '{action}'. Use: start, stop, restart, "
                        f"add_room, remove_room, list, status.",
                break_loop=False,
            )

    async def _start(self) -> Response:
        config = get_matrix_config(self.agent)
        server = config.get("server", {})
        homeserver = (server.get("homeserver", "") or "").strip()
        user_id = (server.get("user_id", "") or "").strip()
        access_token = (server.get("access_token", "") or "").strip()
        password = (server.get("password", "") or "").strip()
        device_name = (server.get("device_name", "") or "AgentZero").strip()

        if not homeserver:
            return Response(
                message="Error: Homeserver not configured. Set MATRIX_HOMESERVER "
                        "or configure in plugin settings.",
                break_loop=False,
            )

        status = get_bot_status()
        if status.get("running") and status.get("status") == "connected":
            return Response(
                message=f"Chat bridge is already running as {status.get('user', 'unknown')}.",
                break_loop=False,
            )

        self.set_progress("Starting Matrix chat bridge...")
        try:
            bot = await start_chat_bridge(
                homeserver=homeserver,
                user_id=user_id,
                access_token=access_token,
                password=password,
                device_name=device_name,
            )
            status = get_bot_status()
            rooms = get_room_list()
            msg = f"Chat bridge started as **{status.get('user', 'unknown')}**."
            if rooms:
                msg += f"\nListening in {len(rooms)} room(s)."
            else:
                msg += "\nNo bridge rooms configured yet. Use action 'add_room' to designate a room."
            return Response(message=msg, break_loop=False)
        except TimeoutError:
            return Response(
                message="Error: Bot failed to connect within 30 seconds. Check credentials.",
                break_loop=False,
            )
        except Exception as e:
            return Response(
                message=f"Error starting chat bridge: {type(e).__name__}: {e}",
                break_loop=False,
            )

    async def _stop(self) -> Response:
        status = get_bot_status()
        if not status.get("running"):
            return Response(message="Chat bridge is not running.", break_loop=False)

        self.set_progress("Stopping Matrix chat bridge...")
        try:
            await stop_chat_bridge()
            return Response(message="Chat bridge stopped.", break_loop=False)
        except Exception as e:
            return Response(
                message=f"Error stopping chat bridge: {type(e).__name__}",
                break_loop=False,
            )

    async def _restart(self) -> Response:
        self.set_progress("Restarting Matrix chat bridge...")
        await stop_chat_bridge()

        config = get_matrix_config(self.agent)
        server = config.get("server", {})
        homeserver = (server.get("homeserver", "") or "").strip()
        user_id = (server.get("user_id", "") or "").strip()
        access_token = (server.get("access_token", "") or "").strip()
        password = (server.get("password", "") or "").strip()
        device_name = (server.get("device_name", "") or "AgentZero").strip()

        if not homeserver:
            return Response(
                message="Error: Homeserver not configured.",
                break_loop=False,
            )

        try:
            await start_chat_bridge(
                homeserver=homeserver,
                user_id=user_id,
                access_token=access_token,
                password=password,
                device_name=device_name,
            )
            status = get_bot_status()
            return Response(
                message=f"Chat bridge restarted as **{status.get('user', 'unknown')}**.",
                break_loop=False,
            )
        except Exception as e:
            return Response(
                message=f"Error restarting: {type(e).__name__}",
                break_loop=False,
            )

    def _add_room(self) -> Response:
        room_id = self.args.get("room_id", "")
        label = self.args.get("label", "")

        try:
            room_id = validate_room_id(room_id, "room_id")
        except ValueError as e:
            return Response(message=f"Error: {e}", break_loop=False)

        add_room(room_id, label)
        msg = f"Room {room_id} added to bridge"
        if label:
            msg += f" ({label})"
        msg += ". Messages in this room will be routed to Agent Zero's LLM."
        return Response(message=msg, break_loop=False)

    def _remove_room(self) -> Response:
        room_id = self.args.get("room_id", "")
        try:
            room_id = validate_room_id(room_id, "room_id")
        except ValueError as e:
            return Response(message=f"Error: {e}", break_loop=False)

        remove_room(room_id)
        return Response(
            message=f"Room {room_id} removed from bridge.",
            break_loop=False,
        )

    def _list_rooms(self) -> Response:
        rooms = get_room_list()
        if not rooms:
            return Response(
                message="No bridge rooms configured. Use action 'add_room' to add one.",
                break_loop=False,
            )

        lines = [f"Bridge rooms ({len(rooms)}):"]
        for rid, info in rooms.items():
            label = info.get("label", rid)
            added = info.get("added_at", "unknown")
            lines.append(f"  - {label} (ID: {rid}, added: {added})")

        status = get_bot_status()
        if status.get("running"):
            lines.append(f"\nBot status: {status.get('status')} as {status.get('user', '?')}")
        else:
            lines.append("\nBot status: not running")

        return Response(message="\n".join(lines), break_loop=False)

    def _status(self) -> Response:
        status = get_bot_status()
        rooms = get_room_list()

        if not status.get("running"):
            msg = f"Chat bridge is **not running** (status: {status.get('status', 'stopped')})."
            if rooms:
                msg += f"\n{len(rooms)} room(s) configured but bot is offline."
            return Response(message=msg, break_loop=False)

        lines = [
            f"Chat bridge is **{status.get('status')}** as **{status.get('user', '?')}**",
            f"  Bridge rooms: {len(rooms)}",
        ]

        for rid, info in rooms.items():
            label = info.get("label", rid)
            lines.append(f"    - {label} (ID: {rid})")

        return Response(message="\n".join(lines), break_loop=False)

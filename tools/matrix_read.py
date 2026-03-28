from helpers.tool import Tool, Response
from usr.plugins.matrix_chat.helpers.matrix_client import (
    MatrixChatClient, MatrixAPIError, format_messages, get_matrix_config,
)
from usr.plugins.matrix_chat.helpers.sanitize import require_auth, sanitize_room_name


class MatrixRead(Tool):
    """Read messages from Matrix rooms, list joined rooms, or get room info."""

    async def execute(self, **kwargs) -> Response:
        room_id = self.args.get("room_id", "")
        limit = int(self.args.get("limit", "50"))
        action = self.args.get("action", "messages")

        config = get_matrix_config(self.agent)
        try:
            require_auth(config)
        except ValueError as e:
            return Response(message=f"Auth error: {e}", break_loop=False)

        try:
            client = MatrixChatClient.from_config(agent=self.agent)

            if action == "room_info":
                if not room_id:
                    return Response(
                        message="Error: room_id is required for room_info.",
                        break_loop=False,
                    )
                info = await client.get_room_info(room_id)
                await client.close()
                return Response(
                    message=_format_room_info(info),
                    break_loop=False,
                )

            elif action == "rooms":
                # Get joined rooms
                joined = await client.get_joined_rooms()
                await client.close()

                # Also check message store for enriched info
                from usr.plugins.matrix_chat.helpers.message_store import get_all_rooms
                store_rooms = get_all_rooms()

                if not joined and not store_rooms:
                    return Response(
                        message="No rooms found. The bot may not have joined any rooms yet.",
                        break_loop=False,
                    )

                lines = []
                all_room_ids = set(joined) | set(store_rooms.keys())
                lines.append(f"Known rooms ({len(all_room_ids)}):")

                for rid in sorted(all_room_ids):
                    store_info = store_rooms.get(rid, {})
                    msg_count = store_info.get("message_count", "")
                    count_str = f", {msg_count} messages" if msg_count else ""
                    joined_str = " [joined]" if rid in joined else ""
                    lines.append(f"  - {rid}{joined_str}{count_str}")

                return Response(message="\n".join(lines), break_loop=False)

            elif action == "messages":
                if not room_id:
                    return Response(
                        message="Error: room_id is required for reading messages.",
                        break_loop=False,
                    )

                # Try message store first (populated by bridge)
                from usr.plugins.matrix_chat.helpers.message_store import get_messages
                messages = get_messages(str(room_id), limit=limit)

                # Fall back to room_messages API if store is empty
                if not messages:
                    try:
                        messages = await client.get_room_messages(room_id, limit=limit)
                    except Exception:
                        messages = []

                await client.close()

                if not messages:
                    return Response(
                        message="No recent messages found for this room. "
                                "If the chat bridge is running, new messages "
                                "will be stored automatically.",
                        break_loop=False,
                    )

                result = format_messages(messages, include_ids=True)
                return Response(
                    message=f"Retrieved {len(messages)} messages from "
                            f"room {room_id}:\n\n{result}",
                    break_loop=False,
                )

            else:
                return Response(
                    message=f"Unknown action '{action}'. "
                            f"Use 'messages', 'rooms', or 'room_info'.",
                    break_loop=False,
                )

        except MatrixAPIError as e:
            return Response(message=f"Matrix API error: {e}", break_loop=False)
        except Exception as e:
            return Response(
                message=f"Error reading Matrix: {type(e).__name__}: {e}",
                break_loop=False,
            )


def _format_room_info(info: dict) -> str:
    name = sanitize_room_name(info.get("name", "")) or info.get("room_id", "unknown")
    lines = [
        f"Room: {name}",
        f"  ID: {info.get('room_id', '?')}",
    ]
    if info.get("topic"):
        lines.append(f"  Topic: {sanitize_room_name(info['topic'], max_length=200)}")
    if info.get("members"):
        lines.append(f"  Members: {info['members']}")
    return "\n".join(lines)

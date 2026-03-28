from helpers.tool import Tool, Response
from usr.plugins.matrix_chat.helpers.matrix_client import (
    MatrixChatClient, MatrixAPIError, get_matrix_config,
)
from usr.plugins.matrix_chat.helpers.sanitize import require_auth, validate_room_id


class MatrixSend(Tool):
    """Send messages or reactions to Matrix rooms."""

    async def execute(self, **kwargs) -> Response:
        room_id = self.args.get("room_id", "")
        content = self.args.get("content", "")
        reply_to = self.args.get("reply_to", "")
        action = self.args.get("action", "send")

        try:
            room_id = validate_room_id(room_id)
        except ValueError as e:
            return Response(message=f"Error: {e}", break_loop=False)

        config = get_matrix_config(self.agent)
        try:
            require_auth(config)
        except ValueError as e:
            return Response(message=f"Auth error: {e}", break_loop=False)

        try:
            client = MatrixChatClient.from_config(agent=self.agent)

            if action == "send":
                if not content:
                    return Response(
                        message="Error: content is required for sending.",
                        break_loop=False,
                    )

                chunks = _split_message(content)
                sent_ids = []
                for i, chunk in enumerate(chunks):
                    ref = reply_to if i == 0 and reply_to else None
                    result = await client.send_message(
                        room_id=room_id,
                        text=chunk,
                        reply_to_event_id=ref,
                    )
                    sent_ids.append(result.get("event_id", "?"))

                await client.close()
                if len(sent_ids) == 1:
                    return Response(
                        message=f"Message sent (event: {sent_ids[0]}).",
                        break_loop=False,
                    )
                return Response(
                    message=f"Message sent in {len(sent_ids)} parts "
                            f"(events: {', '.join(sent_ids)}).",
                    break_loop=False,
                )

            elif action == "reply":
                if not content or not reply_to:
                    return Response(
                        message="Error: content and reply_to are required for replying.",
                        break_loop=False,
                    )
                result = await client.send_message(
                    room_id=room_id,
                    text=content,
                    reply_to_event_id=reply_to,
                )
                await client.close()
                return Response(
                    message=f"Reply sent (event: {result.get('event_id', '?')}).",
                    break_loop=False,
                )

            elif action == "react":
                emoji = self.args.get("emoji", "")
                event_id = self.args.get("event_id", "")
                if not emoji or not event_id:
                    return Response(
                        message="Error: emoji and event_id required for reactions.",
                        break_loop=False,
                    )
                result = await client.send_reaction(room_id, event_id, emoji)
                await client.close()
                return Response(
                    message=f"Reaction {emoji} added to event {event_id}.",
                    break_loop=False,
                )

            else:
                return Response(
                    message=f"Unknown action '{action}'. Use 'send', 'reply', or 'react'.",
                    break_loop=False,
                )

        except MatrixAPIError as e:
            return Response(message=f"Matrix API error: {e}", break_loop=False)
        except Exception as e:
            return Response(
                message=f"Error sending to Matrix: {type(e).__name__}: {e}",
                break_loop=False,
            )


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

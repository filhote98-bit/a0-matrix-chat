"""API endpoint: Matrix Chat plugin custom actions.
URL: POST /api/plugins/matrix_chat/matrix_config_api

Config load/save is handled by A0's built-in plugin settings framework.
This endpoint only handles actions that need server-side logic (key generation, connection test).
"""
from helpers.api import ApiHandler, Request, Response


class MatrixConfigApi(ApiHandler):

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["POST"]

    @classmethod
    def requires_csrf(cls) -> bool:
        return True

    async def process(self, input: dict, request: Request) -> dict | Response:
        action = input.get("action", "")
        if action == "generate_auth_key":
            return self._generate_auth_key()
        elif action == "test_connection":
            return await self._test_connection()
        return {"error": "Unknown action"}

    def _generate_auth_key(self) -> dict:
        """Generate a new random auth key and persist it to config.json."""
        try:
            from pathlib import Path
            import json
            from usr.plugins.matrix_chat.helpers.sanitize import generate_auth_key, secure_write_json

            key = generate_auth_key()

            config_candidates = [
                Path("/a0/usr/plugins/matrix_chat/config.json"),
                Path(__file__).parent.parent / "config.json",
            ]
            for cp in config_candidates:
                if cp.exists():
                    existing = json.loads(cp.read_text())
                    existing.setdefault("chat_bridge", {})["auth_key"] = key
                    secure_write_json(cp, existing)
                    break

            return {"auth_key": key}
        except Exception:
            return {"error": "Failed to generate auth key."}

    async def _test_connection(self) -> dict:
        """Test Matrix connection with current config."""
        try:
            from usr.plugins.matrix_chat.helpers.matrix_client import MatrixChatClient, get_matrix_config

            config = get_matrix_config()
            server = config.get("server", {})
            homeserver = (server.get("homeserver", "") or "").strip()
            if not homeserver:
                return {"ok": False, "error": "No homeserver configured"}

            client = MatrixChatClient.from_config()
            info = await client.whoami()
            await client.close()

            return {
                "ok": True,
                "user_id": info.get("user_id"),
                "device_id": info.get("device_id"),
            }
        except Exception as e:
            return {"ok": False, "error": f"Connection failed: {type(e).__name__}: {e}"}

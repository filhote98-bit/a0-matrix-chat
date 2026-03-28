"""API endpoint: Chat bridge start/stop/status.
URL: POST /api/plugins/matrix_chat/matrix_bridge_api
"""
import logging
from helpers.api import ApiHandler, Request, Response

logger = logging.getLogger("matrix_bridge_api")


class MatrixBridgeApi(ApiHandler):

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["POST"]

    @classmethod
    def requires_csrf(cls) -> bool:
        return True

    async def process(self, input: dict, request: Request) -> dict | Response:
        action = input.get("action", "status")

        try:
            if action == "status":
                return self._status()
            elif action == "start":
                return await self._start()
            elif action == "stop":
                return await self._stop()
            elif action == "restart":
                return await self._restart()
            else:
                return {"ok": False, "error": f"Unknown action: {action}"}
        except Exception as e:
            logger.error("Bridge API error on '%s': %s", action, type(e).__name__, exc_info=True)
            return {"ok": False, "error": f"Bridge error: {type(e).__name__}"}

    def _status(self) -> dict:
        from usr.plugins.matrix_chat.helpers.matrix_bridge import get_bot_status, get_room_list
        status = get_bot_status()
        status["room_count"] = len(get_room_list())
        return {"ok": True, **status}

    async def _start(self) -> dict:
        from usr.plugins.matrix_chat.helpers.matrix_bridge import get_bot_status, start_chat_bridge
        from usr.plugins.matrix_chat.helpers.matrix_client import get_matrix_config

        status = get_bot_status()
        if status.get("running"):
            return {"ok": True, "message": "Bridge is already running", **status}

        config = get_matrix_config()
        server = config.get("server", {})
        homeserver = (server.get("homeserver", "") or "").strip()
        user_id = (server.get("user_id", "") or "").strip()
        access_token = (server.get("access_token", "") or "").strip()
        password = (server.get("password", "") or "").strip()
        device_name = (server.get("device_name", "") or "AgentZero").strip()

        if not homeserver:
            return {"ok": False, "error": "No homeserver configured"}

        await start_chat_bridge(
            homeserver=homeserver,
            user_id=user_id,
            access_token=access_token,
            password=password,
            device_name=device_name,
        )
        final_status = get_bot_status()
        return {"ok": True, "message": "Bridge started", **final_status}

    async def _stop(self) -> dict:
        from usr.plugins.matrix_chat.helpers.matrix_bridge import get_bot_status, stop_chat_bridge
        await stop_chat_bridge()
        return {"ok": True, "message": "Bridge stopped", **get_bot_status()}

    async def _restart(self) -> dict:
        from usr.plugins.matrix_chat.helpers.matrix_bridge import get_bot_status, start_chat_bridge, stop_chat_bridge
        from usr.plugins.matrix_chat.helpers.matrix_client import get_matrix_config

        await stop_chat_bridge()

        config = get_matrix_config()
        server = config.get("server", {})
        homeserver = (server.get("homeserver", "") or "").strip()
        user_id = (server.get("user_id", "") or "").strip()
        access_token = (server.get("access_token", "") or "").strip()
        password = (server.get("password", "") or "").strip()
        device_name = (server.get("device_name", "") or "AgentZero").strip()

        if not homeserver:
            return {"ok": False, "error": "No homeserver configured"}

        await start_chat_bridge(
            homeserver=homeserver,
            user_id=user_id,
            access_token=access_token,
            password=password,
            device_name=device_name,
        )
        return {"ok": True, "message": "Bridge restarted", **get_bot_status()}

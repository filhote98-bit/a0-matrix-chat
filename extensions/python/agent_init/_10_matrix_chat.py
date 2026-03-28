"""Auto-start the Matrix chat bridge on agent initialization.

Only starts if:
  - Matrix credentials are configured
  - chat_bridge.auto_start is true in config
  - At least one bridge room is registered

NOTE: agent_init is dispatched via call_extensions_sync(), so execute()
must be synchronous. start_chat_bridge() is async, so we schedule it on
the running event loop with create_task().

The dedup flag lives on the bridge module (a true singleton) rather than
on this extension module, which A0 may reimport from multiple search paths.
"""

import asyncio
import logging

from helpers.extension import Extension

logger = logging.getLogger("matrix_chat_bridge")


class MatrixChatBridgeInit(Extension):

    def execute(self, **kwargs):
        if not self.agent:
            return

        # Only run for the main agent, not subordinates
        if self.agent.number != 0:
            return

        try:
            import usr.plugins.matrix_chat.helpers.matrix_bridge as bridge

            # Only attempt once per process lifetime
            if bridge._auto_start_attempted or bridge.is_bridge_running():
                return

            bridge._auto_start_attempted = True

            from helpers import plugins

            config = plugins.get_plugin_config("matrix_chat", agent=self.agent)
            server = config.get("server", {})
            homeserver = (server.get("homeserver", "") or "").strip()
            user_id = (server.get("user_id", "") or "").strip()
            access_token = (server.get("access_token", "") or "").strip()
            password = (server.get("password", "") or "").strip()
            device_name = (server.get("device_name", "") or "AgentZero").strip()

            if not homeserver:
                return  # No homeserver, skip
            if not access_token and not password:
                return  # No credentials, skip

            bridge_config = config.get("chat_bridge", {})
            if not bridge_config.get("auto_start", False):
                return  # Auto-start disabled

            rooms = bridge.get_room_list()
            if not rooms:
                return  # No rooms configured

            logger.warning(
                "Auto-starting Matrix chat bridge (%d room(s))...", len(rooms)
            )

            # start_chat_bridge is async — schedule it on the running loop
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(bridge.start_chat_bridge(
                    homeserver=homeserver,
                    user_id=user_id,
                    access_token=access_token,
                    password=password,
                    device_name=device_name,
                ))
            except RuntimeError:
                asyncio.run(bridge.start_chat_bridge(
                    homeserver=homeserver,
                    user_id=user_id,
                    access_token=access_token,
                    password=password,
                    device_name=device_name,
                ))

            logger.warning("Matrix chat bridge auto-start scheduled.")

        except Exception as e:
            logger.warning(
                "Matrix chat bridge auto-start failed: %s",
                type(e).__name__, exc_info=True,
            )

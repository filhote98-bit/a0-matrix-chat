from helpers.extension import Extension
from agent import LoopData


class MatrixContextPrompt(Extension):

    async def execute(
        self,
        system_prompt: list[str] = [],
        loop_data: LoopData = LoopData(),
        **kwargs,
    ):
        if not self.agent:
            return

        context = self.agent.context
        if not context or not context.data:
            return

        # Check if this is a Matrix context
        from usr.plugins.matrix_chat.helpers.matrix_bridge import CTX_MX_ROOM, CTX_MX_RESTRICTED
        if not context.data.get(CTX_MX_ROOM):
            return

        # Inject the Matrix system context prompt
        system_prompt.append(
            self.agent.read_prompt("fw.matrix.system_context.md")
        )

        # If restricted mode, inject the restricted chat constraints
        if context.data.get(CTX_MX_RESTRICTED):
            system_prompt.append(
                "# Matrix restricted mode\n"
                "You are in restricted (chat-only) mode.\n"
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
            )

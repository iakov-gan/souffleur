import asyncio
import sys

from copilot import CopilotClient
from copilot.session import PermissionHandler, AssistantMessageData

# WorkIQ summaries contain em-dashes / smart quotes; force UTF-8 console output.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORKIQ = {
    "workiq": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@microsoft/workiq", "mcp"],
        "tools": ["*"],
        "timeout": 120000,  # milliseconds! WorkIQ M365 calls take ~15-20s
    }
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def ask(prompt: str) -> str:
    async with CopilotClient() as client:
        async with await client.create_session(
            model="auto",
            on_permission_request=PermissionHandler.approve_all,
            mcp_servers=WORKIQ,
        ) as session:
            ev = await session.send_and_wait(prompt, timeout=420)
            if ev and isinstance(ev.data, AssistantMessageData):
                return ev.data.content
            return "(no assistant message)"


async def main() -> int:
    prompt = "Use WorkIQ to summarize my most recent email."
    log(f"===== PROMPT: {prompt}")
    try:
        answer = await ask(prompt)
    except Exception as e:
        log(f"[FAIL] {type(e).__name__}: {e}")
        return 1
    print(f"\n--- ANSWER ---\n{answer}\n", flush=True)
    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

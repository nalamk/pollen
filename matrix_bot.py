#!/usr/bin/env python3
"""Matrix bot for the Pollen/Petals chat-ui project.

Connects to a Matrix homeserver and responds to commands and mentions
using the Mixtral model via the local Petals HTTP API.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from urllib.request import urlopen, Request

from dotenv import load_dotenv
from nio import AsyncClient, MatrixRoom, RoomMessageText, InviteMemberEvent

load_dotenv()

# -- Configuration --
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "")
MATRIX_USER_ID = os.getenv("MATRIX_USER_ID", "")
MATRIX_ACCESS_TOKEN = os.getenv("MATRIX_ACCESS_TOKEN", "")
MATRIX_ROOM_ID = os.getenv("MATRIX_ROOM_ID", "")
MATRIX_ENABLED = os.getenv("MATRIX_ENABLED", "false").lower() == "true"
BOT_DISPLAY_NAME = os.getenv("MATRIX_DISPLAY_NAME", "PollenBot")
API_BASE = os.getenv("MATRIX_API_BASE", "http://127.0.0.1:5000")
MAX_MSG_LEN = 2000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("matrix_bot")


# -- Helpers --
def api_get(path):
    """GET a JSON endpoint from the local chat-ui API."""
    try:
        with urlopen(f"{API_BASE}{path}", timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log.error("API GET %s failed: %s", path, e)
        return {"ok": False, "error": str(e)}


def api_generate(prompt):
    """Call the /api/v1/generate endpoint and return the output text."""
    try:
        data = (
            f"model=mistralai/Mixtral-8x7B-Instruct-v0.1"
            f"&inputs={prompt}"
            f"&do_sample=1&temperature=0.6&top_p=0.9"
            f"&repetition_penalty=1.1&max_new_tokens=150"
        ).encode()
        req = Request(f"{API_BASE}/api/v1/generate", data=data)
        with urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
        if resp.get("ok"):
            return clean_response(resp["outputs"])
        return "(generation error: {})".format(resp.get("traceback", "unknown")[:120])
    except Exception as e:
        log.error("Generate failed: %s", e)
        return "(error: {})".format(e)


def clean_response(text):
    """Strip Mixtral special tokens and leading/trailing quotes."""
    for tok in ["</s>", "<s>", "[INST]", "[/INST]", "<<SYS>>", "<</SYS>>"]:
        text = text.replace(tok, "")
    text = re.sub(r"</?(?:s|unk|pad|mask)>", "", text)
    text = text.strip()
    if len(text) >= 2 and text[0] in ('"', "'") and text[-1] == text[0]:
        text = text[1:-1].strip()
    return text


def format_uptime(seconds):
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "{}h {}m".format(int(h), int(m))
    return "{}m {}s".format(int(m), int(s))


def truncate(text, limit=MAX_MSG_LEN):
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


# -- Command handlers --
def cmd_status():
    data = api_get("/api/status")
    if not data.get("ok"):
        return "Status unavailable: {}".format(data.get("error", "?"))
    coverage = data.get("block_coverage", 0)
    total = data.get("total_blocks", 0)
    peers = data.get("num_peers", 0)
    tps = data.get("tokens_per_second", 0)
    uptime = format_uptime(data.get("uptime_seconds", 0))
    peer_details = ""
    for p in data.get("peers", []):
        peer_details += "\n  {} \u2014 blocks {}-{} ({}) @ {} tok/s".format(
            p["peer_id"], p["start"], p["end"] - 1, p["length"], p["throughput"],
        )
    return (
        "**Cluster Status**\n"
        "Peers online: {} | Blocks: {}/{} | Speed: {} tok/s | Uptime: {}"
        "{}".format(peers, coverage, total, tps, uptime, peer_details)
    )


def cmd_speed():
    data = api_get("/api/status")
    if not data.get("ok"):
        return "Speed unavailable"
    return "Current speed: {} tokens/sec".format(data.get("tokens_per_second", 0))


def cmd_model():
    data = api_get("/api/status")
    model = data.get("model_name", "unknown") if data.get("ok") else "unknown"
    try:
        sys.path.insert(0, "/data/chat-ui")
        from config import MODEL_DISPLAY_NAME, MODEL_BADGE, MODEL_CARD_URL
        return "{} [{}] \u2014 {}".format(MODEL_DISPLAY_NAME, MODEL_BADGE, MODEL_CARD_URL)
    except ImportError:
        return "Model: {}".format(model)


def cmd_help():
    return (
        "**PollenBot Commands**\n"
        "\u2022 `!status` \u2014 cluster info (peers, blocks, speed)\n"
        "\u2022 `!speed` \u2014 current tokens/sec\n"
        "\u2022 `!model` \u2014 model name and info\n"
        "\u2022 `!help` \u2014 this message\n"
        "\nMention **{}** to chat with Mixtral.".format(BOT_DISPLAY_NAME)
    )


COMMANDS = {
    "!status": cmd_status,
    "!speed": cmd_speed,
    "!model": cmd_model,
    "!help": cmd_help,
}


# -- Bot class --
class PollenMatrixBot:
    def __init__(self):
        self.client = AsyncClient(MATRIX_HOMESERVER, MATRIX_USER_ID)
        self.client.access_token = MATRIX_ACCESS_TOKEN
        self.client.user_id = MATRIX_USER_ID
        self.client.device_id = "POLLENBOT"
        self._ready = False

    async def start(self):
        log.info("Connecting to %s as %s", MATRIX_HOMESERVER, MATRIX_USER_ID)

        self.client.add_event_callback(self._on_message, RoomMessageText)
        self.client.add_event_callback(self._on_invite, InviteMemberEvent)

        # Do an initial sync to skip old messages
        resp = await self.client.sync(timeout=10000)
        if hasattr(resp, "next_batch"):
            self.client.next_batch = resp.next_batch
        self._ready = True
        log.info("Initial sync complete, now listening for messages")

        # Send online announcement to configured room
        if MATRIX_ROOM_ID:
            await self._send(
                MATRIX_ROOM_ID,
                "PollenBot online \u2014 powered by Mixtral on Petals. Type !help for commands.",
            )

        await self.client.sync_forever(timeout=30000)

    async def _on_invite(self, room: MatrixRoom, event: InviteMemberEvent):
        if event.state_key == MATRIX_USER_ID:
            log.info("Invited to %s, joining", room.room_id)
            await self.client.join(room.room_id)

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText):
        # Skip messages from before we started
        if not self._ready:
            return

        # Don't respond to our own messages
        if event.sender == MATRIX_USER_ID:
            return

        body = event.body.strip()
        if not body:
            return

        # Determine if this message is in a thread
        thread_root = None
        relates_to = event.source.get("content", {}).get("m.relates_to", {})
        if relates_to.get("rel_type") == "m.thread":
            thread_root = relates_to.get("event_id")

        # Commands
        cmd_key = body.split()[0].lower()
        if cmd_key in COMMANDS:
            handler = COMMANDS[cmd_key]
            reply = await asyncio.get_event_loop().run_in_executor(None, handler)
            await self._send(room.room_id, reply, thread_root=thread_root or event.event_id)
            return

        # Respond to mentions of bot name
        bot_names = [BOT_DISPLAY_NAME.lower(), MATRIX_USER_ID.lower()]
        # Also match the localpart of the Matrix ID (e.g. "pollenbot" from "@pollenbot:example.com")
        localpart = MATRIX_USER_ID.split(":")[0].lstrip("@").lower() if ":" in MATRIX_USER_ID else ""
        if localpart:
            bot_names.append(localpart)

        text_lower = body.lower()
        if any(name in text_lower for name in bot_names if name):
            reply = await asyncio.get_event_loop().run_in_executor(
                None, self._generate_reply, event.sender, body
            )
            await self._send(room.room_id, reply, thread_root=thread_root or event.event_id)

    def _generate_reply(self, sender, text):
        # Strip bot name references from the message
        clean = text
        for name in [BOT_DISPLAY_NAME, MATRIX_USER_ID]:
            for variant in [name, name + ":", name + ","]:
                clean = re.sub(re.escape(variant), "", clean, flags=re.IGNORECASE).strip()
        # Also strip localpart mention
        if ":" in MATRIX_USER_ID:
            localpart = MATRIX_USER_ID.split(":")[0].lstrip("@")
            clean = re.sub(re.escape(localpart), "", clean, flags=re.IGNORECASE).strip()
        if not clean:
            clean = "Hello!"

        prompt = (
            "[INST] You are {}, a helpful Matrix bot powered by Mixtral "
            "on the Petals network. Keep answers concise (under 300 chars). "
            "User {} says: {} [/INST]"
        ).format(BOT_DISPLAY_NAME, sender, clean)
        return api_generate(prompt)

    async def _send(self, room_id, text, thread_root=None):
        text = truncate(text)
        content = {
            "msgtype": "m.text",
            "body": text,
        }

        # Add formatted body for markdown rendering
        if "**" in text or "`" in text or "\n" in text:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = _markdown_to_html(text)

        # Thread support
        if thread_root:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_root,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": thread_root},
            }

        try:
            await self.client.room_send(
                room_id, message_type="m.room.message", content=content
            )
        except Exception as e:
            log.error("Failed to send message to %s: %s", room_id, e)

    async def shutdown(self):
        await self.client.close()


def _markdown_to_html(text):
    """Minimal markdown-to-HTML for bold, code, and newlines."""
    html = text
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"`(.+?)`", r"<code>\1</code>", html)
    html = html.replace("\n", "<br>")
    return html


# -- Main --
def main():
    if not MATRIX_ENABLED:
        log.info("Matrix bot is disabled (MATRIX_ENABLED=false). Exiting.")
        sys.exit(0)

    missing = []
    if not MATRIX_HOMESERVER:
        missing.append("MATRIX_HOMESERVER")
    if not MATRIX_USER_ID:
        missing.append("MATRIX_USER_ID")
    if not MATRIX_ACCESS_TOKEN:
        missing.append("MATRIX_ACCESS_TOKEN")
    if missing:
        log.error("Missing required config: %s", ", ".join(missing))
        sys.exit(1)

    bot = PollenMatrixBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        log.info("Shutting down")
        asyncio.run(bot.shutdown())


if __name__ == "__main__":
    main()

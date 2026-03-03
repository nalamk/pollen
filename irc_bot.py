#!/usr/bin/env python3
"""IRC bot for the Pollen/Petals chat-ui project.

Connects to an IRC server and responds to commands and mentions
using the Mixtral model via the local Petals HTTP API.
"""

import os
import ssl
import sys
import json
import re
import textwrap
import threading
import time
import logging
from urllib.request import urlopen, Request
from urllib.error import URLError

import irc.bot
import irc.connection
import irc.strings
from dotenv import load_dotenv

load_dotenv()

# -- Configuration --
IRC_SERVER = os.getenv("IRC_SERVER", "irc.livefreeonline.club")
IRC_PORT = int(os.getenv("IRC_PORT", "6697"))
IRC_CHANNEL = os.getenv("IRC_CHANNEL", "#chat")
IRC_NICKNAME = os.getenv("IRC_NICKNAME", "PollenBot")
IRC_ENABLED = os.getenv("IRC_ENABLED", "true").lower() == "true"
IRC_SSL = os.getenv("IRC_SSL", "true").lower() == "true"
API_BASE = os.getenv("IRC_API_BASE", "http://127.0.0.1:5000")
MAX_MSG_LEN = 300

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("irc_bot")


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


def split_irc(text):
    """Split text into IRC-friendly chunks (max MAX_MSG_LEN chars each)."""
    lines = []
    for paragraph in text.replace("\r", "").split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        lines.extend(textwrap.wrap(paragraph, MAX_MSG_LEN))
    return lines[:6]  # cap at 6 lines to avoid flooding




def clean_response(text):
    """Strip Mixtral special tokens and leading/trailing quotes."""
    for tok in ['</s>', '<s>', '[INST]', '[/INST]', '<<SYS>>', '<</SYS>>']:
        text = text.replace(tok, '')
    text = re.sub(r'</?(?:s|unk|pad|mask)>', '', text)
    text = text.strip()
    if len(text) >= 2 and text[0] in ('"', chr(39)) and text[-1] == text[0]:
        text = text[1:-1].strip()
    return text

def format_uptime(seconds):
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "{}h {}m".format(h, m)
    return "{}m {}s".format(m, s)


# -- Bot class --
class PollenIRCBot(irc.bot.SingleServerIRCBot):
    def __init__(self):
        connect_params = {}
        if IRC_SSL:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            connect_factory = irc.connection.Factory(wrapper=ssl_ctx.wrap_socket)
            connect_params["connect_factory"] = connect_factory
            log.info("SSL enabled for connection")

        server_list = [(IRC_SERVER, IRC_PORT)]
        super().__init__(server_list, IRC_NICKNAME, IRC_NICKNAME, **connect_params)
        self.channel = IRC_CHANNEL
        self._nick_lower = IRC_NICKNAME.lower()

    # -- Connection events --
    def on_nicknameinuse(self, conn, event):
        new_nick = conn.get_nickname() + "_"
        log.warning("Nickname in use, trying %s", new_nick)
        conn.nick(new_nick)
        self._nick_lower = new_nick.lower()

    def on_welcome(self, conn, event):
        log.info("Connected to %s, joining %s", IRC_SERVER, self.channel)
        conn.join(self.channel)

    def on_join(self, conn, event):
        if event.source.nick == conn.get_nickname():
            log.info("Joined %s", self.channel)
            conn.privmsg(self.channel, "PollenBot online — powered by Mixtral on Petals. Type !help for commands.")

    def on_disconnect(self, conn, event):
        log.warning("Disconnected, will reconnect in 30s")
        time.sleep(30)
        self._connect()

    # -- Message handling --
    def on_pubmsg(self, conn, event):
        text = event.arguments[0].strip()
        nick = event.source.nick
        target = event.target

        # Commands
        if text.startswith("!"):
            self._handle_command(conn, target, nick, text)
            return

        # Respond to mentions of the bot name
        if self._nick_lower in text.lower():
            self._handle_mention(conn, target, nick, text)

    def on_privmsg(self, conn, event):
        """Handle DMs the same way as channel messages."""
        text = event.arguments[0].strip()
        nick = event.source.nick
        if text.startswith("!"):
            self._handle_command(conn, nick, nick, text)
        else:
            self._handle_mention(conn, nick, nick, text)

    # -- Commands --
    def _handle_command(self, conn, target, nick, text):
        cmd = text.split()[0].lower()
        handler = {
            "!status": self._cmd_status,
            "!speed": self._cmd_speed,
            "!model": self._cmd_model,
            "!help": self._cmd_help,
        }.get(cmd)
        if handler:
            threading.Thread(
                target=handler, args=(conn, target, nick), daemon=True
            ).start()

    def _reply(self, conn, target, nick, text):
        for line in split_irc(text):
            conn.privmsg(target, "{}: {}".format(nick, line))
            time.sleep(0.5)  # avoid flood

    def _cmd_status(self, conn, target, nick):
        data = api_get("/api/status")
        if not data.get("ok"):
            self._reply(conn, target, nick, "Status unavailable: {}".format(data.get("error", "?")))
            return
        coverage = data.get("block_coverage", 0)
        total = data.get("total_blocks", 0)
        peers = data.get("num_peers", 0)
        tps = data.get("tokens_per_second", 0)
        uptime = format_uptime(data.get("uptime_seconds", 0))
        msg = "Peers online: {} | Blocks: {}/{} | Speed: {} tok/s | Uptime: {}".format(
            peers, coverage, total, tps, uptime
        )
        self._reply(conn, target, nick, msg)

    def _cmd_speed(self, conn, target, nick):
        data = api_get("/api/status")
        if not data.get("ok"):
            self._reply(conn, target, nick, "Speed unavailable")
            return
        tps = data.get("tokens_per_second", 0)
        self._reply(conn, target, nick, "Current speed: {} tokens/sec".format(tps))

    def _cmd_model(self, conn, target, nick):
        data = api_get("/api/status")
        model = data.get("model_name", "unknown") if data.get("ok") else "unknown"
        try:
            sys.path.insert(0, "/data/chat-ui")
            from config import MODEL_DISPLAY_NAME, MODEL_BADGE, MODEL_CARD_URL
            self._reply(
                conn, target, nick,
                "{} [{}] -- {}".format(MODEL_DISPLAY_NAME, MODEL_BADGE, MODEL_CARD_URL),
            )
        except ImportError:
            self._reply(conn, target, nick, "Model: {}".format(model))

    def _cmd_help(self, conn, target, nick):
        self._reply(
            conn, target, nick,
            "Commands: !status (cluster info) | !speed (tokens/sec) "
            "| !model (model info) | !help (this message). "
            "Mention {} to chat with Mixtral.".format(conn.get_nickname()),
        )

    # -- Mention handler (LLM) --
    def _handle_mention(self, conn, target, nick, text):
        threading.Thread(
            target=self._generate_reply,
            args=(conn, target, nick, text),
            daemon=True,
        ).start()

    def _generate_reply(self, conn, target, nick, text):
        # Strip bot name from message
        clean = text
        for variant in [self._nick_lower, self._nick_lower + ":", self._nick_lower + ","]:
            clean = clean.lower().replace(variant, "").strip()
        if not clean:
            clean = "Hello!"

        prompt = (
            "[INST] You are {}, a helpful IRC bot powered by Mixtral "
            "on the Petals network. Keep answers concise (under 250 chars). "
            "User {} says: {} [/INST]"
        ).format(IRC_NICKNAME, nick, clean)
        output = api_generate(prompt)
        self._reply(conn, target, nick, output)


# -- Main --
def main():
    if not IRC_ENABLED:
        log.info("IRC bot is disabled (IRC_ENABLED=false). Exiting.")
        sys.exit(0)

    proto = "SSL" if IRC_SSL else "plain"
    log.info(
        "Starting %s -> %s:%d %s (%s)",
        IRC_NICKNAME, IRC_SERVER, IRC_PORT, IRC_CHANNEL, proto,
    )
    bot = PollenIRCBot()
    bot.start()


if __name__ == "__main__":
    main()

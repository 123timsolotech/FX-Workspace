"""
channels.py
===========
Talk to the assistant from Discord, Slack or a webhook (1.4.2) - Settings > Connectors > Channels.

  One framework   every channel hands an incoming message to handle(). A message from YOU (an allow-listed account,
                  or the webhook's secret) starts a turn in that channel's own chat; anyone else is ignored and logged.
  Never blocks    the turn runs on its own thread while the channel keeps listening - for "stop", for an approval
                  answer, or to say "busy" (the deadlock in the Pithagoras video, designed out).
  Approvals       the running turn's approval card goes to the channel too: Discord and Slack get Allow / Refuse
                  buttons, the webhook a one-time code; the PC can still answer it. Screen/mouse control stays off
                  for channel turns unless Settings > Phone access allows it (schemas.origin_off).
  Secrets         bot tokens and the webhook secret are sealed with Windows DPAPI (mail.seal) in data/channels.json.

The listeners: Discord's gateway and Slack's Socket Mode (no public address needed), each a small aiohttp client on
its own thread; the webhook is POST /hook on this app (127.0.0.1:8000, or the phone listener's address).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

import core.state as shared

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "channels.json")
KINDS = ("webhook", "discord", "slack")
MAX_REPLY = {"discord": 1900, "slack": 3800, "webhook": 20000}
_lock = threading.RLock()
_runner = None                       # api/server sets it: (chat_id, message, origin, on_approval) -> (chat_id, result)
_listeners: dict = {}                # kind -> Listener
_busy = threading.Lock()             # one channel turn at a time (turns.TurnManager allows one anyway)
_hook_codes: dict = {}               # one-time approval codes for the webhook -> (approval id, expires)
_hook_turns: dict = {}               # webhook turn id -> result, for GET /hook/result/<id>
_hook_fails: dict = {}


def configure(runner) -> None:
    global _runner
    _runner = runner


# ---- config (data/channels.json) -------------------------------------------------------------------------------
def _load() -> dict:
    try:
        with open(DATA, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, DATA)


def _seal(v: str) -> str:
    import mail
    return mail.seal(v)


def _unseal(v: str) -> str:
    import mail
    try:
        return mail.unseal(v) if v else ""
    except Exception:
        return ""


def _ids(value) -> list:
    raw = value if isinstance(value, list) else re.split(r"[\s,]+", str(value or ""))
    return [str(x).strip() for x in raw if re.fullmatch(r"[A-Za-z0-9_.@-]{2,64}", str(x).strip())][:20]


def configure_channel(kind: str, enabled: bool | None = None, token: str | None = None, app_token: str | None = None,
                      allow=None) -> dict:
    """Save one channel's settings (empty token: keep the saved one) and restart its listener."""
    if kind not in KINDS:
        return {"ok": False, "message": f"Unknown channel '{kind}'."}
    with _lock:
        d = _load()
        c = d.setdefault(kind, {})
        if enabled is not None:
            c["enabled"] = bool(enabled)
        if token:
            c["token"] = _seal(token.strip())
        if app_token:
            c["app_token"] = _seal(app_token.strip())
        if allow is not None:
            c["allow"] = _ids(allow)
        if kind == "webhook" and not c.get("secret"):
            c["secret"] = _seal(secrets.token_urlsafe(24))
        _save(d)
    restart(kind)
    import events as event_log
    event_log.log_action(f"Channels: {kind} {'on' if _load().get(kind, {}).get('enabled') else 'off'}.")
    return {"ok": True, "channels": status()}


def new_webhook_secret() -> str:
    with _lock:
        d = _load()
        d.setdefault("webhook", {})["secret"] = _seal(secrets.token_urlsafe(24))
        _save(d)
    return webhook_secret()


def webhook_secret() -> str:
    return _unseal((_load().get("webhook") or {}).get("secret", ""))


def status() -> dict:
    d = _load()
    out = {}
    for kind in KINDS:
        c = d.get(kind) or {}
        lst = _listeners.get(kind)
        out[kind] = {"enabled": bool(c.get("enabled")), "has_token": bool(c.get("token")),
                     "has_app_token": bool(c.get("app_token")), "allow": c.get("allow") or [],
                     "chat_id": c.get("chat_id"), "connected": bool(lst and lst.connected),
                     "problem": lst.problem if lst else None}
    out["webhook"]["secret"] = webhook_secret() if out["webhook"]["enabled"] else ""
    return out


def _chat_id(kind: str) -> str | None:
    return (_load().get(kind) or {}).get("chat_id")


def _set_chat_id(kind: str, chat_id: str | None) -> None:
    if not chat_id:
        return
    with _lock:
        d = _load()
        d.setdefault(kind, {})["chat_id"] = chat_id
        _save(d)


def _forget_chat_id(kind: str) -> None:
    with _lock:
        d = _load()
        if (d.get(kind) or {}).pop("chat_id", None) is not None:
            _save(d)


def allowed(kind: str, user_id: str) -> bool:
    return str(user_id) in ((_load().get(kind) or {}).get("allow") or [])


# ---- the shared handling ---------------------------------------------------------------------------------------
_YES = re.compile(r"^\s*(?:yes|y|allow|approve|ok|go|do it)\s*[.!]?\s*$", re.I)
_NO = re.compile(r"^\s*(?:no|n|refuse|deny|reject|don'?t|cancel)\s*[.!]?\s*$", re.I)
_STOP = re.compile(r"^\s*(?:stop|/stop|halt|abort)\s*[.!]?\s*$", re.I)


def answer_approval(approval_id: str, yes: bool, who: str) -> bool:
    import tools.approvals as approvals
    ok = approvals.broker.answer(approval_id, bool(yes))
    if ok:
        import events as event_log
        event_log.log_action(f"{'Allowed' if yes else 'Refused'} from {who}.")
    return ok


def approval_text(p: dict) -> str:
    lines = [p.get("title") or "Allow this action?"]
    for a in (p.get("actions") or [])[:5]:
        what = a.get("label") or a.get("tool") or a.get("tool_name") or "an action"
        detail = a.get("target") or next((str(v) for v in (a.get("args") or {}).values()), "")
        lines.append(f"- {str(what)[:80]}" + (f": {str(detail)[:160]}" if detail else ""))
        if a.get("undo_text"):
            lines.append(f"  ({a['undo_text']})")
    return "\n".join(lines)


def handle(kind: str, user_id: str, text: str, reply, ask=None) -> str:
    """One incoming message. reply(text) sends to the channel; ask(text, approval_id) sends Allow/Refuse buttons
    (None: the channel asks in words). Returns what happened (for tests and the log); never blocks on the turn."""
    import events as event_log
    if not allowed(kind, user_id):
        event_log.log_action(f"Channels: ignored a {kind} message from {str(user_id)[:40]} (not on the allow-list).")
        return "ignored"
    text = (text or "").strip()
    if not text:
        return "empty"
    import tools.approvals as approvals
    pending = approvals.broker.pending()
    if pending and (_YES.match(text) or _NO.match(text)):
        answer_approval(pending["id"], bool(_YES.match(text)), kind)
        reply("Allowed." if _YES.match(text) else "Refused.")
        return "answered"
    if _STOP.match(text):
        from run_control import run_control
        run_control.stop()
        reply("Stopping.")
        return "stopped"
    if not _busy.acquire(blocking=False):
        reply("Still working on your last message - say \"stop\" to end it.")
        return "busy"

    def work():
        try:
            def on_approval(p):
                if ask:
                    ask(approval_text(p), p["id"])
                else:
                    reply(approval_text(p) + "\n\nReply \"yes\" or \"no\".")
            chat_id, result = run_turn(kind, text, on_approval)
            msg = str((result or {}).get("message") or "(no answer)")
            limit = MAX_REPLY.get(kind, 1900)
            for i in range(0, min(len(msg), limit * 4), limit):          # long answers in parts, at most four
                reply(msg[i:i + limit])
        except Exception as e:
            reply(f"Something went wrong: {str(e)[:200]}")
        finally:
            _busy.release()
    threading.Thread(target=work, daemon=True, name=f"channel-{kind}").start()
    return "started"


def run_turn(kind: str, message: str, on_approval) -> tuple:
    if _runner is None:
        return None, {"status": "error", "message": "The app isn't ready yet."}
    chat_id, result = _runner(_chat_id(kind), message, kind, on_approval)
    if isinstance(result, dict) and (result.get("locked") or (result.get("guard") or {}).get("locked")):
        _forget_chat_id(kind)                   # its chat is closed (chat_guard): the next message starts a new one
    else:
        _set_chat_id(kind, chat_id)
    return chat_id, result


# ---- the webhook (POST /hook, api/server) ----------------------------------------------------------------------
def hook_check(secret: str, ip: str) -> str | None:
    """None when the secret is right; else why not (wrong, off, or too many tries from this address)."""
    if not (_load().get("webhook") or {}).get("enabled"):
        return "The webhook is off - turn it on in Settings > Connections > Channels."
    now = time.time()
    recent = [t for t in _hook_fails.get(ip, []) if now - t < 600]
    _hook_fails[ip] = recent
    if len(recent) >= 10:
        return "Too many wrong secrets - wait 10 minutes."
    want = webhook_secret()
    if not want or not hmac.compare_digest(hashlib.sha256(str(secret or "").encode()).digest(),
                                           hashlib.sha256(want.encode()).digest()):
        recent.append(now)
        return "Wrong secret."
    return None


def hook_message(message: str, wait: float = 120) -> dict:
    """Run one webhook message. Waits up to `wait` seconds for the answer; an approval returns at once with
    one-time codes (POST /hook/answer/<code>), and the answer is fetched later (GET /hook/result/<id>)."""
    if not _busy.acquire(blocking=False):
        return {"status": "busy", "message": "Still working on the last message."}
    run_id = secrets.token_urlsafe(8)
    _hook_turns[run_id] = {"status": "running"}
    asked = threading.Event()

    def on_approval(p):
        yes, no = secrets.token_urlsafe(12), secrets.token_urlsafe(12)
        expires = time.time() + 600
        _hook_codes[yes] = (p["id"], True, expires)
        _hook_codes[no] = (p["id"], False, expires)
        _hook_turns[run_id] = {"status": "needs_approval", "question": approval_text(p), "allow": yes, "refuse": no}
        asked.set()

    def work():
        try:
            _cid, result = run_turn("webhook", message, on_approval)
            _hook_turns[run_id] = {"status": "done", "message": str((result or {}).get("message") or "")}
        except Exception as e:
            _hook_turns[run_id] = {"status": "error", "message": str(e)[:300]}
        finally:
            _busy.release()
            asked.set()
    threading.Thread(target=work, daemon=True, name="channel-webhook").start()
    deadline = time.time() + wait
    while time.time() < deadline:
        asked.wait(0.5)
        state = _hook_turns.get(run_id) or {}
        if state.get("status") != "running":
            return {"id": run_id, **state}
    return {"id": run_id, "status": "running", "message": "Still working - fetch /hook/result/" + run_id}


def hook_answer(code: str) -> dict:
    entry = _hook_codes.pop(code, None)
    if not entry or entry[2] < time.time():
        return {"ok": False, "message": "That code was used or has expired."}
    for k, v in list(_hook_codes.items()):         # the other code of the pair is spent too
        if v[0] == entry[0]:
            _hook_codes.pop(k, None)
    ok = answer_approval(entry[0], entry[1], "the webhook")
    return {"ok": ok, "message": ("Allowed." if entry[1] else "Refused.") if ok else "Nothing is waiting for that answer."}


def hook_result(run_id: str) -> dict:
    return _hook_turns.get(run_id) or {"status": "unknown"}


# ---- listeners -----------------------------------------------------------------------------------------------
class Listener:
    kind = ""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.connected = False
        self.problem = None
        self._stop = threading.Event()
        self._loop = None
        self.thread = threading.Thread(target=self._main, daemon=True, name=f"channel-{self.kind}-listen")

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: None)

    def _main(self):
        import netlayer
        self._loop = asyncio.new_event_loop()
        delay = 2
        while not self._stop.is_set():
            try:
                with netlayer.component("other"):
                    self._loop.run_until_complete(self.run())
                delay = 2
            except Exception as e:
                self.problem = str(e)[:200]
            self.connected = False
            self._stop.wait(delay)
            delay = min(120, delay * 2)

    def send(self, coro):
        """Schedule a coroutine on the listener's loop from another thread (a reply from the turn's thread)."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)


# Discord: the gateway (op 2 identify, op 1 heartbeats), MESSAGE_CREATE for DMs and mentions, INTERACTION_CREATE for
# the Allow / Refuse buttons. Intents: GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT.
DISCORD_API = "https://discord.com/api/v10"
DISCORD_INTENTS = (1 << 9) | (1 << 12) | (1 << 15)


def discord_message(event: dict, bot_id: str | None = None) -> tuple | None:
    """(user id, channel id, text) of a MESSAGE_CREATE meant for the bot, or None."""
    d = event.get("d") or {}
    author = d.get("author") or {}
    if event.get("t") != "MESSAGE_CREATE" or author.get("bot"):
        return None
    text = d.get("content") or ""
    is_dm = not d.get("guild_id")
    mentioned = bot_id and any(str(m.get("id")) == str(bot_id) for m in d.get("mentions") or [])
    if not (is_dm or mentioned):
        return None
    text = re.sub(r"<@!?\d+>", "", text).strip()
    return str(author.get("id")), str(d.get("channel_id")), text


def discord_button(event: dict) -> tuple | None:
    """(user id, approval id, yes?, interaction id, token) of an Allow / Refuse click, or None."""
    d = event.get("d") or {}
    if event.get("t") != "INTERACTION_CREATE":
        return None
    m = re.fullmatch(r"fx-(yes|no)-([A-Za-z0-9]+)", str((d.get("data") or {}).get("custom_id") or ""))
    if not m:
        return None
    user = (d.get("member") or {}).get("user") or d.get("user") or {}
    return str(user.get("id")), m.group(2), m.group(1) == "yes", str(d.get("id")), str(d.get("token"))


class DiscordListener(Listener):
    kind = "discord"

    async def run(self):
        import aiohttp
        token = _unseal(self.cfg.get("token", ""))
        if not token:
            self.problem = "Add the bot token."
            self._stop.set()
            return
        headers = {"Authorization": f"Bot {token}", "User-Agent": "FXWorkspace (local, 1.4.2)"}
        import netlayer
        async with netlayer.ws_session("other", headers=headers) as http:
            async with http.get(f"{DISCORD_API}/gateway/bot") as r:
                if r.status == 401:
                    self.problem = "Discord refused the bot token."
                    self._stop.set()
                    return
                url = (await r.json())["url"]
            async with http.ws_connect(f"{url}?v=10&encoding=json", heartbeat=None) as ws:
                hello = await ws.receive_json()
                interval = hello["d"]["heartbeat_interval"] / 1000
                seq = {"s": None}
                await ws.send_json({"op": 2, "d": {"token": token, "intents": DISCORD_INTENTS,
                                                   "properties": {"os": "windows", "browser": "fxworkspace", "device": "fxworkspace"}}})

                async def beat():
                    while not ws.closed:
                        await asyncio.sleep(interval)
                        await ws.send_json({"op": 1, "d": seq["s"]})
                beater = asyncio.ensure_future(beat())
                bot_id = None
                try:
                    async for msg in ws:
                        if self._stop.is_set():
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                        ev = json.loads(msg.data)
                        seq["s"] = ev.get("s", seq["s"])
                        if ev.get("op") == 7 or ev.get("op") == 9:          # reconnect / invalid session
                            break
                        if ev.get("t") == "READY":
                            bot_id = ev["d"]["user"]["id"]
                            self.connected, self.problem = True, None
                        got = discord_message(ev, bot_id)
                        if got:
                            uid, channel_id, text = got
                            self._handle(http, uid, channel_id, text)
                        click = discord_button(ev)
                        if click:
                            uid, approval_id, yes, iid, itoken = click
                            done = allowed("discord", uid) and answer_approval(approval_id, yes, "Discord")
                            content = ("Allowed." if yes else "Refused.") if done else "Not answered (not you, or already answered)."
                            await http.post(f"{DISCORD_API}/interactions/{iid}/{itoken}/callback",
                                            json={"type": 7, "data": {"content": content, "components": []}})
                finally:
                    beater.cancel()

    def _handle(self, http, uid, channel_id, text):
        async def post(body):
            await http.post(f"{DISCORD_API}/channels/{channel_id}/messages", json=body)

        def reply(t):
            self.send(post({"content": t[:2000]}))

        def ask(t, approval_id):
            self.send(post({"content": t[:1900], "components": [{"type": 1, "components": [
                {"type": 2, "style": 3, "label": "Allow", "custom_id": f"fx-yes-{approval_id}"},
                {"type": 2, "style": 4, "label": "Refuse", "custom_id": f"fx-no-{approval_id}"}]}]}))
        handle("discord", uid, text, reply, ask)


# Slack: Socket Mode (apps.connections.open with the app-level xapp- token), every envelope acknowledged; DMs and
# mentions start turns, block_actions answer approvals. Replies go out with the bot's xoxb- token.
SLACK_API = "https://slack.com/api"


def slack_message(envelope: dict) -> tuple | None:
    """(user, channel, text) of a DM or mention to the bot, or None."""
    if envelope.get("type") != "events_api":
        return None
    ev = ((envelope.get("payload") or {}).get("event")) or {}
    if ev.get("bot_id") or ev.get("subtype"):
        return None
    if ev.get("type") == "app_mention" or (ev.get("type") == "message" and ev.get("channel_type") == "im"):
        return str(ev.get("user")), str(ev.get("channel")), re.sub(r"<@[A-Z0-9]+>", "", ev.get("text") or "").strip()
    return None


def slack_button(envelope: dict) -> tuple | None:
    """(user, approval id, yes?, response_url) of an Allow / Refuse click, or None."""
    if envelope.get("type") != "interactive":
        return None
    p = envelope.get("payload") or {}
    acts = p.get("actions") or []
    if p.get("type") != "block_actions" or not acts or acts[0].get("action_id") not in ("fx-yes", "fx-no"):
        return None
    return str((p.get("user") or {}).get("id")), str(acts[0].get("value")), acts[0]["action_id"] == "fx-yes", p.get("response_url")


class SlackListener(Listener):
    kind = "slack"

    async def run(self):
        import aiohttp
        bot, app_token = _unseal(self.cfg.get("token", "")), _unseal(self.cfg.get("app_token", ""))
        if not (bot and app_token):
            self.problem = "Add the bot token (xoxb-) and the app-level token (xapp-)."
            self._stop.set()
            return
        import netlayer
        async with netlayer.ws_session("other") as http:
            async with http.post(f"{SLACK_API}/apps.connections.open", headers={"Authorization": f"Bearer {app_token}"}) as r:
                d = await r.json()
            if not d.get("ok"):
                self.problem = f"Slack: {d.get('error', 'not connected')}"
                if d.get("error") in ("invalid_auth", "not_allowed_token_type"):
                    self._stop.set()
                return
            async with http.ws_connect(d["url"]) as ws:
                async for msg in ws:
                    if self._stop.is_set() or msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    env = json.loads(msg.data)
                    if env.get("envelope_id"):
                        await ws.send_json({"envelope_id": env["envelope_id"]})
                    if env.get("type") == "hello":
                        self.connected, self.problem = True, None
                    if env.get("type") == "disconnect":
                        break
                    got = slack_message(env)
                    if got:
                        self._handle(http, bot, *got)
                    click = slack_button(env)
                    if click:
                        uid, approval_id, yes, url = click
                        done = allowed("slack", uid) and answer_approval(approval_id, yes, "Slack")
                        if url:
                            await http.post(url, json={"replace_original": True,
                                                       "text": ("Allowed." if yes else "Refused.") if done else "Not answered."})

    def _handle(self, http, bot, uid, channel, text):
        async def post(body):
            await http.post(f"{SLACK_API}/chat.postMessage", json={"channel": channel, **body},
                            headers={"Authorization": f"Bearer {bot}"})

        def reply(t):
            self.send(post({"text": t[:3900]}))

        def ask(t, approval_id):
            self.send(post({"text": t[:2900], "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": t[:2900]}},
                {"type": "actions", "elements": [
                    {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Allow"}, "action_id": "fx-yes", "value": approval_id},
                    {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "Refuse"}, "action_id": "fx-no", "value": approval_id}]}]}))
        handle("slack", uid, text, reply, ask)


_CLASSES = {"discord": DiscordListener, "slack": SlackListener}


def restart(kind: str) -> None:
    old = _listeners.pop(kind, None)
    if old:
        old.stop()
    cfg = _load().get(kind) or {}
    if kind in _CLASSES and cfg.get("enabled"):
        lst = _CLASSES[kind](cfg)
        _listeners[kind] = lst
        lst.start()


def start_all() -> None:
    for kind in _CLASSES:
        try:
            restart(kind)
        except Exception:
            pass

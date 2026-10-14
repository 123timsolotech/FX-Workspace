"""
messaging.py
============
Provider-agnostic messaging for the agent: list, search and read
conversations, draft a reply, and send a draft.

  * MessagingProvider   - the adapter interface a real service implements
  * MessagingHub        - the registry the tools talk to
  (The in-memory fake used by tests lives in testing_fakes.py.)

Design rules:
  * Drafting never sends. A draft is stored locally with an id.
  * Sending only accepts a draft id - the agent cannot send text it has not
    first drafted, so the approval card always shows exactly what goes out.
  * Read / draft / send are separate tools with separate permissions
    (permissions.py MESSAGE_KINDS / messaging_permissions). Send can only be
    "ask" or "deny" - never auto-approved, not by override and not in
    "Trust fully" mode.
  * No real provider ships with the app. With none configured every tool
    answers "unavailable" and the tools are not offered to the model.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

CAPABILITIES = ("list", "search", "read", "draft", "send")


class MessagingUnavailable(Exception):
    pass


class MessagingProvider(ABC):
    id = "provider"
    name = "Messaging provider"
    capabilities: tuple = ()

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    @abstractmethod
    def list_conversations(self, limit: int = 20) -> list[dict]: ...

    @abstractmethod
    def search(self, query: str, limit: int = 20) -> list[dict]: ...

    @abstractmethod
    def read(self, conversation_id: str, limit: int = 30) -> dict: ...

    @abstractmethod
    def create_draft(self, conversation_id: str, text: str) -> dict: ...

    @abstractmethod
    def send_draft(self, draft_id: str) -> dict: ...


class MessagingHub:
    def __init__(self):
        self._providers: dict[str, MessagingProvider] = {}
        self.default_id: str | None = None

    def register(self, provider: MessagingProvider, default: bool = True):
        self._providers[provider.id] = provider
        if default or self.default_id is None:
            self.default_id = provider.id

    def configured(self) -> bool:
        return bool(self._providers)

    def get(self, provider_id: str | None = None) -> MessagingProvider:
        pid = provider_id or self.default_id
        if not pid or pid not in self._providers:
            raise MessagingUnavailable("No messaging provider is configured.")
        return self._providers[pid]

    def status(self) -> dict:
        return {"configured": self.configured(), "default": self.default_id,
                "providers": [{"id": p.id, "name": p.name, "capabilities": list(p.capabilities)}
                              for p in self._providers.values()]}

    # ---- tool entry points: each returns a tool-result dict -------------
    def run_tool(self, tool: str, args: dict) -> dict:
        capability = {"messages_list_conversations": "list", "messages_search": "search",
                      "messages_read": "read", "messages_draft": "draft", "messages_send": "send"}[tool]
        try:
            provider = self.get(args.get("provider"))
        except MessagingUnavailable as e:
            return {"ok": False, "status": "unavailable", "message": str(e)}
        if not provider.supports(capability):
            return {"ok": False, "status": "unavailable",
                    "message": f"{provider.name} does not support '{capability}'."}
        try:
            if capability == "list":
                items = provider.list_conversations(int(args.get("limit") or 20))
                return {"ok": True, "data": items, "message": "\n".join(f"{c['id']}: {c['title']} - {c['last'][:80]}" for c in items) or "No conversations."}
            if capability == "search":
                hits = provider.search(args["query"], int(args.get("limit") or 20))
                return {"ok": True, "data": hits, "message": "\n".join(f"{h['conversation_id']} ({h['title']}) {h['from']}: {h['text'][:120]}" for h in hits) or "No matches."}
            if capability == "read":
                conv = provider.read(args["conversation_id"], int(args.get("limit") or 30))
                return {"ok": True, "data": conv, "message": f"{conv['title']}:\n" + "\n".join(f"{m['from']}: {m['text']}" for m in conv["messages"])}
            if capability == "draft":
                draft = provider.create_draft(args["conversation_id"], args["text"])
                return {"ok": True, "data": draft, "verified": True,
                        "message": f"Draft {draft['id']} saved (NOT sent): {draft['text'][:300]}\nSend it with messages_send(draft_id='{draft['id']}') only if the user asked to send."}
            result = provider.send_draft(args["draft_id"])
            return {"ok": True, "data": result, "verified": bool(result.get("sent") or result.get("already_sent")),
                    "message": f"Draft {args['draft_id']} sent." if result.get("sent") else f"Draft {args['draft_id']} was already sent."}
        except KeyError as e:
            return {"ok": False, "status": "permanent_failure", "message": str(e).strip("'\"")}


MESSAGING_TOOLS = [
    {"type": "function", "function": {"name": "messages_list_conversations", "description": "Lists recent conversations from the user's configured messaging provider.", "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {"name": "messages_search", "description": "Searches the user's messages for text.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "messages_read", "description": "Reads the recent history of one conversation.", "parameters": {"type": "object", "properties": {"conversation_id": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["conversation_id"]}}},
    {"type": "function", "function": {"name": "messages_draft", "description": "Saves a reply as a DRAFT in a conversation. Never sends anything.", "parameters": {"type": "object", "properties": {"conversation_id": {"type": "string"}, "text": {"type": "string"}, "justification": {"type": "string"}}, "required": ["conversation_id", "text"]}}},
    {"type": "function", "function": {"name": "messages_send", "description": "Sends a previously saved draft. Only when the user explicitly asked to send; always requires the user's approval.", "parameters": {"type": "object", "properties": {"draft_id": {"type": "string"}, "justification": {"type": "string"}}, "required": ["draft_id"]}}},
]
MESSAGING_READ_TOOLS = {"messages_list_conversations", "messages_search", "messages_read"}
MESSAGING_TOOL_NAMES = {t["function"]["name"] for t in MESSAGING_TOOLS}


def hub_from_env() -> MessagingHub:
    """The app's hub. Real providers register here when one is configured;
    none ships with the app (and no fake can be switched on)."""
    return MessagingHub()

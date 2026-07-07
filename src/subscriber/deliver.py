"""Send the digest by email via AgentMail."""

import os

from agentmail import AgentMail

from .state import State


def send_email(digest: str, subject: str, to: str, state: State) -> str:
    api_key = os.environ.get("AGENTMAIL_API_KEY")
    if not api_key:
        raise RuntimeError("环境变量 AGENTMAIL_API_KEY 未设置")
    client = AgentMail(api_key=api_key)

    inbox_id = state.kv_get("agentmail_inbox_id")
    if not inbox_id:
        inbox = client.inboxes.create()
        inbox_id = inbox.inbox_id
        state.kv_set("agentmail_inbox_id", inbox_id)

    client.inboxes.messages.send(inbox_id, to=to, subject=subject, text=digest)
    return inbox_id

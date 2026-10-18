"""Allowlisted activity labels, never model reasoning or tool arguments.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

LABELS = {
    "thinking": "Thinking", "reading_topic": "Reading topic history", "searching_messages": "Searching conversations",
    "reading_ledger": "Reading ledger", "writing_ledger": "Writing ledger entry",
    "searching_bibliography": "Searching bibliography", "writing_bibliography": "Writing bibliography",
    "reading_blog": "Reading blog", "writing_blog": "Writing blog draft", "reading_attachment": "Reading attachment",
    "reading_workspace": "Reading workspace", "writing_workspace": "Writing workspace",
    "waiting_approval": "Waiting for approval", "running_compute": "Running approved computation",
    "running_lean": "Running Lean", "compiling_tex": "Compiling TeX", "using_tools": "Using tools",
    "writing_response": "Writing response", "finishing": "Finishing",
}


def tool_activity(name):
    if not isinstance(name, str):
        return "using_tools"
    if name == "topic_history":
        return "reading_topic"
    if name in {"messages_search", "topics_search"}:
        return "searching_messages"
    if name == "workspace_read":
        return "reading_workspace"
    if name == "workspace_write":
        return "writing_workspace"
    if name.startswith("media_") or name in {"artifact_list", "research_file_read"}:
        return "reading_attachment"
    if name.startswith("blog_"):
        return "reading_blog" if name == "blog_read" else "writing_blog"
    if "reference" in name:
        return "searching_bibliography" if any(part in name for part in ("read", "search", "list")) else "writing_bibliography"
    if name.startswith("ledger_"):
        return "reading_ledger" if any(part in name for part in ("read", "search", "list")) else "writing_ledger"
    return "using_tools"


def cli_stage(event):
    """Session-ready or visible-text markers of the CLI protocols, and a reported model; never text content."""
    if not isinstance(event, dict):
        return None, None
    kind, item = event.get("type"), event.get("item")
    if kind == "thread.started":
        return "ready", None
    if kind == "system" and event.get("subtype") == "init":
        model = event.get("model")
        from .bot_runtime import MODEL

        return "ready", model if isinstance(model, str) and MODEL.fullmatch(model) else None
    if (kind in {"item.started", "item.updated", "item.completed"} and isinstance(item, dict)
            and item.get("type") == "agent_message" and isinstance(item.get("text"), str) and item["text"]):
        return "text", None
    message = event.get("message") if kind == "assistant" else None
    if isinstance(message, dict) and isinstance(message.get("content"), list) and any(
            isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"]
            for block in message["content"]):
        return "text", None
    return None, None


def cli_activity(event):
    """Recognize public protocol events without forwarding their free-form fields."""
    if not isinstance(event, dict):
        return None
    item = event.get("item", {})
    if event.get("type") in {"item.started", "item.completed"} and isinstance(item, dict):
        if item.get("type") == "mcp_tool_call":
            return tool_activity(item.get("tool", ""))
        if item.get("type") == "agent_message":
            return "writing_response"
        if item.get("type") == "reasoning":
            return "thinking"
    if event.get("type") == "assistant":
        message = event.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            return None
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                return tool_activity(name.removeprefix("mcp__reasoning__") if isinstance(name, str) else name)
            if isinstance(block, dict) and block.get("type") == "text":
                return "writing_response"
    return None

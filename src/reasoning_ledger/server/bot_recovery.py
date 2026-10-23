"""Content-free inspection and exact commands for a retained local invocation."""

import shlex


def guidance(directory, *, runtime=True):
    command = "uv run --locked reasoning-bot recover --state-dir " + shlex.quote(str(directory))
    return {"inspect_command": command + " --action inspect", "skip_command": command + " --action skip",
            "retry_command": None if runtime else command + " --action retry",
            "restart_command": "make bot-run BOT_STATE=" + shlex.quote(str(directory)),
            "notice": "Inspect the retained receipt and local CLI session first. Skipping releases the local runner fence; "
                      "it does not settle remote work or prove that no charge or side effect occurred."}


def interrupted(directory, *, runtime=True):
    value = guidance(directory, runtime=runtime)
    return ("an invocation has an unknown outcome; inspect its local session and retained receipt before retrying. "
            + value["inspect_command"])

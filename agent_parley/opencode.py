"""Adapts native OpenCode configuration and plugin events to shared hooks.

OpenCode reads ``opencode.json`` from its configuration directory and runs
JavaScript plugins from the ``plugin`` subdirectory there; it has no hook
command contract of its own. The lane therefore receives a private copy of
that directory, selected with ``OPENCODE_CONFIG_DIR`` for one launch only,
holding the user's configuration plus the lane's MCP server and one plugin
that runs the configured hook command for each supported native event.
"""

import json
import os
import shlex
import shutil
from pathlib import Path

from agent_parley import protocol
from agent_parley.state import BridgeError, write_json, write_text

EVENTS = {
    "session.created": "SessionStart",
    "chat.message": "UserPromptSubmit",
    "tool.execute.before": "PreToolUse",
    "tool.execute.after": "PostToolUse",
    "permission.ask": "PermissionRequest",
    "session.idle": "Stop",
}
CONFIG = "opencode.json"
PLUGIN = """import { spawnSync } from "node:child_process";

const COMMAND = __COMMAND__;

function decide(payload) {
  const result = spawnSync(COMMAND[0], COMMAND.slice(1), {
    input: JSON.stringify(payload),
    encoding: "utf8",
    timeout: 3000,
  });
  if (result.status === 2) {
    throw new Error(result.stderr || "Agent Parley checkpoint refused");
  }
  if (result.status !== 0) {
    return {};
  }
  try {
    return JSON.parse(result.stdout || "{}");
  } catch {
    return {};
  }
}

export const AgentParley = async ({ client, directory }) => {
  const payload = (name, sessionID, fields) => ({
    hook_event_name: name,
    session_id: sessionID,
    cwd: directory,
    ...fields,
  });
  return {
    event: async ({ event }) => {
      if (event.type === "session.created") {
        decide(payload("session.created", event.properties.info.id, {}));
      } else if (event.type === "session.idle") {
        const id = event.properties.sessionID;
        const reply = decide(payload("session.idle", id, {}));
        if (reply.decision === "block" && reply.reason) {
          await client.session.prompt({
            path: { id },
            body: { parts: [{ type: "text", text: reply.reason }] },
          });
        }
      }
    },
    "chat.message": async (input, output) => {
      const reply = decide(payload("chat.message", input.sessionID, {}));
      if (reply.context) {
        output.parts.push({ type: "text", text: reply.context });
      }
    },
    "tool.execute.before": async (input, output) => {
      const reply = decide(
        payload("tool.execute.before", input.sessionID, {
          tool_name: input.tool,
          tool_input: output.args,
        }),
      );
      if (reply.decision === "deny") {
        throw new Error(reply.reason || "Agent Parley refused this call");
      }
    },
    "tool.execute.after": async (input, output) => {
      decide(
        payload("tool.execute.after", input.sessionID, {
          tool_name: input.tool,
          tool_response: output.output,
        }),
      );
    },
    "permission.ask": async (input) => {
      decide(
        payload("permission.ask", input.sessionID, {
          tool_name: input.type,
          tool_input: input.metadata,
        }),
      );
    },
  };
};
"""


def configure(
    directory: Path,
    name: str,
    url: str,
    hooks: dict,
    source_dir: str | None = None,
) -> Path:
    """Copies the user's OpenCode configuration into a lane-private directory.

    The lane's MCP server is added under ``mcp`` and one plugin file is
    written that runs the shared hook command for each event in ``EVENTS``.
    Every other key of the user's ``opencode.json`` is carried unchanged, the
    source directory is never written, and OpenCode's own authentication
    store outside the configuration directory stays in effect.

    Args:
        directory: Private project state directory.
        name: Validated participant name.
        url: Authenticated loopback MCP endpoint.
        hooks: Common checkpoint hook definitions.
        source_dir: Configuration directory selected by the launch
            environment; OpenCode's default location when unset.

    Returns:
        Directory for OPENCODE_CONFIG_DIR in this launch only.

    Raises:
        BridgeError: If the user's configuration cannot safely accept the
            lane server or plugin.
    """
    source = Path(
        source_dir
        or os.environ.get(
            "OPENCODE_CONFIG_DIR", str(Path.home() / ".config" / "opencode")
        )
    )
    if (source / "opencode.jsonc").exists() and not (source / CONFIG).exists():
        raise BridgeError(
            f"{source / 'opencode.jsonc'} cannot be carried into a lane "
            "overlay unchanged; save it as opencode.json first."
        )
    path = source / CONFIG
    settings = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(settings, dict):
        raise BridgeError("OpenCode configuration must be an object.")
    servers = settings.setdefault("mcp", {})
    plugins = settings.setdefault("plugin", [])
    if not isinstance(servers, dict) or not isinstance(plugins, list):
        raise BridgeError(
            "OpenCode mcp and plugin settings must be an object and an array."
        )
    if "agent_parley" in servers:
        raise BridgeError(
            "OpenCode configuration already defines agent_parley."
        )
    servers["agent_parley"] = {
        "type": "remote",
        "url": url,
        "enabled": True,
        "headers": {
            "Authorization": "Bearer {env:AGENT_PARLEY_TOKEN}",
            protocol.HEADER: str(protocol.PROTOCOL),
        },
    }
    command = shlex.split(hooks["PreToolUse"][0]["hooks"][0]["command"])
    command += ["--adapter", "opencode"]
    overlay = directory / f"{name}-opencode"
    shutil.rmtree(overlay, ignore_errors=True)
    (overlay / "plugin").mkdir(parents=True, mode=0o700)
    write_json(overlay / CONFIG, settings)
    write_text(
        overlay / "plugin" / "agent-parley.js",
        PLUGIN.replace("__COMMAND__", json.dumps(command)),
    )
    return overlay


def payload(value: dict) -> dict:
    """Translates plugin event and MCP tool names for shared checkpoints."""
    translated = dict(value)
    event = value.get("hook_event_name", "")
    translated["hook_event_name"] = EVENTS.get(event, event)
    tool = str(value.get("tool_name", ""))
    if tool.startswith("agent_parley_"):
        translated["tool_name"] = tool.replace(
            "agent_parley_", "mcp__agent_parley__", 1
        )
    return translated


def response(value: dict) -> dict:
    """Flattens shared checkpoint output into the fields the plugin reads."""
    details = value.get("hookSpecificOutput", {})
    result: dict = {}
    if details.get("permissionDecision") == "deny":
        result.update(
            decision="deny", reason=details.get("permissionDecisionReason", "")
        )
    elif value.get("decision") == "block":
        result.update(decision="block", reason=value.get("reason", ""))
    if context := details.get("additionalContext"):
        result["context"] = context
    return result

"""Runs the Claude agent that controls agent_search.py.

The agent gets the SearchTools (agent_tools.py) and runs the whole search. A
runner connects Claude to those tools and returns when the agent finishes:

  ApiRunner         Anthropic Messages API tool-use loop (billed as API usage).
  ClaudeCodeRunner  One Claude Code session (``claude -p``). It uses a Claude
                    subscription's usage limits instead of API billing. The
                    tools reach it as an MCP server: Claude Code starts
                    agent_mcp_proxy.py, which forwards each call over a Unix
                    socket to a ToolRelay in this process. The tools, the
                    Evaluator and the server state therefore stay here,
                    whichever backend runs the agent.
"""
import json
import logging
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading

from agent_tools import AgentError

log = logging.getLogger("agent_runner")

DEFAULT_MODEL = "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MCP_SERVER_NAME = "dnssearch"
PROXY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "agent_mcp_proxy.py")
# Claude Code's own tools the agent has no use for. The search is done
# entirely through the MCP tools, from an empty working directory.
CLAUDE_CODE_DISALLOWED = ["Bash", "Edit", "Write", "NotebookEdit", "WebFetch",
                          "WebSearch", "Task", "Agent"]
USAGE_LABELS = {"five_hour": "session (5-hour)", "seven_day": "weekly"}
# How far past a usage limit the agent may go (writing its report) before
# the session is stopped outright.
USAGE_HARD_MARGIN = 0.05
MAX_NUDGES = 2
NUDGE = ("You ended your turn without calling finish. Continue the search, or "
         "call finish if you are done.")


def _log_text(prefix, text, limit=2000):
    text = (text or "").strip()
    if text:
        log.info("%s %s", prefix, text if len(text) <= limit else text[:limit] + "...")


# ---------------------------------------------------------------------------
# Anthropic API
# ---------------------------------------------------------------------------

class ApiRunner:
    """Manual tool-use loop over the Messages API."""

    name = "api"

    def __init__(self, model=None, effort="high", max_turns=200, client=None):
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        self.max_turns = max_turns
        if client is None:
            import anthropic
            # An API key that is not scoped to a workspace needs the workspace
            # named on every request.
            workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
            headers = {"anthropic-workspace-id": workspace} if workspace else None
            client = anthropic.Anthropic(default_headers=headers)
        self.client = client

    def _stream(self, messages, tool_defs, system):
        import anthropic
        try:
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=64000,
                system=system,
                tools=tool_defs,
                messages=messages,
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                return stream.get_final_message()
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise AgentError(f"Anthropic API credentials rejected: {e}") from e
        except anthropic.BadRequestError as e:
            raise AgentError(f"Anthropic API rejected the request: {e}") from e
        except anthropic.RateLimitError as e:
            raise AgentError(f"Anthropic API rate limit (after retries): {e}") from e
        except anthropic.APIStatusError as e:
            raise AgentError(f"Anthropic API error {e.status_code}: {e}") from e
        except anthropic.APIConnectionError as e:
            raise AgentError(f"Could not reach the Anthropic API: {e}") from e

    def run(self, tools, transcript):
        tool_defs = tools.definitions()
        prompt = tools.initial_prompt()
        messages = [{"role": "user", "content": prompt}]
        transcript.record("prompt", backend=self.name, model=self.model,
                          system=tools.system_prompt, prompt=prompt)
        nudges = 0

        for turn in range(1, self.max_turns + 1):
            response = self._stream(messages, tool_defs, tools.system_prompt)
            # Append the full content: thinking blocks must go back unchanged.
            messages.append({"role": "assistant", "content": response.content})
            usage = getattr(response, "usage", None)
            transcript.record(
                "response", turn=turn, stop_reason=response.stop_reason,
                model=response.model,
                content=[block.to_dict() for block in response.content],
                usage=usage.to_dict() if hasattr(usage, "to_dict") else None)

            for block in response.content:
                if block.type == "thinking":
                    _log_text("Agent (thinking):", block.thinking, 600)
                elif block.type == "text":
                    _log_text("Agent:", block.text)

            if response.stop_reason == "refusal":
                raise AgentError(f"agent refused: {response.stop_details}")
            if response.stop_reason == "max_tokens":
                raise AgentError("agent response was cut off at max_tokens")

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                if tools.done:
                    return
                if nudges >= MAX_NUDGES:
                    log.warning("Agent stopped without calling finish")
                    return
                nudges += 1
                messages.append({"role": "user", "content": NUDGE})
                continue

            results = []
            for block in tool_uses:
                log.info("Agent calls %s %s", block.name,
                         json.dumps(block.input, default=str)[:500])
                text, is_error = tools.call(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": text, "is_error": is_error})
            # All results for one turn go back in a single user message.
            messages.append({"role": "user", "content": results})
            if tools.done:
                return

        raise AgentError(f"agent did not finish within {self.max_turns} turns")


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

class ToolRelay:
    """Serves SearchTools on a Unix socket for agent_mcp_proxy.py.

    One JSON request per connection, one JSON line back:
      {"op": "list"}                              -> {"tools": [...]}
      {"op": "call", "name": ..., "arguments": {}} -> {"text": ..., "is_error": ...}
    """

    def __init__(self, tools):
        self.tools = tools
        self._dir = tempfile.mkdtemp(prefix="agent_relay_")
        os.chmod(self._dir, 0o700)
        self.socket_path = os.path.join(self._dir, "tools.sock")
        relay = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                line = self.rfile.readline()
                try:
                    reply = relay._handle(json.loads(line))
                except Exception as e:  # noqa: BLE001 - reported to the agent
                    log.exception("Tool relay request failed")
                    reply = {"text": f"internal error: {e}", "is_error": True}
                self.wfile.write((json.dumps(reply, default=str) + "\n").encode())

        self._server = socketserver.ThreadingUnixStreamServer(self.socket_path, Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="tool-relay", daemon=True)

    def _handle(self, request):
        if request.get("op") == "list":
            return {"tools": self.tools.definitions()}
        if request.get("op") == "call":
            text, is_error = self.tools.call(request.get("name"),
                                             request.get("arguments") or {})
            return {"text": text, "is_error": is_error}
        return {"text": f"unknown op {request.get('op')!r}", "is_error": True}

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        shutil.rmtree(self._dir, ignore_errors=True)


class ClaudeCodeRunner:
    """Runs the agent as one ``claude -p`` session. Claude Code must be
    installed and logged in (run ``claude`` once and sign in)."""

    name = "claude-code"

    def __init__(self, model=None, effort="high", max_turns=200,
                 claude_bin="claude", tool_timeout_s=21600, usage_limits=None,
                 workdir=None, builtin_tools=()):
        self.model = model
        self.effort = effort
        self.max_turns = max_turns
        self.claude_bin = claude_bin
        # Source mode: the session runs in a source checkout and may use these
        # Claude Code tools there (e.g. Read, Grep, Glob, Edit). Otherwise it
        # runs in an empty directory with only the search tools.
        self.workdir = workdir
        self.builtin_tools = list(builtin_tools)
        # {window: fraction}, windows as Claude Code reports them in
        # rate_limit_event ("five_hour", "seven_day"); None = no limit.
        self.usage_limits = {w: f for w, f in (usage_limits or {}).items()
                             if f is not None}
        self._usage = {}
        # A measurement can run up to --point-timeout; leave margin for the
        # install and restore around it, including reboots in open mode.
        self.tool_timeout_ms = (tool_timeout_s + 3600) * 1000
        if shutil.which(claude_bin) is None:
            raise AgentError(
                f"'{claude_bin}' not found. Install Claude Code and log in, or "
                "pass --claude-bin with its path.")

    def _command(self, socket_path, system):
        mcp_config = {"mcpServers": {MCP_SERVER_NAME: {
            "type": "stdio",
            "command": sys.executable,
            "args": [PROXY_SCRIPT, socket_path],
        }}}
        command = [
            self.claude_bin, "-p",
            "--output-format", "stream-json", "--verbose",
            "--mcp-config", json.dumps(mcp_config),
            "--strict-mcp-config",
            "--allowedTools", f"mcp__{MCP_SERVER_NAME}", *self.builtin_tools,
            "--disallowedTools", *[t for t in CLAUDE_CODE_DISALLOWED
                                   if t not in self.builtin_tools],
            "--append-system-prompt", system,
            "--no-session-persistence",
            "--max-turns", str(self.max_turns),
        ]
        if self.model:
            command += ["--model", self.model]
        if self.effort:
            command += ["--effort", self.effort]
        return command

    def usage_exhausted(self):
        """Windows ("five_hour", "seven_day") at or past their usage limit,
        as last reported by Claude Code."""
        return [w for w, limit in self.usage_limits.items()
                if self._usage.get(w) is not None and self._usage[w] >= limit]

    def _check_usage(self, info, tools, proc):
        """Track subscription usage from a rate_limit_event. At a window's
        limit the agent may not start another measurement and is told to
        finish; USAGE_HARD_MARGIN past it, the session is stopped."""
        windows = info.get("unifiedWindows") or {}
        usage = {w: v.get("utilization") for w, v in windows.items()
                 if isinstance(v, dict) and v.get("utilization") is not None}
        if usage and usage != self._usage:
            self._usage = usage
            log.info("Subscription usage: %s", ", ".join(
                f"{USAGE_LABELS.get(w, w)} {u:.0%}"
                + (f" (limit {self.usage_limits[w]:.0%})" if w in self.usage_limits else "")
                for w, u in usage.items()))
        for window, limit in self.usage_limits.items():
            used = usage.get(window)
            if used is None or used < limit:
                continue
            label = USAGE_LABELS.get(window, window)
            if round(used - limit, 6) > USAGE_HARD_MARGIN:
                proc.terminate()
                raise AgentError(f"{label} usage at {used:.0%}, past the "
                                 f"{limit:.0%} limit; stopped the session")
            if not tools.stop_reason:
                tools.stop_reason = (f"the {label} usage limit was reached "
                                     f"({used:.0%} of {limit:.0%})")
                log.warning("%s: no more measurements; asking the agent to "
                            "finish", tools.stop_reason)

    def _handle_event(self, event, proc, tools):
        """Log one stream-json event. Returns the final result event, if this
        is it."""
        kind = event.get("type")
        if kind == "rate_limit_event":
            self._check_usage(event.get("rate_limit_info") or {}, tools, proc)
        elif kind == "system" and event.get("subtype") == "init":
            servers = {s.get("name"): s.get("status")
                       for s in event.get("mcp_servers") or []}
            status = servers.get(MCP_SERVER_NAME)
            log.info("Claude Code session started (model %s, tools server: %s)",
                     event.get("model"), status)
            if status != "connected":
                proc.terminate()
                raise AgentError(f"Claude Code could not start the search tools "
                                 f"(MCP server status: {status})")
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text":
                    _log_text("Agent:", block.get("text"))
                elif block.get("type") == "tool_use":
                    log.info("Agent calls %s %s", block.get("name"),
                             json.dumps(block.get("input"), default=str)[:500])
        elif kind == "result":
            return event
        return None

    def run(self, tools, transcript):
        prompt = tools.initial_prompt()
        transcript.record("prompt", backend=self.name, model=self.model,
                          system=tools.system_prompt, prompt=prompt)
        env = {**os.environ, "MCP_TOOL_TIMEOUT": str(self.tool_timeout_ms)}

        with ToolRelay(tools) as relay, \
                tempfile.TemporaryDirectory(prefix="agent_search_") as empty_dir, \
                tempfile.TemporaryFile(mode="w+") as stderr:
            proc = subprocess.Popen(
                self._command(relay.socket_path, tools.system_prompt),
                cwd=self.workdir or empty_dir, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                text=True,
            )
            result = None
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        log.debug("claude: %s", line)
                        continue
                    transcript.record("claude_code", message=event)
                    result = self._handle_event(event, proc, tools) or result
                proc.wait()
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            stderr.seek(0)
            errors = stderr.read().strip()

        if result is None:
            raise AgentError(f"claude -p exited (rc={proc.returncode}) without a "
                             f"result: {errors[-1000:]}")
        log.info("Claude Code session ended: %s, %s turns, cost $%s",
                 result.get("subtype"), result.get("num_turns"),
                 result.get("total_cost_usd"))
        if result.get("is_error") or result.get("subtype") != "success":
            if tools.done:
                log.warning("Claude Code ended with %s after the agent finished",
                            result.get("subtype"))
                return
            raise AgentError(f"claude -p ended with {result.get('subtype')}: "
                             f"{str(result.get('result') or result.get('errors'))[:600]}")
        if not tools.done:
            log.warning("Agent stopped without calling finish")


BACKENDS = {ApiRunner.name: ApiRunner, ClaudeCodeRunner.name: ClaudeCodeRunner}


def make_runner(name, model=None, effort="high", max_turns=200,
                claude_bin="claude", tool_timeout_s=21600, usage_limits=None,
                workdir=None, builtin_tools=()):
    if name == ApiRunner.name:
        if builtin_tools:
            raise AgentError("source mode needs Claude Code's file tools; use the "
                             "claude-code backend")
        return ApiRunner(model=model, effort=effort, max_turns=max_turns)
    if name == ClaudeCodeRunner.name:
        return ClaudeCodeRunner(model=model, effort=effort, max_turns=max_turns,
                                claude_bin=claude_bin, tool_timeout_s=tool_timeout_s,
                                usage_limits=usage_limits, workdir=workdir,
                                builtin_tools=builtin_tools)
    raise ValueError(f"unknown agent backend {name!r}; choose from {sorted(BACKENDS)}")

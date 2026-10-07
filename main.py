import base64
import json
import os
import subprocess
import sys
import time

from dotenv import load_dotenv
from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.filters import has_completions
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text

load_dotenv()

# Optional integrations (code graph, Sentry, Freshdesk, logs) live in ./integrations. Without that folder the chat runs as a plain NVIDIA NIM chat.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "integrations"))
try:
    import freshdesk  # noqa: E402  (reads env at import)
    import opensearch  # noqa: E402  (reads env at import)
    import sentry  # noqa: E402  (reads env at import)
    from codegraph import SYSTEM_PROMPT, TOOLS, CodeGraph, run_tool  # noqa: E402
    INTEGRATIONS = True
except ImportError:
    from types import SimpleNamespace
    INTEGRATIONS = False
    _off = dict(ENABLED=False, TOOLS=[], PROMPT="")
    sentry = SimpleNamespace(**_off, ORG="", PROJECTS=[], REDACT=True)
    freshdesk = SimpleNamespace(**_off, DOMAIN="", GROUP_NAME="", WRITE_NOTES=False)
    opensearch = SimpleNamespace(**_off, MODE="off", CONFIRM=None, URL="", PREFIX="", STATS={"sent": 0},
                                 reset_budget=lambda limit: None)
    SYSTEM_PROMPT, TOOLS = "You are a helpful assistant in a terminal chat. Answer in Markdown.", []

    class CodeGraph:
        meta, cross_links = {}, 0

        def load(self, force=False):
            pass

    def run_tool(graph, name, args):
        return f"Unknown tool {name}"
import skills  # noqa: E402
from models import BY_ID, DEFAULT_EFFORT, EFFORTS, MODELS, effort_params  # noqa: E402
from picker import pick  # noqa: E402

skills.install_builtins()
ALL_TOOLS = TOOLS + skills.TOOLS + (sentry.TOOLS if sentry.ENABLED else []) + (freshdesk.TOOLS if freshdesk.ENABLED else [])
LOGS_TOOLS = opensearch.TOOLS if opensearch.ENABLED else []
FULL_PROMPT = f"Today is {time.strftime('%A %Y-%m-%d')}.\n" + SYSTEM_PROMPT + (sentry.PROMPT if sentry.ENABLED else "") + (freshdesk.PROMPT if freshdesk.ENABLED else "") + (opensearch.PROMPT if opensearch.ENABLED else "")

NIM_URL = "https://integrate.api.nvidia.com/v1"
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
client = OpenAI(base_url=NIM_URL, api_key=os.getenv("NVIDIA_API_KEY") or "missing")


def mask(key):
    return f"{key[:8]}…{key[-4:]}" if len(key) > 16 else "…"


def save_env(name, value):
    """Set NAME=value in the project's .env (replace or append on its own line), mode 600."""
    try:
        with open(ENV_FILE) as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []
    lines = [ln for ln in lines if not ln.startswith(f"{name}=")] + [f"{name}={value}"]
    with open(ENV_FILE, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(ENV_FILE, 0o600)


def setup_key(session=None):
    """Ask for an NVIDIA NIM key (hidden), check it against the API, save it to .env and switch to it."""
    global client
    console.print(f"  [dim]Get a key at https://build.nvidia.com (starts with nvapi-). Enter to cancel.[/]")
    ask = PromptSession().prompt  # fresh session: no history file, so the key is never written to disk there
    try:
        key = ask([("class:prompt", "  NVIDIA API key: ")], is_password=True, default="").strip()
    except (KeyboardInterrupt, EOFError):
        key = ""
    if not key:
        note("Key unchanged")
        return False
    test = OpenAI(base_url=NIM_URL, api_key=key, max_retries=1, timeout=20)
    try:
        with console.status("Checking key…", spinner="dots", spinner_style=ACCENT):
            next(iter(test.chat.completions.create(model=DEFAULT_MODEL, messages=[{"role": "user", "content": "hi"}],
                                                     max_tokens=1).choices), None)
    except Exception as e:
        status = getattr(e, "status_code", None)
        if status in (401, 403):
            note(f"Key rejected by NVIDIA ({status}); nothing saved")
            return False
        note(f"Could not verify ({type(e).__name__}); saving anyway")
    save_env("NVIDIA_API_KEY", key)
    os.environ["NVIDIA_API_KEY"] = key
    client = test.with_options(max_retries=2, timeout=None)
    note(f"Key {mask(key)} saved to {ENV_FILE} and in use")
    return True

DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
ACCENT = "#d97757"
# Shift+Tab cycles these, like Claude Code's permission modes
MODES = {
    "default": ("", "Paid log queries follow /logs"),
    "auto": ("⏵⏵ auto mode on", "Paid log queries run without asking (budgeted)"),
    "plan": ("⏸ plan mode on", "Read-only research, ends with a plan; no paid logs, no artifacts"),
}
STATE = {"mode": "default"}
PLAN_NOTE = ("\n\n[Plan mode: research with read-only tools only. Do not query paid logs or create artifacts. "
             "Finish with a short numbered plan of what to check or change next, and stop.]")
SUGGEST = "#b1b9f9"  # Claude CLI's highlight for the selected suggestion
THINKING_TAIL = 6  # lines of live reasoning to show while streaming

COMMANDS = {
    "/models": "List available models (optional filter)",
    "/model": "Pick a model (or /model <id>)",
    "/effort": "Pick thinking effort: off · low · medium · high",
    "/logs": "Backend log queries (paid): ask · auto · off",
    "/think": "Toggle thinking off / back on",
    "/tools": "Toggle code-graph tools on/off",
    "/graph": "Show code graph status (/graph reload to rebuild)",
    "/clear": "New conversation (/clear <name> labels the old one); alias /reset",
    "/resume": "Resume a saved session (or /resume <id|name>)",
    "/rename": "Name this session (no name = auto from the conversation)",
    "/branch": "Copy this conversation into a new session and switch to it",
    "/rewind": "Go back to an earlier message and drop everything after it",
    "/compact": "Summarize the conversation to free context (optional focus)",
    "/context": "Show how much context the conversation uses",
    "/usage": "Token usage this session; aliases /cost, /stats",
    "/status": "Model, effort, tools, logs, session and memory at a glance",
    "/btw": "Side question that is not added to the conversation",
    "/recap": "One-line summary of this session",
    "/copy": "Copy the last reply to the clipboard (/copy N = Nth-latest)",
    "/export": "Save the conversation as text (/export <file>)",
    "/memory": "Edit ~/.nim-chat/MEMORY.md, added to every conversation",
    "/output-style": "Answer style: default · concise · explanatory",
    "/doctor": "Check API keys and connectivity",
    "/key": "Set up or change the NVIDIA NIM API key (saved to .env)",
    "/skills": "List skills (~/.nim-chat/skills); run one with /<skill> <request>",
    "/artifacts": "List HTML pages made with create_artifact (/artifacts N opens one)",
    "/help": "Show commands",
    "/exit": "Quit",
}

SESSIONS_DIR = os.path.expanduser(os.getenv("CHAT_SESSIONS_DIR", "~/.nim-chat/sessions"))
MEMORY_FILE = os.path.expanduser(os.getenv("CHAT_MEMORY_FILE", "~/.nim-chat/MEMORY.md"))
CONTEXT_WINDOW = int(os.getenv("CHAT_CONTEXT_TOKENS", "131072"))  # rough; varies by model
STYLES = {
    "default": "",
    "concise": "\nAnswer style: be brief. Lead with the answer, short bullets, no preamble or recap.",
    "explanatory": "\nAnswer style: explain your reasoning and the why behind code paths, as a teacher would.",
}
USAGE = {"prompt": 0, "completion": 0, "calls": 0}

console = Console()
graph = CodeGraph()
MAX_STEPS = 30  # tool-call rounds per user message


class SlashCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        width = 0
        cmds = {**COMMANDS, **{f"/{n}": "skill · " + v["description"][:60] for n, v in skills.skills().items()}}
        width = max(len(c) for c in cmds) + 4
        for cmd, desc in cmds.items():
            if cmd.startswith(text):
                yield Completion(cmd, start_position=-len(text), display=f"{cmd:<{width}}", display_meta=desc)


def graph_status():
    if not INTEGRATIONS:
        return "Plain chat: no ./integrations folder, so code graph, Sentry, Freshdesk and logs are off"
    parts = [f"{side} {m['nodes']:,} nodes @ {m['gitHead']}" for side, m in graph.meta.items()]
    sentry_state = f"Sentry {sentry.ORG}: {', '.join(sentry.PROJECTS)}" + ("" if sentry.REDACT else " (redaction OFF)") \
        if sentry.ENABLED else "Sentry off (set SENTRY_AUTH_TOKEN, SENTRY_ORG, SENTRY_PROJECT in .env)"
    return ("Code graph: " + " · ".join(parts) + f" · {graph.cross_links} frontend→backend API links\n"
            f"     {sentry_state}\n"
            f"     " + (f"Freshdesk {freshdesk.DOMAIN} · {freshdesk.GROUP_NAME} only" if freshdesk.ENABLED
                        else "Freshdesk off (set FRESHDESK_API_KEY in .env)") + "\n"
            f"     " + (f"Logs {opensearch.URL.split('//')[-1]} ({opensearch.PREFIX}*) · /logs {opensearch.MODE}"
                        if opensearch.ENABLED else "Logs off (set OPENSEARCH_USER / OPENSEARCH_PASSWORD in .env)"))


def welcome(model):
    body = Text.assemble(
        (f"✻ ", ACCENT),
        ("Welcome to Nemotron Chat!\n\n", "bold"),
        ("  /help for commands\n", "dim"),
        (f"  model: {model}\n", "dim"),
        (f"  cwd: {os.getcwd()}", "dim"),
    )
    console.print(Panel(body, border_style=ACCENT, expand=False, padding=(0, 2)))
    console.print(
        "\n [dim]Tips: type / for commands · Ctrl-C interrupts a reply · Ctrl-D quits[/]\n"
    )


def show_help():
    table = Table(show_header=False, box=None, padding=(0, 2))
    for cmd, desc in COMMANDS.items():
        table.add_row(Text(cmd, style=ACCENT), Text(desc, style="dim"))
    console.print(Padding(table, (0, 0, 1, 2)))


def note(msg):
    console.print(f"  [dim]⎿  {msg}[/]\n")


class Tail:
    """Show only the bottom rows of a renderable that fit on screen. Live can't redraw rows
    that have scrolled off the top, so a taller preview leaves duplicated output behind."""

    def __init__(self, renderable, margin=4):
        self.renderable, self.margin = renderable, margin

    def __rich_console__(self, console, options):
        room = max(console.size.height - self.margin, 3)
        lines = console.render_lines(self.renderable, options.update(height=None), pad=False)
        if len(lines) > room:
            lines = [[Segment("  …", RichStyle(dim=True))]] + lines[-(room - 1):]
        for line in lines:
            yield from line
            yield Segment.line()


def render(reasoning, reply, started, done):
    parts = []
    if reasoning:
        elapsed = time.time() - started
        if reply or done:
            parts.append(Text(f"✻ Thought for {elapsed:.0f}s", style=f"dim italic"))
        else:
            tail = "\n".join(reasoning.strip().splitlines()[-THINKING_TAIL:])
            parts.append(Text("✻ Thinking…", style=f"italic {ACCENT}"))
            parts.append(Padding(Text(tail, style="dim italic"), (0, 0, 0, 2)))
    if reply.strip():
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=1)
        grid.add_column()
        grid.add_row(Text("⏺", style="white"), Markdown(reply))
        parts.append(grid)
    if not done and not reasoning and not reply.strip():
        parts.append(Spinner("dots", text=Text("Pondering…", style=ACCENT), style=ACCENT))
    return Group(*parts) if done else Tail(Group(*parts))


def is_transient(err):
    status = getattr(err, "status_code", None)
    text = str(err).lower()
    return status in (429, 500, 502, 503, 504) or "overloaded" in text or "internal server error" in text \
        or type(err).__name__ in ("APIConnectionError", "APITimeoutError")


def stream_turn_with_retry(messages, model, effort, tools_on, attempts=4):
    for attempt in range(attempts):
        try:
            return stream_turn(messages, model, effort, tools_on)
        except Exception as e:
            if attempt == attempts - 1 or not is_transient(e):
                raise
            wait = 2 ** (attempt + 1)
            console.print(f"  [yellow]⎿  {e} — retrying in {wait}s ({attempt + 1}/{attempts - 1})[/]")
            time.sleep(wait)


def stream_turn(messages, model, effort, tools_on):
    """One model call. Streams reasoning/content live; returns (content, tool_calls)."""
    started = time.time()
    reasoning, reply, calls = "", "", {}
    tools = ALL_TOOLS + (LOGS_TOOLS if opensearch.MODE != "off" else [])
    if STATE["mode"] == "plan":
        tools = [t for t in tools if t in ALL_TOOLS and t["function"]["name"] != "create_artifact"]
    kwargs = {"tools": tools, "tool_choice": "auto"} if tools_on else {}
    with Live(render("", "", started, False), console=console, refresh_per_second=15,
              vertical_overflow="crop", transient=True) as live:
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=1,
                top_p=0.95,
                max_tokens=16384,
                stream=True,
                stream_options={"include_usage": True},
                **effort_params(model, effort),
                **kwargs,
            )
            for chunk in completion:
                if getattr(chunk, "usage", None):
                    USAGE["prompt"] += chunk.usage.prompt_tokens or 0
                    USAGE["completion"] += chunk.usage.completion_tokens or 0
                    USAGE["calls"] += 1
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reasoning += getattr(delta, "reasoning_content", None) or ""
                reply += delta.content or ""
                for tc in delta.tool_calls or []:
                    c = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    c["id"] += tc.id or ""
                    if tc.function:
                        c["name"] += tc.function.name or ""
                        c["arguments"] += tc.function.arguments or ""
                live.update(render(reasoning, reply, started, False))
        finally:
            final = render(reasoning, reply, started, True)
    # Live was transient: it cleared its streaming preview; print the full answer once.
    console.print(final)
    return reply, [calls[i] for i in sorted(calls)]


def show_tool_call(name, args, result):
    arg_str = ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items())
    if len(arg_str) > 90:
        arg_str = arg_str[:87] + "…"
    console.print(Text.assemble(("⏺ ", "green"), (name, "bold"), (f"({arg_str})", "dim")))
    lines = result.splitlines()
    preview = lines[0][:110] if lines else ""
    more = f" (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
    console.print(f"  [dim]⎿  {preview}{more}[/]")


def chat(messages, model, effort, tools_on):
    """Agent loop: keeps calling the model until it answers without tool calls.
    Appends everything to messages; on interrupt/error rolls back to where it started."""
    mark = len(messages)
    try:
        for _ in range(MAX_STEPS):
            reply, calls = stream_turn_with_retry(messages, model, effort, tools_on)
            if not calls:
                messages.append({"role": "assistant", "content": reply})
                console.print()
                return
            messages.append({
                "role": "assistant",
                "content": reply or None,
                "tool_calls": [{"id": c["id"] or f"call_{i}", "type": "function",
                                "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                               for i, c in enumerate(calls)],
            })
            for i, c in enumerate(calls):
                try:
                    args = json.loads(c["arguments"] or "{}")
                except json.JSONDecodeError:
                    args, result = {}, f"Invalid JSON arguments: {c['arguments']}"
                else:
                    with console.status(f"Running {c['name']}…", spinner="dots", spinner_style=ACCENT):
                        result = skills.run(c["name"], args) if c["name"] in ("load_skill", "create_artifact") or c["name"] in skills.skills() \
                            else run_tool(graph, c["name"], args)
                show_tool_call(c["name"], args, result)
                messages.append({"role": "tool", "tool_call_id": c["id"] or f"call_{i}", "content": result})
            console.print()
        console.print(f"  [yellow]⎿  Stopped after {MAX_STEPS} tool rounds[/]\n")
    except BaseException:
        del messages[mark:]
        raise


def bottom_toolbar(model, effort, tools_on):
    name = BY_ID[model].name if model in BY_ID else model
    dim, sep = "#6b6b6b", '<style fg="#4a4a4a"> │ </style>'
    effort_fg = {"off": dim, "low": "#7fb3d5", "medium": "#e5c07b", "high": "#e06c75"}.get(effort, dim)
    parts = [f'<style fg="{ACCENT}"><b>🤖 {name}</b></style>',
             f'<style fg="{effort_fg}">🧠 {effort}</style>',
             f'<style fg="{"#98c379" if tools_on else dim}">🕸️ graph {"on" if tools_on else "off"}</style>']
    if opensearch.ENABLED:
        logs = "auto" if STATE["mode"] == "auto" and opensearch.MODE == "ask" else opensearch.MODE
        logs = "off" if STATE["mode"] == "plan" else logs
        logs_fg = {"ask": "#e5c07b", "auto": "#98c379", "off": dim}.get(logs, dim)
        parts.append(f'<style fg="{logs_fg}">📜 logs {logs}</style> <style fg="{dim}">({opensearch.STATS["sent"]} sent)</style>')
    label = MODES[STATE["mode"]][0]
    if label:
        mode_fg = SUGGEST if STATE["mode"] == "auto" else "#48968c"
        parts.insert(0, f'<style fg="{mode_fg}"><b>{label}</b></style>')
    hint = f'<style fg="{dim}">⇧⇥ modes · / commands</style>'
    return HTML(" " + sep.join(parts) + sep + hint)


def confirm_log_query(preview):
    console.print(f"  [yellow]?[/] Paid log query: [bold]{preview}[/]")
    try:
        answer = input("    run it? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    return answer in ("y", "yes")


opensearch.CONFIRM = confirm_log_query


LOG_MODES = {
    "ask": "Ask before every paid log query (default)",
    "auto": f"Query without asking, at most {os.getenv('LOGS_MAX_PER_TURN', '3')} per question",
    "off": "Never query logs; tools hidden from the model",
}


def pick_model(model):
    options = [(m.id, m.name, m.blurb + ("" if m.tools else " · no tools")) for m in MODELS]
    if model not in BY_ID:
        options.insert(0, (model, model, "custom model (set with /model <id>)"))
    return pick("Select model", "Switches from your next message; the conversation is kept.", options, model)


def pick_effort(effort):
    options = [(name, name.capitalize(), desc) for name, (desc, *_rest) in EFFORTS.items()]
    return pick("Thinking effort", "How long the model reasons before answering.", options, effort)


def new_session_id():
    return time.strftime("%Y%m%d-%H%M%S")


def save_session(sid, messages, model, effort, name=None):
    """Write the conversation (minus the system prompt, rebuilt fresh on resume) to SESSIONS_DIR/<sid>.json."""
    if len(messages) < 2:
        return
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    title = next((m["content"] for m in messages if m["role"] == "user"), "")
    data = {"id": sid, "updated": time.strftime("%Y-%m-%d %H:%M"), "model": model, "effort": effort,
            "name": name, "title": name or " ".join(title.split())[:80], "messages": messages[1:]}
    tmp = os.path.join(SESSIONS_DIR, f".{sid}.tmp")
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, os.path.join(SESSIONS_DIR, f"{sid}.json"))


def list_sessions():
    try:
        names = [f for f in os.listdir(SESSIONS_DIR) if f.endswith(".json")]
    except OSError:
        return []
    out = []
    for f in names:
        try:
            with open(os.path.join(SESSIONS_DIR, f)) as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda d: d.get("updated", ""), reverse=True)


def pick_session(current):
    sessions = list_sessions()
    if not sessions:
        return None
    turns = lambda d: sum(m["role"] == "user" for m in d["messages"])
    options = [(d["id"], d["title"] or "(empty)", f"{d['updated']} · {turns(d)} msgs · {d['id']}") for d in sessions[:20]]
    chosen = pick("Resume session", f"Saved in {SESSIONS_DIR}", options, current)
    return next((d for d in sessions if d["id"] == chosen), None)


def replay(messages):
    """Show the resumed conversation's user and assistant text (tool output is skipped)."""
    for m in messages:
        if m["role"] == "user":
            console.print(f"[{ACCENT} bold]> [/]{m['content']}")
        elif m["role"] == "assistant" and m.get("content") and not m.get("tool_calls"):
            console.print(Padding(Markdown(m["content"]), (0, 0, 1, 2)))


def system_prompt(style):
    try:
        with open(MEMORY_FILE) as fh:
            mem = fh.read().strip()
    except OSError:
        mem = ""
    return FULL_PROMPT + skills.prompt() + (f"\n\nUser memory (from {MEMORY_FILE}):\n{mem}" if mem else "") + STYLES[style]


def quick(messages, model, ask, max_tokens=1024):
    """One non-streaming, tool-free call on a copy of the conversation (for /btw, /recap, /compact, /rename)."""
    msgs = [m for m in messages if m["role"] in ("system", "user") or (m["role"] == "assistant" and not m.get("tool_calls"))]
    with console.status("Thinking…", spinner="dots", spinner_style=ACCENT):
        r = client.chat.completions.create(model=model, messages=msgs + [{"role": "user", "content": ask}],
                                           temperature=0.3, max_tokens=max_tokens, **effort_params(model, "off"))
    if r.usage:
        USAGE["prompt"] += r.usage.prompt_tokens or 0
        USAGE["completion"] += r.usage.completion_tokens or 0
        USAGE["calls"] += 1
    return (r.choices[0].message.content or "").strip()


def est_tokens(messages):
    return sum(len(json.dumps(m)) for m in messages) // 4


def assistant_texts(messages):
    return [m["content"] for m in messages if m["role"] == "assistant" and m.get("content") and not m.get("tool_calls")]


def transcript(messages):
    out = []
    for m in messages[1:]:
        if m["role"] == "user":
            out.append(f"> {m['content']}\n")
        elif m["role"] == "assistant" and m.get("tool_calls"):
            out += [f"[tool] {c['function']['name']}({c['function']['arguments']})" for c in m["tool_calls"]]
        elif m["role"] == "assistant" and m.get("content"):
            out.append(m["content"] + "\n")
        elif m["role"] == "tool":
            out.append(f"[result] {m['content'][:300]}\n")
    return "\n".join(out)


def copy_to_clipboard(text):
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]):
        try:
            subprocess.run(cmd, input=text.encode(), check=True, timeout=5)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    # OSC 52: most modern terminals put this on the system clipboard
    sys.stdout.write(f"\033]52;c;{base64.b64encode(text.encode()).decode()}\a")
    sys.stdout.flush()
    return False


def doctor(model):
    rows = []
    try:
        next(iter(client.models.list()), None)
        rows.append(("NVIDIA NIM", "ok", model))
    except Exception as e:
        rows.append(("NVIDIA NIM", "fail", str(e)[:80]))
    if sentry.ENABLED:
        try:
            sentry._ids(sentry.PROJECTS)
            rows.append(("Sentry", "ok", sentry.ORG))
        except Exception as e:
            rows.append(("Sentry", "fail", str(e)[:80]))
    else:
        rows.append(("Sentry", "off", "no token/org in .env"))
    if freshdesk.ENABLED:
        try:
            freshdesk._get("/ticket_fields")
            rows.append(("Freshdesk", "ok", freshdesk.DOMAIN + (" · notes WRITE on" if freshdesk.WRITE_NOTES else " · read-only")))
        except Exception as e:
            rows.append(("Freshdesk", "fail", str(e)[:80]))
    else:
        rows.append(("Freshdesk", "off", "no FRESHDESK_API_KEY"))
    rows.append(("Logs", "on" if opensearch.ENABLED else "off", f"{opensearch.URL} · /logs {opensearch.MODE} (not queried: paid)"))
    rows.append(("Code graph", "ok" if graph.meta else "empty", ", ".join(graph.meta) or "run /graph reload"))
    rows.append(("Sessions", "ok", SESSIONS_DIR))
    t = Table(show_header=False, box=None, padding=(0, 2))
    for name, state, detail in rows:
        t.add_row(name, Text(state, style={"ok": "green", "fail": "red"}.get(state, "yellow")), Text(detail, style="dim"))
    console.print(Padding(t, (0, 0, 1, 2)))


def main():
    style = "default"
    messages = [{"role": "system", "content": system_prompt(style)}]
    name = None
    pending = ""  # text pre-filled into the next prompt (after /rewind)
    started_at = time.time()
    effort = DEFAULT_EFFORT
    last_effort = DEFAULT_EFFORT  # what /think turns back on
    tools_on = True
    model = DEFAULT_MODEL
    sid = new_session_id()

    welcome(model)
    if not os.getenv("NVIDIA_API_KEY"):
        console.print("  [yellow]No NVIDIA_API_KEY yet.[/]")
        if not setup_key():
            console.print("  [dim]Run /key any time to add it.[/]\n")
    with console.status("Loading code graph…", spinner="dots", spinner_style=ACCENT):
        graph.load()
    note(graph_status())
    kb = KeyBindings()

    @kb.add("s-tab", filter=~has_completions)
    def _(event):
        names = list(MODES)
        STATE["mode"] = names[(names.index(STATE["mode"]) + 1) % len(names)]
        event.app.invalidate()

    session = PromptSession(
        key_bindings=kb,
        history=FileHistory(os.path.expanduser("~/.nemotron_chat_history")),
        completer=SlashCompleter(),
        complete_while_typing=True,
        reserve_space_for_menu=10,
        style=Style.from_dict({
            "prompt": f"{ACCENT} bold",
            "bottom-toolbar": "noreverse #888888",
            # Claude CLI look: no menu background, plain rows, the selected row in the suggestion colour
            "completion-menu": "bg:default noreverse",
            "completion-menu.completion": "bg:default #ffffff",
            "completion-menu.completion.current": f"bg:default {SUGGEST} noreverse",
            "completion-menu.meta.completion": "bg:default #888888",
            "completion-menu.meta.completion.current": f"bg:default {SUGGEST} noreverse",
            "completion-menu.multi-column-meta": "bg:default #888888",
            "scrollbar.background": "bg:default",
            "scrollbar.button": "bg:default",
        }),
    )

    while True:
        console.rule(style="grey35")
        try:
            user = session.prompt(
                [("class:prompt", "> ")],
                bottom_toolbar=lambda: bottom_toolbar(model, effort, tools_on),
                default=pending,
            ).strip()
            pending = ""
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        console.rule(style="grey35")
        console.print()
        if not user:
            continue

        if user in ("/exit", "/quit"):
            break
        if user == "/help":
            show_help()
            continue
        cmd, _, arg = user.partition(" ")
        arg = arg.strip()
        if cmd in ("/clear", "/reset"):
            save_session(sid, messages, model, effort, arg or name)
            del messages[1:]
            sid, name = new_session_id(), None
            note("Conversation cleared" + (f" · previous saved as {arg!r}" if arg else ""))
            continue
        if cmd == "/rename":
            if len(messages) < 2 and not arg:
                note("Nothing to name yet")
                continue
            name = arg or quick(messages, model, "Give this conversation a 3-6 word title. Reply with the title only.", 40).strip('"\' .')
            save_session(sid, messages, model, effort, name)
            note(f"Session named {name!r}")
            continue
        if cmd == "/branch":
            save_session(sid, messages, model, effort, name)
            old = sid
            sid = new_session_id() + "-b"
            name = arg or (f"{name} (branch)" if name else None)
            save_session(sid, messages, model, effort, name)
            note(f"Branched to {sid}; the original stays as {old} (/resume {old})")
            continue
        if cmd == "/rewind":
            users = [i for i, m in enumerate(messages) if m["role"] == "user"]
            if not users:
                note("Nothing to rewind")
                continue
            opts = [(i, " ".join(messages[i]["content"].split())[:70], f"message {n + 1}") for n, i in enumerate(users)][-20:][::-1]
            i = pick("Rewind", "Drop this message and everything after it; its text is put back in the prompt.", opts)
            if i is None:
                note("Kept conversation")
                continue
            pending = messages[i]["content"]
            del messages[i:]
            save_session(sid, messages, model, effort, name)
            note("Rewound; edit and resend the message, or clear the prompt")
            continue
        if cmd == "/compact":
            if len(messages) < 3:
                note("Nothing to compact")
                continue
            before = est_tokens(messages)
            summary = quick(messages, model, "Summarize this conversation so it can continue without the full history: "
                            "goals, key findings (with file paths, ticket ids, Sentry ids), decisions, open questions. "
                            + (f"Focus: {arg}" if arg else ""), 4096)
            messages[1:] = [{"role": "user", "content": "Summary of our conversation so far:\n" + summary},
                            {"role": "assistant", "content": "Understood, continuing from that summary."}]
            save_session(sid, messages, model, effort, name)
            note(f"Compacted ~{before:,} → ~{est_tokens(messages):,} tokens")
            continue
        if cmd == "/context":
            parts = {"system": est_tokens(messages[:1]),
                     "messages": est_tokens([m for m in messages[1:] if m["role"] != "tool" and not m.get("tool_calls")]),
                     "tool calls/results": est_tokens([m for m in messages[1:] if m["role"] == "tool" or m.get("tool_calls")])}
            total = sum(parts.values())
            bar = int(40 * min(total / CONTEXT_WINDOW, 1))
            console.print(f"  [{ACCENT}]{'█' * bar}[/][dim]{'░' * (40 - bar)}[/]  ~{total:,} / {CONTEXT_WINDOW:,} tokens ({total / CONTEXT_WINDOW:.0%})")
            for k, v in parts.items():
                console.print(f"  [dim]{k:<20}~{v:,}[/]")
            note("Estimate (chars/4). /compact frees context")
            continue
        if cmd in ("/usage", "/cost", "/stats"):
            mins = (time.time() - started_at) / 60
            note(f"{USAGE['calls']} model calls · {USAGE['prompt']:,} input + {USAGE['completion']:,} output tokens · "
                 f"{sum(m['role'] == 'user' for m in messages)} messages · {mins:.0f} min. NVIDIA NIM free tier: no $ cost shown")
            continue
        if cmd == "/status":
            info = BY_ID.get(model)
            note(f"Model {info.name if info else model} · effort {effort} · style {style} · tools {'on' if tools_on else 'off'} · "
                 f"logs {opensearch.MODE}\n     Session {sid}{f' ({name})' if name else ''} · {SESSIONS_DIR}\n     "
                 f"Memory {MEMORY_FILE} ({'present' if os.path.exists(MEMORY_FILE) else 'none'}) · cwd {os.getcwd()}")
            continue
        if cmd == "/btw":
            if not arg:
                note("Usage: /btw <question>")
                continue
            console.print(Padding(Markdown(quick(messages, model, arg, 2048)), (0, 0, 1, 2)))
            note("Side answer, not added to the conversation")
            continue
        if cmd == "/recap":
            if len(messages) < 2:
                note("Nothing to recap")
                continue
            note(quick(messages, model, "One sentence: what has this session been about and where does it stand?", 120))
            continue
        if cmd == "/copy":
            texts = assistant_texts(messages)
            n = int(arg) if arg.isdigit() else 1
            if not 1 <= n <= len(texts):
                note("No such reply")
                continue
            ok = copy_to_clipboard(texts[-n])
            note("Copied" if ok else "Sent to the terminal clipboard (install wl-clipboard or xclip if it didn't arrive)")
            continue
        if cmd == "/export":
            path = os.path.expanduser(arg or f"nim-chat-{sid}.txt")
            with open(path, "w") as fh:
                fh.write(transcript(messages))
            note(f"Exported to {os.path.abspath(path)}")
            continue
        if cmd == "/memory":
            os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
            if not os.path.exists(MEMORY_FILE):
                with open(MEMORY_FILE, "w") as fh:
                    fh.write("# Notes added to every Nemotron chat\n\n")
            subprocess.call([os.getenv("EDITOR") or "nano", MEMORY_FILE])
            messages[0] = {"role": "system", "content": system_prompt(style)}
            note(f"Memory reloaded from {MEMORY_FILE}")
            continue
        if cmd == "/output-style":
            chosen = arg or pick("Output style", "How answers are written.",
                                 [(k, k, v.strip() or "No extra instruction") for k, v in STYLES.items()], style)
            if chosen in STYLES:
                style = chosen
                messages[0] = {"role": "system", "content": system_prompt(style)}
                note(f"Output style {style}")
            elif chosen:
                note("Use default, concise or explanatory")
            continue
        if cmd == "/key":
            if os.getenv("NVIDIA_API_KEY"):
                note(f"Current key {mask(os.environ['NVIDIA_API_KEY'])}")
            setup_key(session)
            continue
        if cmd == "/doctor":
            doctor(model)
            continue
        if user == "/resume" or user.startswith("/resume "):
            arg = user[len("/resume"):].strip()
            if arg:
                d = next((d for d in list_sessions() if arg in (d["id"], d.get("name"))), None)
            else:
                d = pick_session(sid)
            if d is None:
                note(f"No session {arg!r}" if arg else f"No saved sessions in {SESSIONS_DIR}" if not list_sessions() else "Kept current session")
                continue
            save_session(sid, messages, model, effort, name)
            sid, model, effort, name = d["id"], d.get("model", model), d.get("effort", effort), d.get("name")
            messages[:] = [{"role": "system", "content": system_prompt(style)}] + d["messages"]
            replay(messages[1:])
            note(f"Resumed {d['title']!r} ({sid}) on {BY_ID[model].name if model in BY_ID else model}")
            continue
        if user == "/tools":
            tools_on = not tools_on
            note(f"Code-graph tools {'on' if tools_on else 'off'}")
            continue
        if user == "/graph" or user == "/graph reload":
            if user.endswith("reload"):
                with console.status("Rebuilding code graph…", spinner="dots", spinner_style=ACCENT):
                    graph.__init__()
                    graph.load(force=True)
            note(graph_status())
            continue
        if user == "/logs" or user.startswith("/logs "):
            arg = user[len("/logs"):].strip().lower()
            chosen = arg or pick("Backend logs", "Each OpenSearch query is paid.",
                                 [(k, k, v) for k, v in LOG_MODES.items()], opensearch.MODE)
            if chosen in LOG_MODES:
                opensearch.MODE = chosen
                note(f"Logs {chosen} — {LOG_MODES[chosen]}")
            elif chosen:
                note("Use /logs ask, /logs auto or /logs off")
            continue
        if user == "/think":
            effort = last_effort if effort == "off" else "off"
            note(f"Effort {effort}")
            continue
        if user == "/effort" or user.startswith("/effort "):
            arg = user[len("/effort"):].strip().lower()
            chosen = arg if arg else pick_effort(effort)
            if chosen is None:
                note("Effort unchanged")
            elif chosen not in EFFORTS:
                note(f"Unknown effort {arg!r} — use off, low, medium or high")
            else:
                effort = chosen
                if effort != "off":
                    last_effort = effort
                note(f"Effort {effort} — {EFFORTS[effort][0]}")
            continue
        if user == "/models" or user.startswith("/models "):
            query = user[len("/models"):].strip().lower()
            try:
                with console.status("Fetching models…", spinner="dots", spinner_style=ACCENT):
                    ids = sorted(m.id for m in client.models.list() if query in m.id.lower())
            except Exception as e:
                console.print(f"  [red]⎿  {e}[/]\n")
                continue
            for i in ids:
                if i == model:
                    console.print(f"  [{ACCENT}]● {i}[/]")
                else:
                    console.print(f"  [dim]  {i}[/]")
            note(f"{len(ids)} models")
            continue
        if user == "/model" or user.startswith("/model "):
            new = user[len("/model"):].strip() or pick_model(model)
            if new is None or new == model:
                note(f"Kept {BY_ID[model].name if model in BY_ID else model}")
            else:
                model = new
                info = BY_ID.get(model)
                note(f"Switched to {info.name} ({model})" if info else f"Switched to {model}")
                if info and not info.tools and tools_on:
                    note("This model skipped tool calls in testing; code-graph/Sentry tools may not run")
            continue
        if cmd == "/skills":
            found = skills.skills()
            for n, v in found.items():
                console.print(f"  [{ACCENT}]/{n}[/]  [dim]{v['description'][:100]}[/]")
            note(f"{len(found)} skills in {skills.SKILLS_DIR} · add a folder with SKILL.md to make one")
            continue
        if cmd == "/artifacts":
            try:
                pages = sorted((os.path.join(skills.ARTIFACTS_DIR, f) for f in os.listdir(skills.ARTIFACTS_DIR) if f.endswith(".html")),
                               key=os.path.getmtime, reverse=True)
            except OSError:
                pages = []
            if arg.isdigit() and 1 <= int(arg) <= len(pages):
                subprocess.Popen(["xdg-open", pages[int(arg) - 1]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                note(f"Opened {pages[int(arg) - 1]}")
                continue
            for i, f in enumerate(pages[:20], 1):
                console.print(f"  [dim]{i:>2}.[/] {os.path.basename(f)}  [dim]{time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(f)))}[/]")
            note(f"{len(pages)} pages in {skills.ARTIFACTS_DIR}" + (" · /artifacts N to open" if pages else ""))
            continue
        found = skills.skills().get(cmd[1:]) if user.startswith("/") else None
        if found:
            if not tools_on:
                tools_on = True
                note("Tools turned on for this skill")
            page = skills.skills().get("html-artifact") if cmd[1:] != "html-artifact" else None
            user = (f"Use the {cmd[1:]} skill. Its instructions:\n\n{found['body']}\n\n"
                    + (f"Deliver it as one HTML page with the html-artifact skill (its contract wins over any "
                       f"React/npm advice above; build now, don't recommend a kit):\n\n{page['body']}\n\n" if page else "")
                    + f"Request: {arg or 'ask me what I want'}")
            note(f"Running skill {cmd[1:]}")
        elif user.startswith("/"):
            note(f"Unknown command {user.split()[0]} — try /help")
            continue

        mode = STATE["mode"]
        messages.append({"role": "user", "content": user + (PLAN_NOTE if mode == "plan" else "")})
        saved_logs = opensearch.MODE
        if mode == "auto" and opensearch.MODE == "ask":
            opensearch.MODE = "auto"
        opensearch.reset_budget(int(os.getenv("LOGS_MAX_PER_TURN", "3")) if opensearch.MODE == "auto" else None)
        try:
            chat(messages, model, effort, tools_on)
            save_session(sid, messages, model, effort, name)
        except KeyboardInterrupt:
            console.print("  [dim]⎿  Interrupted[/]\n")
            messages.pop()
        except Exception as e:
            console.print(f"  [red]⎿  Error: {e}[/]\n")
            messages.pop()
        finally:
            opensearch.MODE = saved_logs

    if len(messages) > 1:
        save_session(sid, messages, model, effort, name)
        console.print(f"[dim]Session saved · /resume {sid}[/]")
    console.print("[dim]Bye![/]")


if __name__ == "__main__":
    main()

"""Claude-style skills for the chat, plus the create_artifact tool that HTML skills use.

A skill is a folder ~/.nim-chat/skills/<name>/SKILL.md with frontmatter:

    ---
    name: html-artifact
    description: one line telling the model when to use it
    ---
    instructions…

The model sees every skill's name + description and loads the full text with the load_skill
tool when a task matches; the user can also run one directly with /<name> <request>.
create_artifact saves an HTML page to ~/.nim-chat/artifacts/ and opens it in the browser.
"""

import os
import re
import subprocess
import time

SKILLS_DIR = os.path.expanduser(os.getenv("CHAT_SKILLS_DIR", "~/.nim-chat/skills"))
ARTIFACTS_DIR = os.path.expanduser(os.getenv("CHAT_ARTIFACTS_DIR", "~/.nim-chat/artifacts"))
MAX_HTML = 2_000_000

BUILTIN = {"html-artifact": """---
name: html-artifact
description: Build a self-contained HTML page (report, dashboard, explainer, chart, tool, prototype) and open it in the browser. Use whenever the user asks for an artifact, page, dashboard, report, visual, chart or HTML.
---
# HTML artifact

Deliver the page by calling the create_artifact tool with a short title and the complete HTML. Never paste the HTML in chat.

## Contract
- One file: `<!doctype html>`, `<meta charset="utf-8">`, `<meta name="viewport" content="width=device-width, initial-scale=1">`, a `<title>` of 2-4 words.
- Inline CSS and JS. External scripts only from cdn.jsdelivr.net/npm or cdnjs.cloudflare.com (e.g. Chart.js); fonts only from Google Fonts. No other network calls.
- Works offline-ish: if a CDN fails the text content must still read.
- No secrets, tokens, emails or phone numbers in the page; use redacted values from tools as they are.

## Design
- Colours as CSS variables on :root, with a dark variant under `@media (prefers-color-scheme: dark)`. Set an explicit body background and text colour.

## Dark mode (required, the tool refuses a page without it)
Start the <style> from this and use only the variables for every colour (no hard-coded #fff/#000, no Tailwind bg-white/text-black without a dark: pair):
```css
:root { color-scheme: light dark;
  --bg: #fafaf9; --surface: #ffffff; --border: #e7e5e4; --text: #1c1917; --muted: #78716c;
  --accent: #d97757; --good: #16a34a; --warn: #d97706; --bad: #dc2626; }
@media (prefers-color-scheme: dark) { :root {
  --bg: #0c0a09; --surface: #1c1917; --border: #292524; --text: #f5f5f4; --muted: #a8a29e;
  --accent: #e8916f; --good: #4ade80; --warn: #fbbf24; --bad: #f87171; } }
body { background: var(--bg); color: var(--text); }
```
- Cards, tables, inputs: `background: var(--surface); border: 1px solid var(--border)`.
- Charts (Chart.js etc.) do not read CSS: get colours in JS with `getComputedStyle(document.documentElement).getPropertyValue('--text').trim()` for ticks, labels, legend and grid (`--border`), and redraw on `matchMedia('(prefers-color-scheme: dark)').addEventListener('change', …)`.
- Inline SVG: `fill="currentColor"` or `var(--…)`, never black.
- Before calling create_artifact, check every colour in the page against both modes.
- System font stack or one Google font; body 15-16px, line-height 1.5; max content width ~960px, 16px side gutter, no horizontal scroll on a phone.
- Lead with the answer: a one-sentence headline finding, then the supporting tables/charts. No "This page shows…" intros.
- Tables: header row, numbers right-aligned with units in the header, sorted by what the reader scans for.
- Charts: one accent colour, others muted; label axes with units; a line for change over time, bars for comparing items; no 3D, no pie with more than 4 slices.
- One accent colour for emphasis; status colours (green/amber/red) only when they mean status.
- Plain words, specific numbers, ticket/issue ids and file paths where they are the evidence.

## Data
- Use only facts from the conversation or from tool results; never invent numbers. A missing value is shown as an open question, not a guess.
- Embed data as a JS const near the top so it is easy to edit.

## With a design skill
Design skills (design-taste-frontend, minimalist-ui, high-end-visual-design, industrial-brutalist-ui, gpt-taste, redesign-existing-projects) were written for React/Next projects. Here, keep this page contract (one HTML file via create_artifact) and apply their taste rules: typography, colour, spacing, layout, motion, banned "AI tells". Swap npm packages for CDN builds (Tailwind: cdn.jsdelivr.net/npm/@tailwindcss/browser@4; icons: cdn.jsdelivr.net/npm/lucide). Skip install steps and project scaffolding.

## After creating
Reply with one line: what the page shows and its path. To change it, call create_artifact again with the same title (it overwrites).
"""}

TOOLS = [
    {"type": "function", "function": {
        "name": "load_skill",
        "description": "Load the full instructions of a skill by name. Call this before doing a task that matches a skill's description.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "create_artifact",
        "description": "Save a complete self-contained HTML page to ~/.nim-chat/artifacts and open it in the browser. "
                       "Same title again = overwrite (update) that page.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "2-4 word page name, also used for the file name"},
            "html": {"type": "string", "description": "The full HTML document, starting with <!doctype html>"}},
            "required": ["title", "html"]}}},
]


def install_builtins():
    for name, text in BUILTIN.items():
        path = os.path.join(SKILLS_DIR, name, "SKILL.md")
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(text)


def _parse(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    meta, body = ({}, text) if not m else ({}, m.group(2))
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if v:
                meta[k.strip()] = v.strip()
    return meta, body.strip()


def skills():
    """{name: {"description", "body", "path"}} for every SKILL.md under SKILLS_DIR."""
    out = {}
    try:
        names = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return out
    for d in names:
        path = os.path.join(SKILLS_DIR, d, "SKILL.md")
        try:
            with open(path) as fh:
                meta, body = _parse(fh.read())
        except OSError:
            continue
        name = meta.get("name", d)
        out[name] = {"description": meta.get("description", ""), "body": body, "path": path}
    return out


def prompt():
    s = skills()
    if not s:
        return ""
    rows = "\n".join(f"- {n}: {v['description']}" for n, v in s.items())
    return ("\n\nSkills: packaged instructions. When a request matches one, call load_skill(name) first and follow it.\n"
            + rows + "\nIn this chat the only output for a page is create_artifact (one HTML file): for any design skill, "
            "also follow html-artifact.")


def _slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or time.strftime("page-%Y%m%d-%H%M%S")


def create_artifact(title, html):
    if not html.lstrip().lower().startswith(("<!doctype", "<html")):
        return "Refused: html must be a full document starting with <!doctype html>."
    low = html.lower()
    if "prefers-color-scheme" not in low and "dark:" not in low:
        return ("Refused: no dark mode. Add the --bg/--surface/--text variables with a "
                "@media (prefers-color-scheme: dark) block (see html-artifact skill) and call again.")
    if len(html) > MAX_HTML:
        return f"Refused: page is {len(html):,} chars, limit {MAX_HTML:,}."
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    path = os.path.join(ARTIFACTS_DIR, _slug(title) + ".html")
    existed = os.path.exists(path)
    with open(path, "w") as fh:
        fh.write(html)
    try:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        opened = "opened in browser"
    except OSError:
        opened = "open it manually"
    return f"{'Updated' if existed else 'Created'} {path} ({len(html):,} chars), {opened}."


def run(name, args):
    if name == "load_skill":
        s = skills().get(args.get("name", ""))
        return s["body"] if s else f"No skill {args.get('name')!r}. Available: {', '.join(skills()) or 'none'}"
    if name == "create_artifact":
        return create_artifact(args.get("title", "page"), args.get("html", ""))
    if name in skills():  # model called a skill as if it were a tool
        return skills()[name]["body"]
    return f"Unknown tool {name}"

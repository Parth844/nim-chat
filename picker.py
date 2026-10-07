"""Inline Claude-style picker: ↑/↓ or number keys to move, Enter to choose, Esc to cancel."""

import shutil

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

STYLE = Style.from_dict({
    "title": "bold",
    "hint": "#888888",
    "cursor": "#d97757 bold",
    "selected": "#d97757 bold",
    "label": "",
    "desc": "#888888",
    "current": "#5fd75f",
})


def pick(title, subtitle, options, current=None):
    """options: list of (value, label, description). Returns the chosen value or None."""
    index = next((i for i, (v, _, _) in enumerate(options) if v == current), 0)
    num_w = len(str(len(options))) + 2  # "10. "
    width = max(len(label) for _, label, _ in options) + num_w + 2

    def lines():
        out = [("class:title", f" {title}\n"), ("class:hint", f" {subtitle}\n\n")]
        for i, (value, label, desc) in enumerate(options):
            on = i == index
            out.append(("class:cursor", " ❯ " if on else "   "))
            out.append(("class:selected" if on else "class:label", f"{f'{i + 1}.':<{num_w}}{label}".ljust(width)))
            out.append(("class:current", " ✔ " if value == current else "   "))
            room = shutil.get_terminal_size().columns - 3 - width - 3 - 1
            if len(desc) > room:
                desc = desc[: max(room - 1, 10)] + "…"
            out.append(("class:desc", f"{desc}\n"))
        out.append(("class:hint", "\n Enter to confirm · Esc to cancel"))
        return out

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _(event):
        nonlocal index
        index = (index - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    @kb.add("tab")
    def _(event):
        nonlocal index
        index = (index + 1) % len(options)

    for n in range(1, min(len(options), 9) + 1):
        @kb.add(str(n))
        def _(event, n=n):
            nonlocal index
            index = n - 1

    @kb.add("enter")
    def _(event):
        event.app.exit(result=options[index][0])

    @kb.add("escape")
    @kb.add("c-c")
    @kb.add("q")
    def _(event):
        event.app.exit(result=None)

    app = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(lines), wrap_lines=True)])),
        key_bindings=kb, style=STYLE, full_screen=False, erase_when_done=True, mouse_support=False,
    )
    return app.run()

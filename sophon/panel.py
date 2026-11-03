"""SOPHON // SWARM CONTROL: the live control panel for a swarm run (Textual).

Started by `swarm panel` (or `swarm go`). Reads the run folder from SWARM_RUN, drives the run only through the
`swarm` CLI as the human, so every action lands on the board like any other message.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Markdown,
    ProgressBar,
    RichLog,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
)

from . import api as sw
from . import openrouter as OR
from .config import HOST_LABEL, OLLAMA_CPU_URL, OLLAMA_URL, RUNS, SSH_HOST, SWARM

RUN = sw.run_dir()
ME = SWARM
ENV = dict(os.environ, SWARM_ID="human", SWARM_RUN=str(RUN))

ACCENT, AMBER, RED, GREEN, PURPLE, DIM = "#4fd1ff", "#ffb347", "#ff5f5f", "#5af78e", "#c792ea", "#5a5a5a"
PHANTOM = "#ff5c8a"
MODE_COLOUR = {"claude": PURPLE, "light": ACCENT, "heavy": AMBER, "local": GREEN, "phantom": PHANTOM}
MODE_SHORT = {"claude": "CLAUDE", "light": "LIGHT", "heavy": "HEAVY", "local": "LOCAL", "phantom": "PHANTOM"}
STATE_COLOUR = {"working": GREEN, "idle": "#9a9a9a", "dialog": AMBER, "dead": RED, "intake": ACCENT}
SENDER_COLOURS = [ACCENT, AMBER, PURPLE, GREEN, "#ff8fab", "#7aa2f7", "#e0af68", "#9ece6a", "#bb9af7", "#2ac3de"]
LOGO = ("█▀▀ █▀█ █▀█ █ █ █▀█ █▄ █", "▄▄█ █▄█ █▀▀ █▀█ █▄█ █ ▀█")
BOX = re.compile(r"^[\s┃│╹▀▣⬝■─━═╭╮╰╯┌┐└┘|>=]*$")


def swarm(*args, timeout=120):
    r = subprocess.run([ME, *args], capture_output=True, text=True, env=ENV, timeout=timeout)
    lines = (r.stdout or r.stderr).strip().splitlines()
    return lines[-1] if lines else ""


PROBLEM = re.compile(r"Traceback|Error|error:|failed|refused|timed? ?out|waits on a permission|not answered|"
                     r"cancelled a stuck|STOP set", re.I)


def board_kind(line):
    if "REPORT:" in line:
        return "report"
    if "memo-request" in line or "memo request" in line:
        return "memo"
    if PROBLEM.search(line):
        return "problem"
    if re.match(r"\[\d\d:\d\d:\d\d\] human\b", line):
        return "human"
    return "other"


def pill(text, colour, fg="black"):
    return Text(f" {text} ", style=f"bold {fg} on {colour}")


def fmt_age(seconds):
    s = int(max(0, seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def sender_colour(name):
    if name == sw.LEAD:
        return ACCENT
    if name == "human":
        return "#ffffff"
    if name in ("panel", "swarm"):
        return DIM
    return SENDER_COLOURS[sum(map(ord, name)) % len(SENDER_COLOURS)]


class Confirm(ModalScreen[bool]):
    """A yes/no question for actions that cost money, take hours or send data outside the user's machines."""

    DEFAULT_CSS = f"""
    Confirm {{ align: center middle; background: #000000 70%; }}
    #dlg {{ width: 64; height: auto; padding: 1 2; border: heavy {AMBER}; background: #0b0700; }}
    #dlg-text {{ height: auto; margin: 0 0 1 0; }}
    #dlg-row {{ height: 1; }}
    #dlg-row Button {{ width: 1fr; margin: 0 1 0 0; }}
    #dlg-yes {{ background: {AMBER}; color: #000000; }}
    """
    BINDINGS = [Binding("y", "answer(True)", "yes"), Binding("n,escape", "answer(False)", "no")]

    def __init__(self, title, body):
        super().__init__()
        self.t, self.b = title, body

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Static(Text.assemble((self.t + "\n\n", f"bold {AMBER}"), (self.b, "#d0d0d0")), id="dlg-text")
            with Horizontal(id="dlg-row"):
                yield Button("YES  (y)", id="dlg-yes")
                yield Button("NO  (n / esc)", id="dlg-no")

    @on(Button.Pressed)
    def _pressed(self, ev):
        self.dismiss(ev.button.id == "dlg-yes")

    def action_answer(self, yes):
        self.dismiss(yes)


CONFIRM = {
    "mode:claude": ("Switch to CLAUDE mode?", "Every agent runs on Claude: the most tokens of any mode (about 200 to "
                    "250K fresh tokens per run in the tests) for no measured gain over LIGHT."),
    "mode:phantom": ("Switch to PHANTOM mode?", "Big models streamed from disk: 2.5 to 3.7 hours per run, the model host's RAM "
                     "held the whole time, scored 5 to 5.5 in tests. Experimental."),
    "or:paid": ("Allow PAID OpenRouter models?", "Paid models only when Sophon needs one, inside the run cap and the "
                "day cap, and never above the price ceiling. This spends real credit."),
    "pol:allow": ("Let ANY OpenRouter provider see prompts?", "More free models answer, but some providers store or "
                  "train on what they receive. The profile is already cut to judging rules; the brief and job text "
                  "still go out."),
}


class AgentCard(Static):
    """One agent: identity, engine, live state and the last lines of its terminal. Click to bring it forward."""

    def __init__(self, name):
        super().__init__(id=f"card-{name}")
        self.agent = name
        self.border_title = name.upper()

    def show(self, a, state, tail, sent, out_kb):
        for s in STATE_COLOUR:
            self.set_class(s == state, s)
        self.set_class(self.agent in sw.LEADS, "lead")
        engine = a.get("engine", "claude")
        model = (a.get("model") or "default")
        role = a.get("persona", "")
        role = role if len(role) <= 18 else "custom role"
        head = Text()
        head.append_text(pill(state.upper() if state != "dialog" else "NEEDS YOU", STATE_COLOUR[state]))
        head.append("  ")
        if engine == "intake":
            head.append("  triage, clarifying questions, mode choice", style="#8a8a8a")
        else:
            is_or = str(a.get("model", "")).startswith("or:")
            head.append_text(pill("OPENROUTER" if is_or else {"local": HOST_LABEL, "local-lead": f"{HOST_LABEL} LEAD"}.get(
                engine, engine.upper()), PURPLE if engine == "claude" else AMBER if is_or else GREEN))
            head.append(f" {model}", style="bold #d0d0d0")
        head.append(f"\n{role}", style="#8a8a8a")
        head.append(f"   up {fmt_age(time.time() - a.get('started', time.time()))}", style=DIM)
        head.append(f"   msgs {sent}", style=DIM)
        head.append(f"   out {out_kb}", style=DIM)
        body = Text()
        for line in tail:
            body.append("\n▏ ", style=STATE_COLOUR[state])
            body.append(line, style="#b8b8b8" if state != "dead" else DIM)
        self.border_subtitle = "click: open / bring to front"
        self.update(head + body)

    def on_click(self):
        self.app.select(self.agent)
        self.app.bring_forward(self.agent)


class SophonPanel(App):
    TITLE = "SOPHON // SWARM CONTROL"
    ENABLE_COMMAND_PALETTE = False
    CSS = f"""
    Screen {{ background: #000000; color: #c8c8c8; }}
    #hdr {{ height: 4; padding: 1 1 0 1; background: #02070a; border-bottom: heavy #0e3a4d; }}
    #logo {{ width: 28; height: 2; }}
    #status {{ width: 1fr; height: 2; padding: 0 0 0 2; }}
    #left {{ width: 50; padding: 0 1 0 0; scrollbar-size-vertical: 1; }}
    * {{ scrollbar-color: #1d3a47; scrollbar-color-hover: {ACCENT}; scrollbar-background: #000000;
         scrollbar-background-hover: #000000; scrollbar-color-active: {ACCENT}; scrollbar-size-vertical: 1; }}
    .box {{ border: round #262626; border-title-color: {ACCENT}; border-title-style: bold; padding: 0 1;
            margin: 1 0 0 0; height: auto; background: #050505; }}
    .row {{ height: auto; }}
    Button {{ min-width: 5; height: 3; margin: 0 1 0 0; background: #0d0d0d; color: #bdbdbd; border: tall #222222;
              text-style: bold; }}
    Button:hover {{ background: #161616; border: tall #444444; }}
    Button:focus {{ text-style: bold; }}
    Button.on {{ color: #000000; text-style: bold; }}
    #o-off.on {{ background: #c8c8c8; border: none; color: #000000; text-style: bold; }}
    #o-free.on {{ background: {GREEN}; border: none; color: #000000; text-style: bold; }}
    #o-paid.on {{ background: {AMBER}; border: none; color: #000000; text-style: bold; }}
    #g-or {{ grid-size: 3; height: auto; }}
    #g-pol {{ grid-size: 4; grid-columns: 6 1fr 1fr 1fr; height: auto; }}
    #pol-label {{ color: #6a6a6a; height: 1; }}
    #p-deny.on {{ background: {GREEN}; color: #000000; }}
    #p-zdr.on {{ background: {ACCENT}; color: #000000; }}
    #p-allow.on {{ background: {AMBER}; color: #000000; }}
    #o-private.on {{ background: {PURPLE}; color: #000000; }}
    #or-key {{ margin: 0 0 1 0; }}
    #or-key.hidden {{ display: none; }}
    #m-claude.on {{ background: {PURPLE}; border: tall {PURPLE}; }}
    #m-light.on {{ background: {ACCENT}; border: tall {ACCENT}; }}
    #m-heavy.on {{ background: {AMBER}; border: tall {AMBER}; }}
    #m-local.on {{ background: {GREEN}; border: tall {GREEN}; }}
    .mode {{ width: 1fr; }}
    .pip {{ width: 1fr; min-width: 4; }}
    .pip.on {{ background: {ACCENT}; border: tall {ACCENT}; }}
    .danger {{ color: {RED}; border: tall #3a1010; }}
    .danger.armed {{ background: {RED}; color: #000000; border: tall {RED}; }}
    .big {{ width: 1fr; }}
    #count {{ width: 1fr; content-align: center middle; height: 3; text-style: bold; }}
    #budget-bar {{ width: 100%; }}
    #budget-bar Bar {{ width: 1fr; }}
    #budget-bar > .bar--bar {{ color: {ACCENT}; background: #1a1a1a; }}
    #budget-bar > .bar--complete {{ color: {RED}; }}
    Sparkline {{ height: 2; margin: 0; }}
    Sparkline > .sparkline--max-color {{ color: {AMBER}; }}
    Sparkline > .sparkline--min-color {{ color: {ACCENT} 40%; }}
    #lock {{ color: {AMBER}; height: auto; }}
    #center {{ width: 1fr; }}
    #intake {{ height: auto; padding: 1 2; margin: 1 0 0 0; border: round #0e3a4d; background: #02070a;
               border-title-color: {ACCENT}; border-title-style: bold; }}
    TabbedContent {{ height: 1fr; }}
    #comms {{ height: 40%; margin: 0; padding: 0 0; border-title-color: {ACCENT}; }}
    #comms RichLog {{ height: 1fr; }}
    Tabs {{ background: #000000; }}
    Tab {{ color: #6a6a6a; text-style: bold; }}
    Tab.-active {{ color: {ACCENT}; }}
    #cards {{ grid-size: 2; grid-gutter: 1 1; grid-rows: auto; height: auto; padding: 1 0; }}
    AgentCard {{ height: 10; border: round #333333; border-title-style: bold; border-title-color: #e0e0e0;
                 border-subtitle-color: #333333; padding: 0 1; background: #050505; }}
    AgentCard:hover {{ background: #0a0f14; border-subtitle-color: {ACCENT}; }}
    AgentCard.working {{ border: round {GREEN}; }}
    AgentCard.idle {{ border: round #3a3a3a; }}
    AgentCard.dialog {{ border: heavy {AMBER}; background: #140e00; }}
    AgentCard.dead {{ border: round #1c1c1c; color: #444444; height: 3; border-title-color: #555555; }}
    RichLog {{ scrollbar-size-horizontal: 0; }}
    AgentCard.intake {{ border: round {ACCENT}; }}
    AgentCard.lead {{ border: double {ACCENT}; border-title-color: {ACCENT}; }}
    AgentCard.selected {{ background: #07131c; }}
    RichLog {{ background: #000000; border: none; padding: 0 1; }}
    Input {{ background: #080808; border: tall #262626; }}
    Input:focus {{ border: tall {ACCENT}; }}
    DataTable {{ background: #000000; height: auto; max-height: 14; }}
    DataTable > .datatable--header {{ background: #0a0a0a; color: {ACCENT}; text-style: bold; }}
    DataTable > .datatable--cursor {{ background: #0b2a3d; }}
    #memo-view, #out-view, #run-view {{ border: round #262626; height: 1fr; padding: 0 1; }}
    Markdown {{ background: #000000; }}
    Footer {{ background: #030303; }}
    Button:disabled {{ background: #050505; border: tall #121212; color: #333333; }}
    #m-claude {{ color: {PURPLE}; border: tall {PURPLE} 25%; }}
    #m-light {{ color: {ACCENT}; border: tall {ACCENT} 25%; }}
    #m-heavy {{ color: {AMBER}; border: tall {AMBER} 25%; }}
    #m-local {{ color: {GREEN}; border: tall {GREEN} 25%; }}
    #x-pause {{ color: {AMBER}; }}
    #x-resume {{ color: {GREEN}; }}
    #x-digest {{ color: {ACCENT}; }}
    #a-plus {{ color: {GREEN}; }}
    #a-minus {{ color: {RED}; }}
    AgentCard.lead {{ height: 14; }}
    Button {{ height: 1; min-width: 4; border: none; margin: 0 1 0 0; padding: 0 1; background: #161616;
              color: #bdbdbd; text-style: bold; }}
    Button:hover {{ background: #262626; border: none; }}
    Button:focus {{ text-style: bold reverse; }}
    Button:disabled {{ background: #0a0a0a; color: #333333; border: none; }}
    #m-claude, #m-light, #m-heavy, #m-local {{ border: none; }}
    #m-claude.on {{ background: {PURPLE}; border: none; color: #000000; }}
    #m-light.on {{ background: {ACCENT}; border: none; color: #000000; }}
    #m-heavy.on {{ background: {AMBER}; border: none; color: #000000; }}
    #m-local.on {{ background: {GREEN}; border: none; color: #000000; }}
    #m-phantom {{ color: {PHANTOM}; border: none; }}
    #m-phantom.on {{ background: {PHANTOM}; border: none; color: #000000; }}
    .pip.on {{ background: {ACCENT}; border: none; color: #000000; }}
    .danger {{ color: {RED}; border: none; background: #1a0808; }}
    .danger.armed {{ background: {RED}; color: #000000; border: none; }}
    #x-sleep.on {{ background: #6c5ce7; color: #ffffff; }}
    .row {{ height: 1; margin: 0 0 1 0; }}
    .mode, .big, .pip {{ padding: 0; width: 1fr; min-width: 3; }}
    .gap {{ margin: 0; }}
    #count {{ height: 1; }}
    .box {{ margin: 0 0 0 0; }}
    #left {{ scrollbar-size-vertical: 0; }}
    * {{ scrollbar-size-vertical: 0; scrollbar-size-horizontal: 0; }}
    Sparkline {{ height: 1; }}

    AgentCard.lead.dead {{ height: 4; }}
    .bgrid {{ grid-size: 4; grid-rows: 1; grid-gutter: 1 1; height: auto; margin: 0 0 1 0; }}
    .bgrid Button {{ margin: 0; width: 1fr; }}
    """
    BINDINGS = [
        Binding("plus,equals_sign", "agents(1)", "+agent"),
        Binding("minus", "agents(-1)", "-agent"),
        Binding("left_square_bracket", "depth(-1)", "depth-"),
        Binding("right_square_bracket", "depth(1)", "depth+"),
        Binding("1", "mode('claude')", "claude", show=False),
        Binding("2", "mode('light')", "light", show=False),
        Binding("3", "mode('heavy')", "heavy", show=False),
        Binding("4", "mode('local')", "local", show=False),
        Binding("5", "mode('phantom')", "phantom", show=False),
        Binding("p", "pause", "pause"),
        Binding("r", "resume", "resume"),
        Binding("d", "digest", "digest"),
        Binding("t", "retile", "retile"),
        Binding("z", "sleep", "sleep mode"),
        Binding("f", "board_filter", "filter board"),
        Binding("slash", "focus_comms", "message"),
        Binding("escape", "blur", show=False),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self):
        super().__init__()
        self.cards = {}
        self.selected = None
        self.token_hist = [0]
        self.tokens = 0
        self.last_digest = time.time()
        self.board_pos = 0
        self.armed_end = 0.0
        self.host = {"link": None, "models": [], "ram": "?"}
        m = re.search(r"(\d{8}-\d{4})$", RUN.name)
        self.started = time.mktime(time.strptime(m[1], "%Y%m%d-%H%M")) if m else RUN.stat().st_ctime
        self.st = {}

    # ---------- layout ----------
    def compose(self) -> ComposeResult:
        with Horizontal(id="hdr"):
            yield Static(id="logo")
            yield Static(id="status")
        with Horizontal():
            with VerticalScroll(id="left"):
                with Vertical(classes="box", id="b-mode"):
                    with Grid(classes="bgrid", id="g-mode"):
                        for m in ("claude", "light", "heavy", "local", "phantom"):
                            yield Button(MODE_SHORT[m], id=f"m-{m}", classes="mode")
                    yield Static(id="mode-desc")
                with Vertical(classes="box", id="b-team"):
                    with Horizontal(classes="row"):
                        yield Button("−", id="a-minus")
                        yield Static(id="count")
                        yield Button("+", id="a-plus")
                    yield Static(id="caps")
                with Vertical(classes="box", id="b-depth"):
                    with Horizontal(classes="row"):
                        for i in range(1, 6):
                            yield Button(str(i), id=f"d-{i}", classes="pip")
                    yield Static(id="depth-desc")
                with Vertical(classes="box", id="b-budget"):
                    yield ProgressBar(total=sw.CLAUDE_BUDGET, show_eta=False, show_percentage=True, id="budget-bar")
                    yield Static(id="budget-text")
                    yield Sparkline([0], summary_function=max, id="spark")
                with Vertical(classes="box", id="b-host"):
                    yield Static(id="host")
                with Vertical(classes="box", id="b-or"):
                    with Grid(classes="bgrid", id="g-or"):
                        yield Button("OFF", id="o-off")
                        yield Button("FREE", id="o-free")
                        yield Button("PAID", id="o-paid")
                    with Grid(classes="bgrid", id="g-pol"):
                        yield Static("data", id="pol-label")
                        yield Button("PRIVATE", id="p-deny")
                        yield Button("ZDR", id="p-zdr")
                        yield Button("ANY", id="p-allow")
                    yield Static(id="or-status")
                    with Horizontal(classes="row gap", id="or-keyrow"):
                        yield Button("PRIVATE RUN", id="o-private")
                        yield Button("CHANGE KEY", id="o-change")
                        yield Button("DELETE KEY", id="o-del", classes="danger")
                    yield Input(placeholder="paste OpenRouter key (sk-or-...), Enter to save", password=True,
                                id="or-key")
                with Vertical(classes="box", id="b-act"):
                    with Grid(classes="bgrid", id="g-act"):
                        yield Button("PAUSE", id="x-pause", classes="big")
                        yield Button("RESUME", id="x-resume", classes="big")
                        yield Button("DIGEST", id="x-digest", classes="big")
                        yield Button("RETILE", id="x-tile", classes="big")
                    with Horizontal(classes="row gap"):
                        yield Button("☾ SLEEP MODE", id="x-sleep", classes="big")
                        yield Button("END RUN", id="x-end", classes="big danger")
                yield Static(id="lock")
            with Vertical(id="center"):
              with TabbedContent(initial="t-agents"):
                with TabPane("AGENTS", id="t-agents"):
                    with VerticalScroll():
                        yield Static(id="intake")
                        yield Grid(id="cards")
                with TabPane("MEMORY", id="t-memory"):
                    with Vertical():
                        yield DataTable(id="memory", cursor_type="row", zebra_stripes=True)
                        with Horizontal(classes="row"):
                            yield Button("APPROVE (ask Sophon to store)", id="r-approve", classes="big")
                            yield Button("REJECT", id="r-reject", classes="big danger")
                        with VerticalScroll(id="memo-view"):
                            yield Markdown("Select a request.", id="memo-md")
                with TabPane("OUTPUT", id="t-out"):
                    with Vertical():
                        yield DataTable(id="outs", cursor_type="row", zebra_stripes=True)
                        with VerticalScroll(id="out-view"):
                            yield Markdown("Select a file.", id="out-md")
                with TabPane("RUNS", id="t-runs"):
                    with Vertical():
                        yield DataTable(id="runs", cursor_type="row", zebra_stripes=True)
                        with VerticalScroll(id="run-view"):
                            yield Markdown("Select a run to read its FINAL.md.", id="run-md")
              with Vertical(id="comms", classes="box"):
                yield RichLog(id="log", wrap=True, markup=False, highlight=False, max_lines=4000, min_width=20)
                yield Input(placeholder="message Sophon  ('name: text' or 'all: text' for others)   / to focus, Enter to send",
                            id="say")
        yield Footer()

    def on_mount(self):
        for box, title in (("b-mode", "MODE"), ("b-team", "TEAM"), ("b-depth", "THOROUGHNESS"),
                           ("b-budget", "CLAUDE BUDGET"), ("b-host", HOST_LABEL), ("b-or", "OPENROUTER"),
                           ("b-act", "CONTROL"),
                           ("comms", "COMMS  ·  everything  ·  f: filter")):
            self.query_one(f"#{box}").border_title = title
        self.query_one("#memory", DataTable).add_columns("ID", "FROM", "TITLE", "STATUS")
        self.query_one("#outs", DataTable).add_columns("FILE", "SIZE", "UPDATED")
        self.query_one("#runs", DataTable).add_columns("RUN", "MODE", "MIN", "CLAUDE", "CARDS", "JOB")
        self.board_lines, self.board_filter = [], "all"
        self.or_info, self.or_info_t = {}, 0.0
        self.scan_runs()
        self.tick()
        self.set_interval(1.0, self.tick)
        self.set_interval(2.0, self.scan_agents)
        self.set_interval(10.0, self.scan_tokens)
        self.set_interval(6.0, self.scan_host)
        self.set_interval(5.0, self.scan_files)
        self.set_interval(5.0, self.scan_or)
        self.or_msg, self.or_del_armed, self.or_show_key = "", 0, False
        self.scan_or()
        self.scan_agents()
        self.scan_tokens()
        self.scan_host()
        self.scan_files()

    # ---------- periodic ----------
    def tick(self):
        self.st = st = sw.load(RUN)
        mode = st.get("mode")
        m = sw.MODES.get(mode, {})
        live = [n for n in st["agents"] if sw.alive(n)]
        blink = int(time.time()) % 2 == 0
        logo = Text()
        for row, glyph in ((LOGO[0], ACCENT), (LOGO[1], "#2b8fb3")):
            logo.append(row + "\n", style=f"bold {glyph}")
        self.query_one("#logo", Static).update(logo)
        h = Text()
        h.append_text(pill(MODE_SHORT.get(mode, "INTAKE"), MODE_COLOUR.get(mode, "#888888")))
        h.append("  ")
        stop = (RUN / "STOP").exists()
        h.append_text(pill("ENDED" if stop else "LIVE", RED if stop else GREEN))
        if st.get("sleep"):
            h.append("  ")
            h.append_text(pill("☾ ASLEEP", "#6c5ce7", fg="white"))
        h.append(f"   T+{fmt_age(time.time() - self.started)}", style="bold #e0e0e0")
        h.append(f"   {len(live)} live", style="#9a9a9a")
        link = self.host["link"]
        h.append(f"   {HOST_LABEL} ", style="#9a9a9a")
        h.append("●" if link else "○", style=GREEN if link else RED if link is False else DIM)
        digest = m.get("digest", 0)
        if digest:
            left = digest - (time.time() - self.last_digest)
            h.append(f"   next digest {fmt_age(left)}", style=AMBER)
        h.append(f"\n{RUN.name}", style=DIM)
        h.append("   ◉ the sophon is watching" if blink else "   ○ the sophon is watching",
                 style="italic #2b7a99" if live else "italic #333333")
        self.query_one("#status", Static).update(h)

        for k in sw.MODES:
            self.query_one(f"#m-{k}", Button).set_class(k == mode, "on")
        sb = self.query_one("#x-sleep", Button)
        sb.set_class(bool(st.get("sleep")), "on")
        sb.label = "☾ ASLEEP" if st.get("sleep") else "☾ SLEEP MODE"
        desc = Text()
        if m:
            desc.append(m["label"], style=f"bold {MODE_COLOUR[mode]}")
            if mode == "phantom":
                desc.append("\nQwen3-Next 80B + gpt-oss 120B, swapped per step", style="#8a8a8a")
            else:
                desc.append(f"\nlead {m['lead'][1]}  ·  workers {m['worker'][1]}",
                            style="#8a8a8a")
        else:
            desc.append("choosing a mode (intake running)", style=AMBER)
        self.query_one("#mode-desc", Static).update(desc)

        workers = [n for n in live if n not in sw.LEADS and n not in ("q1", "q2")]
        target = st.get("agents_target", "-")
        self.query_one("#count", Static).update(Text.assemble((f"{len(workers)}", f"bold {ACCENT}"),
                                                              (f" / {target} workers", "#9a9a9a")))
        self.query_one("#caps", Static).update(
            Text(f"caps  claude {m.get('cap_claude', '-')}  ·  local {m.get('cap_local', '-')}", style=DIM))

        t = int(st.get("thorough") or 0)
        for i in range(1, 6):
            self.query_one(f"#d-{i}", Button).set_class(i <= t, "on")
        names = {1: "quick pass", 2: "light check", 3: "normal, sources for key claims",
                 4: "cross-checked by a second agent", 5: "exhaustive, every claim verified"}
        self.query_one("#depth-desc", Static).update(Text(names.get(t, "not set"), style="#8a8a8a"))

        locked = mode not in sw.MODES
        for b in self.query("#b-team Button, #b-depth Button, #b-mode Button, #x-pause, #x-resume, #x-digest"):
            b.disabled = locked
        self.query_one("#lock", Static).update(
            Text("\n  controls unlock once intake picks a mode", style=AMBER) if locked else "")
        if self.armed_end and time.time() > self.armed_end:
            self.armed_end = 0
            self.query_one("#x-end", Button).set_class(False, "armed")
            self.query_one("#x-end", Button).label = "END RUN"

        self.read_board()
        self.housekeeping(st, mode, m)
        self.render_intake(locked)

    def render_intake(self, locked):
        box = self.query_one("#intake", Static)
        box.display = locked
        if not locked:
            return
        box.border_title = "INTAKE"
        ctx = RUN / "context"
        job = (ctx / "JOB.md").read_text().strip() if (ctx / "JOB.md").exists() else ""
        tri = json.loads((ctx / "triage.json").read_text()) if (ctx / "triage.json").exists() else None
        qf = RUN / "out" / "questions.md"
        qs = [l.strip() for l in qf.read_text().splitlines() if l.strip()] if qf.exists() else []
        dec = json.loads((ctx / "decision.json").read_text()) if (ctx / "decision.json").exists() else None
        q_live = any(sw.alive(q) for q in ("q1", "q2"))
        steps = [("TRIAGE", bool(tri), bool(job) and not tri), ("QUESTIONS", bool(qs) or (bool(tri) and not q_live and
                 bool(dec)), q_live), ("MODE", bool(dec), bool(qs) and not dec), ("LAUNCH", False, bool(dec))]
        t = Text()
        for i, (name, done, active) in enumerate(steps, 1):
            colour = GREEN if done else ACCENT if active else "#2a2a2a"
            t.append_text(pill(f"{i}  {name}", colour, fg="black" if done or active else "#777777"))
            if i < 4:
                t.append("  ━━━  ", style=GREEN if done else "#2a2a2a")
        t.append("\n\n")
        if not job:
            t.append("Waiting for the job. Type it in the SOPHON window, then press Enter on an empty line. Ctrl+C there cancels.",
                     style=f"bold {AMBER}")
        else:
            t.append("JOB\n", style=f"bold {ACCENT}")
            t.append(job[:900] + "\n", style="#e0e0e0")
        if tri:
            t.append("\nTRIAGE  ", style=f"bold {ACCENT}")
            t.append(f"by {tri.get('by')}  ·  understood {tri.get('understood')}  ·  confidence "
                     f"{float(tri.get('confidence', 0)):.2f}  ·  stakes {tri.get('stakes')}  ·  suggests "
                     f"{tri.get('mode')} x{tri.get('agents')}\n", style="#b0b0b0")
            if tri.get("reason"):
                t.append(f"{tri['reason'][:400]}\n", style="#7a7a7a")
        if q_live and not qs:
            t.append("\nQUESTIONERS  ", style=f"bold {ACCENT}")
            t.append("q1 and q2 are working out what to ask you", style="#b0b0b0")
            t.append(" ●" if int(time.time()) % 2 else " ○", style=ACCENT)
            t.append("\n")
        if qs:
            t.append("\nQUESTIONS FOR YOU  ", style=f"bold {ACCENT}")
            t.append("answer them in the Sophon terminal\n", style="#7a7a7a")
            for q in qs[:6]:
                t.append(f"  {q}\n", style="#e0e0e0")
        if dec:
            t.append("\nDECISION  ", style=f"bold {ACCENT}")
            t.append_text(pill(MODE_SHORT.get(dec["mode"], dec["mode"]), MODE_COLOUR.get(dec["mode"], "#888")))
            t.append(f"  {dec['agents']} agents  ·  thoroughness {dec['thorough']}  ·  by {dec['by']}\n",
                     style="#e0e0e0")
            t.append(f"{dec.get('reason', '')[:300]}\n", style="#7a7a7a")
            t.append("Accept or override in the Sophon terminal.", style=AMBER)
        box.update(t)

    def housekeeping(self, st, mode, m):
        if self.tokens > sw.CLAUDE_BUDGET and mode in ("claude", "light") and not st.get("_downgraded"):
            st["_downgraded"] = True
            with sw.state(RUN) as s_:
                s_["_downgraded"] = True
            self.run_cli("mode", "heavy", note=f"Claude budget passed ({self.tokens:,}): switched to HEAVY")
            lead = sw.lead_of(st)
            if sw.alive(lead) and st.get("agents", {}).get(lead, {}).get("engine") == "claude":
                # the running lead keeps its model after a mode change, and it is the biggest spender
                self.run_cli("say", lead, f"The Claude budget has passed ({self.tokens:,} tokens). Wrap up now: no new "
                             "workers, no new rounds. Write FINAL.md from what you have, list what is unfinished, "
                             "then run `swarm end`.", note="budget passed: told Sophon to wrap up")
        self.flush_queue()
        digest = m.get("digest", 0)
        dfile = RUN / "inbox" / f"{sw.lead_of(st)}-digest.md"
        if digest and time.time() - self.last_digest > digest:
            self.last_digest = time.time()
            if dfile.exists() and dfile.stat().st_size:
                stamped = dfile.with_name(f"digest-{time.strftime('%H%M%S')}.md")
                dfile.rename(stamped)
                self.deliver_digest(str(stamped.relative_to(RUN)))

    @work(thread=True, exclusive=True, group="inbox")
    def flush_queue(self):
        for name in sw.flush_inbox(RUN):
            self.call_from_thread(self.notify, f"queued messages delivered to {name}")

    @work(thread=True, exclusive=True, group="digest")
    def deliver_digest(self, rel):
        sw.deliver(RUN, "panel", sw.lead_of(sw.load(RUN)), f"digest ready: read {rel}")
        self.call_from_thread(self.notify, f"digest sent to Sophon ({rel})")

    def read_board(self):
        f = RUN / "board.log"
        if not f.exists():
            return
        with f.open() as fh:
            fh.seek(self.board_pos)
            new = fh.read()
            self.board_pos = fh.tell()
        log = self.query_one("#log", RichLog)
        for line in new.splitlines():
            kind = board_kind(line)
            m = re.match(r"\[(\d\d:\d\d:\d\d)\] (\S+?)(?: -> (\S+?))?:? (.*)", line)
            t = Text()
            if not m:
                t.append(line, style=DIM)
            else:
                ts, frm, to, msg = m.groups()
                t.append(f"{ts} ", style=DIM)
                if frm == "**":
                    t.append(line[11:], style=f"bold {GREEN}")
                else:
                    t.append(frm, style=f"bold {sender_colour(frm)}")
                    if to:
                        t.append(" → ", style=DIM)
                        t.append(to.rstrip(":"), style=f"bold {sender_colour(to.rstrip(':'))}")
                    body = msg
                    style = "#c8c8c8"
                    if body.startswith("REPORT:"):
                        t.append(" REPORT ", style=f"bold black on {ACCENT}")
                        body = body[7:]
                    elif "memo-request" in body:
                        t.append(" MEMO ", style=f"bold black on {PURPLE}")
                    elif "settings changed" in body:
                        style = AMBER
                    body = " ".join(body.split())
                    if kind == "problem":
                        style = RED
                    t.append(" " + body, style=style)
            self.board_lines.append((kind, t))
            if self.board_filter in ("all", kind):
                log.write(t)
        self.board_lines = self.board_lines[-4000:]

    def action_board_filter(self):
        order = ["all", "report", "memo", "problem", "human"]
        self.board_filter = order[(order.index(self.board_filter) + 1) % len(order)]
        log = self.query_one("#log", RichLog)
        log.clear()
        for kind, t in self.board_lines:
            if self.board_filter in ("all", kind):
                log.write(t)
        names = {"all": "everything", "report": "reports", "memo": "memo requests", "problem": "problems",
                 "human": "your messages"}
        self.query_one("#comms").border_title = f"COMMS  ·  {names[self.board_filter]}  ·  f: filter"

    @work(thread=True, exclusive=True, group="runs")
    def scan_runs(self):
        rows = []
        runs = sorted((p for p in (RUNS).iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)[:60]
        for r in runs:
            try:
                st = json.loads((r / "state.json").read_text()) if (r / "state.json").exists() else {}
            except ValueError:
                st = {}
            brief = r / "context" / "BRIEF.md"
            job = ""
            if brief.exists():
                m = re.search(r"## Job[^\n]*\n(.+?)\n", brief.read_text())
                job = (m[1] if m else "").strip()
            final = r / "FINAL.md"
            cards = len(re.findall(r"^#{2,3} \d+[.)]", final.read_text(), re.M)) if final.exists() else 0
            mins = ""
            if final.exists():
                m = re.search(r"(\d{8}-\d{4})$", r.name)
                if m:
                    mins = str(round((final.stat().st_mtime - time.mktime(time.strptime(m[1], "%Y%m%d-%H%M"))) / 60))
            rows.append((r.name, st.get("mode", "?"), mins, st.get("_tokens", 0), cards if final.exists() else "-",
                         job[:70]))
        self.call_from_thread(self.apply_runs, rows)

    def apply_runs(self, rows):
        t = self.query_one("#runs", DataTable)
        t.clear()
        for name, mode, mins, tok, cards, job in rows:
            t.add_row(Text(name, style="bold" if name == RUN.name else ""),
                      Text(mode.upper(), style=f"bold {MODE_COLOUR.get(mode, DIM)}"), mins,
                      f"{tok / 1000:.0f}K" if tok else "0", str(cards), job, key=name)

    @work(thread=True, exclusive=True, group="agents")
    def scan_agents(self):
        st = sw.load(RUN)
        rows = []
        for n, a in st["agents"].items():
            if n in (sw.BOARD, sw.PANEL):
                continue
            if not sw.alive(n):
                rows.append((n, a, "dead", [], st.get("sent", {}).get(n, 0)))
                continue
            screen = sw.screen_text(n, 40)
            sfile = RUN / "status" / n
            if a.get("engine") == "intake":
                state = "intake"
            elif a.get("engine") in ("local", "local-lead"):
                state = "working" if sfile.exists() and sfile.read_text().startswith("working") else "idle"
            elif sw.busy_with_dialog(n, RUN):
                state = "dialog"
            elif re.search(r"esc (to )?interrupt", screen):
                state = "working"
            else:
                state = "idle"
            lines = [l.strip(" ┃│▏").rstrip() for l in screen.splitlines()]
            lines = [l for l in lines if l and not BOX.match(l) and "ctrl+p" not in l and "shift+tab" not in l
                     and not l.startswith(("───", "▣", "⏵"))]
            rows.append((n, a, state, lines[-16:] if n in sw.LEADS else lines[-5:], st.get("sent", {}).get(n, 0)))
        self.call_from_thread(self.apply_agents, rows)

    def watchdog(self, rows):
        if not self.st.get("sleep"):
            self.dialog_since = {}
            return
        since = getattr(self, "dialog_since", {})
        now = time.time()
        for n, a, state, tail, sent in rows:
            if state != "dialog":
                since.pop(n, None)
                continue
            since.setdefault(n, now)
            if now - since[n] > 90:
                subprocess.run(["tmux", "send-keys", "-t", f"=sw-{n}:", "Escape"], capture_output=True)
                threading.Thread(target=self.nudge_after_cancel, args=(n,), daemon=True).start()
                sw.log(RUN, f"[{sw.stamp()}] panel: cancelled a stuck permission prompt for {n} (sleep mode)")
                since[n] = now + 600
        self.dialog_since = since

    def nudge_after_cancel(self, n):
        """Tell the agent to carry on once the cancelled dialog has gone (a queued message would never arrive)."""
        for _ in range(20):
            time.sleep(1.5)
            if not sw.busy_with_dialog(n, RUN):
                break
        msg = ("The panel cancelled that permission prompt, not the user: they are asleep. Do NOT wait for them. Carry on "
               "with the job now. Write files with the Write or Edit tool (never Bash heredocs, cat > or echo >). "
               "If an action truly needs the user, add it to a 'needs you' list in FINAL.md and keep going.")
        sw.deliver(RUN, "panel", n, msg)

    def apply_agents(self, rows):
        self.watchdog(rows)
        grid = self.query_one("#cards", Grid)
        order = sorted(rows, key=lambda r: (r[0] not in sw.LEADS, r[2] == "dead", r[0]))
        for n, a, state, tail, sent in order:
            card = self.cards.get(n)
            if card is None:
                card = self.cards[n] = AgentCard(n)
                grid.mount(card)
            out = RUN / "out" / f"{n}.md"
            kb = f"{out.stat().st_size / 1024:.1f}KB" if out.exists() else "-"
            card.show(a, state, tail, sent, kb)
            card.sw_state = state
            card.set_class(n == self.selected, "selected")
        want = [self.cards[r[0]] for r in order]
        if list(grid.children) != want:
            for i, card in enumerate(want):
                if grid.children[i] is not card:
                    grid.move_child(card, before=i)
        self.fit_cards()

    def fit_cards(self):
        """Size cards so everything fits the AGENTS tab without scrolling."""
        grid = self.query_one("#cards", Grid)
        area = grid.parent.region.height or self.size.height // 2
        cols = grid.styles.grid_size_columns or 1
        lead = [c for n, c in self.cards.items() if n in sw.LEADS]
        live = [c for n, c in self.cards.items() if n not in sw.LEADS and getattr(c, "sw_state", "") != "dead"]
        dead = [c for n, c in self.cards.items() if n not in sw.LEADS and getattr(c, "sw_state", "") == "dead"]
        for c in dead:
            c.display = False
        for c in live:
            c.display = True
        rows = -(-len(live) // cols) if live else 0
        room = area - 2
        heights = []
        for c in lead:
            c.styles.column_span = cols
            st = getattr(c, "sw_state", "")
            if st == "dead":
                h = 4
            elif not live:
                h = max(6, room)
            else:
                h = max(7, min(16, room - rows * 5 - rows))
            c.styles.height = h
            heights.append(h)
            room -= h + 1
        if rows:
            h = max(4, min(10, (room - (rows - 1)) // rows))
            for c in live:
                c.styles.height = h
            heights += [h] * rows
        grid.styles.grid_rows = " ".join(str(x) for x in heights) or "auto"
        grid.refresh(layout=True)
        try:
            tabs = self.query_one(TabbedContent)
            tabs.get_tab("t-agents").label = f"AGENTS ({len(live)})" if live else "AGENTS"
        except Exception:
            pass

    @work(thread=True, exclusive=True, group="tokens")
    def scan_tokens(self):
        u = sw.claude_usage(RUN)
        self.call_from_thread(self.apply_tokens, u["fresh"], u["cache_read"])

    def apply_tokens(self, used, cache_read=0):
        self.token_hist.append(max(0, used - self.tokens) if self.tokens else 0)
        self.token_hist = self.token_hist[-60:]
        self.tokens = used
        self.query_one("#budget-bar", ProgressBar).update(total=sw.CLAUDE_BUDGET, progress=min(used, sw.CLAUDE_BUDGET))
        pct = used / sw.CLAUDE_BUDGET
        colour = GREEN if pct < 0.5 else AMBER if pct < 0.85 else RED
        self.query_one("#budget-text", Static).update(Text.assemble(
            (f"{used:,}", f"bold {colour}"), (f" / {sw.CLAUDE_BUDGET:,} fresh", "#8a8a8a"),
            (f"\n+ {cache_read / 1e6:.2f}M cache reads", DIM), ("   cap: wrap up", DIM)))
        self.query_one("#spark", Sparkline).data = self.token_hist

    @work(thread=True, exclusive=True, group="host")
    def scan_host(self):
        models, link = [], True
        for url, where in ((OLLAMA_URL, "main"), (OLLAMA_CPU_URL, "cpu")):
            if not url:
                continue
            try:
                ps = json.load(urllib.request.urlopen(f"{url}/api/ps", timeout=3))
                for mm in ps.get("models", []):
                    models.append((where, mm["name"].replace(":latest", ""), mm.get("size", 0), mm.get("context_length")))
            except Exception:  # noqa: BLE001
                if url == OLLAMA_URL:
                    link = False
        big = None
        try:  # a big MoE streamed from NVMe by llama-server (phantom mode)
            props = json.load(urllib.request.urlopen(f"{sw.BIG_URL}/props", timeout=2))
            fname = Path(props.get("model_path", "")).name
            label = next((v["label"] for v in sw.BIG_LLM.values() if v["file"] == fname), fname)
            slots = json.load(urllib.request.urlopen(f"{sw.BIG_URL}/slots", timeout=2))
            big = (label, any(x.get("is_processing") for x in slots))
        except Exception:  # noqa: BLE001
            pass
        ram = self.host.get("ram", "?")
        if int(time.time()) % 30 < 7:
            try:
                argv = ["free", "-m"] if not SSH_HOST else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4",
                                                             SSH_HOST, "free", "-m"]
                r = subprocess.run(argv, capture_output=True, text=True, timeout=8)
                f = r.stdout.split("\n")[1].split()
                ram = f"{int(f[6]) / 1024:.1f} of {int(f[1]) / 1024:.1f} GiB free"
            except Exception:  # noqa: BLE001
                pass
        self.host = {"link": link, "models": models, "ram": ram, "big": big}
        self.call_from_thread(self.apply_host)

    def apply_host(self):
        s = self.host
        t = Text()
        t.append("link  ", style="#8a8a8a")
        t.append("UP" if s["link"] else "DOWN  (check ollama.url, or swarm tunnel)", style=f"bold {GREEN if s['link'] else RED}")
        t.append(f"\nram   {s['ram']}", style="#8a8a8a")
        if s.get("big"):
            t.append("\nbig   ", style=DIM)
            t.append(s["big"][0], style=f"bold {PHANTOM}")
            t.append(f"  cap {sw.BIG_CAP}  " + ("thinking" if s["big"][1] else "idle"), style=DIM)
        if s["models"]:
            for where, name, size, ctx in s["models"]:
                t.append(f"\n{where}   ", style=DIM)
                t.append(name, style=f"bold {GREEN}")
                t.append(f"  {size / 2**30:.1f}G" + (f"  ctx {ctx // 1024}k" if ctx else ""), style=DIM)
        elif not s.get("big"):
            t.append("\nno model loaded", style=DIM)
        self.query_one("#host", Static).update(t)

    @work(thread=True, exclusive=True, group="files")
    def scan_files(self):
        reqs = []
        for f in sorted((RUN / "requests").glob("*.md")):
            txt = f.read_text()
            g = lambda k: (re.search(rf"^{k}: (.*)$", txt, re.M) or [None, ""])[1]
            reqs.append((f.stem, g("from"), g("title"), g("status")))
        outs = [RUN / "FINAL.md"] if (RUN / "FINAL.md").exists() else []
        outs += sorted((RUN / "out").glob("**/*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        outs += [RUN / "context" / "BRIEF.md"] if (RUN / "context" / "BRIEF.md").exists() else []
        rows = [(str(p.relative_to(RUN)), f"{p.stat().st_size / 1024:.1f}KB",
                 time.strftime("%H:%M:%S", time.localtime(p.stat().st_mtime))) for p in outs]
        self.call_from_thread(self.apply_files, reqs, rows)

    def apply_files(self, reqs, rows):
        for tid, data in (("#memory", reqs), ("#outs", rows)):
            table = self.query_one(tid, DataTable)
            keep = table.cursor_row
            table.clear()
            for r in data:
                styled = list(r)
                if tid == "#memory":
                    status = r[3].split()[0] if r[3] else "?"
                    styled[3] = Text(r[3], style={"pending": AMBER, "approved": GREEN, "rejected": RED}.get(status, DIM))
                table.add_row(*styled, key=r[0])
            if data:
                table.move_cursor(row=min(keep, len(data) - 1))
        pending = sum(1 for r in reqs if r[3].startswith("pending"))
        tabs = self.query_one(TabbedContent)
        tabs.get_tab("t-memory").label = f"MEMORY ({pending})" if pending else "MEMORY"
        tabs.get_tab("t-out").label = f"OUTPUT ({len(rows)})"

    def on_resize(self, event):
        grid = self.query_one("#cards", Grid)
        narrow = self.size.width < 115
        left = self.query_one("#left")
        left.styles.width = 36 if narrow else 50
        self.query_one("#g-mode", Grid).styles.grid_size_columns = 3 if narrow else 5
        self.query_one("#g-act", Grid).styles.grid_size_columns = 2 if narrow else 4
        self.query_one("#spark").display = not narrow
        self.query_one("#comms").styles.height = "35%" if self.size.height < 45 else "40%"
        cols = max(1, min(4, (self.size.width - (38 if narrow else 52)) // 48))
        grid.styles.grid_size_columns = cols
        for card in self.cards.values():
            card.styles.column_span = cols if card.has_class("lead") else 1
        self.call_after_refresh(self.fit_cards)

    # ---------- actions ----------
    def select(self, name):
        self.selected = name

    @work(thread=True, group="windows")
    def bring_forward(self, name):
        if not sw.alive(name):
            self.call_from_thread(self.notify, f"{name} is not running", severity="warning")
            return
        wid = sw.window_id(name, timeout=0.2)
        if not wid:
            a = self.st.get("agents", {}).get(name, {})
            sw.open_window(name, a.get("screen", "big"))
            sw.tile(RUN, only=name)
            wid = sw.window_id(name, timeout=5)
        if wid:
            subprocess.run(["wmctrl", "-i", "-a", wid], capture_output=True)

    def run_cli(self, *args, note=None):
        self._cli(args, note)

    @work(thread=True, group="cli")
    def _cli(self, args, note):
        try:
            out = swarm(*args)
        except subprocess.TimeoutExpired:
            out = "timed out"
        self.call_from_thread(self.notify, note or out or " ".join(args), timeout=6)

    # ---------- OpenRouter ----------
    @work(thread=True, exclusive=True, group="or-scan")
    def scan_or(self):
        """Settings every 5 s; balance from OpenRouter every 5 min (network), in this worker thread."""
        k = OR.key()
        if k and time.time() - self.or_info_t > 300:
            try:
                self.or_info = OR.key_info()
            except OR.ORError as e:
                self.or_info = {"error": str(e)[:80]}
            self.or_info_t = time.time()
        st = sw.load(RUN) if RUN else {}
        self.call_from_thread(self.apply_or, OR.settings(), bool(k), OR.masked(k) if k else "",
                              OR.spent(since=OR.today()), OR.spent(run=RUN.name) if RUN else 0.0, bool(st.get("private")))

    def apply_or(self, s, has_key, masked, today, run_spent, private):
        mode = s["mode"]
        for m in ("off", "free", "paid"):
            self.query_one(f"#o-{m}", Button).set_class(m == mode, "on")
        pol = "zdr" if s.get("zdr") else s.get("data_collection", "deny")
        for p in ("deny", "zdr", "allow"):
            self.query_one(f"#p-{p}", Button).set_class(p == pol, "on")
        self.query_one("#o-private", Button).set_class(private, "on")
        self.query_one("#o-private", Button).label = "PRIVATE RUN ✓" if private else "PRIVATE RUN"
        self.query_one("#or-key", Input).set_class(has_key and not self.or_show_key, "hidden")
        self.query_one("#o-change", Button).display = has_key
        self.query_one("#o-del", Button).display = has_key
        t = Text()
        colour = {"off": "#8a8a8a", "free": GREEN, "paid": AMBER}[mode]
        t.append(mode.upper(), style=f"bold {colour}")
        t.append({"off": "  nothing is sent", "free": "  free models only", "paid": "  free first, paid within caps"}[mode],
                 style="#8a8a8a")
        if private:
            t.append("\nthis run is PRIVATE: OpenRouter blocked", style=f"bold {PURPLE}")
        if has_key:
            i = self.or_info
            bal = i.get("balance")
            t.append("\nbalance ", style=DIM)
            t.append(f"${bal:.2f}" if bal is not None else ("?" if "error" in i else "…"), style="bold #e0e0e0")
            if i.get("remaining") is not None and (bal is None or float(i["remaining"]) < bal):
                t.append(f"  key limit left ${float(i['remaining']):.2f}", style=DIM)
            t.append(f"\ntoday ${today:.3f}/{s['day_cap_usd']:.2f}  run ${run_spent:.3f}/{s['run_cap_usd']:.2f}",
                     style="#9a9a9a")
            t.append(f"\nkey {masked}", style=DIM)
        else:
            t.append("\nno key: paste one below, Enter saves", style=AMBER)
        if self.or_msg:
            t.append("\n" + self.or_msg, style="#cfcfcf")
        self.query_one("#or-status", Static).update(t)

    def confirm(self, key, then):
        """Ask first for costly or outward-facing switches; `then` runs only on yes."""
        title, body = CONFIRM[key]
        self.push_screen(Confirm(title, body), lambda yes: then() if yes else self.notify("cancelled"))

    def or_policy(self, pol):
        def do():
            OR.save_settings(data_collection="allow" if pol == "allow" else "deny", zdr=pol == "zdr")
            self.or_msg = {"deny": "only providers that do not store or train on prompts (fewer free models)",
                           "zdr": "only zero-data-retention endpoints (fewest models)",
                           "allow": "any provider: more free models, some may keep prompts"}[pol]
            sw.log(RUN, f"[{sw.stamp()}] human: OpenRouter data policy {pol.upper()}")
            self.scan_or()
        self.confirm("pol:allow", do) if pol == "allow" else do()

    def or_private(self):
        st = sw.load(RUN)
        self.run_cli("private", "off" if st.get("private") else "on",
                     note="private run OFF" if st.get("private") else "private run ON: OpenRouter blocked")
        self.set_timer(1.0, self.scan_or)

    def or_set_mode(self, mode, confirmed=False):
        if mode == "paid" and not confirmed and OR.settings()["mode"] != "paid" and OR.key():
            return self.confirm("or:paid", lambda: self.or_set_mode("paid", True))
        if mode != "off" and not OR.key():
            self.or_msg = "paste a key first, then press SAVE KEY"
        else:
            OR.save_settings(mode=mode)
            self.or_msg = {"off": "OpenRouter off: nothing is sent to it.",
                           "free": "Free models only. Each is checked before use; failures fall back.",
                           "paid": "Free first; paid only when Sophon needs it, inside the run and day caps."}[mode]
            if hasattr(sw, "log") and RUN:
                try:
                    sw.log(RUN, f"[{sw.stamp()}] human: OpenRouter set to {mode.upper()}")
                except Exception:  # noqa: BLE001
                    pass
        self.scan_or()

    @work(thread=True, exclusive=True, group="or")
    def or_save(self, k):
        self.or_msg = "checking the key with OpenRouter ..."
        self.call_from_thread(self.scan_or)
        try:
            i = OR.set_key(k)
            bal = i.get("balance")
            self.or_msg = (f"saved and verified{' (free tier)' if i['free_tier'] else ''}"
                           + (f", balance ${bal:.2f}" if bal is not None else "") + ". Now pick FREE or PAID.")
            self.or_info, self.or_info_t, self.or_show_key = i, time.time(), False
        except OR.ORError as e:
            self.or_msg = f"not saved: {e}"[:160]
        self.call_from_thread(self.scan_or)

    @on(Input.Submitted, "#or-key")
    def or_key_enter(self, ev):
        k, ev.input.value = ev.value.strip(), ""
        if k:
            self.or_save(k)

    @on(Button.Pressed)
    def pressed(self, ev):
        bid = ev.button.id or ""
        if bid in ("o-off", "o-free", "o-paid"):
            return self.or_set_mode(bid[2:])
        if bid in ("p-deny", "p-zdr", "p-allow"):
            return self.or_policy(bid[2:])
        if bid == "o-private":
            return self.or_private()
        if bid == "o-change":
            self.or_show_key = True
            self.or_msg = "paste the new key, Enter saves (the old one stays until the new one is verified)"
            self.scan_or()
            self.set_timer(0.2, lambda: self.query_one("#or-key", Input).focus())
            return
        if bid == "o-del":
            b = ev.button
            if self.or_del_armed > time.time():
                OR.delete_key()
                self.or_del_armed = 0
                b.label = "DELETE KEY"
                self.or_msg = "key deleted; OpenRouter is off"
            else:
                self.or_del_armed = time.time() + 4
                b.label = "PRESS AGAIN"
                self.or_msg = "press DELETE again within 4 s to delete the saved key"
            self.scan_or()
            return
        if bid.startswith("m-"):
            self.action_mode(bid[2:])
        elif bid.startswith("d-"):
            self.run_cli("mode", "--thorough", bid[2:])
        elif bid == "a-plus":
            self.action_agents(1)
        elif bid == "a-minus":
            self.action_agents(-1)
        elif bid == "x-pause":
            self.action_pause()
        elif bid == "x-resume":
            self.action_resume()
        elif bid == "x-digest":
            self.action_digest()
        elif bid == "x-tile":
            self.action_retile()
        elif bid == "x-sleep":
            self.action_sleep()
        elif bid == "x-end":
            b = ev.button
            if self.armed_end:
                self.armed_end = 0
                b.set_class(False, "armed")
                b.label = "END RUN"
                self.run_cli("end", note="run ended: STOP flag set, agents told to wrap up")
            else:
                self.armed_end = time.time() + 4
                b.set_class(True, "armed")
                b.label = "PRESS AGAIN TO END"
        elif bid in ("r-approve", "r-reject"):
            table = self.query_one("#memory", DataTable)
            if not table.row_count:
                return
            rid = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
            if bid == "r-reject":
                self.run_cli("memo-done", rid, "rejected", "by the user from the panel")
            else:
                self.run_cli("say", sw.lead_of(self.st), f"The user approves memo request {rid}: store it in memory now "
                             f"and close it with swarm memo-done {rid} approved <memo id>.",
                             note=f"asked Sophon to store request {rid}")

    @on(DataTable.RowHighlighted)
    def highlighted(self, ev):
        key = ev.row_key.value if ev.row_key else None
        if not key:
            return
        if ev.data_table.id == "runs":
            r = RUNS / key
            f = r / "FINAL.md" if (r / "FINAL.md").exists() else r / "context" / "BRIEF.md"
            self.query_one("#run-md", Markdown).update(f.read_text()[:60000] if f.exists() else "Nothing written yet.")
            return
        if ev.data_table.id == "memory":
            f = RUN / "requests" / f"{key}.md"
            if f.exists():
                self.query_one("#memo-md", Markdown).update(f"```\n{f.read_text()}\n```")
        else:
            f = RUN / key
            if f.exists():
                self.query_one("#out-md", Markdown).update(f.read_text()[:60000])

    @on(Input.Submitted, "#say")
    def send(self, ev):
        text = ev.value.strip()
        ev.input.value = ""
        if not text:
            return
        m = re.match(r"^([a-z0-9-]+):\s*(.+)$", text, re.S)
        to, msg = (m[1], m[2]) if m and (m[1] == "all" or m[1] in self.st.get("agents", {})) else (
            sw.lead_of(self.st), text)
        self.run_cli("say", to, msg, note=f"sent to {to}")

    def action_mode(self, mode):
        if self.st.get("mode") in sw.MODES and mode != self.st.get("mode"):
            if f"mode:{mode}" in CONFIRM:
                return self.confirm(f"mode:{mode}", lambda: self.run_cli("mode", mode))
            self.run_cli("mode", mode)

    def action_depth(self, step):
        t = max(1, min(5, int(self.st.get("thorough") or 3) + step))
        self.run_cli("mode", "--thorough", str(t))

    def action_agents(self, step):
        st = self.st
        if st.get("mode") not in sw.MODES:
            self.notify("intake is still running", severity="warning")
            return
        workers = [n for n in st["agents"] if n not in sw.LEADS and n not in ("q1", "q2") and sw.alive(n)]
        target = max(0, int(st.get("agents_target") or len(workers)) + step)
        self.agent_change(step, workers, target)

    @work(thread=True, group="cli")
    def agent_change(self, step, workers, target):
        swarm("mode", "--agents", str(target))
        lead = sw.lead_of(sw.load(RUN))
        if step > 0:
            n = 1
            while f"w{n}" in self.st["agents"]:
                n += 1
            out = swarm("spawn", f"w{n}", "worker")
            if sw.alive(f"w{n}") and sw.alive(lead):
                sw.deliver(RUN, "human", lead, f"The user added worker w{n} from the panel (target now {target}); "
                           f"give it a task.")
        elif workers:
            victim = workers[-1]
            swarm("kill", victim)
            out = f"removed {victim}"
            if sw.alive(lead):
                sw.deliver(RUN, "human", lead, f"The user removed worker {victim} from the panel (target now {target}).")
        else:
            out = "no workers to remove"
        self.call_from_thread(self.notify, out)

    def worker_names(self):
        return [n for n in self.st.get("agents", {}) if n not in sw.LEADS and sw.alive(n)]

    def action_pause(self):
        ws = self.worker_names()
        for n in ws:
            local = self.st["agents"].get(n, {}).get("engine") in ("local", "local-lead")
            subprocess.run(["tmux", "send-keys", "-t", f"=sw-{n}:", "C-c" if local else "Escape"], capture_output=True)
        sw.log(RUN, f"[{sw.stamp()}] human: paused {', '.join(ws) or 'nobody'} from the panel")
        self.notify(f"paused {len(ws)} workers")

    def action_resume(self):
        for n in self.worker_names():
            self.run_cli("say", n, "resume where you left off", note="resumed")

    def action_digest(self):
        self.last_digest = 0
        self.notify("digest goes out on the next tick")

    def action_sleep(self):
        with sw.state(RUN) as st:
            st["sleep"] = not st.get("sleep")
        lead = sw.lead_of(st)
        if st["sleep"]:
            msg = ("The user is going to sleep now. From here: never wait for them or ask them; use `swarm ask \"q\"` "
                   "for decisions they would make, put anything needing their consent in a list in FINAL.md, and use "
                   "the time for extra critique and refinement rounds.")
        else:
            msg = "The user is back and watching. Ask them directly again when needed."
        if st["sleep"]:
            sw.keep_awake(RUN)  # no suspend, idle sleep or lid suspend until sleep mode ends
        sw.log(RUN, f"[{sw.stamp()}] human: sleep mode {'ON' if st['sleep'] else 'OFF'}")
        if sw.alive(lead):
            self.run_cli("say", lead, msg, note="sleep mode ON: nothing will wait for you" if st["sleep"]
                         else "sleep mode OFF")

    def action_retile(self):
        self.run_cli("tile", note="windows retiled")

    def action_focus_comms(self):
        self.query_one("#say", Input).focus()

    def action_blur(self):
        self.set_focus(None)


if __name__ == "__main__":
    SophonPanel().run()

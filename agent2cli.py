#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent2cli.py  —  Agent 2 CLI
────────────────────────────
Run via:  python run.py --cli
      or: venv/bin/python agent2cli.py   (after run.py setup)

Keys are loaded ONLY from .env in the same folder as this script.
Key rotation: if one key hits quota, the next is tried automatically.

Commands:
  /help          show all commands
  /addapi        add an API key to .env
  /model [name]  switch model
  /mode  [name]  switch mode (fast | pro | thinking)
  /clear         clear current conversation
  /history       show recent messages
  /clearhistory  clear message history
  /memory        list saved memories
  /addmem <txt>  add a memory
  /workspace [p] show or set working directory
  /run <cmd>     run a shell command directly
  /read <file>   read a file
  /write <f>     write to a file
  /ls [path]     list directory
  /analyze <p>   analyze a project
  /search <q>    web search
  /exit          quit
"""

import os, sys, re, json, shutil, threading, time, platform
from turtle import clearscreen
import subprocess, urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime

# ── Locate project root (.env lives next to run.py / agent2cli.py) ────────────
ROOT     = Path(__file__).parent.resolve()
ENV_FILE = ROOT / ".env"
DATA_DIR = Path.home() / ".agent2"
HST_FILE = DATA_DIR / "history.json"
MEM_FILE = DATA_DIR / "memories.json"
PT_HISTORY = DATA_DIR / "cli_history.txt"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Windows console fixes ──────────────────────────────────────────────────────
OS_NAME = platform.system()
IS_WIN  = OS_NAME == "Windows"
IS_MAC  = OS_NAME == "Darwin"

if IS_WIN:
    os.system("chcp 65001 >nul 2>&1")
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except Exception: pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

# ── Rich (installed by run.py) ─────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich import box as rbox
    _RICH = True
    _con  = Console(highlight=False)
except ImportError:
    _RICH = False
    _con  = None

# ── prompt_toolkit ─────────────────────────────────────────────────────────────
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.styles import Style
    _PTK = True
except ImportError:
    print("\n  [ERR]  prompt-toolkit not installed.")
    print("         Run:  pip install prompt-toolkit\n")
    sys.exit(1)

# ── Gemini ─────────────────────────────────────────────────────────────────────
try:
    import google.genai as genai
    from google.genai import types as gtypes
except ImportError:
    print("\n  [ERR]  google-genai not installed.")
    print("         Run:  python run.py --cli   (it installs everything)\n")
    sys.exit(1)

# ── ANSI colour helpers ────────────────────────────────────────────────────────
R  = "\033[0m"; B  = "\033[1m"; D  = "\033[2m"
PU = "\033[38;5;135m"; CY = "\033[38;5;81m";  GR = "\033[38;5;83m"
YW = "\033[38;5;221m"; RD = "\033[38;5;203m"; WH = "\033[38;5;255m"
MG = "\033[38;5;177m"

def _p(col, text): return f"{col}{text}{R}"
def ok(t):   return _p(GR, t)
def warn(t): return _p(YW, t)
def err(t):  return _p(RD, t)
def dim(t):  return _p(D,  t)
def pu(t):   return _p(PU, t)
def cy(t):   return _p(CY, t)

# ── Platform ───────────────────────────────────────────────────────────────────
def detect_shell():
    if IS_WIN:
        ps = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if ps: return ps, "PowerShell", "-Command"
        return "cmd.exe", "CMD", "/c"
    sh = os.environ.get("SHELL", "")
    for s in [sh, "/bin/bash", "/bin/zsh", "/bin/sh"]:
        if s and shutil.which(s):
            return s, Path(s).name.upper(), "-c"
    return "/bin/sh", "SH", "-c"

SHELL_BIN, SHELL_LABEL, SHELL_FLAG = detect_shell()

def shell_argv(cmd: str) -> list:
    if IS_WIN and SHELL_BIN.lower().endswith("cmd.exe"):
        return ["cmd.exe", "/c", cmd]
    return [SHELL_BIN, SHELL_FLAG, cmd]

# ── Models & modes ─────────────────────────────────────────────────────────────
MODELS = {
    "2.5-flash-lite": "gemini-2.5-flash-lite",
    "2.5-flash":      "gemini-2.5-flash",
    "2.5-pro":        "gemini-2.5-pro",
    "3.1-flash-lite": "gemini-3.1-flash-lite",
    "3.1-flash":      "gemini-3.1-flash",
    "3.1-pro":        "gemini-3.1-pro",
}
DEFAULT_MODEL = "2.5-flash-lite"

MODES = {
    "fast":     {"icon": "⚡", "max_tokens": 2048,  "thinking": False},
    "pro":      {"icon": "★",  "max_tokens": 8192,  "thinking": False},
    "thinking": {"icon": "🧠", "max_tokens": 16384, "thinking": True, "thinking_budget": 8000},
}
DEFAULT_MODE = "pro"

# ── .env key management ────────────────────────────────────────────────────────
def _read_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def _write_env(env: dict):
    ENV_FILE.write_text(
        "\n".join(f"{k}={v}" for k, v in env.items()) + "\n",
        encoding="utf-8"
    )

def load_keys() -> list[dict]:
    """Return list of {key, label, active, errs} from .env."""
    env = _read_env()
    keys, seen = [], set()
    placeholder = "your_gemini_api_key_here"
    for i, name in enumerate(["GEMINI_API_KEY"] + [f"GEMINI_API_KEY_{j}" for j in range(2, 10)]):
        v = env.get(name, "").strip()
        if v and v != placeholder and len(v) > 10 and v not in seen:
            keys.append({"key": v, "label": str(i + 1), "active": True, "errs": 0})
            seen.add(v)
    return keys

def save_key_to_env(new_key: str) -> tuple[bool, str]:
    """Append a new key to .env. Returns (success, label)."""
    new_key = new_key.strip().replace(" ", "")
    if len(new_key) < 15:
        return False, "key too short"
    existing_vals = [k["key"] for k in load_keys()]
    if new_key in existing_vals:
        return False, "already exists"
    env = _read_env()
    # find next free slot
    used_names = {n for n in env if re.match(r"^GEMINI_API_KEY", n)}
    label = 1
    while True:
        name = "GEMINI_API_KEY" if label == 1 else f"GEMINI_API_KEY_{label+1}"
        if name not in used_names:
            break
        label += 1
    env[name] = new_key
    _write_env(env)
    return True, str(label)

# ── Key rotator (in-memory, seeded from .env) ──────────────────────────────────
class KeyRotator:
    _lock = threading.Lock()

    def __init__(self):
        self._entries: list[dict] = []
        self.reload()

    def reload(self):
        with self._lock:
            self._entries = load_keys()

    def get(self) -> tuple:
        """Return (client, raw_key, label) — picks first active key."""
        with self._lock:
            active = [e for e in self._entries if e["active"]]
            if not active:
                # reset all and retry once
                for e in self._entries: e["active"] = True; e["errs"] = 0
                active = self._entries
            if not active:
                return None, None, None
            e = active[0]
            return genai.Client(api_key=e["key"]), e["key"], e["label"]

    def fail(self, key: str, quota: bool = False):
        with self._lock:
            for e in self._entries:
                if e["key"] == key:
                    e["errs"] += 1
                    if quota or e["errs"] >= 3:
                        e["active"] = False
                    break

    def next_active(self, current_key: str) -> tuple:
        """After a failure, get the next different active key."""
        with self._lock:
            active = [e for e in self._entries if e["active"] and e["key"] != current_key]
            if not active:
                return None, None, None
            e = active[0]
            return genai.Client(api_key=e["key"]), e["key"], e["label"]

    def status(self) -> list[dict]:
        with self._lock:
            return [{"label": e["label"], "preview": e["key"][:14] + "…",
                     "active": e["active"]} for e in self._entries]

_rotator = KeyRotator()

# ── Memories ───────────────────────────────────────────────────────────────────
def load_mems() -> list:
    if MEM_FILE.exists():
        try: return json.loads(MEM_FILE.read_text(encoding="utf-8"))
        except: pass
    return []

def save_mems(mems: list):
    MEM_FILE.write_text(json.dumps(mems, indent=2, ensure_ascii=False), encoding="utf-8")

def add_mem(content: str, importance: int = 5, tags: list = None):
    mems = load_mems()
    mems.append({"id": f"{time.time():.0f}", "content": content.strip(),
                 "importance": importance, "tags": tags or [],
                 "created": datetime.now().isoformat()})
    save_mems(mems)

# ── History ────────────────────────────────────────────────────────────────────
def load_history() -> list:
    if HST_FILE.exists():
        try: return json.loads(HST_FILE.read_text(encoding="utf-8"))[-60:]
        except: pass
    return []

def save_history(h: list):
    HST_FILE.write_text(json.dumps(h[-100:], indent=2, ensure_ascii=False), encoding="utf-8")

# ── Terminal width ─────────────────────────────────────────────────────────────
def tw() -> int:
    return min(shutil.get_terminal_size((100, 30)).columns, 120)

# ── Print helpers ──────────────────────────────────────────────────────────────
def hr(char="─", col=D):
    print(f"{col}{char * (tw() - 2)}{R}")

def status_line(msg: str, kind: str = "info"):
    sym  = {"info": "ℹ", "success": "✓", "warning": "⚠", "error": "✗"}.get(kind, "•")
    _col = {"info": CY, "success": GR, "warning": YW, "error": RD}.get(kind, D)
    if _RICH:
        style = {"info":"#60b8ff","success":"#3ddc84","warning":"#f0c060","error":"#ff5555"}.get(kind,"dim")
        _con.print(f"  [{style}]{sym}[/] {msg}")
    else:
        print(f"  {_col}{sym}{R} {msg}")

def print_banner():
    os.system("cls" if IS_WIN else "clear")
    keys = _rotator.status()
    if _RICH:
        title = Text()
        title.append("  ⚡ ", style="bold yellow")
        title.append("Agent 2 CLI", style="bold #7c6af7")
        title.append(f"  {OS_NAME}/{SHELL_LABEL}", style="dim")
        _con.print(Panel(title, border_style="#1e1e30", padding=(0, 1)))
        for k in keys:
            st = "[bold #3ddc84]●[/]" if k["active"] else "[bold #ff5555]●[/]"
            _con.print(f"  {st} Key #{k['label']}: [dim]{k['preview']}[/]")
        if not keys:
            _con.print("  [bold #ff5555]⚠[/] No API keys — run [bold]/addapi[/]")
    else:
        w = min(tw(), 56)
        print(f"{PU}{'═' * w}{R}")
        print(f"{PU}{B}  ⚡ Agent 2 CLI{R}  {D}{OS_NAME}/{SHELL_LABEL}{R}")
        for k in keys:
            col = GR if k["active"] else RD
            print(f"  {col}●{R}  Key #{k['label']}: {D}{k['preview']}{R}")
        if not keys:
            print(f"  {YW}⚠  No API keys — type /addapi{R}")
        print(f"{PU}{'═' * w}{R}")
    print()

def print_help():
    cmds = [
        ("/help",             "Show this help"),
        ("/addapi",           "Add a Gemini API key to .env"),
        ("/model [name]",     "Switch model  (2.5-flash-lite | 2.5-flash | 2.5-pro | 3.1-*)"),
        ("/mode [name]",      "Switch mode   (fast ⚡ | pro ★ | thinking 🧠)"),
        ("/clear",            "Clear conversation (start fresh)"),
        ("/history",          "Show last 10 messages"),
        ("/clearhistory",     "Clear message history"),
        ("/memory",           "List all saved memories"),
        ("/addmem <text>",    "Save a memory manually"),
        ("/workspace [path]", "Show or set working directory for commands"),
        ("/run <cmd>",        "Run a shell command directly"),
        ("/read <file>",      "Read a file's contents"),
        ("/write <file>",     "Write text to a file (prompts for content)"),
        ("/ls [path]",        "List directory tree"),
        ("/analyze <path>",   "Detect framework / language / run command"),
        ("/search <query>",   "Web search (DuckDuckGo)"),
        ("/keys",             "Show API key status"),
        ("/exit  or  Ctrl+C", "Quit"),
    ]
    if _RICH:
        t = Table(show_header=True, header_style="bold #7c6af7",
                  box=rbox.SIMPLE_HEAD, border_style="dim")
        t.add_column("Command",     style="#60b8ff", no_wrap=True)
        t.add_column("Description", style="#c4c4dc")
        for cmd, desc in cmds:
            t.add_row(cmd, desc)
        _con.print(t)
    else:
        print(f"\n{PU}{B}  Commands:{R}")
        for cmd, desc in cmds:
            print(f"  {CY}{cmd:<28}{R}{D}{desc}{R}")
        print()

def print_agent_reply(text: str):
    """Render agent markdown reply."""
    if _RICH:
        hr_style = "#1e1e30"
        _con.rule(style=hr_style)
        _con.print(f"  [bold #7c6af7]⚡ Agent 2[/]  [dim]{datetime.now().strftime('%H:%M')}[/]")
        _con.print()
        _con.print(Markdown(text), style="#c4c4dc")
        _con.print()
    else:
        hr()
        print(f"  {PU}{B}⚡ Agent 2{R}  {D}{datetime.now().strftime('%H:%M')}{R}")
        print()
        _render_markdown_plain(text)
        print()

def _render_markdown_plain(text: str):
    in_code = False
    lang    = ""
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            lang = line[3:].strip() if in_code else ""
            if in_code:  print(f"  {D}┌{'─' * 50}{R}")
            else:        print(f"  {D}└{'─' * 50}{R}")
            continue
        if in_code:
            print(f"  {YW}│ {line}{R}"); continue
        if   line.startswith("# "):   print(f"\n  {WH}{B}{line[2:]}{R}")
        elif line.startswith("## "):  print(f"\n  {CY}{B}{line[3:]}{R}")
        elif line.startswith("### "): print(f"  {PU}{line[4:]}{R}")
        elif re.match(r"^[-*] ", line): print(f"  {D}•{R} {line[2:]}")
        elif re.match(r"^\d+\. ", line):
            n, rest = line.split(". ", 1); print(f"  {PU}{n}.{R} {rest}")
        else:
            line = re.sub(r"\*\*(.+?)\*\*", f"{WH}{B}\\1{R}", line)
            line = re.sub(r"`(.+?)`",        f"{YW}\\1{R}", line)
            print(f"  {line}")

def print_tool_call(name: str, desc: str, detail: str = ""):
    icons = {"run_command":"⚙️","read_file":"📄","write_file":"✏️",
             "list_directory":"📁","analyze_project":"🔍",
             "web_search":"🌐","save_memory":"🧠","emit_plan":"📋"}
    icon = icons.get(name, "🔧")
    if _RICH:
        body = Text()
        body.append(f" {icon} ", style="bold")
        body.append(name, style="bold #f0c060")
        body.append(f"  {desc}", style="dim")
        if detail: body.append(f"\n   $ {detail}", style="#f0c060")
        _con.print(Panel(body, border_style="#2a2a40", padding=(0, 1)))
    else:
        print(f"\n  {YW}▶ {name}{R}  {D}{desc}{R}")
        if detail: print(f"  {YW}$ {detail}{R}")

def print_plan(title: str, steps: list):
    if _RICH:
        body = Text()
        body.append(f"{title}\n\n", style="bold white")
        for i, s in enumerate(steps, 1):
            body.append(f"  {i}. ", style="bold #7c6af7")
            body.append(f"{s}\n",   style="#c4c4dc")
        _con.print(Panel(body, title="[bold #7c6af7]📋 Plan[/]",
                         border_style="#3a2a70", padding=(0, 1)))
    else:
        print(f"\n  {PU}{B}📋 {title}{R}")
        for i, s in enumerate(steps, 1):
            print(f"  {PU}{i}.{R} {s}")
        print()

# ── Spinner ────────────────────────────────────────────────────────────────────
class Spinner:
    _frames = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

    def __init__(self, msg: str = "Thinking"):
        self._msg  = msg
        self._stop = threading.Event()
        self._t    = None
        self._prog = None

    def start(self):
        if _RICH:
            self._prog = Progress(SpinnerColumn(), TextColumn("[dim]{task.description}"),
                                  transient=True, console=_con)
            self._prog.start()
            self._prog.add_task(self._msg)
        else:
            self._t = threading.Thread(target=self._spin, daemon=True)
            self._t.start()

    def stop(self):
        self._stop.set()
        if _RICH and self._prog:
            self._prog.stop()
        if self._t:
            self._t.join(timeout=0.5)
        if not _RICH:
            print(f"\r{' ' * (tw() - 2)}\r", end="", flush=True)

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            print(f"\r  {PU}{self._frames[i % len(self._frames)]}{R} {D}{self._msg}…{R}",
                  end="", flush=True)
            i += 1
            time.sleep(0.08)

# ── Run command (streaming) ────────────────────────────────────────────────────
def run_cmd_stream(cmd: str, cwd: str | None = None) -> tuple[str, int]:
    work_dir = str(Path(cwd).expanduser()) if cwd else str(Path.cwd())
    output   = []
    if _RICH:
        _con.print(f"  [dim]$ {cmd}[/]")
    else:
        print(f"  {D}$ {cmd}{R}")
    try:
        proc = subprocess.Popen(
            shell_argv(cmd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, bufsize=1, universal_newlines=True,
            env=os.environ.copy(), cwd=work_dir,
        )
        for line in proc.stdout:
            output.append(line)
            stripped = line.rstrip("\n")
            if _RICH: _con.print(f"  [dim]│[/] {stripped}")
            else:     print(f"  {D}│{R} {stripped}")
        proc.wait()
        rc  = proc.returncode
        sym = "✓" if rc == 0 else "✗"
        col_r = GR if rc == 0 else RD
        if _RICH:
            style = "bold #3ddc84" if rc == 0 else "bold #ff5555"
            _con.print(f"  [{style}]{sym} exit {rc}[/]")
        else:
            print(f"  {col_r}{B}{sym} exit {rc}{R}")
        return "".join(output), rc
    except Exception as ex:
        msg = str(ex)
        if _RICH: _con.print(f"  [bold #ff5555]✗ {msg}[/]")
        else:     print(f"  {RD}✗ {msg}{R}")
        return msg, -1

# ── Tool implementations (same logic as web app) ───────────────────────────────
MAX_FILE = 64_000
_SKIP    = {"__pycache__", ".git", "node_modules", ".venv", "venv", "env",
            "dist", "build", ".next", "target", ".DS_Store"}

def _impl_read(args: dict) -> dict:
    p = Path(args["path"]).expanduser()
    s = args.get("start_line"); e = args.get("end_line")
    try:
        if not p.exists(): return {"error": f"Not found: {p}"}
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        sl, el = (s - 1 if s else 0), (e if e else total)
        content = "".join(lines[sl:el])
        if len(content) > MAX_FILE:
            content = content[:MAX_FILE] + "\n…[truncated]"
        return {"content": content, "total_lines": total, "path": str(p)}
    except Exception as ex: return {"error": str(ex)}

def _impl_write(args: dict) -> dict:
    p = Path(args["path"]).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if args.get("append") else "w"
        with open(p, mode, encoding="utf-8") as f: f.write(args["content"])
        return {"success": True, "path": str(p), "lines": args["content"].count("\n") + 1}
    except Exception as ex: return {"error": str(ex)}

def _impl_ls(args: dict) -> dict:
    p     = Path(args.get("path", ".")).expanduser()
    depth = min(int(args.get("depth", 3)), 6)

    def _tree(pp: Path, d: int, pfx: str = "") -> list[str]:
        if d > depth: return []
        lines = []
        try:
            entries = sorted(pp.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except PermissionError:
            return [f"{pfx}[permission denied]"]
        for i, e in enumerate(entries[:200]):
            if any(e.name == ig or e.name.endswith(ig.lstrip("*")) for ig in _SKIP):
                continue
            last    = (i == len(entries) - 1)
            conn    = "└── " if last else "├── "
            ext     = "    " if last else "│   "
            if e.is_dir():
                lines.append(f"{pfx}{conn}{e.name}/")
                lines.extend(_tree(e, d + 1, pfx + ext))
            else:
                sz = e.stat().st_size
                s  = f"{sz // 1024}KB" if sz >= 1024 else f"{sz}B"
                lines.append(f"{pfx}{conn}{e.name} ({s})")
        return lines

    try:
        if not p.exists(): return {"error": f"Not found: {p}"}
        tree_lines = [str(p) + "/"] + _tree(p, 1)
        return {"tree": "\n".join(tree_lines)}
    except Exception as ex: return {"error": str(ex)}

_FP = [
    ("package.json",       "Node.js",        "JavaScript",      "npm",     "npm start"),
    ("requirements.txt",   "Python",          "Python",          "pip",     "python main.py"),
    ("pyproject.toml",     "Python",          "Python",          "pip",     "python main.py"),
    ("manage.py",          "Django",          "Python",          "pip",     "python manage.py runserver"),
    ("app.py",             "Flask/FastAPI",   "Python",          "pip",     "python app.py"),
    ("Cargo.toml",         "Rust",            "Rust",            "cargo",   "cargo run"),
    ("go.mod",             "Go",              "Go",              "go",      "go run ."),
    ("pom.xml",            "Maven/Java",      "Java",            "mvn",     "mvn exec:java"),
    ("next.config.js",     "Next.js",         "TypeScript",      "npm",     "npm run dev"),
    ("vite.config.js",     "Vite",            "JavaScript",      "npm",     "npm run dev"),
    ("vite.config.ts",     "Vite",            "TypeScript",      "npm",     "npm run dev"),
    ("docker-compose.yml", "Docker Compose",  "Any",             "docker",  "docker-compose up"),
    ("Dockerfile",         "Docker",          "Any",             "docker",  "docker build ."),
    ("Makefile",           "Make/C++",        "C/C++",           "make",    "make"),
    ("pubspec.yaml",       "Flutter",         "Dart",            "flutter", "flutter run"),
]

def _impl_analyze(args: dict) -> dict:
    p = Path(args["path"]).expanduser()
    if not p.exists(): return {"error": f"Not found: {p}"}
    if not p.is_dir(): p = p.parent
    files  = [f.name for f in p.iterdir() if f.is_file()]
    result = {"path": str(p), "framework": None, "language": None,
              "package_manager": None, "run_cmd": None, "deps": []}
    for fname, fw, lang, pm, run in _FP:
        if fname in files:
            result.update({"framework": fw, "language": lang,
                           "package_manager": pm, "run_cmd": run})
            break
    # Parse deps
    for fp_name, parser in [
        ("requirements.txt", lambda fp: [
            l.strip().split(">=")[0].split("==")[0]
            for l in fp.read_text().splitlines() if l.strip() and not l.startswith("#")
        ]),
        ("package.json", lambda fp: list({
            **json.loads(fp.read_text()).get("dependencies", {}),
            **json.loads(fp.read_text()).get("devDependencies", {}),
        }.keys())),
    ]:
        fp = p / fp_name
        if fp.exists():
            try: result["deps"] = parser(fp)[:30]
            except: pass
    # Detect venv
    for vd in ["venv", ".venv", "env"]:
        if (p / vd).is_dir():
            act = f"{vd}\\Scripts\\activate && " if IS_WIN else f"source {vd}/bin/activate && "
            result["run_cmd"] = act + (result["run_cmd"] or "python main.py")
            break
    # README hint
    for rname in ["README.md", "README.txt", "readme.md"]:
        if (p / rname).exists():
            result["readme"] = rname; break
    return result

def _impl_search(args: dict) -> dict:
    q = args.get("query", "")
    try:
        url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
            {"q": q, "format": "json", "no_html": "1", "skip_disambig": "1"})
        req = urllib.request.Request(url, headers={"User-Agent": "Agent 2CLI/2.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        results = []
        if data.get("AbstractText"):
            results.append({"title": data.get("Heading",""), "snippet": data["AbstractText"][:400]})
        for t in data.get("RelatedTopics", [])[:4]:
            if isinstance(t, dict) and t.get("Text"):
                results.append({"title": t["Text"][:80], "snippet": t["Text"][:300]})
        return {"query": q, "results": results[:5]} if results else {"query": q, "results": [], "note": "No results"}
    except Exception as ex:
        return {"error": str(ex), "query": q}

def _impl_save_mem(args: dict) -> dict:
    c = args.get("content", "").strip()
    if not c: return {"error": "content required"}
    imp  = min(10, max(1, int(args.get("importance", 5))))
    tags = [t.strip() for t in args.get("tags", "").split(",") if t.strip()]
    add_mem(c, imp, tags)
    return {"saved": True}

def _impl_plan(args: dict) -> dict:
    title = args.get("title", "Plan")
    try:    steps = json.loads(args.get("steps", "[]"))
    except: steps = [args.get("steps", "")]
    print_plan(title, steps)
    return {"plan_emitted": True}

def dispatch_tool(name: str, args: dict, cwd: str | None = None) -> dict:
    if name == "read_file":       return _impl_read(args)
    if name == "write_file":      return _impl_write(args)
    if name == "list_directory":  return _impl_ls(args)
    if name == "analyze_project": return _impl_analyze(args)
    if name == "web_search":      return _impl_search(args)
    if name == "save_memory":     return _impl_save_mem(args)
    if name == "emit_plan":       return _impl_plan(args)
    return {"error": f"Unknown tool: {name}"}

# ── Gemini tool declarations ────────────────────────────────────────────────────
def _build_tools() -> gtypes.Tool:
    S = gtypes.Schema; T = gtypes.Type
    return gtypes.Tool(function_declarations=[
        gtypes.FunctionDeclaration(name="run_command",
            description=f"Execute a shell command on {OS_NAME} ({SHELL_LABEL}). Use for running scripts, installs, scans, builds.",
            parameters=S(type=T.OBJECT, properties={
                "command":     S(type=T.STRING),
                "description": S(type=T.STRING),
                "cwd":         S(type=T.STRING),
            }, required=["command","description"])),
        gtypes.FunctionDeclaration(name="read_file",
            description="Read a file's contents. Always read before editing.",
            parameters=S(type=T.OBJECT, properties={
                "path":       S(type=T.STRING),
                "start_line": S(type=T.INTEGER),
                "end_line":   S(type=T.INTEGER),
            }, required=["path"])),
        gtypes.FunctionDeclaration(name="write_file",
            description="Create or overwrite a file with content.",
            parameters=S(type=T.OBJECT, properties={
                "path":    S(type=T.STRING),
                "content": S(type=T.STRING),
                "append":  S(type=T.BOOLEAN),
            }, required=["path","content"])),
        gtypes.FunctionDeclaration(name="list_directory",
            description="List directory contents as a tree.",
            parameters=S(type=T.OBJECT, properties={
                "path":  S(type=T.STRING),
                "depth": S(type=T.INTEGER),
            }, required=["path"])),
        gtypes.FunctionDeclaration(name="analyze_project",
            description="Analyze a project: detect framework, language, deps, run command. Call FIRST before running any project.",
            parameters=S(type=T.OBJECT, properties={
                "path": S(type=T.STRING),
            }, required=["path"])),
        gtypes.FunctionDeclaration(name="web_search",
            description="Search the web for CVEs, docs, error messages, latest info.",
            parameters=S(type=T.OBJECT, properties={
                "query":       S(type=T.STRING),
                "max_results": S(type=T.INTEGER),
            }, required=["query"])),
        gtypes.FunctionDeclaration(name="save_memory",
            description="Save an important fact to long-term memory (persists across sessions).",
            parameters=S(type=T.OBJECT, properties={
                "content":    S(type=T.STRING),
                "importance": S(type=T.INTEGER),
                "tags":       S(type=T.STRING),
            }, required=["content"])),
        gtypes.FunctionDeclaration(name="emit_plan",
            description="Show a step-by-step plan before a complex multi-step task.",
            parameters=S(type=T.OBJECT, properties={
                "title": S(type=T.STRING),
                "steps": S(type=T.STRING),
            }, required=["title","steps"])),
    ])

# ── System prompt ──────────────────────────────────────────────────────────────
def build_sys_prompt(workspace: str | None = None) -> str:
    if IS_WIN:
        plat = ("PLATFORM: Windows / CMD+PowerShell\n"
                "ipconfig | dir | type | python | pip | ping -n 4 | winget/choco for packages")
    elif IS_MAC:
        plat = "PLATFORM: macOS / zsh\nifconfig | ls | python3 | pip3 | brew install"
    else:
        plat = "PLATFORM: Linux / bash\nip addr | ls | python3 | pip3 | apt/dnf/pacman"

    mems = load_mems()
    mem_block = ""
    if mems:
        top = sorted(mems, key=lambda x: -x.get("importance", 5))[:20]
        mem_block = "\n\n## MEMORIES:\n" + "\n".join(
            f"- [{m['importance']}/10] {m['content']}" for m in top)

    ws_block = f"\n\n## ACTIVE WORKSPACE: {workspace}\nAll relative paths resolve here." if workspace else ""

    return f"""You are Agent 2 — an elite autonomous AI development and security agent in a terminal.

{plat}

## WHAT YOU CAN DO
- Write, debug, refactor any code with full fenced code blocks
- Execute real shell commands with live output
- Read/write files, analyze projects, search the web
- Run security tools: nmap, nikto, gobuster, sqlmap, hydra, metasploit
- Remember facts across sessions with save_memory
- Emit step-by-step plans for complex tasks

## TOOL DECISION RULES
- Ask to run a project? → analyze_project FIRST, then run
- Edit any existing file? → read_file FIRST
- Task with 3+ steps? → emit_plan FIRST
- Need current CVEs, docs, error info? → web_search
- Learned something important? → save_memory

## RESPONSE STYLE
- Use markdown: headers, **bold**, `code`, tables
- Always include language tag on code blocks: ```python, ```bash
- Summarize command output clearly
- After finishing: confirm what was done + suggest next steps{mem_block}{ws_block}
"""

# ── Agent loop ─────────────────────────────────────────────────────────────────
def run_agent(
    user_msg:  str,
    history:   list,
    model_key: str,
    mode_key:  str,
    workspace: str | None = None,
) -> list:
    """One full agentic turn. Returns updated history."""

    client, key, label = _rotator.get()
    if not client:
        status_line("No API keys found. Run:  python run.py --addapi", "error")
        return history

    api_model = MODELS.get(model_key, MODELS[DEFAULT_MODEL])
    mode_cfg  = MODES.get(mode_key,  MODES[DEFAULT_MODE])

    # Generation config
    cfg_kw: dict = dict(
        system_instruction=build_sys_prompt(workspace),
        tools=[_build_tools()],
        tool_config=gtypes.ToolConfig(
            function_calling_config=gtypes.FunctionCallingConfig(mode="AUTO")
        ),
        max_output_tokens=mode_cfg["max_tokens"],
    )
    if mode_cfg.get("thinking") and model_key in ("2.5-pro","3.1-flash","3.1-pro","3.1-flash-lite","2.5-flash","2.5-flash-lite"):
        try:
            cfg_kw["thinking_config"] = gtypes.ThinkingConfig(
                thinking_budget=mode_cfg.get("thinking_budget", 8000))
        except Exception: pass

    gen_cfg = gtypes.GenerateContentConfig(**cfg_kw)

    # Build context from history (last 20 turns)
    context = []
    for h in history[-20:]:
        if   h["role"] == "user":      context.append(gtypes.Content(role="user",  parts=[gtypes.Part(text=h["content"])]))
        elif h["role"] == "assistant": context.append(gtypes.Content(role="model", parts=[gtypes.Part(text=h["content"])]))

    context.append(gtypes.Content(role="user", parts=[gtypes.Part(text=user_msg)]))
    history.append({"role": "user", "content": user_msg, "ts": datetime.now().isoformat()})

    total_tokens = 0

    for _iteration in range(12):
        mode_icon = mode_cfg["icon"]
        spin_msg  = f"Agent 2  [{model_key} / {mode_key} {mode_icon}]  key #{label}"
        spin = Spinner(spin_msg)
        spin.start()

        try:
            resp = client.models.generate_content(model=api_model, contents=context, config=gen_cfg)
        except KeyboardInterrupt:
            spin.stop()
            print()
            status_line("Interrupted.", "warning")
            return history
        except Exception as exc:
            spin.stop()
            es = str(exc)
            is_quota  = "429" in es or "quota" in es.lower() or "exhausted" in es.lower()
            is_model  = any(k in es.lower() for k in ("not found","invalid","unsupported","model"))
            _rotator.fail(key, quota=is_quota)

            if is_quota:
                # try next key
                c2, k2, l2 = _rotator.next_active(key)
                if c2:
                    status_line(f"Quota hit on key #{label} — switching to key #{l2}", "warning")
                    client, key, label = c2, k2, l2
                    continue
            hint = "\n  Tip: /model 2.5-flash-lite" if is_model else ""
            status_line(f"API Error ({model_key}): {es}{hint}", "error")
            return history
        finally:
            spin.stop()

        # Parse response
        try:
            candidate = resp.candidates[0] if resp.candidates else None
            if not candidate or not candidate.content:
                fr = getattr(candidate, "finish_reason", "?") if candidate else "none"
                status_line(f"Empty response (finish_reason={fr}). Try /model 2.5-flash-lite", "warning")
                return history
            parts = candidate.content.parts or []
        except Exception as ex:
            status_line(f"Parse error: {ex}", "error")
            return history

        func_calls: list = []
        texts:      list = []
        for p in parts:
            try:
                if p.function_call and p.function_call.name: func_calls.append(p.function_call)
                elif p.text: texts.append(p.text)
            except Exception: pass

        # Tokens
        try:    tok = getattr(resp.usage_metadata, "total_token_count", 0) or 0
        except: tok = 0
        total_tokens += tok
        if tok:
            if _RICH: _con.print(f"  [dim]tokens: {total_tokens:,}[/]")
            else:     print(f"  {D}tokens: {total_tokens:,}{R}")

        # Interim text (before tool calls)
        if texts and func_calls:
            print()
            for t in texts: print(f"  {D}{t[:200]}{R}")

        # Tool calls
        if func_calls:
            context.append(gtypes.Content(role="model",
                parts=[gtypes.Part(function_call=fc) for fc in func_calls]))
            tool_result_parts = []

            for fc in func_calls:
                name = fc.name
                args = dict(fc.args)
                print()

                if name == "run_command":
                    cmd  = args.get("command", "")
                    desc = args.get("description", "Running…")
                    cwd  = args.get("cwd", workspace)
                    print_tool_call(name, desc, cmd)
                    out, rc = run_cmd_stream(cmd, cwd)
                    result  = {"output": out[:3000], "returncode": rc, "success": rc == 0}
                else:
                    labels = {
                        "read_file":       f"Reading {args.get('path','?')}",
                        "write_file":      f"Writing {args.get('path','?')}",
                        "list_directory":  f"Listing {args.get('path','?')}",
                        "analyze_project": f"Analyzing {args.get('path','?')}",
                        "web_search":      f"Searching: {args.get('query','?')}",
                        "save_memory":     f"Saving memory",
                        "emit_plan":       f"Planning: {args.get('title','?')}",
                    }
                    print_tool_call(name, labels.get(name, name))
                    result = dispatch_tool(name, args, workspace)

                    # Pretty display
                    if name == "list_directory" and "tree" in result:
                        tree = result["tree"][:2000]
                        if _RICH: _con.print(f"[dim]{tree}[/]")
                        else:     print(f"{D}{tree}{R}")
                    elif name == "read_file" and "content" in result:
                        preview = result["content"][:600]
                        lang    = Path(args.get("path","")).suffix.lstrip(".")
                        if _RICH:
                            try:   _con.print(Syntax(preview, lang or "text", theme="monokai", line_numbers=True))
                            except: _con.print(f"[dim]{preview}[/]")
                        else: print(f"{YW}{preview}{R}")
                    elif name == "analyze_project":
                        if _RICH:
                            t = Table(show_header=False, box=rbox.SIMPLE, border_style="dim")
                            t.add_column(style="#60b8ff"); t.add_column()
                            for k, v in result.items():
                                if k not in ("deps",) and v:
                                    t.add_row(k, str(v)[:80])
                            if result.get("deps"):
                                t.add_row("deps", ", ".join(result["deps"][:10]))
                            _con.print(t)
                        else:
                            for k, v in result.items():
                                if v and k != "deps":
                                    print(f"  {CY}{k:<18}{R}{v}")
                            if result.get("deps"):
                                print(f"  {CY}{'deps':<18}{R}{', '.join(result['deps'][:10])}")
                    elif name == "web_search" and "results" in result:
                        for res in result["results"][:3]:
                            if _RICH: _con.print(f"  [bold #60b8ff]{res.get('title','')[:70]}[/]\n  [dim]{res.get('snippet','')[:220]}[/]\n")
                            else:     print(f"  {CY}{res.get('title','')[:70]}{R}\n  {D}{res.get('snippet','')[:220]}{R}\n")
                    elif name == "write_file" and result.get("success"):
                        status_line(f"Written → {result.get('path','?')}  ({result.get('lines',0)} lines)", "success")
                    elif name == "save_memory" and result.get("saved"):
                        status_line("Memory saved", "success")
                    elif "error" in result:
                        status_line(f"Tool error: {result['error']}", "error")

                tool_result_parts.append(gtypes.Part(function_response=gtypes.FunctionResponse(
                    name=name, response=result)))

            context.append(gtypes.Content(role="user", parts=tool_result_parts))

        else:
            # Final text response
            final = "\n".join(texts) or "Done."
            print_agent_reply(final)
            history.append({"role": "assistant", "content": final,
                            "ts": datetime.now().isoformat()})
            return history

    status_line(f"Reached max iterations (12).", "warning")
    return history

# ── /addapi command (interactive, writes to .env) ──────────────────────────────
def cmd_addapi():
    keys = load_keys()
    print()
    if _RICH:
        _con.print(Panel("[bold #7c6af7]Add Gemini API Key[/]\nFree: [link=https://aistudio.google.com/app/apikey]aistudio.google.com/app/apikey[/link]",
                         border_style="#3a2a70"))
    else:
        print(f"  {PU}{B}Add Gemini API Key{R}")
        print(f"  Free key: https://aistudio.google.com/app/apikey\n")

    status_line(f"Keys currently in .env: {len(keys)}", "info")
    for k in keys:
        col = GR if k["active"] else RD
        print(f"    {col}●{R}  #{k['label']}: {D}{k['key'][:14]}…{R}")
    print()

    while True:
        try:
            raw = input(f"  {PU}paste key (or Enter to cancel):{R} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if not raw:
            return
        raw = raw.replace(" ", "").replace("\n", "")
        ok_save, msg = save_key_to_env(raw)
        if ok_save:
            _rotator.reload()
            status_line(f"Key saved as #{msg}", "success")
            status_line(f"Total keys in .env: {len(load_keys())}", "info")
            ans = input(f"  Add another? [y/N]: ").strip().lower()
            if ans != "y":
                break
        else:
            status_line(f"Could not save key: {msg}", "error")

# ── Read multi-line input helper ───────────────────────────────────────────────
def read_input(prompt_str: str) -> str:
    """Read one line, stripping leading/trailing whitespace."""
    try:
        if _RICH:
            return _con.input(prompt_str)
        else:
            return input(prompt_str)
    except (EOFError, KeyboardInterrupt):
        raise KeyboardInterrupt


def get_prompt_style(mode: str):
    mode_color = {
        "fast": "#00ff9c",     # neon green
        "pro": "#7c6af7",      # purple
        "thinking": "#ff9f43"  # orange
    }.get(mode, "#7c6af7")

    return Style.from_dict({
        "user": f"{mode_color} bold",
        "meta": "#888888",
        "arrow": f"{mode_color} bold",
    })

# ── Main interactive loop ──────────────────────────────────────────────────────
def main():
    import argparse
    ap = argparse.ArgumentParser(description="Agent 2 CLI — autonomous dev agent")
    ap.add_argument("message", nargs="?", help="One-shot message (no REPL)")
    ap.add_argument("--model", default=None, choices=list(MODELS.keys()))
    ap.add_argument("--mode",  default=None, choices=list(MODES.keys()))
    ap.add_argument("--clear", action="store_true", help="Start with fresh chats")
    args = ap.parse_args()

    # Session state
    model     = args.model or DEFAULT_MODEL
    mode      = args.mode  or DEFAULT_MODE
    workspace = None     # set via /workspace command or auto-detected
    history   = [] if args.clear else load_history()

    # One-shot mode (like `gemini -m flash "hello"`)
    if args.message:
        _rotator.reload()
        run_agent(args.message, history, model, mode, workspace)
        return

    # Interactive REPL
    print_banner()
    status_line(f"Model: {model}  Mode: {mode}  Shell: {SHELL_LABEL}", "info")
    status_line("Type /help for commands.  Ctrl+C or /exit to quit.", "info")
    if not load_keys():
        status_line("No API keys — type /addapi to add one.", "warning")
    if history:
        status_line(f"Restored {len(history)} messages from last session.  /clearhistory to start fresh.", "info")

    session = PromptSession(history=FileHistory(str(PT_HISTORY)))
    
    while True:
        # Build prompt line
        ws_name   = Path(workspace).name if workspace else "no-ws"
        mo_icon   = MODES[mode]["icon"]
        style = get_prompt_style(mode)

        prompt = HTML(
            f'<user>you</user> '
            f'<meta>[{SHELL_LABEL}|{model}|{mo_icon} ]</meta>'
            f'<arrow>></arrow> '
        )

        try:
            print()
            user_input = session.prompt(prompt, style=style).strip()
        except KeyboardInterrupt:
            print()
            status_line("Goodbye.", "info")
            save_history(history)
            break

        if not user_input:
            continue

        low = user_input.lower()

        # ── Slash commands ─────────────────────────────────────────────────────
        if low in ("/exit", "/quit", "exit", "quit"):
            status_line("Goodbye.", "info")
            save_history(history)
            break

        elif low == "/help":
            print_help()

        elif low == "/addapi":
            cmd_addapi()

        elif low == "/keys":
            for k in _rotator.status():
                sym = ok("●") if k["active"] else err("●")
                print(f"  {sym}  Key #{k['label']}: {D}{k['preview']}{R}")

        elif low.startswith("/model"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 1:
                if _RICH:
                    t = Table(show_header=False, box=rbox.SIMPLE)
                    t.add_column(); t.add_column()
                    for k in MODELS:
                        cur = "[bold #7c6af7]← current[/]" if k == model else ""
                        t.add_row(f"[#60b8ff]{k}[/]", cur)
                    _con.print(t)
                else:
                    for k in MODELS:
                        print(f"  {CY}{k}{R}{'  ← current' if k==model else ''}")
            else:
                m = parts[1].strip()
                if m in MODELS:
                    model = m
                    status_line(f"Model → {m}", "success")
                else:
                    status_line(f"Unknown model. Options: {', '.join(MODELS)}", "error")

        elif low.startswith("/mode"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 1:
                for k, v in MODES.items():
                    print(f"  {YW}{k}{R}  {D}{v['max_tokens']} tokens{R}{'  ← current' if k==mode else ''}")
            else:
                m = parts[1].strip()
                if m in MODES:
                    mode = m
                    status_line(f"Mode → {m}  {MODES[m]['icon']}", "success")
                else:
                    status_line(f"Unknown mode. Options: {', '.join(MODES)}", "error")

        elif low == "/clear":
            os.system("cls" if IS_WIN else "clear")
            print_banner()

        elif low == "/clearhistory":
            history = []
            save_history(history)

        elif low == "/history":
            if not history:
                status_line("No history.", "info")
            else:
                for h in history[-10:]:
                    col = CY if h["role"] == "user" else PU
                    sym = "you" if h["role"] == "user" else " a2"
                    ts  = h.get("ts","")[-8:][:5]
                    print(f"  {col}{sym}{R}  {D}{ts}{R}  {h['content'][:90]}")

        elif low == "/memory":
            mems = load_mems()
            if not mems:
                status_line("No memories saved yet.", "info")
            else:
                if _RICH:
                    t = Table(show_header=True, header_style="bold #7c6af7", box=rbox.SIMPLE_HEAD)
                    t.add_column("#", width=3, style="dim")
                    t.add_column("Imp", width=5)
                    t.add_column("Content")
                    t.add_column("Tags", style="dim")
                    for i, m in enumerate(sorted(mems, key=lambda x: -x.get("importance", 5)), 1):
                        t.add_row(str(i), f"{m.get('importance',5)}/10",
                                  m["content"][:80],
                                  ", ".join(m.get("tags", [])))
                    _con.print(t)
                else:
                    for i, m in enumerate(sorted(mems, key=lambda x: -x.get("importance", 5)), 1):
                        print(f"  {D}{i}.{R}  {YW}[{m.get('importance',5)}/10]{R}  {m['content'][:80]}")

        elif low.startswith("/addmem"):
            parts = user_input.split(maxsplit=1)
            if len(parts) > 1:
                add_mem(parts[1].strip())
                status_line("Memory saved.", "success")
            else:
                status_line("Usage: /addmem <text>", "warning")

        elif low.startswith("/workspace"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 1:
                status_line(f"Workspace: {workspace or 'none (using cwd)'}", "info")
            else:
                p = Path(parts[1].strip()).expanduser()
                if p.is_dir():
                    workspace = str(p)
                    status_line(f"Workspace → {workspace}", "success")
                else:
                    try:
                        p.mkdir(parents=True)
                        workspace = str(p)
                        status_line(f"Created & set workspace → {workspace}", "success")
                    except Exception as ex:
                        status_line(f"Cannot use path: {ex}", "error")

        elif low.startswith("/run "):
            cmd = user_input[5:].strip()
            if cmd: run_cmd_stream(cmd, workspace)

        elif low.startswith("/read "):
            path = user_input[6:].strip()
            result = _impl_read({"path": path})
            if "error" in result:
                status_line(result["error"], "error")
            else:
                lang = Path(path).suffix.lstrip(".")
                if _RICH:
                    try:   _con.print(Syntax(result["content"][:3000], lang or "text", theme="monokai", line_numbers=True))
                    except: _con.print(result["content"][:3000])
                else:
                    print(f"{YW}{result['content'][:3000]}{R}")

        elif low.startswith("/write "):
            path = user_input[7:].strip()
            if not path:
                status_line("Usage: /write <filepath>", "warning")
            else:
                print(f"  {D}Enter content (Ctrl+D / empty line × 2 to finish):{R}")
                lines_in, empty_count = [], 0
                while True:
                    try:
                        line = input()
                        if line == "":
                            empty_count += 1
                            if empty_count >= 2: break
                        else:
                            empty_count = 0
                        lines_in.append(line)
                    except EOFError:
                        break
                content = "\n".join(lines_in)
                result  = _impl_write({"path": path, "content": content})
                if result.get("success"):
                    status_line(f"Written → {path}  ({result.get('lines',0)} lines)", "success")
                else:
                    status_line(result.get("error","Failed"), "error")

        elif low.startswith("/ls"):
            p = user_input[3:].strip() or (workspace or ".")
            result = _impl_ls({"path": p})
            if "tree" in result:
                if _RICH: _con.print(f"[dim]{result['tree']}[/]")
                else:     print(f"{D}{result['tree']}{R}")
            else:
                status_line(result.get("error", "?"), "error")

        elif low.startswith("/analyze "):
            p = user_input[9:].strip()
            result = _impl_analyze({"path": p})
            if "error" in result:
                status_line(result["error"], "error")
            else:
                if _RICH:
                    t = Table(show_header=False, box=rbox.SIMPLE, border_style="dim")
                    t.add_column(style="#60b8ff", no_wrap=True); t.add_column()
                    for k, v in result.items():
                        if k == "deps": v = ", ".join(v[:10]) if v else "—"
                        t.add_row(k, str(v) if v else "—")
                    _con.print(t)
                else:
                    for k, v in result.items():
                        if k == "deps": v = ", ".join(v[:10]) if v else "—"
                        print(f"  {CY}{k:<18}{R}{v or '—'}")

        elif low.startswith("/search "):
            q = user_input[8:].strip()
            result = _impl_search({"query": q})
            for res in result.get("results", [])[:5]:
                print()
                if _RICH: 
                    _con.print(f"  [bold #60b8ff]{res.get('title','')[:70]}[/]\n  [dim]{res.get('snippet','')[:250]}[/]\n")
                else:     
                    print(f"  {CY}{res.get('title','')[:70]}{R}\n  {D}{res.get('snippet','')[:250]}{R}\n")
            if not result.get("results"):
                status_line("No results.", "info")

        elif low.startswith("/"):
            status_line(f"Unknown command: {user_input}  →  /help", "warning")

        # ── Agent call ─────────────────────────────────────────────────────────
        else:
            try:
                history = run_agent(user_input, history, model, mode, workspace)
                save_history(history)
            except KeyboardInterrupt:
                print()
                status_line("Interrupted.", "warning")

if __name__ == "__main__":
    main()

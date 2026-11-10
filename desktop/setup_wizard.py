"""First-run setup wizard for the Kiyra desktop overlay.

Shown *before* the main overlay window:

  1. Sign in        — local access-code gate (no backend accounts needed)
  2. Job description — pick a saved one or add a new one, then activate it
  3. Instructions   — how to drive the overlay, then "Start Kiyra"

The wizard is plain Tkinter (stdlib only) so it works before any other part of
the app is alive. Network calls use ``urllib`` and are best-effort: if the
backend is unreachable the user can still add/skip a job description and
continue.

Hide mode
---------
The wizard never opens when the overlay is launched hidden (``--hidden`` /
``KIYRA_OVERLAY_HIDDEN``) or when ``KIYRA_SKIP_SETUP=1`` is set, so automated
and background runs never cover the desktop. It also honours
``KIYRA_OVERLAY_OFFSCREEN`` by placing itself far off-screen (used by tests).
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from typing import Optional

# ── Palette (mirrors stealth_overlay's dark slate / green-accent tokens) ─────
BG = "#0f1219"
PANEL = "#171b25"
CARD = "#1e2430"
CODE = "#0f131b"
BORDER = "#2b3342"
ACCENT = "#3ddc84"
INK = "#06210f"
PRIMARY = "#e8ecf3"
SECONDARY = "#8d97ab"
DANGER = "#f87171"

FONT = "Segoe UI" if sys.platform == "win32" else "Helvetica"
MONO = "Consolas" if sys.platform == "win32" else "Menlo"

# The wizard mirrors the overlay's +2pt font bump so the pre-flight
# screens and the main window read at the same size.
FONT_BUMP = 2


def _fs(size) -> int:
    """Apply the global +2pt font bump to an integer point size."""
    return int(size) + FONT_BUMP

DEFAULT_ACCESS_CODE = "kiyra"

WIZARD_WIDTH = 780
WIZARD_HEIGHT = 660

_STEP_NAMES = ("Sign in", "Job description", "Instructions")


# ── Environment helpers ─────────────────────────────────────────────────────

def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def should_skip_setup() -> bool:
    """True when the wizard must not open (automation / hide mode)."""
    return (_env_flag("KIYRA_SKIP_SETUP", False)
            or _env_flag("KIYRA_OVERLAY_HIDDEN", False))


def access_code() -> str:
    """The configured local access code.

    ``KIYRA_ACCESS_CODE`` overrides the built-in default. Setting it to an
    empty string disables the check (handy for demos); otherwise the entered
    code must match exactly.
    """
    raw = os.environ.get("KIYRA_ACCESS_CODE")
    if raw is None:
        return DEFAULT_ACCESS_CODE
    return raw.strip()


# ── Tiny JSON HTTP helpers (stdlib only) ────────────────────────────────────

def _api_get(api_url: str, path: str, timeout: float = 5.0):
    import urllib.request
    req = urllib.request.Request(f"{api_url}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


def _api_post(api_url: str, path: str, payload: dict, timeout: float = 10.0):
    import urllib.request
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{api_url}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


# ── Wizard ──────────────────────────────────────────────────────────────────

class SetupWizard:
    """Three-step pre-flight: sign in → job description → instructions."""

    def __init__(self, root: tk.Tk, api_url: str, *,
                 offscreen: Optional[bool] = None):
        self.root = root
        self.api_url = api_url.rstrip("/")
        self.result: Optional[dict] = None
        self.offscreen = (
            _env_flag("KIYRA_OVERLAY_OFFSCREEN", False)
            if offscreen is None else offscreen
        )
        self.username = "local"
        self.job_description_id: Optional[int] = None

        self._jobs: list[dict] = []
        self._jobs_loaded = False
        self._jobs_queue: "queue.Queue[tuple]" = queue.Queue()
        self._page = 0
        self._closed = False

        self.win = tk.Toplevel(root)
        self.win.title("Kiyra — Setup")
        self.win.configure(bg=BG)
        self.win.resizable(False, False)
        self._place_window()
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)
        self.win.bind("<Escape>", lambda _e: self._cancel())

        self._build()
        self._show(0)
        try:
            self.win.deiconify()
            self.win.lift()
        except Exception:
            pass
        self._load_jobs_async()

    # ── layout ──────────────────────────────────────────────────────────────

    def _place_window(self):
        w, h = WIZARD_WIDTH, WIZARD_HEIGHT
        if self.offscreen:
            self.win.geometry(f"{w}x{h}+20000+20000")
            return
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
        except Exception:
            sw, sh = w + 120, h + 120
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 40)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def _button(self, parent, text, command, *, bg=CARD, fg=PRIMARY,
                bold=False, hover=None, padx=18, pady=9):
        return tk.Button(
            parent, text=text, command=command, bg=bg, fg=fg,
            activebackground=hover or bg, activeforeground=fg,
            relief="flat", bd=0, cursor="hand2",
            font=(FONT, _fs(10), "bold" if bold else "normal"),
            padx=padx, pady=pady)

    def _labeled_entry(self, parent, label, *, show=None, value=""):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", pady=(0, 14))
        tk.Label(row, text=label, bg=PANEL, fg=SECONDARY, font=(FONT, _fs(9)),
                 anchor="w").pack(fill="x", pady=(0, 6))
        ent = tk.Entry(
            row, bg=CARD, fg=PRIMARY, insertbackground=PRIMARY,
            relief="flat", bd=0, font=(FONT, _fs(11)),
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT)
        if show:
            ent.config(show=show)
        if value:
            ent.insert(0, value)
        ent.pack(fill="x", ipady=8, ipadx=8)
        return ent

    def _build(self):
        shell = tk.Frame(self.win, bg=BG)
        shell.pack(fill="both", expand=True)

        head = tk.Frame(shell, bg=BG)
        head.pack(fill="x", padx=32, pady=(26, 0))
        tk.Label(head, text="◆  Kiyra", bg=BG, fg=ACCENT,
                 font=(FONT, _fs(15), "bold")).pack(side="left")
        tk.Label(head, text="AI Interview Assistant", bg=BG, fg=SECONDARY,
                 font=(FONT, _fs(9))).pack(side="left", padx=(10, 0), pady=(4, 0))
        tk.Label(head, text="Setup", bg=BG, fg=SECONDARY,
                 font=(FONT, _fs(9))).pack(side="right", pady=(4, 0))

        self.steps = tk.Frame(shell, bg=BG)
        self.steps.pack(fill="x", padx=32, pady=(18, 0))
        self._step_labels = []
        for i, name in enumerate(_STEP_NAMES):
            lbl = tk.Label(self.steps, text=f"{i + 1}. {name}", bg=BG,
                           fg=SECONDARY, font=(FONT, _fs(9)))
            lbl.pack(side="left", padx=(0, 18))
            self._step_labels.append(lbl)

        body = tk.Frame(shell, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1)
        body.pack(fill="both", expand=True, padx=32, pady=(14, 0))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.pages = [
            self._build_login(body),
            self._build_jd(body),
            self._build_instructions(body),
        ]
        for page in self.pages:
            page.grid(row=0, column=0, sticky="nsew")

        foot = tk.Frame(shell, bg=BG)
        foot.pack(fill="x", padx=32, pady=(14, 22))
        self.btn_cancel = self._button(foot, "Exit", self._cancel,
                                       bg=CARD, fg=SECONDARY, hover=BORDER)
        self.btn_cancel.pack(side="left")
        self.btn_next = self._button(foot, "Continue", self._next,
                                     bg=ACCENT, fg=INK, bold=True,
                                     hover="#5fe79c")
        self.btn_next.pack(side="right")
        self.btn_back = self._button(foot, "Back", self._back,
                                     bg=CARD, fg=PRIMARY, hover=BORDER)
        self.btn_back.pack(side="right", padx=(0, 10))
        self.status = tk.Label(foot, text="", bg=BG, fg=DANGER,
                               font=(FONT, _fs(9)))
        self.status.pack(side="left", padx=(14, 0))

    def _build_login(self, parent):
        page = tk.Frame(parent, bg=PANEL)
        inner = tk.Frame(page, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=40, pady=34)

        tk.Label(inner, text="Sign in", bg=PANEL, fg=PRIMARY,
                 font=(FONT, _fs(20), "bold"), anchor="w").pack(fill="x")
        tk.Label(inner, text="Welcome back. Sign in to start your session.",
                 bg=PANEL, fg=SECONDARY, font=(FONT, _fs(10)),
                 anchor="w").pack(fill="x", pady=(4, 22))

        self.ent_user = self._labeled_entry(
            inner, "Name", value=os.environ.get("USERNAME") or "local")
        self.ent_code = self._labeled_entry(inner, "Access code", show="•")

        code = access_code()
        if code == DEFAULT_ACCESS_CODE:
            hint = "Default access code: kiyra  ·  set KIYRA_ACCESS_CODE to change"
        elif not code:
            hint = "Access-code check is disabled (KIYRA_ACCESS_CODE is empty)."
        else:
            hint = "Enter the access code configured for this machine."
        tk.Label(inner, text=hint, bg=PANEL, fg=SECONDARY, font=(FONT, _fs(8)),
                 anchor="w").pack(fill="x")

        self.login_error = tk.Label(inner, text="", bg=PANEL, fg=DANGER,
                                    font=(FONT, _fs(9)), anchor="w")
        self.login_error.pack(fill="x", pady=(10, 0))
        self.ent_user.bind("<Return>", lambda _e: self.ent_code.focus_set())
        self.ent_code.bind("<Return>", lambda _e: self._next())
        return page

    def _build_jd(self, parent):
        page = tk.Frame(parent, bg=PANEL)
        inner = tk.Frame(page, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=40, pady=34)

        tk.Label(inner, text="Job description", bg=PANEL, fg=PRIMARY,
                 font=(FONT, _fs(20), "bold"), anchor="w").pack(fill="x")
        tk.Label(
            inner,
            text="Pick the role you're interviewing for. Kiyra grounds its "
                 "answers in this description.",
            bg=PANEL, fg=SECONDARY, font=(FONT, _fs(10)), anchor="w",
            justify="left", wraplength=620).pack(fill="x", pady=(4, 16))

        self.jd_mode = tk.StringVar(value="existing")
        mode_row = tk.Frame(inner, bg=PANEL)
        mode_row.pack(fill="x", pady=(0, 12))
        for val, text in (("existing", "Use an existing description"),
                          ("new", "Add a new one"),
                          ("none", "Skip for now")):
            tk.Radiobutton(
                mode_row, text=text, value=val, variable=self.jd_mode,
                command=self._on_jd_mode, bg=PANEL, fg=PRIMARY,
                selectcolor=CARD, activebackground=PANEL,
                activeforeground=PRIMARY, highlightthickness=0, bd=0,
                font=(FONT, _fs(9)), anchor="w").pack(side="left", padx=(0, 16))

        # Existing descriptions (list + scrollbar + status line)
        self.jd_existing = tk.Frame(inner, bg=PANEL)
        self.jd_existing.pack(fill="both", expand=True)
        list_wrap = tk.Frame(self.jd_existing, bg=PANEL)
        list_wrap.pack(fill="both", expand=True)
        self.jd_list = tk.Listbox(
            list_wrap, bg=CARD, fg=PRIMARY, selectbackground=ACCENT,
            selectforeground=INK, relief="flat", bd=0, font=(FONT, _fs(10)),
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT, activestyle="none", height=6)
        self.jd_list.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(list_wrap, command=self.jd_list.yview)
        sb.pack(side="right", fill="y")
        self.jd_list.config(yscrollcommand=sb.set)
        self.jd_hint = tk.Label(self.jd_existing, text="Loading…", bg=PANEL,
                                fg=SECONDARY, font=(FONT, _fs(8)), anchor="w")
        self.jd_hint.pack(fill="x", pady=(6, 0))

        # New description form (hidden until "Add a new one" is chosen)
        self.jd_new = tk.Frame(inner, bg=PANEL)
        self.ent_jd_title = self._labeled_entry(self.jd_new, "Job title")
        self.ent_jd_company = self._labeled_entry(
            self.jd_new, "Company (optional)")
        text_row = tk.Frame(self.jd_new, bg=PANEL)
        text_row.pack(fill="both", expand=True)
        tk.Label(text_row, text="Description", bg=PANEL, fg=SECONDARY,
                 font=(FONT, _fs(9)), anchor="w").pack(fill="x", pady=(0, 6))
        text_wrap = tk.Frame(text_row, bg=PANEL)
        text_wrap.pack(fill="both", expand=True)
        self.txt_jd = tk.Text(
            text_wrap, bg=CARD, fg=PRIMARY, insertbackground=PRIMARY,
            relief="flat", bd=0, font=(FONT, _fs(10)), wrap="word", height=7,
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT)
        self.txt_jd.pack(side="left", fill="both", expand=True)
        sb2 = tk.Scrollbar(text_wrap, command=self.txt_jd.yview)
        sb2.pack(side="right", fill="y")
        self.txt_jd.config(yscrollcommand=sb2.set)
        return page

    def _build_instructions(self, parent):
        page = tk.Frame(parent, bg=PANEL)
        inner = tk.Frame(page, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=40, pady=34)

        tk.Label(inner, text="You're all set", bg=PANEL, fg=PRIMARY,
                 font=(FONT, _fs(20), "bold"), anchor="w").pack(fill="x")
        tk.Label(
            inner,
            text="Kiyra stays invisible to screen sharing. Here's how to "
                 "drive it.",
            bg=PANEL, fg=SECONDARY, font=(FONT, _fs(10)), anchor="w",
            justify="left", wraplength=620).pack(fill="x", pady=(4, 16))

        tips = (
            ("F9", "Show or hide the overlay at any time."),
            ("F10", "Toggle click-through so clicks reach the app behind it."),
            ("SEND", "Answer the live transcript — listening starts automatically."),
            ("Ask box", "Type a question and press Enter for an instant answer."),
            ("Camera", "Capture a screenshot and extract the question from it."),
            ("Shift+Alt+W / X", "Scroll the answer up / down."),
            ("Top handle", "Collapse into the bubble; click it to restore."),
        )
        grid = tk.Frame(inner, bg=PANEL)
        grid.pack(fill="x")
        grid.grid_columnconfigure(1, weight=1)
        for r, (key, desc) in enumerate(tips):
            tk.Label(grid, text=key, bg=CODE, fg=ACCENT,
                     font=(MONO, _fs(8), "bold"), padx=8, pady=4).grid(
                row=r, column=0, sticky="nw", pady=4)
            tk.Label(grid, text=desc, bg=PANEL, fg=PRIMARY, font=(FONT, _fs(10)),
                     anchor="w", justify="left", wraplength=520).grid(
                row=r, column=1, sticky="w", padx=(12, 0), pady=4)

        self.summary = tk.Label(inner, text="", bg=PANEL, fg=SECONDARY,
                                font=(FONT, _fs(9)), anchor="w")
        self.summary.pack(fill="x", pady=(16, 0))
        return page

    # ── navigation ──────────────────────────────────────────────────────────

    def _show(self, index: int):
        self._page = index
        self.pages[index].tkraise()
        for i, lbl in enumerate(self._step_labels):
            if i == index:
                lbl.config(fg=ACCENT)
            elif i < index:
                lbl.config(fg=PRIMARY)
            else:
                lbl.config(fg=SECONDARY)
        self.btn_back.config(state="normal" if index > 0 else "disabled")
        if index == 0:
            self.btn_next.config(text="Continue")
            try:
                self.ent_code.focus_set()
            except Exception:
                pass
        elif index == 1:
            self.btn_next.config(text="Continue")
        else:
            self.btn_next.config(text="Start Kiyra")
            self._update_summary()

    def _next(self):
        self.status.config(text="")
        if self._page == 0:
            if self._validate_login():
                self._show(1)
        elif self._page == 1:
            if self._save_jd():
                self._show(2)
        else:
            self._finish()

    def _back(self):
        if self._page > 0:
            self._show(self._page - 1)

    def _on_jd_mode(self):
        for frame in (self.jd_existing, self.jd_new):
            frame.pack_forget()
        if self.jd_mode.get() == "new":
            self.jd_new.pack(fill="both", expand=True)
        elif self.jd_mode.get() == "existing":
            self.jd_existing.pack(fill="both", expand=True)

    # ── login ───────────────────────────────────────────────────────────────

    def _validate_login(self) -> bool:
        user = self.ent_user.get().strip()
        if not user:
            self.login_error.config(text="Enter your name.")
            return False
        code = access_code()
        if code and self.ent_code.get().strip() != code:
            self.login_error.config(text="Incorrect access code.")
            return False
        self.username = user
        self.login_error.config(text="")
        return True

    # ── job description ─────────────────────────────────────────────────────

    def _load_jobs_async(self):
        if self._jobs_loaded:
            return
        self._jobs_loaded = True

        def work():
            try:
                jobs = _api_get(self.api_url, "/api/jobs") or []
                err = None
            except Exception as exc:  # noqa: BLE001 — surfaced in the UI
                jobs, err = [], str(exc)
            self._jobs_queue.put((jobs, err))

        threading.Thread(target=work, name="setup-jobs", daemon=True).start()
        self._poll_jobs()

    def _poll_jobs(self):
        if self._closed:
            return
        try:
            jobs, err = self._jobs_queue.get_nowait()
        except queue.Empty:
            if not self._closed:
                try:
                    self.win.after(100, self._poll_jobs)
                except Exception:
                    pass
            return
        if self._closed:
            return
        self._render_jobs(jobs, err)

    def _render_jobs(self, jobs, err):
        if self._closed:
            return
        self._jobs = list(jobs or [])
        self.jd_list.delete(0, "end")
        if err:
            self.jd_hint.config(
                text=f"Could not load descriptions ({err}). "
                     f"You can add one or skip.")
            return
        if not self._jobs:
            self.jd_hint.config(
                text="No saved descriptions yet — add one or skip.")
            return
        for job in self._jobs:
            title = job.get("title") or "Untitled"
            company = job.get("company") or ""
            active = "  · active" if job.get("is_active") else ""
            label = f"{title}{(' — ' + company) if company else ''}{active}"
            self.jd_list.insert("end", label)
        for i, job in enumerate(self._jobs):
            if job.get("is_active"):
                self.jd_list.selection_set(i)
                self.jd_list.see(i)
                break
        else:
            self.jd_list.selection_set(0)
        self.jd_hint.config(text=f"{len(self._jobs)} saved description(s).")

    def _save_jd(self) -> bool:
        mode = self.jd_mode.get()
        if mode == "none":
            self.job_description_id = None
            return True

        if mode == "existing":
            sel = self.jd_list.curselection()
            if not sel:
                self.status.config(
                    text="Select a description, or choose 'Skip for now'.")
                return False
            job = self._jobs[sel[0]]
            try:
                _api_post(self.api_url, f"/api/jobs/{job['id']}/activate", {})
            except Exception as exc:  # noqa: BLE001
                self.status.config(
                    text=f"Could not activate that description: {exc}")
                return False
            self.job_description_id = job["id"]
            return True

        # mode == "new"
        title = self.ent_jd_title.get().strip()
        raw_text = self.txt_jd.get("1.0", "end").strip()
        if not title:
            self.status.config(text="Enter a job title.")
            return False
        if not raw_text:
            self.status.config(text="Paste the job description.")
            return False
        try:
            job = _api_post(self.api_url, "/api/jobs", {
                "title": title,
                "company": self.ent_jd_company.get().strip() or None,
                "raw_text": raw_text,
            })
        except Exception as exc:  # noqa: BLE001
            self.status.config(text=f"Could not save the description: {exc}")
            return False
        self.job_description_id = (job or {}).get("id")
        return True

    def _update_summary(self):
        jd = "None"
        if self.job_description_id:
            for job in self._jobs:
                if job.get("id") == self.job_description_id:
                    jd = job.get("title") or f"#{self.job_description_id}"
                    break
            else:
                jd = f"#{self.job_description_id}"
        self.summary.config(
            text=f"Signed in as {self.username}   ·   Job description: {jd}")

    # ── finish / close ──────────────────────────────────────────────────────

    def _finish(self):
        self.result = {
            "username": self.username,
            "job_description_id": self.job_description_id,
        }
        self._close()

    def _cancel(self):
        self.result = None
        self._close()

    def _close(self):
        self._closed = True
        try:
            self.win.destroy()
        except Exception:
            pass


def run_setup_wizard(root: tk.Tk, api_url: str, *,
                     offscreen: Optional[bool] = None,
                     skip: bool = False,
                     force: bool = False) -> Optional[dict]:
    """Show the wizard modally and return the collected setup.

    Returns ``{"username": ..., "job_description_id": ...}`` on finish, or
    ``None`` if the user cancelled. When skipped (hide mode / automation) the
    defaults are returned without opening a window. ``force`` overrides both
    the skip flag and the environment (used by ``--setup``).
    """
    if not force and (skip or should_skip_setup()):
        return {"username": "local", "job_description_id": None}
    wizard = SetupWizard(root, api_url, offscreen=offscreen)
    try:
        root.wait_window(wizard.win)
    except Exception:
        pass
    return wizard.result

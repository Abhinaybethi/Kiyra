"""First-run setup wizard for the Kiyra desktop overlay.

Shown *before* the main overlay window:

  1. Sign in        — email one-time code (full name + phone on first signup)
  2. Job description — pick a saved one or add a new one, then activate it
  3. Instructions   — how to drive the overlay, then "Start Kiyra"

The wizard is plain Tkinter (stdlib only) so it works before any other part of
the app is alive. Network calls use ``urllib`` and are best-effort: if the
backend is unreachable the user can still add/skip a job description and
continue.

Sign-in is mandatory
--------------------
There is no skip flag and no stored-session auto-login: every launch requires
a fresh email code. ``KIYRA_OVERLAY_OFFSCREEN`` only moves the window far
off-screen (used by tests); it does not bypass authentication.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
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


WIZARD_WIDTH = 780
WIZARD_HEIGHT = 720

_STEP_NAMES = ("Sign in", "Job description", "Instructions")


# ── Environment helpers ─────────────────────────────────────────────────────

def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ── Local session store ─────────────────────────────────────────────────────
# The token is intentionally NOT persisted. Every launch requires a fresh email
# OTP. These helpers only remove a token left behind by an older build.

def auth_store_path() -> Path:
    """Where the desktop keeps its bearer token between launches."""
    override = os.environ.get("KIYRA_AUTH_STORE")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "Kiyra" / "auth.json"
    return Path.home() / ".kiyra" / "auth.json"


def clear_auth_session() -> None:
    try:
        auth_store_path().unlink(missing_ok=True)
    except Exception:
        pass


# ── Tiny JSON HTTP helpers (stdlib only) ────────────────────────────────────

def _api_get(api_url: str, path: str, timeout: float = 5.0,
             token: Optional[str] = None):
    import urllib.request
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{api_url}{path}", headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


def _api_post(api_url: str, path: str, payload: dict, timeout: float = 10.0,
              token: Optional[str] = None):
    import urllib.request
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{api_url}{path}", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


def _api_error(exc: Exception) -> str:
    """Turn a urllib/HTTP failure into a sentence a user can act on."""
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):
        try:
            raw = exc.read().decode("utf-8", "replace")
            data = json.loads(raw)
            detail = data.get("detail") if isinstance(data, dict) else None
            if isinstance(detail, list) and detail:
                detail = detail[0].get("msg") if isinstance(detail[0], dict) else detail[0]
            if detail:
                return str(detail)
        except Exception:
            pass
        if exc.code == 429:
            return "Too many attempts. Wait a moment and try again."
        return f"Request failed ({exc.code}). Please try again."
    if isinstance(exc, urllib.error.URLError):
        return "Cannot reach the Kiyra backend. Is it running?"
    if isinstance(exc, TimeoutError):
        return "The request timed out. Please try again."
    return str(exc) or "Something went wrong. Please try again."


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

        # Authentication state. `account` is only set after a verified email
        # OTP; the footer's Continue stays disabled until then.
        self.account: Optional[dict] = None
        self.auth_token: Optional[str] = None
        self.dev_bypass = False  # set only when the backend opts in locally
        self._pending_email: str = ""
        self._pending_purpose: str = "login"
        self._otp_length: int = 6
        self._smtp_configured = False
        self._auth_busy = False
        self._resend_until = 0.0
        self._resend_job = None

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
        # No stored-session auto-login: purge any token from an older build and
        # require a fresh email code for this launch.
        clear_auth_session()
        # Jobs are loaded after sign-in, when the bearer token exists.
        # Local development can opt out of sign-in entirely via the backend.
        self._maybe_dev_bypass()

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
        # Keep a handle so callers can hide the whole label+entry row.
        ent._row = row
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

        self.login_view = tk.Frame(page, bg=PANEL)
        self.login_view.pack(fill="both", expand=True)

        # ── credentials view ────────────────────────────────────────────────
        self.login_credentials = tk.Frame(self.login_view, bg=PANEL)
        inner = tk.Frame(self.login_credentials, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=40, pady=26)

        inner.grid_columnconfigure(0, weight=1)
        inner.grid_rowconfigure(3, weight=1)

        self.login_title = tk.Label(inner, text="Welcome back", bg=PANEL,
                                    fg=PRIMARY, font=(FONT, _fs(20), "bold"),
                                    anchor="w")
        self.login_title.grid(row=0, column=0, sticky="ew")
        self.login_subtitle = tk.Label(
            inner, text="Sign in with your email — we'll send a one-time code.",
            bg=PANEL, fg=SECONDARY, font=(FONT, _fs(10)), anchor="w",
            justify="left", wraplength=620)
        self.login_subtitle.grid(row=1, column=0, sticky="ew", pady=(4, 14))

        # After a verified OTP this becomes a confirmation panel; there is no
        # restored-session path.
        self.login_signed = tk.Label(
            inner, text="", bg=CARD, fg=ACCENT, font=(FONT, _fs(10)),
            anchor="w", justify="left", padx=12, pady=10)
        self.login_signed.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self.login_signed.grid_remove()

        self.login_form = tk.Frame(inner, bg=PANEL)
        self.login_form.grid(row=3, column=0, sticky="nsew")

        self.auth_mode = tk.StringVar(value="signin")
        self.mode_row = tk.Frame(self.login_form, bg=PANEL)
        self.mode_row.pack(fill="x", pady=(0, 12))
        for val, text in (("signin", "Sign in"), ("signup", "Create account")):
            tk.Radiobutton(
                self.mode_row, text=text, value=val, variable=self.auth_mode,
                command=self._on_auth_mode, bg=PANEL, fg=PRIMARY,
                selectcolor=CARD, activebackground=PANEL,
                activeforeground=PRIMARY, highlightthickness=0, bd=0,
                font=(FONT, _fs(9)), anchor="w").pack(side="left", padx=(0, 18))

        self.ent_full_name = self._labeled_entry(
            self.login_form, "Full name",
            value=(os.environ.get("USERNAME") or "").strip())
        self.ent_phone = self._labeled_entry(self.login_form, "Phone number")
        self.ent_email = self._labeled_entry(self.login_form, "Email address")

        self.btn_send = self._button(
            self.login_form, "Send code", self._request_code, bg=ACCENT, fg=INK,
            bold=True, hover="#5fe79c")
        self.btn_send.pack(fill="x", pady=(2, 10))

        self.login_hint = tk.Label(
            self.login_form, text="We'll email you a 6-digit code. No password needed.",
            bg=PANEL, fg=SECONDARY, font=(FONT, _fs(8)), anchor="w",
            justify="left", wraplength=620)
        self.login_hint.pack(fill="x")

        self.login_error = tk.Label(inner, text="", bg=PANEL, fg=DANGER,
                                    font=(FONT, _fs(9)), anchor="w",
                                    justify="left", wraplength=620)
        self.login_error.grid(row=4, column=0, sticky="ew", pady=(8, 0))

        self.btn_signout = self._button(
            inner, "Sign out", self._sign_out, bg=PANEL, fg=SECONDARY,
            hover=CARD, padx=0, pady=2)
        self.btn_signout.grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.btn_signout.grid_remove()

        self.ent_full_name.bind("<Return>", lambda _e: self.ent_phone.focus_set())
        self.ent_phone.bind("<Return>", lambda _e: self.ent_email.focus_set())
        self.ent_email.bind("<Return>", lambda _e: self._request_code())

        # ── OTP view ────────────────────────────────────────────────────────
        self.login_otp = tk.Frame(self.login_view, bg=PANEL)
        otp_inner = tk.Frame(self.login_otp, bg=PANEL)
        otp_inner.pack(fill="both", expand=True, padx=40, pady=26)

        tk.Label(otp_inner, text="Check your email", bg=PANEL, fg=PRIMARY,
                 font=(FONT, _fs(20), "bold"), anchor="w").pack(fill="x")
        self.otp_subtitle = tk.Label(
            otp_inner, text="", bg=PANEL, fg=SECONDARY, font=(FONT, _fs(10)),
            anchor="w", justify="left", wraplength=620)
        self.otp_subtitle.pack(fill="x", pady=(4, 16))

        otp_row = tk.Frame(otp_inner, bg=PANEL)
        otp_row.pack(fill="x", pady=(0, 6))
        self.ent_otp = tk.Entry(
            otp_row, bg=CARD, fg=ACCENT, insertbackground=ACCENT,
            relief="flat", bd=0, font=(MONO, _fs(22), "bold"),
            justify="center", width=12,
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT,
            validate="key", validatecommand=(self.win.register(self._only_digits), "%P"))
        self.ent_otp.pack(fill="x", ipady=10)
        self.ent_otp.bind("<Return>", lambda _e: self._verify_code())

        self.btn_verify = self._button(
            otp_inner, "Verify & continue", self._verify_code, bg=ACCENT,
            fg=INK, bold=True, hover="#5fe79c")
        self.btn_verify.pack(fill="x", pady=(12, 8))

        self.btn_resend = self._button(
            otp_inner, "Resend code", self._resend_code, bg=CARD, fg=PRIMARY,
            hover=BORDER)
        self.btn_resend.pack(fill="x")

        self.otp_hint = tk.Label(
            otp_inner, text="", bg=PANEL, fg=SECONDARY, font=(FONT, _fs(8)),
            anchor="w", justify="left", wraplength=620)
        self.otp_hint.pack(fill="x", pady=(12, 0))

        self.otp_error = tk.Label(
            otp_inner, text="", bg=PANEL, fg=DANGER, font=(FONT, _fs(9)),
            anchor="w", justify="left", wraplength=620)
        self.otp_error.pack(fill="x", pady=(6, 0))

        self.btn_different = self._button(
            otp_inner, "Use a different email", self._use_different_email,
            bg=PANEL, fg=SECONDARY, hover=CARD, padx=0, pady=2)
        self.btn_different.pack(anchor="w", pady=(10, 0))

        self._on_auth_mode()
        self._show_credentials_view()
        return page

    @staticmethod
    def _only_digits(proposed: str) -> bool:
        return proposed == "" or (proposed.isdigit() and len(proposed) <= 10)


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
            self.btn_next.config(state="normal" if self.account else "disabled")
            self._show_credentials_view()
            try:
                if self.account:
                    self.btn_next.focus_set()
                elif self.auth_mode.get() == "signup":
                    self.ent_full_name.focus_set()
                else:
                    self.ent_email.focus_set()
            except Exception:
                pass
        elif index == 1:
            self.btn_next.config(state="normal")
            self.btn_next.config(text="Continue")
        else:
            self.btn_next.config(state="normal")
            self.btn_next.config(text="Start Kiyra")
            self._update_summary()

    def _next(self):
        self.status.config(text="")
        if self._page == 0:
            if self.account:
                self._show(1)
            else:
                self._show_credentials_view()
                self.login_error.config(
                    text="Sign in to continue — request a code and verify it first.")
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

    # ── authentication (email OTP) ──────────────────────────────────────────

    def _on_auth_mode(self):
        """Show name/phone only while creating an account."""
        signup = self.auth_mode.get() == "signup"
        if signup:
            self.login_title.config(text="Create your account")
            self.login_subtitle.config(
                text="Just your name, phone and email. We'll verify it with a "
                     "one-time code — no password to remember.")
            self.ent_full_name._row.pack(fill="x", pady=(0, 14),
                                         before=self.ent_email._row)
            self.ent_phone._row.pack(fill="x", pady=(0, 14),
                                     before=self.ent_email._row)
        else:
            self.login_title.config(text="Welcome back")
            self.login_subtitle.config(
                text="Sign in with your email — we'll send a one-time code.")
            self.ent_full_name._row.pack_forget()
            self.ent_phone._row.pack_forget()
            self.ent_email._row.pack(fill="x", pady=(0, 14),
                                     before=self.btn_send)
        self.login_error.config(text="")

    def _show_credentials_view(self):
        self.login_otp.pack_forget()
        if not self.login_credentials.winfo_manager():
            self.login_credentials.pack(fill="both", expand=True)
        self._update_signed_in_state()

    def _show_otp_view(self):
        self.login_credentials.pack_forget()
        if not self.login_otp.winfo_manager():
            self.login_otp.pack(fill="both", expand=True)
        self.otp_error.config(text="")
        self.ent_otp.delete(0, "end")
        try:
            self.ent_otp.focus_set()
        except Exception:
            pass

    def _update_signed_in_state(self):
        if self.account:
            name = self.account.get("full_name") or self.account.get("email") or "you"
            email = self.account.get("email") or ""
            phone = self.account.get("phone") or ""
            self.login_title.config(text=f"Signed in as {name}")
            self.login_subtitle.config(
                text="Continue to choose the role you're interviewing for.")
            self.login_signed.config(
                text=f"✓  {name}\n{email}" + (f"   ·   {phone}" if phone else ""))
            self.login_form.grid_remove()
            self.login_signed.grid()
            self.btn_signout.grid()
            self.login_error.config(text="")
        else:
            self.login_form.grid()
            self.login_signed.grid_remove()
            self.btn_signout.grid_remove()
            self._on_auth_mode()
        try:
            self.btn_next.config(state="normal" if self.account else "disabled")
        except Exception:
            pass

    def _maybe_dev_bypass(self):
        """Local-only opt-out: honour KIYRA_DEV_AUTH_BYPASS on the backend."""
        try:
            status = _api_get(self.api_url, "/api/auth/status", timeout=2.0)
        except Exception:
            return  # backend starting or unreachable — keep strict sign-in
        if isinstance(status, dict) and status.get("dev_bypass"):
            self._enable_dev_bypass()

    def _enable_dev_bypass(self):
        self.dev_bypass = True
        self._set_signed_in({
            "full_name": "Local Developer",
            "email": "dev@localhost",
            "phone": "",
            "is_verified": True,
        }, "")
        self.login_subtitle.config(
            text="Local dev mode — sign-in is disabled on this backend.")
        self._show(1)

    def _set_signed_in(self, account: dict, token: str):
        self.account = dict(account or {})
        self.auth_token = token
        self.username = self.account.get("full_name") or self.account.get("email") or "local"
        self._update_signed_in_state()
        # /api/jobs is protected. Loading before this point returned 401 and
        # left the saved job descriptions invisible for the rest of the wizard.
        self._reset_jobs()
        self._load_jobs_async()

    def _sign_out(self):
        if self.dev_bypass:
            self._enable_dev_bypass()
            return
        token = self.auth_token
        if token:
            try:
                _api_post(self.api_url, "/api/auth/logout", {}, token=token)
            except Exception:
                pass
        clear_auth_session()
        self.account = None
        self.auth_token = None
        self._reset_jobs()
        self.username = "local"
        self._pending_email = ""
        self.otp_hint.config(text="")
        self.otp_error.config(text="")
        self.login_error.config(text="")
        self._show_credentials_view()

    def _use_different_email(self):
        self._pending_email = ""
        self._cancel_resend_countdown()
        self._show_credentials_view()
        try:
            self.ent_email.focus_set()
        except Exception:
            pass

    def _set_auth_busy(self, busy: bool):
        self._auth_busy = busy
        state = "disabled" if busy else "normal"
        for btn in (self.btn_send, self.btn_verify):
            try:
                btn.config(state=state)
            except Exception:
                pass
        if not busy:
            self._tick_resend()

    def _run_worker(self, work, on_ok, on_err):
        """Run a blocking HTTP call off the Tk thread, then marshal back."""
        results: "queue.Queue[tuple]" = queue.Queue()

        def runner():
            try:
                results.put((True, work()))
            except Exception as exc:  # noqa: BLE001 — shown in the UI
                results.put((False, exc))

        threading.Thread(target=runner, name="kiyra-auth", daemon=True).start()

        def poll():
            if self._closed:
                return
            try:
                ok, value = results.get_nowait()
            except queue.Empty:
                try:
                    self.win.after(60, poll)
                except Exception:
                    pass
                return
            self._set_auth_busy(False)
            if ok:
                on_ok(value)
            else:
                on_err(value)

        try:
            self.win.after(0, poll)
        except Exception:
            poll()

    def _request_code(self, background: bool = True) -> bool:
        signup = self.auth_mode.get() == "signup"
        email = self.ent_email.get().strip()
        name = self.ent_full_name.get().strip()
        phone = self.ent_phone.get().strip()

        self.login_error.config(text="")
        if signup:
            if not name:
                self.login_error.config(text="Enter your full name.")
                return False
            if not phone:
                self.login_error.config(text="Enter your phone number.")
                return False
        if not email:
            self.login_error.config(text="Enter your email address.")
            return False

        if signup:
            path = "/api/auth/register"
            payload = {"full_name": name, "phone": phone, "email": email}
        else:
            path = "/api/auth/login"
            payload = {"email": email}

        def work():
            return _api_post(self.api_url, path, payload, timeout=20.0)

        if not background:
            try:
                data = work()
            except Exception as exc:  # noqa: BLE001
                self._on_auth_error(exc)
                return False
            self._on_challenge(data)
            return True

        self._set_auth_busy(True)
        self._run_worker(work, self._on_challenge, self._on_auth_error)
        return True

    def _resend_code(self, background: bool = True) -> bool:
        if not self._pending_email:
            self._use_different_email()
            return False
        payload = {"email": self._pending_email,
                   "purpose": self._pending_purpose}

        def work():
            return _api_post(self.api_url, "/api/auth/resend", payload, timeout=20.0)

        if not background:
            try:
                data = work()
            except Exception as exc:  # noqa: BLE001
                self._on_auth_error(exc)
                return False
            self._on_challenge(data)
            return True

        self._set_auth_busy(True)
        self._run_worker(work, self._on_challenge, self._on_auth_error)
        return True

    def _verify_code(self, background: bool = True) -> bool:
        code = self.ent_otp.get().strip()
        if not self._pending_email:
            self._use_different_email()
            return False
        if not code:
            self.otp_error.config(text="Enter the code from your email.")
            return False
        if not code.isdigit():
            self.otp_error.config(text="The code is all digits.")
            return False
        if len(code) != self._otp_length:
            self.otp_error.config(
                text=f"Enter the {self._otp_length}-digit code from your email.")
            return False

        self.otp_error.config(text="")

        def work():
            return _api_post(self.api_url, "/api/auth/verify",
                             {"email": self._pending_email, "code": code,
                              "purpose": self._pending_purpose},
                             timeout=20.0)

        if not background:
            try:
                data = work()
            except Exception as exc:  # noqa: BLE001
                self._on_auth_error(exc)
                return False
            self._on_verified(data)
            return True

        self._set_auth_busy(True)
        self._run_worker(work, self._on_verified, self._on_auth_error)
        return True

    def _on_challenge(self, data: dict):
        data = data or {}
        self._pending_email = (data.get("email") or self.ent_email.get().strip()).lower()
        self._pending_purpose = data.get("purpose") or "login"
        self._otp_length = int(data.get("otp_length") or 6)
        self._smtp_configured = data.get("delivery") == "email"

        ttl = int(data.get("expires_in") or 600)
        minutes = max(1, round(ttl / 60))
        self.otp_subtitle.config(
            text=data.get("message") or
                 f"We emailed a {self._otp_length}-digit code to "
                 f"{self._pending_email}. It expires in {minutes} minutes.")
        self.otp_hint.config(
            text="Didn't get it? Check your spam folder, then resend.")
        self._show_otp_view()
        self._start_resend_countdown(int(data.get("resend_in") or 30))

    def _on_verified(self, data: dict):
        data = data or {}
        account = data.get("account") or {}
        token = data.get("token") or ""
        if not token:
            self.otp_error.config(
                text="Verification succeeded but no session was returned. Try again.")
            return
        self._set_signed_in(account, token)
        self._cancel_resend_countdown()
        self._pending_email = ""
        self.otp_hint.config(text="")
        self.otp_error.config(text="")
        self._show(1)

    def _on_auth_error(self, exc: Exception):
        message = _api_error(exc)
        try:
            if self.login_otp.winfo_manager():
                self.otp_error.config(text=message)
                self.ent_otp.selection_range(0, "end")
                self.ent_otp.focus_set()
            else:
                self.login_error.config(text=message)
        except Exception:
            pass

    # ── resend countdown ────────────────────────────────────────────────────

    def _cancel_resend_countdown(self):
        if self._resend_job is not None:
            try:
                self.win.after_cancel(self._resend_job)
            except Exception:
                pass
        self._resend_job = None
        self._resend_until = 0.0
        try:
            self.btn_resend.config(text="Resend code", state="normal")
        except Exception:
            pass

    def _start_resend_countdown(self, seconds: int):
        self._resend_until = time.monotonic() + max(0, int(seconds))
        self._tick_resend()

    def _tick_resend(self):
        if self._closed:
            return
        remaining = int(self._resend_until - time.monotonic())
        try:
            if remaining > 0:
                self.btn_resend.config(
                    text=f"Resend in {remaining}s", state="disabled")
            else:
                self.btn_resend.config(text="Resend code", state="normal")
        except Exception:
            return
        if remaining > 0:
            self._resend_job = self.win.after(500, self._tick_resend)
        else:
            self._resend_job = None

    # ── job description ─────────────────────────────────────────────────────

    def _reset_jobs(self):
        self._jobs_loaded = False
        while True:
            try:
                self._jobs_queue.get_nowait()
            except queue.Empty:
                break

    def _load_jobs_async(self):
        if self._jobs_loaded:
            return
        self._jobs_loaded = True
        token = self.auth_token

        def work():
            try:
                jobs = _api_get(self.api_url, "/api/jobs", token=token) or []
                err = None
            except Exception as exc:  # noqa: BLE001 — surfaced in the UI
                jobs, err = [], str(exc)
            if self.auth_token != token:
                return  # discarded: the user signed out or changed accounts
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
                _api_post(self.api_url, f"/api/jobs/{job['id']}/activate", {},
                          token=self.auth_token)
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
            }, token=self.auth_token)
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
            "account": self.account,
            "auth_token": self.auth_token,
            "dev_bypass": self.dev_bypass,
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
                     offscreen: Optional[bool] = None) -> Optional[dict]:
    """Show the sign-in wizard modally and return the collected setup.

    Returns ``{"username": ..., "job_description_id": ..., "account": ...,
    "auth_token": ..., "dev_bypass": ...}`` on finish, or ``None`` if cancelled.
    Sign-in can only be skipped when the backend explicitly enables the local
    ``KIYRA_DEV_AUTH_BYPASS`` flag.
    """
    wizard = SetupWizard(root, api_url, offscreen=offscreen)
    try:
        root.wait_window(wizard.win)
    except Exception:
        pass
    return wizard.result

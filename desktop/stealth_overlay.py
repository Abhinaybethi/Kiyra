"""
Stealth Interview Assistant — OS Screen-Share Invisible Desktop Overlay
Uses Windows OS-level capture exclusion (WDA_EXCLUDEFROMCAPTURE = 0x11)
Completely INVISIBLE to Zoom, Google Meet, Teams, OBS, Discord, and Screenshots.
"""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
import urllib.request
import urllib.parse

# ── Windows Win32 Constants ───────────────────────────────────────────────────
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080

# Windows 10 (2004+) & Windows 11 Display Affinity
# WDA_NONE = 0x00
# WDA_MONITOR = 0x01 (black box in captures)
# WDA_EXCLUDEFROMCAPTURE = 0x11 (Completely INVISIBLE / excluded from all capture APIs)
WDA_EXCLUDEFROMCAPTURE = 0x00000011
WDA_NONE = 0x00000000


def apply_stealth_affinity(hwnd: int, enable: bool = True) -> bool:
    """Exclude the window from screen sharing, screen recorders, and screenshot tools."""
    if sys.platform != "win32":
        return False
    try:
        affinity = WDA_EXCLUDEFROMCAPTURE if enable else WDA_NONE
        result = ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, affinity)
        return bool(result)
    except Exception as e:
        print(f"[StealthHUD] Failed to set display affinity: {e}", file=sys.stderr)
        return False


def set_click_through(hwnd: int, enable: bool = True):
    """Enable or disable click-through transparency (mouse events pass through to apps below)."""
    if sys.platform != "win32":
        return
    try:
        ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enable:
            ex_style |= (WS_EX_TRANSPARENT | WS_EX_LAYERED)
        else:
            ex_style &= ~WS_EX_TRANSPARENT
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)
    except Exception as e:
        print(f"[StealthHUD] Failed to set click-through: {e}", file=sys.stderr)


class StealthOverlayApp:
    def __init__(self, root: tk.Tk, api_url: str = "http://localhost:8000"):
        self.root = root
        self.api_url = api_url.rstrip("/")
        self.is_stealth_active = True
        self.is_click_through = False
        self.is_collapsed = False
        self.is_listening = False
        self.session_id: int | None = None
        self.last_question = ""
        self.current_answer_data = {}

        self._init_window()
        self._init_styles()
        self._build_ui()
        self._apply_win32_stealth()
        self._bind_shortcuts()
        self._check_backend_status()

    def _init_window(self):
        self.root.title("InterviewAI — Stealth HUD")
        self.root.geometry("520x640+60+60")
        self.root.minsize(380, 260)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.92)
        self.root.configure(bg="#090d16")
        self.root.overrideredirect(True)  # Frameless for sleek teleprompter HUD

    def _init_styles(self):
        self.font_family = "Segoe UI" if sys.platform == "win32" else "Helvetica"
        self.bg_color = "#090d16"
        self.card_bg = "#111827"
        self.accent_color = "#6366f1"
        self.text_primary = "#f3f4f6"
        self.text_secondary = "#9ca3af"
        self.text_accent = "#818cf8"
        self.border_color = "#1f2937"

    def _apply_win32_stealth(self):
        """Apply Windows OS display affinity after window HWND is initialized."""
        self.root.update_idletasks()
        try:
            self.hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not self.hwnd:
                self.hwnd = self.root.winfo_id()
            success = apply_stealth_affinity(self.hwnd, True)
            self.is_stealth_active = success
            if success:
                print(f"[StealthHUD] Stealth mode ACTIVE (HWND={self.hwnd}). Excluded from all screen capture.")
            else:
                print(f"[StealthHUD] Warning: SetWindowDisplayAffinity returned false. Run as admin if needed.")
        except Exception as e:
            print(f"[StealthHUD] Win32 init error: {e}", file=sys.stderr)

    def _build_ui(self):
        # Top Drag Handle & Controls Bar
        self.header_frame = tk.Frame(self.root, bg="#131b2e", height=38, cursor="fleur")
        self.header_frame.pack(fill=tk.X, side=tk.TOP)
        self.header_frame.bind("<ButtonPress-1>", self._start_drag)
        self.header_frame.bind("<B1-Motion>", self._on_drag)

        # Title & Status Dot
        title_box = tk.Frame(self.header_frame, bg="#131b2e")
        title_box.pack(side=tk.LEFT, padx=10, pady=6)
        title_box.bind("<ButtonPress-1>", self._start_drag)
        title_box.bind("<B1-Motion>", self._on_drag)

        self.status_dot = tk.Canvas(title_box, width=10, height=10, bg="#131b2e", highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT, padx=(0, 6))
        self.dot_id = self.status_dot.create_oval(1, 1, 9, 9, fill="#10b981", outline="")

        self.lbl_title = tk.Label(
            title_box,
            text="STEALTH HUD (INVISIBLE)",
            font=(self.font_family, 9, "bold"),
            fg="#e0e7ff",
            bg="#131b2e",
        )
        self.lbl_title.pack(side=tk.LEFT)
        self.lbl_title.bind("<ButtonPress-1>", self._start_drag)
        self.lbl_title.bind("<B1-Motion>", self._on_drag)

        # Action Buttons in Title Bar
        btn_box = tk.Frame(self.header_frame, bg="#131b2e")
        btn_box.pack(side=tk.RIGHT, padx=6)

        self.btn_stealth_toggle = tk.Button(
            btn_box,
            text="🛡️ Stealth ON",
            font=(self.font_family, 8, "bold"),
            bg="#1e1b4b",
            fg="#a5b4fc",
            activebackground="#312e81",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=6,
            pady=1,
            command=self.toggle_stealth_mode,
        )
        self.btn_stealth_toggle.pack(side=tk.LEFT, padx=2)

        self.btn_clickthrough = tk.Button(
            btn_box,
            text="🖱️ Click-Thru (F10)",
            font=(self.font_family, 8),
            bg="#1e293b",
            fg="#cbd5e1",
            relief=tk.FLAT,
            padx=5,
            pady=1,
            command=self.toggle_click_through,
        )
        self.btn_clickthrough.pack(side=tk.LEFT, padx=2)

        self.btn_collapse = tk.Button(
            btn_box,
            text="–",
            font=(self.font_family, 10, "bold"),
            bg="#1e293b",
            fg="#94a3b8",
            relief=tk.FLAT,
            padx=6,
            pady=0,
            command=self.toggle_collapse,
        )
        self.btn_collapse.pack(side=tk.LEFT, padx=2)

        self.btn_close = tk.Button(
            btn_box,
            text="✕",
            font=(self.font_family, 8, "bold"),
            bg="#3f1d24",
            fg="#f87171",
            activebackground="#ef4444",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=6,
            pady=1,
            command=self.root.destroy,
        )
        self.btn_close.pack(side=tk.LEFT, padx=2)

        # Main Container
        self.main_container = tk.Frame(self.root, bg=self.bg_color)
        self.main_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 10))

        # ── Detected Question Card ──
        q_frame = tk.Frame(self.main_container, bg=self.card_bg, highlightbackground="#374151", highlightthickness=1)
        q_frame.pack(fill=tk.X, pady=(0, 8))

        q_top = tk.Frame(q_frame, bg=self.card_bg)
        q_top.pack(fill=tk.X, padx=8, pady=(6, 2))

        lbl_q_tag = tk.Label(
            q_top,
            text="DETECTED QUESTION",
            font=(self.font_family, 8, "bold"),
            fg="#818cf8",
            bg=self.card_bg,
        )
        lbl_q_tag.pack(side=tk.LEFT)

        self.btn_trigger = tk.Button(
            q_top,
            text="⚡ Generate Answer (F8)",
            font=(self.font_family, 8, "bold"),
            bg="#4338ca",
            fg="#ffffff",
            activebackground="#4f46e5",
            relief=tk.FLAT,
            padx=8,
            pady=2,
            command=self.trigger_answer_generation,
        )
        self.btn_trigger.pack(side=tk.RIGHT)

        self.txt_question = tk.Text(
            q_frame,
            height=2,
            font=(self.font_family, 10, "bold"),
            fg="#f9fafb",
            bg=self.card_bg,
            relief=tk.FLAT,
            wrap=tk.WORD,
            padx=8,
            pady=4,
            insertbackground="white",
        )
        self.txt_question.insert("1.0", "Listening for interviewer questions or type here...")
        self.txt_question.pack(fill=tk.X)

        # ── Answer & Hints Display ──
        ans_frame = tk.Frame(self.main_container, bg=self.card_bg, highlightbackground="#374151", highlightthickness=1)
        ans_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        ans_header = tk.Frame(ans_frame, bg="#1e1b4b", height=26)
        ans_header.pack(fill=tk.X)

        self.lbl_ans_title = tk.Label(
            ans_header,
            text="💡 INSTANT STAR ANSWER & KEY POINTS",
            font=(self.font_family, 8, "bold"),
            fg="#c7d2fe",
            bg="#1e1b4b",
            padx=8,
            pady=4,
        )
        self.lbl_ans_title.pack(side=tk.LEFT)

        self.txt_answer = tk.Text(
            ans_frame,
            font=(self.font_family, 10),
            fg="#f3f4f6",
            bg=self.card_bg,
            relief=tk.FLAT,
            wrap=tk.WORD,
            padx=10,
            pady=8,
            spacing1=3,
            spacing2=2,
        )
        self.txt_answer.pack(fill=tk.BOTH, expand=True)

        # Scrollbar for answer text
        scroll = ttk.Scrollbar(self.txt_answer, command=self.txt_answer.yview)
        self.txt_answer.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self._set_placeholder_answer()

        # ── Bottom Control Bar & Quick Teleprompter Settings ──
        footer = tk.Frame(self.main_container, bg=self.bg_color)
        footer.pack(fill=tk.X, side=tk.BOTTOM)

        # Opacity Slider
        lbl_op = tk.Label(footer, text="Opacity:", font=(self.font_family, 8), fg=self.text_secondary, bg=self.bg_color)
        lbl_op.pack(side=tk.LEFT, padx=(0, 4))

        self.scale_opacity = tk.Scale(
            footer,
            from_=30,
            to=100,
            orient=tk.HORIZONTAL,
            showvalue=False,
            bg=self.bg_color,
            fg="#ffffff",
            highlightthickness=0,
            troughcolor="#1f2937",
            activebackground=self.accent_color,
            length=80,
            command=self._on_opacity_change,
        )
        self.scale_opacity.set(92)
        self.scale_opacity.pack(side=tk.LEFT, padx=(0, 10))

        # Font size toggle
        self.font_size = 10
        btn_font_up = tk.Button(
            footer,
            text="A+",
            font=(self.font_family, 8, "bold"),
            bg="#1f2937",
            fg="#e5e7eb",
            relief=tk.FLAT,
            padx=4,
            command=self._increase_font,
        )
        btn_font_up.pack(side=tk.LEFT, padx=2)

        btn_font_dn = tk.Button(
            footer,
            text="A-",
            font=(self.font_family, 8, "bold"),
            bg="#1f2937",
            fg="#e5e7eb",
            relief=tk.FLAT,
            padx=4,
            command=self._decrease_font,
        )
        btn_font_dn.pack(side=tk.LEFT, padx=2)

        # Clear button
        btn_clear = tk.Button(
            footer,
            text="Clear (F7)",
            font=(self.font_family, 8),
            bg="#1f2937",
            fg="#9ca3af",
            relief=tk.FLAT,
            padx=6,
            command=self.clear_content,
        )
        btn_clear.pack(side=tk.RIGHT, padx=2)

        # Resizer grip at bottom-right
        self.grip = tk.Label(footer, text="⇲", font=(self.font_family, 10), fg="#6b7280", bg=self.bg_color, cursor="size_nw_se")
        self.grip.pack(side=tk.RIGHT, padx=(4, 0))
        self.grip.bind("<ButtonPress-1>", self._start_resize)
        self.grip.bind("<B1-Motion>", self._on_resize)

    def _set_placeholder_answer(self):
        placeholder = (
            "✨ READY FOR LIVE INTERVIEWS (Zoom / Meet / Teams)\n\n"
            "• OS Screen-Share Protection: ACTIVE (Window is 100% hidden in Zoom/Meet/Teams)\n"
            "• Auto-Answer: Generates STAR responses tailored to your uploaded resume\n"
            "• Hotkeys:\n"
            "    [ F9 ] Panic Hide / Show\n"
            "    [ F10 ] Click-Through Mode (Click IDE/Browser underneath)\n"
            "    [ F8 ] Force Generate Answer for Current Question\n"
            "    [ F7 ] Clear Answer\n\n"
            "Waiting for interviewer voice or question input above..."
        )
        self.txt_answer.delete("1.0", tk.END)
        self.txt_answer.insert("1.0", placeholder)

    # ── Drag & Resize ────────────────────────────────────────────────────────
    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    def _start_resize(self, event):
        self._resize_x = event.x_root
        self._resize_y = event.y_root
        self._orig_w = self.root.winfo_width()
        self._orig_h = self.root.winfo_height()

    def _on_resize(self, event):
        w = max(380, self._orig_w + (event.x_root - self._resize_x))
        h = max(200, self._orig_h + (event.y_root - self._resize_y))
        self.root.geometry(f"{w}x{h}")

    # ── Controls ─────────────────────────────────────────────────────────────
    def toggle_stealth_mode(self):
        self.is_stealth_active = not self.is_stealth_active
        apply_stealth_affinity(self.hwnd, self.is_stealth_active)
        if self.is_stealth_active:
            self.btn_stealth_toggle.config(text="🛡️ Stealth ON", bg="#1e1b4b", fg="#a5b4fc")
        else:
            self.btn_stealth_toggle.config(text="⚠️ Stealth OFF", bg="#451a03", fg="#fdba74")

    def toggle_click_through(self):
        self.is_click_through = not self.is_click_through
        set_click_through(self.hwnd, self.is_click_through)
        if self.is_click_through:
            self.btn_clickthrough.config(text="🖱️ Click-Thru ON", bg="#064e3b", fg="#6ee7b7")
            self.root.attributes("-alpha", 0.70)
        else:
            self.btn_clickthrough.config(text="🖱️ Click-Thru (F10)", bg="#1e293b", fg="#cbd5e1")
            self.root.attributes("-alpha", self.scale_opacity.get() / 100.0)

    def toggle_collapse(self):
        self.is_collapsed = not self.is_collapsed
        if self.is_collapsed:
            self.main_container.pack_forget()
            self.root.geometry(f"{self.root.winfo_width()}x38")
            self.btn_collapse.config(text="+")
        else:
            self.root.geometry(f"{self.root.winfo_width()}x640")
            self.main_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 10))
            self.btn_collapse.config(text="–")

    def _on_opacity_change(self, val):
        alpha = float(val) / 100.0
        self.root.attributes("-alpha", alpha)

    def _increase_font(self):
        self.font_size = min(18, self.font_size + 1)
        self.txt_answer.configure(font=(self.font_family, self.font_size))

    def _decrease_font(self):
        self.font_size = max(8, self.font_size - 1)
        self.txt_answer.configure(font=(self.font_family, self.font_size))

    def clear_content(self):
        self.txt_question.delete("1.0", tk.END)
        self.txt_question.insert("1.0", "")
        self._set_placeholder_answer()

    # ── Shortcuts ────────────────────────────────────────────────────────────
    def _bind_shortcuts(self):
        self.root.bind("<F9>", lambda e: self.toggle_visibility())
        self.root.bind("<F10>", lambda e: self.toggle_click_through())
        self.root.bind("<F8>", lambda e: self.trigger_answer_generation())
        self.root.bind("<F7>", lambda e: self.clear_content())

    def toggle_visibility(self):
        if self.root.winfo_viewable():
            self.root.withdraw()
        else:
            self.root.deiconify()
            self.root.attributes("-topmost", True)

    # ── Answer Generation & API Connection ────────────────────────────────────
    def _check_backend_status(self):
        def _check():
            try:
                req = urllib.request.Request(f"{self.api_url}/health", headers={"User-Agent": "StealthHUD"})
                with urllib.request.urlopen(req, timeout=2.0) as res:
                    if res.status == 200:
                        self.root.after(0, lambda: self.status_dot.itemconfig(self.dot_id, fill="#10b981"))
                        return
            except Exception:
                pass
            self.root.after(0, lambda: self.status_dot.itemconfig(self.dot_id, fill="#ef4444"))

        threading.Thread(target=_check, daemon=True).start()

    def trigger_answer_generation(self):
        q_text = self.txt_question.get("1.0", tk.END).strip()
        if not q_text or q_text.startswith("Listening for"):
            return

        self.btn_trigger.config(text="⏳ Thinking...", state=tk.DISABLED)
        self.txt_answer.delete("1.0", tk.END)
        self.txt_answer.insert("1.0", "⏳ Generating personalized answer from your profile and resume...")

        def _fetch():
            try:
                payload = json.dumps({"question": q_text, "question_type": "unknown"}).encode("utf-8")
                req = urllib.request.Request(
                    f"{self.api_url}/api/interviews/suggest-answer",
                    data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "StealthHUD"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15.0) as res:
                    data = json.loads(res.read().decode("utf-8"))
                    self.root.after(0, lambda: self._display_answer(data))
            except Exception as e:
                err_msg = f"❌ Generation error: {e}\nEnsure backend is running at {self.api_url}"
                self.root.after(0, lambda: self._display_error(err_msg))

        threading.Thread(target=_fetch, daemon=True).start()

    def _display_answer(self, data: dict):
        self.btn_trigger.config(text="⚡ Generate Answer (F8)", state=tk.NORMAL)
        self.txt_answer.delete("1.0", tk.END)

        ans = data.get("answer") or ""
        key_points = data.get("key_points") or []
        star = data.get("star") or {}
        follow_ups = data.get("follow_up_questions") or []

        formatted = []
        if ans:
            formatted.append("🎯 DIRECT 30-SEC SOUNDBITE:")
            formatted.append(f"{ans}\n")

        if star and any(star.values()):
            formatted.append("📋 STAR FRAMEWORK:")
            if star.get("situation"):
                formatted.append(f"  • Situation: {star['situation']}")
            if star.get("task"):
                formatted.append(f"  • Task: {star['task']}")
            if star.get("action"):
                formatted.append(f"  • Action: {star['action']}")
            if star.get("result"):
                formatted.append(f"  • Result: {star['result']}")
            formatted.append("")

        if key_points:
            formatted.append("⚡ KEY TALKING POINTS & TRADE-OFFS:")
            for pt in key_points:
                formatted.append(f"  ✓ {pt}")
            formatted.append("")

        if follow_ups:
            formatted.append("❓ LIKELY FOLLOW-UPS:")
            for fu in follow_ups:
                formatted.append(f"  → {fu}")

        full_text = "\n".join(formatted) if formatted else ans or "No answer data returned."
        self.txt_answer.insert("1.0", full_text)

    def _display_error(self, message: str):
        self.btn_trigger.config(text="⚡ Generate Answer (F8)", state=tk.NORMAL)
        self.txt_answer.delete("1.0", tk.END)
        self.txt_answer.insert("1.0", message)


def main():
    root = tk.Tk()
    app = StealthOverlayApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

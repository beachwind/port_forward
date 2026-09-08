#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI Manager - Remote Server Management Utility
커맨드 창 관리 유틸리티
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import json
import os
import subprocess
import threading
import base64
import hashlib
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import paramiko
import stat
import shutil
import tempfile
import datetime

# ─────────────────────────────────────────────
# GVim / Diff 설정
# ─────────────────────────────────────────────
GVIM_PATH = r"C:\Util\cygwin\bin\gvim.exe"

# ─────────────────────────────────────────────
# CLI Logger  — 실행 명령어를 콘솔에 출력
# ─────────────────────────────────────────────
import sys

class CmdLogger:
    """
    앱 운영 중 실행되는 모든 CLI 명령어를 stdout 에 컬러 출력한다.
    Windows cmd / PowerShell / Cygwin 터미널 모두 지원.
    """
    # ANSI 색상 (Windows 10+ 지원, 구버전은 자동 무시)
    _RESET  = "\033[0m"
    _BOLD   = "\033[1m"
    _DIM    = "\033[2m"
    _CYAN   = "\033[36m"
    _GREEN  = "\033[32m"
    _YELLOW = "\033[33m"
    _MAGENTA= "\033[35m"
    _RED    = "\033[31m"
    _BLUE   = "\033[34m"
    _WHITE  = "\033[97m"

    def __init__(self):
        # Windows 에서 ANSI 활성화
        if sys.platform == "win32":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            except Exception:
                pass
        self._print_banner()

    def _ts(self) -> str:
        return datetime.datetime.now().strftime("%H:%M:%S.%f")[:12]

    def _print_banner(self):
        w = 68
        print()
        print(f"{self._CYAN}{'─'*w}{self._RESET}")
        print(f"{self._BOLD}{self._WHITE}  ⚡ CLI Manager  —  명령어 실행 로그 (디버그){self._RESET}")
        print(f"{self._DIM}  {sys.executable}  {' '.join(sys.argv)}{self._RESET}")
        print(f"{self._CYAN}{'─'*w}{self._RESET}")
        print()

    # ── 공개 로그 메서드 ──────────────────────────────────────────────

    def cmd(self, cmd_list: list, category: str = "CMD", note: str = ""):
        """subprocess 실행 명령어 출력"""
        ts   = self._ts()
        tag  = self._tag_color(category)
        line = " ".join(self._quote(c) for c in cmd_list)
        print(f"{self._DIM}[{ts}]{self._RESET} {tag}  "
              f"{self._WHITE}{line}{self._RESET}")
        if note:
            print(f"          {self._DIM}↳ {note}{self._RESET}")

    def ssh(self, host: str, port: int, user: str,
            auth: str = "password", key_path: str = ""):
        """paramiko SSH 연결 출력"""
        ts  = self._ts()
        tag = f"{self._BLUE}[SSH]{self._RESET}"
        key_info = (f"  -i {self._YELLOW}{key_path}{self._RESET}"
                    if key_path else "")
        print(f"{self._DIM}[{ts}]{self._RESET} {tag}  "
              f"{self._GREEN}{user}@{host}:{port}{self._RESET}"
              f"  auth={self._MAGENTA}{auth}{self._RESET}{key_info}")

    def sftp(self, action: str, src: str, dst: str = ""):
        """SFTP 전송 출력"""
        ts  = self._ts()
        tag = f"{self._MAGENTA}[SFTP]{self._RESET}"
        arrow = "→" if dst else ""
        dst_part = f"  {arrow}  {self._YELLOW}{dst}{self._RESET}" if dst else ""
        print(f"{self._DIM}[{ts}]{self._RESET} {tag}  "
              f"{self._CYAN}{action.upper():8s}{self._RESET}  "
              f"{src}{dst_part}")

    def diff(self, mode: str, file_a: str, file_b: str):
        """Diff 실행 출력"""
        ts  = self._ts()
        tag = f"{self._YELLOW}[DIFF]{self._RESET}"
        print(f"{self._DIM}[{ts}]{self._RESET} {tag}  "
              f"{self._BOLD}{mode}{self._RESET}  "
              f"{self._RED}{file_a}{self._RESET}  ↔  "
              f"{self._BLUE}{file_b}{self._RESET}")

    def info(self, msg: str):
        """일반 정보 출력"""
        ts = self._ts()
        print(f"{self._DIM}[{ts}]{self._RESET} "
              f"{self._GREEN}[INFO]{self._RESET}  {msg}")

    def warn(self, msg: str):
        """경고 출력"""
        ts = self._ts()
        print(f"{self._DIM}[{ts}]{self._RESET} "
              f"{self._YELLOW}[WARN]{self._RESET}  {msg}", file=sys.stderr)

    def error(self, msg: str):
        """오류 출력"""
        ts = self._ts()
        print(f"{self._DIM}[{ts}]{self._RESET} "
              f"{self._RED}[ERR ]{self._RESET}  {msg}", file=sys.stderr)

    def sep(self, label: str = ""):
        """구분선 출력"""
        w = 68
        if label:
            pad = (w - len(label) - 4) // 2
            print(f"{self._DIM}{'─'*pad}  {label}  {'─'*pad}{self._RESET}")
        else:
            print(f"{self._DIM}{'─'*w}{self._RESET}")

    # ── 내부 헬퍼 ─────────────────────────────────────────────────────

    @staticmethod
    def _quote(s: str) -> str:
        """공백 포함 토큰을 따옴표로 감싼다"""
        return f'"{s}"' if " " in s else s

    def _tag_color(self, cat: str) -> str:
        colors = {
            "CLI":     self._CYAN,
            "CMD":     self._CYAN,
            "PUTTY":   self._YELLOW,
            "CYGWIN":  self._GREEN,
            "PS":      self._BLUE,
            "VIMDIFF": self._MAGENTA,
            "DIFF":    self._YELLOW,
            "GVIM":    self._YELLOW,
        }
        c = colors.get(cat.upper(), self._WHITE)
        return f"{c}[{cat.upper():7s}]{self._RESET}"


# 전역 로거 인스턴스
LOG = CmdLogger()

# ─────────────────────────────────────────────
# Constants & Config
# ─────────────────────────────────────────────
APP_NAME = "CLI Manager"
CONFIG_DIR = Path.home() / ".cli_manager"
CONFIG_FILE = CONFIG_DIR / "config.json"
KEY_FILE = CONFIG_DIR / ".key"

DEFAULT_COMMANDS = [
    {"name": "PowerShell", "path": "powershell.exe", "args": "", "icon": "🔵"},
    {"name": "CMD", "path": "cmd.exe", "args": "", "icon": "⬛"},
    {"name": "Cygwin", "path": r"C:\Util\cygwin\cygwin64\Cygwin.bat", "args": "", "icon": "🐧"},
    {"name": "PuTTY", "path": r"C:\Util\putty\putty.exe", "args": "", "icon": "🖥️"},
]

DEFAULT_CONFIG = {
    "commands": DEFAULT_COMMANDS,
    "servers": [],
    "theme": "dark",
    "window_geometry": "1280x800",
}

# ─────────────────────────────────────────────
# Encryption Manager
# ─────────────────────────────────────────────
class CryptoManager:
    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not KEY_FILE.exists():
            key = Fernet.generate_key()
            KEY_FILE.write_bytes(key)
            KEY_FILE.chmod(0o600)
        self.fernet = Fernet(KEY_FILE.read_bytes())

    def encrypt(self, text: str) -> str:
        return self.fernet.encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self.fernet.decrypt(token.encode()).decode()
        except Exception:
            return ""


# ─────────────────────────────────────────────
# Config Manager
# ─────────────────────────────────────────────
class ConfigManager:
    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.crypto = CryptoManager()
        self.data = self._load()

    def _load(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Merge defaults for missing keys
                for k, v in DEFAULT_CONFIG.items():
                    if k not in data:
                        data[k] = v
                return data
            except Exception:
                pass
        return dict(DEFAULT_CONFIG)

    def save(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get_commands(self):
        return self.data.get("commands", DEFAULT_COMMANDS)

    def set_commands(self, commands):
        self.data["commands"] = commands
        self.save()

    def get_servers(self):
        return self.data.get("servers", [])

    def add_server(self, server: dict):
        servers = self.get_servers()
        servers.append(server)
        self.data["servers"] = servers
        self.save()

    def update_server(self, idx: int, server: dict):
        self.data["servers"][idx] = server
        self.save()

    def delete_server(self, idx: int):
        self.data["servers"].pop(idx)
        self.save()

    def encrypt_password(self, pw: str) -> str:
        return self.crypto.encrypt(pw)

    def decrypt_password(self, token: str) -> str:
        return self.crypto.decrypt(token)

    def get_saved_paths(self, server_id: str, side: str) -> list:
        """side: 'local' or 'remote'"""
        key = f"paths_{server_id}_{side}"
        return self.data.get(key, [])

    def add_saved_path(self, server_id: str, side: str, path: str):
        key = f"paths_{server_id}_{side}"
        paths = self.data.get(key, [])
        if path not in paths:
            paths.insert(0, path)
            if len(paths) > 20:
                paths = paths[:20]
        self.data[key] = paths
        self.save()


# ─────────────────────────────────────────────
# Color Theme
# ─────────────────────────────────────────────
THEME = {
    "bg": "#0d1117",
    "sidebar": "#161b22",
    "panel": "#1c2128",
    "card": "#21262d",
    "border": "#30363d",
    "accent": "#238636",
    "accent2": "#1f6feb",
    "accent3": "#e36209",
    "text": "#e6edf3",
    "text2": "#8b949e",
    "text3": "#6e7681",
    "danger": "#da3633",
    "warning": "#d29922",
    "success": "#3fb950",
    "hover": "#2d333b",
    "selected": "#264f78",
    "input_bg": "#0d1117",
}


# ─────────────────────────────────────────────
# Styled Widgets Helper
# ─────────────────────────────────────────────
def styled_btn(parent, text, command=None, color=None, width=None, icon=""):
    bg = color or THEME["accent2"]
    btn = tk.Button(
        parent, text=f"{icon} {text}".strip() if icon else text,
        command=command, bg=bg, fg=THEME["text"],
        relief="flat", bd=0, pady=6, padx=12,
        font=("Consolas", 9, "bold"), cursor="hand2",
        activebackground=THEME["hover"], activeforeground=THEME["text"],
    )
    if width:
        btn.config(width=width)
    return btn


def styled_entry(parent, textvariable=None, show=None, width=30):
    e = tk.Entry(
        parent, bg=THEME["input_bg"], fg=THEME["text"],
        insertbackground=THEME["text"], relief="flat",
        highlightthickness=1, highlightbackground=THEME["border"],
        highlightcolor=THEME["accent2"], width=width,
        font=("Consolas", 10),
    )
    if textvariable:
        e.config(textvariable=textvariable)
    if show:
        e.config(show=show)
    return e


def styled_label(parent, text, size=10, color=None, bold=False):
    font = ("Malgun Gothic", size, "bold") if bold else ("Malgun Gothic", size)
    return tk.Label(
        parent, text=text, bg=THEME["bg"],
        fg=color or THEME["text"], font=font,
    )


# ─────────────────────────────────────────────
# Login Dialog
# ─────────────────────────────────────────────
class LoginDialog(tk.Toplevel):
    def __init__(self, parent, server: dict, config: ConfigManager):
        super().__init__(parent)
        self.server = server
        self.config = config
        self.result = None
        self.title(f"🔐 로그인 — {server.get('name','')}")
        self.configure(bg=THEME["bg"])
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._build()
        self.center()

    def center(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = self.winfo_screenwidth() // 2 - w // 2
        y = self.winfo_screenheight() // 2 - h // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self):
        auth_method = self.server.get("auth_method", "password")
        is_key_auth = auth_method in ("ppk", "pem")

        # Dynamic height based on auth method
        h = 420 if is_key_auth else 380
        self.geometry(f"440x{h}")

        # Header
        hdr = tk.Frame(self, bg=THEME["sidebar"], pady=14, padx=20)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🖥️  원격 서버 로그인", bg=THEME["sidebar"],
                 fg=THEME["text"], font=("Malgun Gothic", 13, "bold")).pack(anchor="w")

        # Sub-info line
        auth_icon = "🗝️" if is_key_auth else "🔑"
        auth_label = {"ppk": "PPK 키 인증", "pem": "PEM 키 인증", "password": "비밀번호 인증"}.get(auth_method, "")
        tk.Label(hdr,
                 text=f"{self.server.get('host','')}:{self.server.get('port',22)}  ·  {auth_icon} {auth_label}",
                 bg=THEME["sidebar"], fg=THEME["text2"], font=("Consolas", 9)).pack(anchor="w")

        body = tk.Frame(self, bg=THEME["bg"], padx=28, pady=16)
        body.pack(fill="both", expand=True)

        saved_user = self.server.get("username", "")
        saved_pw_enc = self.server.get("password_enc", "")
        saved_pw = self.config.decrypt_password(saved_pw_enc) if saved_pw_enc else ""

        def lbl(t):
            tk.Label(body, text=t, bg=THEME["bg"], fg=THEME["text2"],
                     font=("Malgun Gothic", 9)).pack(anchor="w", pady=(10, 2))

        lbl("사용자 이름")
        self.var_user = tk.StringVar(value=saved_user)
        e_user = styled_entry(body, textvariable=self.var_user, width=34)
        e_user.pack(anchor="w")

        if is_key_auth:
            # Show key file path (read-only info)
            key_path = self.server.get("ppk_path", "") if auth_method == "ppk" else self.server.get("pem_path", "")
            lbl(f"{'PPK' if auth_method=='ppk' else 'PEM'} 키 파일")
            key_info = tk.Frame(body, bg=THEME["card"], padx=8, pady=6)
            key_info.pack(anchor="w", fill="x")
            icon_color = THEME["warning"] if auth_method == "ppk" else THEME["accent2"]
            tk.Label(key_info, text=f"🗝️  {key_path or '(미설정)'}",
                     bg=THEME["card"], fg=icon_color,
                     font=("Consolas", 9), wraplength=340, justify="left").pack(anchor="w")

            # Key passphrase
            saved_key_pw_enc = self.server.get("key_passphrase_enc", "")
            saved_key_pw = self.config.decrypt_password(saved_key_pw_enc) if saved_key_pw_enc else ""
            lbl("키 패스프레이즈 (있는 경우)")
            self.var_key_pw = tk.StringVar(value=saved_key_pw)
            e_kpw = styled_entry(body, textvariable=self.var_key_pw, show="●", width=34)
            e_kpw.pack(anchor="w")
            e_kpw.bind("<Return>", lambda e: self._login())
        else:
            lbl("비밀번호")
            self.var_pw = tk.StringVar(value=saved_pw)
            e_pw = styled_entry(body, textvariable=self.var_pw, show="●", width=34)
            e_pw.pack(anchor="w")
            e_pw.bind("<Return>", lambda e: self._login())

        # Save checkbox
        self.var_save = tk.BooleanVar(value=bool(saved_user))
        save_frame = tk.Frame(body, bg=THEME["bg"])
        save_frame.pack(anchor="w", pady=(12, 0))
        tk.Checkbutton(
            save_frame, text="로그인 정보 저장", variable=self.var_save,
            bg=THEME["bg"], fg=THEME["text"], selectcolor=THEME["card"],
            activebackground=THEME["bg"], activeforeground=THEME["text"],
            font=("Malgun Gothic", 9),
        ).pack(side="left")

        # Buttons
        btn_frame = tk.Frame(body, bg=THEME["bg"])
        btn_frame.pack(pady=14)
        styled_btn(btn_frame, "로그인", command=self._login,
                   color=THEME["accent"], icon="🔑").pack(side="left", padx=6)
        styled_btn(btn_frame, "취소", command=self.destroy,
                   color=THEME["danger"], icon="✖").pack(side="left", padx=6)

    def _login(self):
        user = self.var_user.get().strip()
        auth_method = self.server.get("auth_method", "password")
        is_key_auth = auth_method in ("ppk", "pem")

        if not user:
            messagebox.showerror("오류", "사용자 이름을 입력하세요.", parent=self)
            return

        if is_key_auth:
            key_path = self.server.get("ppk_path", "") if auth_method == "ppk" else self.server.get("pem_path", "")
            if not key_path:
                messagebox.showerror("오류", f"서버 설정에서 {auth_method.upper()} 파일 경로를 먼저 등록하세요.", parent=self)
                return
            key_pw = getattr(self, "var_key_pw", tk.StringVar()).get()
            self.result = {
                "username": user,
                "password": "",
                "auth_method": auth_method,
                "key_path": key_path,
                "key_passphrase": key_pw,
                "save": self.var_save.get(),
            }
        else:
            pw = self.var_pw.get()
            if not pw:
                messagebox.showerror("오류", "비밀번호를 입력하세요.", parent=self)
                return
            self.result = {
                "username": user,
                "password": pw,
                "auth_method": "password",
                "key_path": "",
                "key_passphrase": "",
                "save": self.var_save.get(),
            }
        self.destroy()


# ─────────────────────────────────────────────
# Command Settings Dialog
# ─────────────────────────────────────────────
class CommandSettingsDialog(tk.Toplevel):
    def __init__(self, parent, config: ConfigManager):
        super().__init__(parent)
        self.config = config
        self.commands = list(config.get_commands())
        self.title("⚙️  커맨드 실행 파일 설정")
        self.configure(bg=THEME["bg"])
        self.geometry("680x520")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()
        self._build()
        self.center()

    def center(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = self.winfo_screenwidth() // 2 - w // 2
        y = self.winfo_screenheight() // 2 - h // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self):
        hdr = tk.Frame(self, bg=THEME["sidebar"], pady=14, padx=20)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚙️  커맨드 실행 파일 관리", bg=THEME["sidebar"],
                 fg=THEME["text"], font=("Malgun Gothic", 13, "bold")).pack(anchor="w")
        tk.Label(hdr, text="CLI 실행 프로그램을 등록하고 관리합니다.",
                 bg=THEME["sidebar"], fg=THEME["text2"], font=("Malgun Gothic", 9)).pack(anchor="w")

        main = tk.Frame(self, bg=THEME["bg"])
        main.pack(fill="both", expand=True, padx=16, pady=12)

        # List + buttons
        list_frame = tk.Frame(main, bg=THEME["bg"])
        list_frame.pack(side="left", fill="both", expand=True)

        tk.Label(list_frame, text="등록된 커맨드", bg=THEME["bg"],
                 fg=THEME["text2"], font=("Malgun Gothic", 9, "bold")).pack(anchor="w")

        lb_frame = tk.Frame(list_frame, bg=THEME["border"], pady=1, padx=1)
        lb_frame.pack(fill="both", expand=True, pady=(4, 0))

        self.listbox = tk.Listbox(
            lb_frame, bg=THEME["card"], fg=THEME["text"],
            selectbackground=THEME["accent2"], selectforeground="white",
            font=("Consolas", 10), relief="flat", bd=0,
            activestyle="none",
        )
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        btn_row = tk.Frame(list_frame, bg=THEME["bg"])
        btn_row.pack(fill="x", pady=6)
        styled_btn(btn_row, "추가", self._add_cmd, color=THEME["accent"], icon="➕").pack(side="left", padx=2)
        styled_btn(btn_row, "삭제", self._del_cmd, color=THEME["danger"], icon="🗑️").pack(side="left", padx=2)
        styled_btn(btn_row, "위로", self._move_up, color=THEME["card"], icon="⬆").pack(side="left", padx=2)
        styled_btn(btn_row, "아래", self._move_down, color=THEME["card"], icon="⬇").pack(side="left", padx=2)

        # Edit panel
        edit = tk.Frame(main, bg=THEME["panel"], padx=16, pady=16, width=280)
        edit.pack(side="right", fill="y", padx=(12, 0))
        edit.pack_propagate(False)

        tk.Label(edit, text="커맨드 편집", bg=THEME["panel"],
                 fg=THEME["text"], font=("Malgun Gothic", 10, "bold")).pack(anchor="w", pady=(0, 10))

        def lbl(t):
            tk.Label(edit, text=t, bg=THEME["panel"],
                     fg=THEME["text2"], font=("Malgun Gothic", 9)).pack(anchor="w", pady=(8, 2))

        lbl("이름")
        self.var_name = tk.StringVar()
        styled_entry(edit, textvariable=self.var_name, width=28).pack(anchor="w")

        lbl("아이콘 (이모지)")
        self.var_icon = tk.StringVar()
        styled_entry(edit, textvariable=self.var_icon, width=6).pack(anchor="w")

        lbl("실행 파일 경로")
        path_frame = tk.Frame(edit, bg=THEME["panel"])
        path_frame.pack(anchor="w", fill="x")
        self.var_path = tk.StringVar()
        e_path = styled_entry(path_frame, textvariable=self.var_path, width=22)
        e_path.pack(side="left")
        styled_btn(path_frame, "...", command=self._browse_path,
                   color=THEME["card"], icon="📂").pack(side="left", padx=4)

        lbl("추가 인수 (선택)")
        self.var_args = tk.StringVar()
        styled_entry(edit, textvariable=self.var_args, width=28).pack(anchor="w")

        styled_btn(edit, "적용", self._apply_edit,
                   color=THEME["accent2"], icon="✔").pack(pady=(14, 0))

        # Save/Cancel
        bottom = tk.Frame(self, bg=THEME["bg"])
        bottom.pack(fill="x", padx=16, pady=10)
        styled_btn(bottom, "저장", self._save, color=THEME["accent"], icon="💾").pack(side="right", padx=4)
        styled_btn(bottom, "닫기", self.destroy, color=THEME["danger"], icon="✖").pack(side="right", padx=4)

        self._refresh_list()

    def _refresh_list(self):
        self.listbox.delete(0, tk.END)
        for cmd in self.commands:
            icon = cmd.get("icon", "▶")
            name = cmd.get("name", "Unknown")
            self.listbox.insert(tk.END, f"  {icon}  {name}")

    def _on_select(self, _=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        cmd = self.commands[sel[0]]
        self.var_name.set(cmd.get("name", ""))
        self.var_icon.set(cmd.get("icon", "▶"))
        self.var_path.set(cmd.get("path", ""))
        self.var_args.set(cmd.get("args", ""))

    def _browse_path(self):
        path = filedialog.askopenfilename(
            parent=self,
            title="실행 파일 선택",
            filetypes=[("실행 파일", "*.exe *.bat *.cmd *.sh"), ("모든 파일", "*.*")],
        )
        if path:
            self.var_path.set(path)

    def _apply_edit(self):
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showwarning("선택 없음", "목록에서 항목을 선택하세요.", parent=self)
            return
        idx = sel[0]
        self.commands[idx] = {
            "name": self.var_name.get().strip(),
            "icon": self.var_icon.get().strip() or "▶",
            "path": self.var_path.get().strip(),
            "args": self.var_args.get().strip(),
        }
        self._refresh_list()
        self.listbox.selection_set(idx)

    def _add_cmd(self):
        self.commands.append({"name": "새 커맨드", "path": "", "args": "", "icon": "▶"})
        self._refresh_list()
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(tk.END)
        self._on_select()

    def _del_cmd(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        if messagebox.askyesno("삭제 확인", "선택한 항목을 삭제하시겠습니까?", parent=self):
            del self.commands[sel[0]]
            self._refresh_list()

    def _move_up(self):
        sel = self.listbox.curselection()
        if not sel or sel[0] == 0:
            return
        i = sel[0]
        self.commands[i], self.commands[i - 1] = self.commands[i - 1], self.commands[i]
        self._refresh_list()
        self.listbox.selection_set(i - 1)

    def _move_down(self):
        sel = self.listbox.curselection()
        if not sel or sel[0] >= len(self.commands) - 1:
            return
        i = sel[0]
        self.commands[i], self.commands[i + 1] = self.commands[i + 1], self.commands[i]
        self._refresh_list()
        self.listbox.selection_set(i + 1)

    def _save(self):
        self.config.set_commands(self.commands)
        messagebox.showinfo("저장 완료", "커맨드 설정이 저장되었습니다.", parent=self)
        self.destroy()


# ─────────────────────────────────────────────
# Server Add/Edit Dialog
# ─────────────────────────────────────────────
class ServerDialog(tk.Toplevel):
    def __init__(self, parent, config: ConfigManager, server=None, index=None):
        super().__init__(parent)
        self.config = config
        self.server = server or {}
        self.index = index
        self.title("서버 추가" if index is None else "서버 편집")
        self.configure(bg=THEME["bg"])
        self.geometry("520x640")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()
        self._build()
        self.center()

    def center(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = self.winfo_screenwidth() // 2 - w // 2
        y = self.winfo_screenheight() // 2 - h // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self):
        hdr = tk.Frame(self, bg=THEME["sidebar"], pady=12, padx=20)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🖥️  원격 서버 등록",
                 bg=THEME["sidebar"], fg=THEME["text"],
                 font=("Malgun Gothic", 12, "bold")).pack(anchor="w")
        tk.Label(hdr, text="서버 접속 정보 및 인증 키 파일을 등록합니다.",
                 bg=THEME["sidebar"], fg=THEME["text2"],
                 font=("Malgun Gothic", 8)).pack(anchor="w")

        # Scrollable body
        canvas = tk.Canvas(self, bg=THEME["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(canvas, bg=THEME["bg"], padx=28, pady=16)
        body_win = canvas.create_window((0, 0), window=body, anchor="nw")

        def on_configure(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        def on_canvas_resize(e):
            canvas.itemconfig(body_win, width=e.width)
        body.bind("<Configure>", on_configure)
        canvas.bind("<Configure>", on_canvas_resize)

        def lbl(t, required=False):
            f = tk.Frame(body, bg=THEME["bg"])
            f.pack(anchor="w", pady=(10, 2))
            tk.Label(f, text=t, bg=THEME["bg"], fg=THEME["text2"],
                     font=("Malgun Gothic", 9)).pack(side="left")
            if required:
                tk.Label(f, text=" *", bg=THEME["bg"], fg=THEME["danger"],
                         font=("Malgun Gothic", 9, "bold")).pack(side="left")

        def section_sep(title):
            sep_f = tk.Frame(body, bg=THEME["bg"])
            sep_f.pack(fill="x", pady=(16, 4))
            tk.Frame(sep_f, bg=THEME["border"], height=1).pack(fill="x", side="left", expand=True, pady=6)
            tk.Label(sep_f, text=f"  {title}  ", bg=THEME["bg"], fg=THEME["text3"],
                     font=("Malgun Gothic", 8)).pack(side="left")
            tk.Frame(sep_f, bg=THEME["border"], height=1).pack(fill="x", side="left", expand=True, pady=6)

        s = self.server

        # ── 기본 정보 ──────────────────────────
        section_sep("📋 기본 정보")

        lbl("서버 이름 (별칭)", required=True)
        self.v_name = tk.StringVar(value=s.get("name", ""))
        styled_entry(body, textvariable=self.v_name, width=40).pack(anchor="w")

        lbl("호스트 / IP", required=True)
        self.v_host = tk.StringVar(value=s.get("host", ""))
        styled_entry(body, textvariable=self.v_host, width=40).pack(anchor="w")

        lbl("포트")
        self.v_port = tk.StringVar(value=str(s.get("port", 22)))
        styled_entry(body, textvariable=self.v_port, width=10).pack(anchor="w")

        lbl("설명 (선택)")
        self.v_desc = tk.StringVar(value=s.get("description", ""))
        styled_entry(body, textvariable=self.v_desc, width=40).pack(anchor="w")

        # ── 접속 디렉토리 ──────────────────────
        section_sep("📁 접속 디렉토리")

        lbl("초기 접속 디렉토리 (선택)")
        tk.Label(body, text="접속 후 자동으로 이동할 디렉토리 경로 (예: /home/user/work)",
                 bg=THEME["bg"], fg=THEME["text3"], font=("Malgun Gothic", 8)).pack(anchor="w")
        self.v_init_dir = tk.StringVar(value=s.get("init_dir", ""))
        styled_entry(body, textvariable=self.v_init_dir, width=40).pack(anchor="w")

        # ── 인증 정보 ──────────────────────────
        section_sep("🔐 인증 정보")

        lbl("사용자 이름 (저장, 선택)")
        self.v_user = tk.StringVar(value=s.get("username", ""))
        styled_entry(body, textvariable=self.v_user, width=40).pack(anchor="w")

        lbl("비밀번호 (저장, 선택)")
        self.v_pw = tk.StringVar()
        pw_dec = self.config.decrypt_password(s.get("password_enc", "")) if s.get("password_enc") else ""
        self.v_pw.set(pw_dec)
        styled_entry(body, textvariable=self.v_pw, show="●", width=40).pack(anchor="w")

        # ── 키 파일 인증 ───────────────────────
        section_sep("🗝️  키 파일 인증 (PPK / PEM)")

        # Auth method radio
        self.v_auth_method = tk.StringVar(value=s.get("auth_method", "password"))
        auth_frame = tk.Frame(body, bg=THEME["bg"])
        auth_frame.pack(anchor="w", pady=(4, 8))

        def make_radio(f, text, val, color):
            tk.Radiobutton(
                f, text=text, variable=self.v_auth_method, value=val,
                bg=THEME["bg"], fg=color, selectcolor=THEME["card"],
                activebackground=THEME["bg"], activeforeground=color,
                font=("Malgun Gothic", 9), command=self._on_auth_method_change,
            ).pack(side="left", padx=(0, 12))

        make_radio(auth_frame, "🔑 비밀번호 인증", "password", THEME["text2"])
        make_radio(auth_frame, "🗝️  PPK 키 파일", "ppk", THEME["warning"])
        make_radio(auth_frame, "🗝️  PEM 키 파일", "pem", THEME["accent2"])

        # PPK file row
        self.ppk_frame = tk.Frame(body, bg=THEME["bg"])
        self.ppk_frame.pack(anchor="w", fill="x")
        tk.Label(self.ppk_frame, text="PPK 파일 경로 (.ppk)",
                 bg=THEME["bg"], fg=THEME["text2"],
                 font=("Malgun Gothic", 9)).pack(anchor="w", pady=(0, 2))
        ppk_row = tk.Frame(self.ppk_frame, bg=THEME["bg"])
        ppk_row.pack(anchor="w", fill="x")
        self.v_ppk = tk.StringVar(value=s.get("ppk_path", ""))
        styled_entry(ppk_row, textvariable=self.v_ppk, width=32).pack(side="left")
        styled_btn(ppk_row, "찾기", command=self._browse_ppk,
                   color=THEME["warning"], icon="📂").pack(side="left", padx=6)
        tk.Label(self.ppk_frame,
                 text="PuTTY PPK 형식 키 파일을 지정합니다. (-i 옵션으로 접속)",
                 bg=THEME["bg"], fg=THEME["text3"],
                 font=("Malgun Gothic", 8)).pack(anchor="w", pady=(2, 0))

        # PEM file row
        self.pem_frame = tk.Frame(body, bg=THEME["bg"])
        self.pem_frame.pack(anchor="w", fill="x", pady=(8, 0))
        tk.Label(self.pem_frame, text="PEM 파일 경로 (.pem / .key)",
                 bg=THEME["bg"], fg=THEME["text2"],
                 font=("Malgun Gothic", 9)).pack(anchor="w", pady=(0, 2))
        pem_row = tk.Frame(self.pem_frame, bg=THEME["bg"])
        pem_row.pack(anchor="w", fill="x")
        self.v_pem = tk.StringVar(value=s.get("pem_path", ""))
        styled_entry(pem_row, textvariable=self.v_pem, width=32).pack(side="left")
        styled_btn(pem_row, "찾기", command=self._browse_pem,
                   color=THEME["accent2"], icon="📂").pack(side="left", padx=6)
        tk.Label(self.pem_frame,
                 text="OpenSSH PEM 형식 키 파일을 지정합니다. (-i 옵션으로 접속)",
                 bg=THEME["bg"], fg=THEME["text3"],
                 font=("Malgun Gothic", 8)).pack(anchor="w", pady=(2, 0))

        # Key passphrase
        self.key_pw_frame = tk.Frame(body, bg=THEME["bg"])
        self.key_pw_frame.pack(anchor="w", fill="x", pady=(8, 0))
        tk.Label(self.key_pw_frame, text="키 파일 패스프레이즈 (선택)",
                 bg=THEME["bg"], fg=THEME["text2"],
                 font=("Malgun Gothic", 9)).pack(anchor="w", pady=(0, 2))
        self.v_key_pw = tk.StringVar()
        key_pw_dec = self.config.decrypt_password(s.get("key_passphrase_enc", "")) if s.get("key_passphrase_enc") else ""
        self.v_key_pw.set(key_pw_dec)
        styled_entry(self.key_pw_frame, textvariable=self.v_key_pw, show="●", width=32).pack(anchor="w")

        # Buttons
        btn_frame = tk.Frame(body, bg=THEME["bg"])
        btn_frame.pack(pady=18)
        styled_btn(btn_frame, "저장", self._save, color=THEME["accent"], icon="💾").pack(side="left", padx=6)
        styled_btn(btn_frame, "취소", self.destroy, color=THEME["danger"], icon="✖").pack(side="left", padx=6)

        # Apply initial visibility
        self._on_auth_method_change()

    def _on_auth_method_change(self):
        method = self.v_auth_method.get()
        # Show/hide ppk frame
        if method == "ppk":
            self.ppk_frame.pack(anchor="w", fill="x")
            self.pem_frame.pack_forget()
            self.key_pw_frame.pack(anchor="w", fill="x", pady=(8, 0))
        elif method == "pem":
            self.pem_frame.pack(anchor="w", fill="x")
            self.ppk_frame.pack_forget()
            self.key_pw_frame.pack(anchor="w", fill="x", pady=(8, 0))
        else:  # password
            self.ppk_frame.pack_forget()
            self.pem_frame.pack_forget()
            self.key_pw_frame.pack_forget()

    def _browse_ppk(self):
        path = filedialog.askopenfilename(
            parent=self, title="PPK 키 파일 선택",
            filetypes=[("PPK 파일", "*.ppk"), ("모든 파일", "*.*")],
        )
        if path:
            self.v_ppk.set(path)

    def _browse_pem(self):
        path = filedialog.askopenfilename(
            parent=self, title="PEM 키 파일 선택",
            filetypes=[("PEM/KEY 파일", "*.pem *.key"), ("모든 파일", "*.*")],
        )
        if path:
            self.v_pem.set(path)

    def _save(self):
        name = self.v_name.get().strip()
        host = self.v_host.get().strip()
        port_s = self.v_port.get().strip()
        if not name or not host:
            messagebox.showerror("오류", "이름과 호스트는 필수입니다.", parent=self)
            return
        try:
            port = int(port_s)
        except ValueError:
            messagebox.showerror("오류", "포트는 숫자여야 합니다.", parent=self)
            return

        auth_method = self.v_auth_method.get()

        # Validate key file if selected
        ppk_path = self.v_ppk.get().strip()
        pem_path = self.v_pem.get().strip()
        if auth_method == "ppk" and not ppk_path:
            messagebox.showerror("오류", "PPK 파일 경로를 지정하세요.", parent=self)
            return
        if auth_method == "pem" and not pem_path:
            messagebox.showerror("오류", "PEM 파일 경로를 지정하세요.", parent=self)
            return

        pw = self.v_pw.get()
        pw_enc = self.config.encrypt_password(pw) if pw else ""

        key_pw = self.v_key_pw.get()
        key_pw_enc = self.config.encrypt_password(key_pw) if key_pw else ""

        server = {
            "id": self.server.get("id", f"srv_{hash(host+name) & 0xFFFFFF}"),
            "name": name,
            "host": host,
            "port": port,
            "username": self.v_user.get().strip(),
            "password_enc": pw_enc,
            "description": self.v_desc.get().strip(),
            "init_dir": self.v_init_dir.get().strip(),
            "auth_method": auth_method,
            "ppk_path": ppk_path,
            "pem_path": pem_path,
            "key_passphrase_enc": key_pw_enc,
        }
        if self.index is None:
            self.config.add_server(server)
        else:
            self.config.update_server(self.index, server)
        self.destroy()


# ─────────────────────────────────────────────
# File Transfer Dialog (SFTP)
# ─────────────────────────────────────────────
class FileTransferDialog(tk.Toplevel):
    def __init__(self, parent, sftp, server: dict, config: ConfigManager):
        super().__init__(parent)
        self.sftp = sftp
        self.server = server
        self.config = config
        self.server_id = server.get("id", "unknown")
        self.title(f"📁  파일 전송 — {server.get('name','')}")
        self.configure(bg=THEME["bg"])
        self.geometry("1000x660")
        self.resizable(True, True)
        self.transient(parent)

        self.local_path = tk.StringVar(value=str(Path.home()))
        self.remote_path = tk.StringVar(value="/home")

        self.drag_source = None
        self.drag_item = None
        self.drag_side = None

        self._build()
        self._refresh_local()
        self._refresh_remote()
        self.center()

    def center(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = self.winfo_screenwidth() // 2 - w // 2
        y = self.winfo_screenheight() // 2 - h // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self):
        hdr = tk.Frame(self, bg=THEME["sidebar"], pady=10, padx=16)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"📁  파일 전송  ←→  {self.server.get('host','')}",
                 bg=THEME["sidebar"], fg=THEME["text"],
                 font=("Malgun Gothic", 11, "bold")).pack(side="left")

        # GVim 경로 설정 버튼 (헤더 우측)
        gvim_frame = tk.Frame(hdr, bg=THEME["sidebar"])
        gvim_frame.pack(side="right")
        tk.Label(gvim_frame, text="gvim:", bg=THEME["sidebar"],
                 fg=THEME["text3"], font=("Consolas", 8)).pack(side="left", padx=(0, 4))
        self.gvim_path_var = tk.StringVar(
            value=self.config.data.get("gvim_path", GVIM_PATH))
        gvim_entry = tk.Entry(
            gvim_frame, textvariable=self.gvim_path_var,
            bg=THEME["input_bg"], fg=THEME["text2"],
            font=("Consolas", 8), relief="flat", width=30,
            insertbackground=THEME["text"],
            highlightthickness=1, highlightbackground=THEME["border"],
        )
        gvim_entry.pack(side="left", padx=(0, 4))
        styled_btn(gvim_frame, "...", command=self._browse_gvim,
                   color=THEME["card"], icon="📂").pack(side="left")

        panes = tk.Frame(self, bg=THEME["bg"])
        panes.pack(fill="both", expand=True, padx=10, pady=10)

        # Left: Local
        left = tk.Frame(panes, bg=THEME["panel"], bd=0)
        left.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self._build_explorer(left, "local")

        # ── 중앙 패널 ───────────────────────────────────────
        mid = tk.Frame(panes, bg=THEME["bg"], width=92)
        mid.pack(side="left", fill="y")
        mid.pack_propagate(False)

        tk.Frame(mid, bg=THEME["bg"]).pack(expand=True)

        # 전송 버튼
        styled_btn(mid, "⬆ 업로드", command=self._upload_selected,
                   color=THEME["accent2"]).pack(pady=3, padx=4, fill="x")
        styled_btn(mid, "⬇ 다운로드", command=self._download_selected,
                   color=THEME["accent"]).pack(pady=3, padx=4, fill="x")

        tk.Frame(mid, bg=THEME["border"], height=1).pack(fill="x", padx=6, pady=8)

        # Diff 모드 레이블
        tk.Label(mid, text="── Diff ──", bg=THEME["bg"], fg=THEME["text3"],
                 font=("Consolas", 7)).pack()

        # gvim -d 버튼
        tk.Button(
            mid, text="gvim -d",
            command=lambda: self._run_diff("gvim"),
            bg="#3d2b00", fg=THEME["warning"],
            relief="flat", bd=0, pady=6, padx=2,
            font=("Consolas", 8, "bold"), cursor="hand2",
            activebackground=THEME["hover"], activeforeground=THEME["warning"],
        ).pack(pady=2, padx=4, fill="x")

        # vimdiff 버튼
        tk.Button(
            mid, text="vimdiff",
            command=lambda: self._run_diff("vimdiff"),
            bg="#002b3d", fg=THEME["accent2"],
            relief="flat", bd=0, pady=6, padx=2,
            font=("Consolas", 8, "bold"), cursor="hand2",
            activebackground=THEME["hover"], activeforeground=THEME["accent2"],
        ).pack(pady=2, padx=4, fill="x")

        # diff (텍스트 출력) 버튼
        tk.Button(
            mid, text="diff",
            command=lambda: self._run_diff("diff"),
            bg="#002b00", fg=THEME["success"],
            relief="flat", bd=0, pady=6, padx=2,
            font=("Consolas", 8, "bold"), cursor="hand2",
            activebackground=THEME["hover"], activeforeground=THEME["success"],
        ).pack(pady=2, padx=4, fill="x")

        tk.Frame(mid, bg=THEME["bg"]).pack(expand=True)

        # Right: Remote
        right = tk.Frame(panes, bg=THEME["panel"], bd=0)
        right.pack(side="right", fill="both", expand=True, padx=(5, 0))
        self._build_explorer(right, "remote")

    def _build_explorer(self, parent, side):
        color = THEME["accent2"] if side == "remote" else THEME["accent3"]
        title = "🌐 원격 서버" if side == "remote" else "💻 로컬"

        # Title bar
        title_bar = tk.Frame(parent, bg=THEME["sidebar"], pady=8, padx=10)
        title_bar.pack(fill="x")
        tk.Label(title_bar, text=title, bg=THEME["sidebar"],
                 fg=color, font=("Malgun Gothic", 10, "bold")).pack(side="left")

        # Saved paths combo + register
        path_row = tk.Frame(parent, bg=THEME["panel"], pady=4, padx=6)
        path_row.pack(fill="x")

        saved = self.config.get_saved_paths(self.server_id, side)
        combo_var = tk.StringVar()
        combo = ttk.Combobox(path_row, textvariable=combo_var, values=saved,
                             width=26, font=("Consolas", 9))
        combo.pack(side="left", padx=(0, 4))

        if side == "local":
            self.local_combo = combo
            self.local_combo_var = combo_var
        else:
            self.remote_combo = combo
            self.remote_combo_var = combo_var

        def on_combo_select(_=None):
            p = combo_var.get()
            if p:
                if side == "local":
                    self.local_path.set(p)
                    self._refresh_local()
                else:
                    self.remote_path.set(p)
                    self._refresh_remote()

        combo.bind("<<ComboboxSelected>>", on_combo_select)

        styled_btn(path_row, "경로등록", command=lambda s=side: self._register_path(s),
                   color=THEME["card"], icon="📌").pack(side="left")

        # Path bar
        nav_row = tk.Frame(parent, bg=THEME["panel"], pady=3, padx=6)
        nav_row.pack(fill="x")
        if side == "local":
            styled_btn(nav_row, "탐색기", command=self._open_explorer,
                       color=THEME["warning"], icon="📂").pack(side="left", padx=(0, 6))
        styled_btn(nav_row, "상위", command=lambda: self._go_up(side),
                   color=THEME["card"], icon="⬆").pack(side="left")

        path_var = self.local_path if side == "local" else self.remote_path
        path_entry = tk.Entry(nav_row, textvariable=path_var,
                              bg=THEME["input_bg"], fg=THEME["text"],
                              font=("Consolas", 9), relief="flat",
                              insertbackground=THEME["text"],
                              highlightthickness=1, highlightbackground=THEME["border"])
        path_entry.pack(side="left", fill="x", expand=True, padx=4)

        def go_enter(_):
            if side == "local":
                self._refresh_local()
            else:
                self._refresh_remote()

        path_entry.bind("<Return>", go_enter)

        # File list
        list_frame = tk.Frame(parent, bg=THEME["border"], padx=1, pady=1)
        list_frame.pack(fill="both", expand=True, padx=6, pady=4)

        cols = ("type", "name", "size", "modified") if side == "remote" else ("type", "name", "size")
        tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="extended")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview",
                        background=THEME["card"], foreground=THEME["text"],
                        rowheight=22, fieldbackground=THEME["card"],
                        borderwidth=0, font=("Consolas", 9))
        style.configure("Treeview.Heading",
                        background=THEME["sidebar"], foreground=THEME["text2"],
                        borderwidth=0, font=("Malgun Gothic", 8, "bold"))
        style.map("Treeview", background=[("selected", THEME["selected"])])

        tree.heading("type", text="")
        tree.heading("name", text="이름")
        tree.heading("size", text="크기")
        tree.column("type", width=24, anchor="center")
        tree.column("name", width=200)
        tree.column("size", width=70, anchor="e")
        if side == "remote":
            tree.heading("modified", text="수정일")
            tree.column("modified", width=130)

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        if side == "local":
            self.local_tree = tree
            tree.bind("<Double-Button-1>", self._local_dbl_click)
            tree.bind("<ButtonPress-1>",   lambda e: self._start_drag("local", e))
            tree.bind("<B1-Motion>",        self._on_drag)
            tree.bind("<ButtonRelease-1>",  lambda e: self._end_drag("local", e))
            tree.bind("<Button-3>",         lambda e: self._show_tree_ctx(e, "local"))
        else:
            self.remote_tree = tree
            tree.bind("<Double-Button-1>", self._remote_dbl_click)
            tree.bind("<ButtonPress-1>",   lambda e: self._start_drag("remote", e))
            tree.bind("<B1-Motion>",        self._on_drag)
            tree.bind("<ButtonRelease-1>",  lambda e: self._end_drag("remote", e))
            tree.bind("<Button-3>",         lambda e: self._show_tree_ctx(e, "remote"))

    # ── Drag & Drop ──────────────────────────
    def _start_drag(self, side, event):
        tree = self.local_tree if side == "local" else self.remote_tree
        item = tree.identify_row(event.y)
        if item:
            self.drag_source = side
            self.drag_item = item
            self.drag_side = side

    def _on_drag(self, event):
        pass  # Visual feedback could be added

    def _end_drag(self, target_side, event):
        if self.drag_item and self.drag_source and self.drag_source != target_side:
            if self.drag_source == "local" and target_side == "remote":
                self._upload_item(self.drag_item)
            elif self.drag_source == "remote" and target_side == "local":
                self._download_item(self.drag_item)
        self.drag_item = None
        self.drag_source = None

    # ── Navigation ───────────────────────────
    def _go_up(self, side):
        if side == "local":
            p = Path(self.local_path.get()).parent
            self.local_path.set(str(p))
            self._refresh_local()
        else:
            parts = self.remote_path.get().rstrip("/").rsplit("/", 1)
            new = parts[0] if parts[0] else "/"
            self.remote_path.set(new)
            self._refresh_remote()

    def _local_dbl_click(self, event):
        item = self.local_tree.identify_row(event.y)
        if not item:
            return
        vals = self.local_tree.item(item, "values")
        if vals[0] == "📁":
            new_path = Path(self.local_path.get()) / vals[1]
            if new_path.is_dir():
                self.local_path.set(str(new_path))
                self._refresh_local()

    def _remote_dbl_click(self, event):
        item = self.remote_tree.identify_row(event.y)
        if not item:
            return
        vals = self.remote_tree.item(item, "values")
        if vals[0] == "📁":
            base = self.remote_path.get().rstrip("/")
            self.remote_path.set(f"{base}/{vals[1]}")
            self._refresh_remote()

    # ── Refresh ──────────────────────────────
    def _refresh_local(self):
        self.local_tree.delete(*self.local_tree.get_children())
        path = Path(self.local_path.get())
        if not path.exists():
            return
        try:
            items = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            for item in items:
                try:
                    icon = "📁" if item.is_dir() else "📄"
                    size = "" if item.is_dir() else self._fmt_size(item.stat().st_size)
                    self.local_tree.insert("", tk.END, values=(icon, item.name, size))
                except PermissionError:
                    pass
        except PermissionError:
            pass

    def _refresh_remote(self):
        self.remote_tree.delete(*self.remote_tree.get_children())
        try:
            path = self.remote_path.get()
            items = self.sftp.listdir_attr(path)
            items.sort(key=lambda x: (not stat.S_ISDIR(x.st_mode), x.filename.lower()))
            for attr in items:
                is_dir = stat.S_ISDIR(attr.st_mode)
                icon = "📁" if is_dir else "📄"
                size = "" if is_dir else self._fmt_size(attr.st_size)
                mtime = datetime.datetime.fromtimestamp(attr.st_mtime).strftime("%Y-%m-%d %H:%M") if attr.st_mtime else ""
                self.remote_tree.insert("", tk.END, values=(icon, attr.filename, size, mtime))
        except Exception as e:
            messagebox.showerror("원격 오류", str(e), parent=self)

    @staticmethod
    def _fmt_size(size):
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.0f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    # ── Transfer ─────────────────────────────
    def _upload_selected(self):
        for item in self.local_tree.selection():
            self._upload_item(item)

    def _download_selected(self):
        for item in self.remote_tree.selection():
            self._download_item(item)

    def _upload_item(self, item):
        vals = self.local_tree.item(item, "values")
        if not vals or vals[0] != "📄":
            return
        local_file  = Path(self.local_path.get()) / vals[1]
        remote_file = self.remote_path.get().rstrip("/") + "/" + vals[1]
        LOG.sftp("upload", str(local_file), remote_file)
        try:
            self.sftp.put(str(local_file), remote_file)
            LOG.info(f"업로드 완료: {vals[1]}")
            messagebox.showinfo("업로드 완료", f"✅ {vals[1]} 업로드 완료", parent=self)
            self._refresh_remote()
        except Exception as e:
            LOG.error(f"업로드 실패: {e}")
            messagebox.showerror("업로드 오류", str(e), parent=self)

    def _download_item(self, item):
        vals = self.remote_tree.item(item, "values")
        if not vals or vals[0] != "📄":
            return
        remote_file = self.remote_path.get().rstrip("/") + "/" + vals[1]
        local_file  = Path(self.local_path.get()) / vals[1]
        LOG.sftp("download", remote_file, str(local_file))
        try:
            self.sftp.get(remote_file, str(local_file))
            LOG.info(f"다운로드 완료: {vals[1]}")
            messagebox.showinfo("다운로드 완료", f"✅ {vals[1]} 다운로드 완료", parent=self)
            self._refresh_local()
        except Exception as e:
            LOG.error(f"다운로드 실패: {e}")
            messagebox.showerror("다운로드 오류", str(e), parent=self)

    def _open_explorer(self):
        path = self.local_path.get()
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer.exe", path])
            elif os.name == "posix":
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("오류", str(e), parent=self)

    def _register_path(self, side):
        path = self.local_path.get() if side == "local" else self.remote_path.get()
        self.config.add_saved_path(self.server_id, side, path)
        saved = self.config.get_saved_paths(self.server_id, side)
        if side == "local":
            self.local_combo["values"] = saved
        else:
            self.remote_combo["values"] = saved
        messagebox.showinfo("등록 완료", f"경로 등록됨:\n{path}", parent=self)

    # ═══════════════════════════════════════════════════════════════════
    #  파일 비교 (Diff)  — gvim -d / vimdiff / diff
    # ═══════════════════════════════════════════════════════════════════

    # ── 우클릭 컨텍스트 메뉴 ─────────────────────────────────────────

    def _show_tree_ctx(self, event, side):
        """트리 우클릭 → 컨텍스트 메뉴"""
        tree = self.local_tree if side == "local" else self.remote_tree
        item = tree.identify_row(event.y)
        if not item:
            return
        # 클릭한 row 선택
        tree.selection_set(item)

        ctx = tk.Menu(self, tearoff=0,
                      bg=THEME["card"], fg=THEME["text"],
                      activebackground=THEME["accent2"],
                      activeforeground="white",
                      font=("Malgun Gothic", 9))

        vals = tree.item(item, "values")
        is_file = (vals[0] == "📄")

        if is_file:
            # ── Diff 서브메뉴
            diff_menu = tk.Menu(ctx, tearoff=0,
                                bg=THEME["card"], fg=THEME["text"],
                                activebackground=THEME["accent2"],
                                activeforeground="white",
                                font=("Malgun Gothic", 9))
            diff_menu.add_command(
                label="gvim -d   (GUI 분할 뷰)",
                command=lambda: self._run_diff("gvim"))
            diff_menu.add_command(
                label="vimdiff   (터미널 분할 뷰)",
                command=lambda: self._run_diff("vimdiff"))
            diff_menu.add_command(
                label="diff      (텍스트 출력 뷰)",
                command=lambda: self._run_diff("diff"))
            ctx.add_cascade(label="◈  파일 비교 (Diff) ▶", menu=diff_menu)
            ctx.add_separator()

            if side == "local":
                ctx.add_command(label="⬆  원격으로 업로드",
                                command=self._upload_selected)
            else:
                ctx.add_command(label="⬇  로컬로 다운로드",
                                command=self._download_selected)
            ctx.add_separator()

        if vals[0] == "📁":
            if side == "local":
                ctx.add_command(label="📂  이 폴더 열기 (탐색기)",
                                command=self._open_explorer)
            ctx.add_command(label="↩  상위 폴더",
                            command=lambda: self._go_up(side))
        ctx.add_command(label="🔄  새로고침",
                        command=self._refresh_local if side == "local" else self._refresh_remote)

        ctx.post(event.x_root, event.y_root)

    # ── gvim 경로 ────────────────────────────────────────────────────

    def _browse_gvim(self):
        path = filedialog.askopenfilename(
            parent=self, title="gvim 실행 파일 선택",
            filetypes=[("실행 파일", "*.exe"), ("모든 파일", "*.*")],
            initialfile="gvim.exe",
        )
        if path:
            self.gvim_path_var.set(path)
            self.config.data["gvim_path"] = path
            self.config.save()

    def _get_gvim_path(self) -> str:
        gvim = self.gvim_path_var.get().strip()
        self.config.data["gvim_path"] = gvim
        self.config.save()
        return gvim

    # ── 선택 파일 추출 ───────────────────────────────────────────────

    def _get_selected_local_files(self) -> list:
        """로컬 트리 선택 파일 목록 (📄 만)"""
        result = []
        for iid in self.local_tree.selection():
            vals = self.local_tree.item(iid, "values")
            if vals[0] == "📄":
                result.append(Path(self.local_path.get()) / vals[1])
        return result

    def _get_selected_remote_files(self) -> list:
        """원격 트리 선택 파일 목록 (📄 만)"""
        result = []
        for iid in self.remote_tree.selection():
            vals = self.remote_tree.item(iid, "values")
            if vals[0] == "📄":
                result.append(vals[1])
        return result

    # ── 원격 파일 임시 다운로드 ──────────────────────────────────────

    def _download_remote_to_tmp(self, remote_fname: str) -> str:
        """원격 파일을 임시 경로에 내려받아 경로 반환"""
        remote_full = self.remote_path.get().rstrip("/") + "/" + remote_fname
        ts = datetime.datetime.now().strftime("%H%M%S%f")[:10]
        suffix = f".{ts}.remote{Path(remote_fname).suffix}"
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=suffix,
            prefix=Path(remote_fname).stem + "_",
        )
        os.close(tmp_fd)
        self.sftp.get(remote_full, tmp_path)
        return tmp_path

    # ── 메인 Diff 진입점 ─────────────────────────────────────────────

    def _run_diff(self, mode: str):
        """
        mode: "gvim" | "vimdiff" | "diff"

        선택 조합 처리:
          로컬 1 + 원격 1  → 원격 파일을 임시 다운로드 후 비교
          로컬 2           → 두 로컬 파일 직접 비교
          원격 2           → 두 원격 파일 임시 다운로드 후 비교
          로컬 1 (원격 미선택) → 원격 동일 파일명 자동 탐색
          원격 1 (로컬 미선택) → 로컬 동일 파일명 자동 탐색
        """
        local_files  = self._get_selected_local_files()
        remote_files = self._get_selected_remote_files()
        n_local  = len(local_files)
        n_remote = len(remote_files)

        # ── 케이스 분기 ──────────────────────────────────────────────

        # 로컬 2개 직접 비교
        if n_local == 2 and n_remote == 0:
            self._exec_diff_local_local(mode,
                str(local_files[0]), str(local_files[1]),
                local_files[0].name, local_files[1].name)
            return

        # 원격 2개 비교 (둘 다 임시 다운로드)
        if n_remote == 2 and n_local == 0:
            try:
                tmp1 = self._download_remote_to_tmp(remote_files[0])
                tmp2 = self._download_remote_to_tmp(remote_files[1])
            except Exception as e:
                messagebox.showerror("다운로드 오류", str(e), parent=self)
                return
            self._exec_diff_two_tmp(mode, tmp1, tmp2,
                f"[remote] {remote_files[0]}", f"[remote] {remote_files[1]}")
            return

        # 로컬 1 + 원격 1
        if n_local == 1 and n_remote == 1:
            try:
                tmp = self._download_remote_to_tmp(remote_files[0])
            except Exception as e:
                messagebox.showerror("다운로드 오류", str(e), parent=self)
                return
            remote_full = self.remote_path.get().rstrip("/") + "/" + remote_files[0]
            self._exec_diff_local_remote(mode,
                str(local_files[0]), tmp,
                str(local_files[0]),
                f"[remote:{self.server.get('host','')}] {remote_full}",
                remote_full, tmp)
            return

        # 로컬 1 → 원격 동일 이름 자동 탐색
        if n_local == 1 and n_remote == 0:
            fname = local_files[0].name
            try:
                names = [a.filename for a in self.sftp.listdir_attr(self.remote_path.get())]
            except Exception as e:
                messagebox.showerror("원격 오류", str(e), parent=self)
                return
            if fname in names:
                try:
                    tmp = self._download_remote_to_tmp(fname)
                except Exception as e:
                    messagebox.showerror("다운로드 오류", str(e), parent=self)
                    return
                remote_full = self.remote_path.get().rstrip("/") + "/" + fname
                self._exec_diff_local_remote(mode,
                    str(local_files[0]), tmp,
                    str(local_files[0]),
                    f"[remote:{self.server.get('host','')}] {remote_full}",
                    remote_full, tmp)
            else:
                messagebox.showwarning("자동 매칭 실패",
                    f"원격 경로에 '{fname}' 이(가) 없습니다.\n"
                    "원격 탐색기에서 비교할 파일을 직접 선택하세요.", parent=self)
            return

        # 원격 1 → 로컬 동일 이름 자동 탐색
        if n_remote == 1 and n_local == 0:
            fname = remote_files[0]
            candidate = Path(self.local_path.get()) / fname
            if candidate.is_file():
                try:
                    tmp = self._download_remote_to_tmp(fname)
                except Exception as e:
                    messagebox.showerror("다운로드 오류", str(e), parent=self)
                    return
                remote_full = self.remote_path.get().rstrip("/") + "/" + fname
                self._exec_diff_local_remote(mode,
                    str(candidate), tmp,
                    str(candidate),
                    f"[remote:{self.server.get('host','')}] {remote_full}",
                    remote_full, tmp)
            else:
                messagebox.showwarning("자동 매칭 실패",
                    f"로컬 경로에 '{fname}' 이(가) 없습니다.\n"
                    "로컬 탐색기에서 비교할 파일을 직접 선택하세요.", parent=self)
            return

        # 그 외 → 안내
        messagebox.showinfo(
            "파일 비교 사용법",
            "◈  파일 비교 사용법\n\n"
            "  ① 로컬 파일 1개 + 원격 파일 1개 선택\n"
            "  ② 로컬 파일 2개 선택 (로컬↔로컬 비교)\n"
            "  ③ 원격 파일 2개 선택 (원격↔원격 비교)\n"
            "  ④ 한 쪽만 선택 시 반대편에서 같은 이름 자동 탐색\n\n"
            "파일을 선택한 뒤 다시 시도하세요.",
            parent=self)

    # ── Diff 실행 엔진 ───────────────────────────────────────────────

    def _exec_diff_local_local(self, mode: str,
                                path_a: str, path_b: str,
                                label_a: str, label_b: str):
        """로컬 파일 2개 직접 비교"""
        self._exec_diff_core(mode, path_a, path_b, label_a, label_b,
                             remote_full=None, tmp_path=None)

    def _exec_diff_two_tmp(self, mode: str,
                            tmp_a: str, tmp_b: str,
                            label_a: str, label_b: str):
        """임시 파일 2개 비교 (원격↔원격)"""
        self._exec_diff_core(mode, tmp_a, tmp_b, label_a, label_b,
                             remote_full=None, tmp_path=tmp_a,
                             extra_tmps=[tmp_a, tmp_b])

    def _exec_diff_local_remote(self, mode: str,
                                 path_local: str, tmp_remote: str,
                                 label_local: str, label_remote: str,
                                 remote_full: str, tmp_path: str):
        """로컬 + 원격 임시파일 비교"""
        self._exec_diff_core(mode, path_local, tmp_remote,
                             label_local, label_remote,
                             remote_full=remote_full, tmp_path=tmp_path)

    def _exec_diff_core(self, mode: str,
                         file_a: str, file_b: str,
                         label_a: str, label_b: str,
                         remote_full=None, tmp_path=None,
                         extra_tmps=None):
        """
        실제 diff 실행 공통 루틴

        mode:
          "gvim"    → gvim.exe -d <file_a> <file_b>
          "vimdiff" → vimdiff <file_a> <file_b>   (WSL / Cygwin 경유)
          "diff"    → diff <file_a> <file_b>      → 결과 팝업
        """
        LOG.diff(mode, label_a, label_b)

        if mode == "gvim":
            gvim = self._get_gvim_path()
            if not gvim:
                messagebox.showerror("오류", "gvim 경로를 설정하세요.", parent=self)
                return
            if not os.path.isfile(gvim):
                messagebox.showerror("오류",
                    f"gvim 을 찾을 수 없습니다:\n{gvim}", parent=self)
                return
            cmd = [gvim, "-d", "--", file_a, file_b]
            LOG.cmd(cmd, category="GVIM")
            try:
                subprocess.Popen(cmd)
            except Exception as e:
                messagebox.showerror("gvim 오류", str(e), parent=self)
                return
            self._show_diff_result_info(mode, label_a, label_b,
                                        remote_full, tmp_path, extra_tmps)

        elif mode == "vimdiff":
            # vimdiff: Cygwin 또는 WSL/bash 경유로 실행
            # Windows에서 Cygwin 경유
            cygwin_bash = r"C:\Util\cygwin\cygwin64\bin\bash.exe"
            # cygwin path 변환 함수
            def to_cygpath(p):
                # C:\foo\bar → /cygdrive/c/foo/bar
                p = p.replace("\\", "/")
                if len(p) >= 2 and p[1] == ":":
                    p = f"/cygdrive/{p[0].lower()}/{p[2:].lstrip('/')}"
                return p

            a_cyg = to_cygpath(file_a)
            b_cyg = to_cygpath(file_b)
            vimdiff_cmd = f"vimdiff '{a_cyg}' '{b_cyg}'"

            tried = []
            launched = False

            # 1) Cygwin bash
            if os.path.isfile(cygwin_bash):
                try:
                    LOG.cmd([cygwin_bash, "--login", "-c", vimdiff_cmd], category="VIMDIFF",
                            note="경유: Cygwin bash")
                    subprocess.Popen([cygwin_bash, "--login", "-c", vimdiff_cmd])
                    launched = True
                except Exception as e:
                    tried.append(f"Cygwin: {e}")

            # 2) WSL bash
            if not launched:
                try:
                    a_wsl = "/mnt/" + file_a.replace("\\","/").replace(":","").lower().lstrip("/")
                    b_wsl = "/mnt/" + file_b.replace("\\","/").replace(":","").lower().lstrip("/")
                    LOG.cmd(["wsl", "vimdiff", a_wsl, b_wsl], category="VIMDIFF",
                            note="경유: WSL")
                    subprocess.Popen(["wsl", "vimdiff", a_wsl, b_wsl])
                    launched = True
                except Exception as e:
                    tried.append(f"WSL: {e}")

            # 3) 직접 vimdiff (PATH에 있는 경우)
            if not launched:
                try:
                    LOG.cmd(["vimdiff", file_a, file_b], category="VIMDIFF",
                            note="직접 실행")
                    subprocess.Popen(["vimdiff", file_a, file_b])
                    launched = True
                except Exception as e:
                    tried.append(f"직접: {e}")

            if not launched:
                messagebox.showerror("vimdiff 오류",
                    "vimdiff 를 실행할 수 없습니다.\n\n" +
                    "\n".join(tried) +
                    "\n\nCygwin 경로를 확인하거나 gvim -d 를 사용하세요.",
                    parent=self)
                return
            self._show_diff_result_info(mode, label_a, label_b,
                                        remote_full, tmp_path, extra_tmps)

        elif mode == "diff":
            diff_cmd = ["diff", "-u", "--label", label_a, "--label", label_b,
                        file_a, file_b]
            LOG.cmd(diff_cmd, category="DIFF")
            try:
                result = subprocess.run(
                    diff_cmd,
                    capture_output=True, text=True, timeout=30,
                )
                output     = result.stdout or result.stderr or "(차이 없음)"
                returncode = result.returncode
                LOG.info(f"diff 종료 코드: {returncode}  "
                         f"({'차이 있음' if returncode != 0 else '동일'})")
            except FileNotFoundError:
                LOG.warn("diff 명령 없음 → Python difflib 사용")
                import difflib
                try:
                    with open(file_a, encoding="utf-8", errors="replace") as f:
                        lines_a = f.readlines()
                    with open(file_b, encoding="utf-8", errors="replace") as f:
                        lines_b = f.readlines()
                    diff_lines = list(difflib.unified_diff(
                        lines_a, lines_b,
                        fromfile=label_a, tofile=label_b, lineterm=""))
                    output = "".join(diff_lines) if diff_lines else "(차이 없음)"
                    returncode = 1 if diff_lines else 0
                except Exception as e:
                    output = f"오류: {e}"
                    returncode = -1
            except Exception as e:
                output = f"오류: {e}"
                returncode = -1

            self._show_diff_text_popup(label_a, label_b, output, returncode,
                                       tmp_path, extra_tmps)

    # ── 결과 팝업 ─────────────────────────────────────────────────────

    def _show_diff_result_info(self, mode: str,
                               label_a: str, label_b: str,
                               remote_full=None, tmp_path=None,
                               extra_tmps=None):
        """gvim -d / vimdiff 실행 후 간단 정보 창"""
        win = tk.Toplevel(self)
        mode_labels = {
            "gvim":    ("gvim -d", THEME["warning"]),
            "vimdiff": ("vimdiff", THEME["accent2"]),
        }
        m_label, m_color = mode_labels.get(mode, (mode, THEME["text"]))

        win.title(f"◈ {m_label} 실행됨")
        win.configure(bg=THEME["panel"])
        win.resizable(False, False)
        win.transient(self)
        win.geometry("560x230")
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width()  - 560) // 2
        y = self.winfo_rooty() + (self.winfo_height() - 230) // 2
        win.geometry(f"560x230+{x}+{y}")

        hdr = tk.Frame(win, bg=m_color, pady=8, padx=14)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"◈  {m_label}  실행됨",
                 bg=m_color, fg="white",
                 font=("Consolas", 11, "bold")).pack(anchor="w")

        body = tk.Frame(win, bg=THEME["panel"], padx=14, pady=10)
        body.pack(fill="both", expand=True)

        def row(lbl, val, color=THEME["text2"]):
            f = tk.Frame(body, bg=THEME["panel"])
            f.pack(fill="x", pady=2)
            tk.Label(f, text=lbl, bg=THEME["panel"], fg=THEME["text3"],
                     font=("Malgun Gothic", 8), width=8, anchor="e").pack(side="left", padx=(0,6))
            tk.Label(f, text=val, bg=THEME["panel"], fg=color,
                     font=("Consolas", 9), anchor="w", wraplength=440,
                     justify="left").pack(side="left")

        row("파일 A", label_a, THEME["accent3"])
        row("파일 B", label_b, THEME["accent2"])
        if tmp_path:
            row("임시파일", tmp_path, THEME["text3"])

        tk.Label(body, text="※ 에디터 창을 닫아도 임시 파일은 자동 삭제되지 않습니다.",
                 bg=THEME["panel"], fg=THEME["text3"],
                 font=("Malgun Gothic", 8)).pack(anchor="w", pady=(6,0))

        btn_row = tk.Frame(win, bg=THEME["panel"], pady=8)
        btn_row.pack()
        styled_btn(btn_row, "닫기", win.destroy,
                   color=THEME["card"], icon="✖").pack(side="left", padx=4)

        all_tmps = extra_tmps or ([tmp_path] if tmp_path else [])
        if all_tmps:
            styled_btn(btn_row, "임시파일 삭제",
                       command=lambda: self._delete_tmps(all_tmps, win),
                       color=THEME["danger"], icon="🗑️").pack(side="left", padx=4)

    def _show_diff_text_popup(self, label_a: str, label_b: str,
                               output: str, returncode: int,
                               tmp_path=None, extra_tmps=None):
        """diff 텍스트 결과를 컬러 하이라이트로 표시하는 팝업"""
        win = tk.Toplevel(self)
        win.title("◈ diff 결과")
        win.configure(bg=THEME["panel"])
        win.resizable(True, True)
        win.transient(self)
        win.geometry("860x560")
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width()  - 860) // 2
        y = self.winfo_rooty() + (self.winfo_height() - 560) // 2
        win.geometry(f"860x560+{x}+{y}")

        # 헤더
        has_diff = returncode != 0
        hdr_color = THEME["danger"] if has_diff else THEME["success"]
        hdr_text  = f"◈ diff  —  {'차이 있음' if has_diff else '동일한 파일'}"
        hdr = tk.Frame(win, bg=hdr_color, pady=8, padx=14)
        hdr.pack(fill="x")
        tk.Label(hdr, text=hdr_text, bg=hdr_color, fg="white",
                 font=("Consolas", 11, "bold")).pack(side="left")
        tk.Label(hdr, text=f"  return code: {returncode}",
                 bg=hdr_color, fg="white",
                 font=("Consolas", 9)).pack(side="left", padx=8)

        # 파일 정보 행
        info = tk.Frame(win, bg=THEME["card"], pady=6, padx=14)
        info.pack(fill="x")
        tk.Label(info, text=f"A: {label_a}",
                 bg=THEME["card"], fg=THEME["accent3"],
                 font=("Consolas", 8)).pack(anchor="w")
        tk.Label(info, text=f"B: {label_b}",
                 bg=THEME["card"], fg=THEME["accent2"],
                 font=("Consolas", 8)).pack(anchor="w")

        # 툴바
        tb = tk.Frame(win, bg=THEME["panel"], pady=4, padx=8)
        tb.pack(fill="x")

        # 검색
        tk.Label(tb, text="🔍", bg=THEME["panel"], fg=THEME["text2"],
                 font=("Malgun Gothic", 9)).pack(side="left")
        search_var = tk.StringVar()
        search_entry = styled_entry(tb, textvariable=search_var, width=22)
        search_entry.pack(side="left", padx=4)

        # 행 번호 토글
        show_lineno = tk.BooleanVar(value=True)
        tk.Checkbutton(tb, text="행 번호", variable=show_lineno,
                       bg=THEME["panel"], fg=THEME["text2"],
                       selectcolor=THEME["card"],
                       activebackground=THEME["panel"],
                       font=("Malgun Gothic", 8)).pack(side="left", padx=6)

        # 복사 버튼
        def copy_all():
            win.clipboard_clear()
            win.clipboard_append(output)
        styled_btn(tb, "전체 복사", copy_all,
                   color=THEME["card"], icon="📋").pack(side="left", padx=4)

        # 저장 버튼
        def save_diff():
            fp = filedialog.asksaveasfilename(
                parent=win, title="diff 결과 저장",
                defaultextension=".diff",
                filetypes=[("Diff 파일", "*.diff *.patch"), ("텍스트", "*.txt")],
            )
            if fp:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(output)
                messagebox.showinfo("저장 완료", f"저장됨:\n{fp}", parent=win)
        styled_btn(tb, "저장", save_diff,
                   color=THEME["card"], icon="💾").pack(side="left", padx=4)

        # ── 텍스트 위젯 (하이라이트) ─────────────────────────────────
        txt_frame = tk.Frame(win, bg=THEME["bg"])
        txt_frame.pack(fill="both", expand=True, padx=6, pady=(0,4))

        txt = tk.Text(
            txt_frame,
            bg=THEME["bg"], fg=THEME["text"],
            font=("Consolas", 9), wrap="none",
            relief="flat", bd=0,
            insertbackground=THEME["text"],
            selectbackground=THEME["selected"],
            state="normal",
        )
        vsb = ttk.Scrollbar(txt_frame, orient="vertical",   command=txt.yview)
        hsb = ttk.Scrollbar(txt_frame, orient="horizontal",  command=txt.xview)
        txt.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        txt.pack(side="left",   fill="both", expand=True)

        # 하이라이트 태그
        txt.tag_configure("add",    foreground="#3fb950", background="#0d2b0d")
        txt.tag_configure("del",    foreground="#f85149", background="#2b0d0d")
        txt.tag_configure("hunk",   foreground="#79c0ff", background="#001a33")
        txt.tag_configure("header", foreground="#e3b341", background="#1a1400")
        txt.tag_configure("same",   foreground=THEME["text3"])
        txt.tag_configure("search_hl", background="#6e3c1e", foreground="white")

        def insert_diff(text_widget, diff_text):
            text_widget.config(state="normal")
            text_widget.delete("1.0", tk.END)
            for lineno, line in enumerate(diff_text.splitlines(keepends=True), 1):
                prefix = line[:1] if line else ""
                lno_str = f"{lineno:4d}│ " if show_lineno.get() else ""
                if prefix == "+":
                    text_widget.insert(tk.END, lno_str + line, "add")
                elif prefix == "-":
                    text_widget.insert(tk.END, lno_str + line, "del")
                elif prefix == "@":
                    text_widget.insert(tk.END, lno_str + line, "hunk")
                elif line.startswith("---") or line.startswith("+++"):
                    text_widget.insert(tk.END, lno_str + line, "header")
                else:
                    text_widget.insert(tk.END, lno_str + line, "same")
            text_widget.config(state="disabled")

        insert_diff(txt, output)

        def on_lineno_toggle():
            insert_diff(txt, output)
        show_lineno.trace_add("write", lambda *_: on_lineno_toggle())

        # 검색 기능
        def do_search(*_):
            txt.tag_remove("search_hl", "1.0", tk.END)
            q = search_var.get()
            if not q:
                return
            start = "1.0"
            while True:
                pos = txt.search(q, start, stopindex=tk.END, nocase=True)
                if not pos:
                    break
                end = f"{pos}+{len(q)}c"
                txt.tag_add("search_hl", pos, end)
                start = end
        search_var.trace_add("write", do_search)
        search_entry.bind("<Return>", do_search)

        # 하단 버튼
        btn_row = tk.Frame(win, bg=THEME["panel"], pady=6)
        btn_row.pack()
        styled_btn(btn_row, "닫기", win.destroy,
                   color=THEME["card"], icon="✖").pack(side="left", padx=4)

        all_tmps = extra_tmps or ([tmp_path] if tmp_path else [])
        if all_tmps:
            styled_btn(btn_row, "임시파일 삭제",
                       command=lambda: self._delete_tmps(all_tmps, win),
                       color=THEME["danger"], icon="🗑️").pack(side="left", padx=4)

        # 통계
        adds = sum(1 for l in output.splitlines() if l.startswith("+") and not l.startswith("+++"))
        dels = sum(1 for l in output.splitlines() if l.startswith("-") and not l.startswith("---"))
        tk.Label(btn_row,
                 text=f"  +{adds} 추가  / -{dels} 삭제",
                 bg=THEME["panel"], fg=THEME["text3"],
                 font=("Consolas", 9)).pack(side="left", padx=8)

    # ── 임시파일 정리 ─────────────────────────────────────────────────

    def _delete_tmps(self, tmp_paths: list, parent_win=None):
        errors = []
        for p in tmp_paths:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception as e:
                errors.append(str(e))
        if parent_win:
            parent_win.destroy()
        if errors:
            messagebox.showerror("삭제 오류", "\n".join(errors), parent=self)
        else:
            messagebox.showinfo("삭제 완료",
                f"{len(tmp_paths)}개 임시 파일 삭제 완료", parent=self)

    def _delete_tmp(self, tmp_path: str, parent_win=None):
        self._delete_tmps([tmp_path], parent_win)


# ─────────────────────────────────────────────
# Main Application
# ─────────────────────────────────────────────
class CLIManagerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.config_mgr = ConfigManager()
        self.title(APP_NAME)
        self.configure(bg=THEME["bg"])
        self.geometry(self.config_mgr.data.get("window_geometry", "1280x800"))
        self.minsize(960, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Active SSH connections: {server_id: (ssh, sftp)}
        self.ssh_sessions = {}

        LOG.info(f"앱 시작  config={CONFIG_FILE}")
        LOG.info(f"등록 서버 {len(self.config_mgr.get_servers())}개  "
                 f"/ 커맨드 {len(self.config_mgr.get_commands())}개")
        LOG.sep()

        self._apply_styles()
        self._build_ui()

    def _apply_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TCombobox",
                        fieldbackground=THEME["input_bg"],
                        background=THEME["card"],
                        foreground=THEME["text"],
                        selectbackground=THEME["accent2"],
                        arrowcolor=THEME["text2"],
                        borderwidth=0)
        style.map("TCombobox",
                  fieldbackground=[("readonly", THEME["input_bg"])],
                  background=[("active", THEME["hover"])])

    def _build_ui(self):
        # ── Sidebar ───────────────────────────
        self.sidebar = tk.Frame(self, bg=THEME["sidebar"], width=200)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        # Logo
        logo = tk.Frame(self.sidebar, bg=THEME["sidebar"], pady=16, padx=14)
        logo.pack(fill="x")
        tk.Label(logo, text="⚡ CLI Manager", bg=THEME["sidebar"],
                 fg=THEME["text"], font=("Malgun Gothic", 13, "bold")).pack(anchor="w")
        tk.Label(logo, text="v2.0", bg=THEME["sidebar"],
                 fg=THEME["text3"], font=("Consolas", 8)).pack(anchor="w")

        tk.Frame(self.sidebar, bg=THEME["border"], height=1).pack(fill="x", pady=2)

        # Navigation items
        self.nav_items = []
        nav_data = [
            ("🏠", "홈", self._show_home),
            ("🖥️", "서버 관리", self._show_servers),
            ("⚙️", "커맨드 설정", self._show_cmd_settings),
            ("📋", "세션 로그", self._show_logs),
        ]
        for icon, label, cmd in nav_data:
            btn = self._nav_button(icon, label, cmd)
            self.nav_items.append(btn)

        tk.Frame(self.sidebar, bg=THEME["sidebar"]).pack(expand=True)
        tk.Frame(self.sidebar, bg=THEME["border"], height=1).pack(fill="x")

        # Status indicator
        self.status_label = tk.Label(
            self.sidebar, text="● 준비", bg=THEME["sidebar"],
            fg=THEME["success"], font=("Malgun Gothic", 8), pady=8,
        )
        self.status_label.pack()

        # ── Main content area ─────────────────
        self.content = tk.Frame(self, bg=THEME["bg"])
        self.content.pack(side="right", fill="both", expand=True)

        self._show_home()

    def _nav_button(self, icon, label, cmd):
        btn = tk.Button(
            self.sidebar, text=f"  {icon}  {label}",
            command=cmd, bg=THEME["sidebar"], fg=THEME["text2"],
            relief="flat", bd=0, pady=10, padx=14, anchor="w",
            font=("Malgun Gothic", 10), cursor="hand2",
            activebackground=THEME["hover"], activeforeground=THEME["text"],
            width=22,
        )
        btn.pack(fill="x")

        def on_enter(_):
            btn.configure(bg=THEME["hover"], fg=THEME["text"])

        def on_leave(_):
            if btn.cget("bg") != THEME["accent2"]:
                btn.configure(bg=THEME["sidebar"], fg=THEME["text2"])

        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        return btn

    def _clear_content(self):
        for w in self.content.winfo_children():
            w.destroy()

    # ── HOME ─────────────────────────────────
    def _show_home(self):
        self._clear_content()

        # Top bar
        top = tk.Frame(self.content, bg=THEME["panel"], pady=12, padx=20)
        top.pack(fill="x")
        tk.Label(top, text="🏠  홈", bg=THEME["panel"],
                 fg=THEME["text"], font=("Malgun Gothic", 14, "bold")).pack(side="left")

        # Command selector row
        cmd_row = tk.Frame(self.content, bg=THEME["bg"], pady=10, padx=16)
        cmd_row.pack(fill="x")
        tk.Label(cmd_row, text="CLI 실행 파일:", bg=THEME["bg"],
                 fg=THEME["text2"], font=("Malgun Gothic", 9, "bold")).pack(side="left", padx=(0, 8))

        cmds = self.config_mgr.get_commands()
        cmd_names = [f"{c.get('icon','')} {c.get('name','')}" for c in cmds]
        self.cmd_var = tk.StringVar(value=cmd_names[0] if cmd_names else "")
        self.cmd_combo = ttk.Combobox(cmd_row, textvariable=self.cmd_var,
                                      values=cmd_names, state="readonly",
                                      width=30, font=("Malgun Gothic", 9))
        self.cmd_combo.pack(side="left", padx=(0, 10))

        styled_btn(cmd_row, "커맨드 설정", command=self._show_cmd_settings,
                   color=THEME["accent3"], icon="⚙️").pack(side="left", padx=4)
        styled_btn(cmd_row, "새로고침", command=self._show_home,
                   color=THEME["card"], icon="🔄").pack(side="left", padx=4)

        # Server grid
        grid_frame = tk.Frame(self.content, bg=THEME["bg"])
        grid_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        tk.Label(grid_frame, text="원격 서버 목록 — 더블 클릭으로 접속",
                 bg=THEME["bg"], fg=THEME["text2"],
                 font=("Malgun Gothic", 9)).pack(anchor="w", pady=(0, 6))

        # Treeview
        tree_frame = tk.Frame(grid_frame, bg=THEME["border"], padx=1, pady=1)
        tree_frame.pack(fill="both", expand=True)

        cols = ("icon", "name", "host", "port", "description", "status")
        self.server_tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                        selectmode="browse")

        self.server_tree.heading("icon", text="")
        self.server_tree.heading("name", text="이름")
        self.server_tree.heading("host", text="호스트")
        self.server_tree.heading("port", text="포트")
        self.server_tree.heading("description", text="설명")
        self.server_tree.heading("status", text="상태")

        self.server_tree.column("icon", width=32, anchor="center")
        self.server_tree.column("name", width=160)
        self.server_tree.column("host", width=180)
        self.server_tree.column("port", width=60, anchor="center")
        self.server_tree.column("description", width=200)
        self.server_tree.column("status", width=100, anchor="center")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.server_tree.yview)
        self.server_tree.configure(yscrollcommand=vsb.set)
        self.server_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.server_tree.bind("<Double-Button-1>", self._on_server_dbl_click)

        # Context menu
        self.ctx_menu = tk.Menu(self, tearoff=0, bg=THEME["card"], fg=THEME["text"],
                                 activebackground=THEME["accent2"])
        self.ctx_menu.add_command(label="🔐 SSH 접속", command=self._connect_selected)
        self.ctx_menu.add_command(label="📁 파일 전송", command=self._open_filetransfer)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label="✏️ 편집", command=lambda: self._show_servers(edit=True))
        self.ctx_menu.add_command(label="🗑️ 삭제", command=self._delete_server)
        self.server_tree.bind("<Button-3>", self._show_ctx_menu)

        self._load_server_grid()

        # Bottom action bar
        act = tk.Frame(self.content, bg=THEME["panel"], pady=8, padx=16)
        act.pack(fill="x", side="bottom")
        styled_btn(act, "서버 추가", command=lambda: ServerDialog(self, self.config_mgr),
                   color=THEME["accent"], icon="➕").pack(side="left", padx=4)
        styled_btn(act, "파일 전송", command=self._open_filetransfer,
                   color=THEME["accent2"], icon="📁").pack(side="left", padx=4)
        styled_btn(act, "연결 끊기", command=self._disconnect_selected,
                   color=THEME["danger"], icon="✖").pack(side="right", padx=4)

    def _load_server_grid(self):
        self.server_tree.delete(*self.server_tree.get_children())
        for srv in self.config_mgr.get_servers():
            sid = srv.get("id", "")
            connected = sid in self.ssh_sessions
            status = "● 연결됨" if connected else "○ 미연결"
            status_tag = "connected" if connected else "disconnected"
            iid = self.server_tree.insert(
                "", tk.END,
                values=("🖥️", srv.get("name", ""), srv.get("host", ""),
                        srv.get("port", 22), srv.get("description", ""), status),
                tags=(status_tag,),
            )
        self.server_tree.tag_configure("connected", foreground=THEME["success"])
        self.server_tree.tag_configure("disconnected", foreground=THEME["text"])

    def _show_ctx_menu(self, event):
        item = self.server_tree.identify_row(event.y)
        if item:
            self.server_tree.selection_set(item)
            self.ctx_menu.post(event.x_root, event.y_root)

    def _get_selected_server(self):
        sel = self.server_tree.selection()
        if not sel:
            return None, None
        idx = self.server_tree.index(sel[0])
        servers = self.config_mgr.get_servers()
        if idx >= len(servers):
            return None, None
        return servers[idx], idx

    def _on_server_dbl_click(self, event):
        item = self.server_tree.identify_row(event.y)
        if item:
            self._connect_selected()

    def _connect_selected(self):
        server, idx = self._get_selected_server()
        if not server:
            messagebox.showwarning("선택 없음", "서버를 선택하세요.", parent=self)
            return

        # Check if already connected via SSH
        sid = server.get("id", "")
        if sid in self.ssh_sessions:
            messagebox.showinfo("이미 연결됨",
                f"{server['name']} 은 이미 SSH 연결되어 있습니다.", parent=self)
            return

        # Get selected CLI command (홈 화면 콤보 기준)
        cmds    = self.config_mgr.get_commands()
        cli_idx = 0
        if hasattr(self, "cmd_combo"):
            cli_idx = self.cmd_combo.current()
        if cli_idx < 0:
            cli_idx = 0
        cli = cmds[cli_idx] if cmds else None

        # Show login dialog
        dlg = LoginDialog(self, server, self.config_mgr)
        self.wait_window(dlg)
        if not dlg.result:
            return

        creds        = dlg.result
        username     = creds["username"]
        password     = creds["password"]
        auth_method  = creds.get("auth_method", "password")
        key_path     = creds.get("key_path", "")
        key_passphrase = creds.get("key_passphrase", "")

        # Save credentials if requested
        if creds["save"]:
            server["username"] = username
            if auth_method == "password":
                server["password_enc"] = self.config_mgr.encrypt_password(password)
            else:
                if key_passphrase:
                    server["key_passphrase_enc"] = self.config_mgr.encrypt_password(key_passphrase)
            self.config_mgr.update_server(idx, server)

        # Launch CLI with auto-login
        self._launch_cli(cli, server, username, password, key_path, key_passphrase, auth_method)

        # Also connect SSH for file transfer
        self._connect_ssh(server, username, password, key_path, key_passphrase, auth_method)
        self._load_server_grid()

    def _launch_cli(self, cli: dict, server: dict, username: str, password: str,
                    key_path: str = "", key_passphrase: str = "", auth_method: str = "password"):
        if not cli:
            return
        host     = server.get("host", "")
        port     = server.get("port", 22)
        path     = cli.get("path", "powershell.exe")
        init_dir = server.get("init_dir", "").strip()

        is_key_auth = auth_method in ("ppk", "pem") and key_path

        def build_ssh_cmd(with_cd=True):
            parts = ["ssh"]
            if is_key_auth:
                parts += ["-i", key_path]
            parts += ["-p", str(port), f"{username}@{host}"]
            if init_dir and with_cd:
                parts += ["-t", f"cd {init_dir} && exec $SHELL"]
            return parts

        LOG.sep(f"CLI 실행  —  {server.get('name','')}  ({host})")

        try:
            if "putty" in path.lower():
                cmd = [path, "-ssh", f"{username}@{host}", "-P", str(port)]
                if is_key_auth and auth_method == "ppk":
                    cmd += ["-i", key_path]
                    if key_passphrase:
                        cmd += ["-pw", "****"]          # 패스프레이즈 마스킹
                elif not is_key_auth:
                    cmd += ["-pw", "****"]               # 비밀번호 마스킹
                if init_dir:
                    cmd += ["-m", "-"]
                LOG.cmd(cmd, category="PUTTY",
                        note=f"auth={auth_method}  init_dir={init_dir or '(기본)'}")
                # 실제 실행 시 마스킹 대신 실제 값 사용
                real_cmd = [c if c != "****" else
                            (key_passphrase if is_key_auth else password)
                            for c in cmd]
                subprocess.Popen(real_cmd)

            elif "cygwin" in path.lower():
                ssh_parts = build_ssh_cmd(with_cd=bool(init_dir))
                if is_key_auth:
                    ssh_cli  = " ".join(f'"{p}"' if " " in p else p for p in ssh_parts)
                    bash_cmd = f'{ssh_cli}; bash'
                else:
                    ssh_no_cd     = ["ssh", "-p", str(port), f"{username}@{host}"]
                    ssh_no_cd_str = " ".join(ssh_no_cd)
                    if init_dir:
                        bash_cmd = (f"sshpass -p '****' {ssh_no_cd_str}"
                                    f" -t 'cd {init_dir} && exec $SHELL'; bash")
                    else:
                        bash_cmd = f"sshpass -p '****' {ssh_no_cd_str}; bash"
                cmd = [path, "--", "bash", "-c", bash_cmd]
                LOG.cmd(cmd, category="CYGWIN",
                        note=f"auth={auth_method}  init_dir={init_dir or '(기본)'}")
                # 실제 실행
                real_bash = bash_cmd.replace("'****'", f"'{password}'")
                subprocess.Popen([path, "--", "bash", "-c", real_bash])

            elif "powershell" in path.lower() or path.lower() == "powershell.exe":
                ssh_parts = build_ssh_cmd(with_cd=bool(init_dir))
                ssh_str   = " ".join(f'"{p}"' if " " in p else p for p in ssh_parts)
                cmd       = ["powershell.exe", "-NoExit", "-Command", ssh_str]
                LOG.cmd(cmd, category="PS",
                        note=f"auth={auth_method}  init_dir={init_dir or '(기본)'}")
                subprocess.Popen(cmd)

            elif path.lower() in ("cmd.exe", "cmd"):
                ssh_parts = build_ssh_cmd(with_cd=bool(init_dir))
                ssh_str   = " ".join(f'"{p}"' if " " in p else p for p in ssh_parts)
                cmd       = ["cmd.exe", "/k", ssh_str]
                LOG.cmd(cmd, category="CMD",
                        note=f"auth={auth_method}  init_dir={init_dir or '(기본)'}")
                subprocess.Popen(cmd)

            else:
                cmd = [path]
                if cli.get("args"):
                    cmd += cli["args"].split()
                LOG.cmd(cmd, category="CLI",
                        note=f"generic terminal  auth={auth_method}")
                subprocess.Popen(cmd)

        except FileNotFoundError:
            LOG.error(f"실행 파일 없음: {path}")
            messagebox.showerror("오류",
                f"실행 파일을 찾을 수 없습니다:\n{path}\n\n커맨드 설정에서 경로를 확인하세요.",
                parent=self)
        except Exception as e:
            LOG.error(f"_launch_cli 예외: {e}")
            messagebox.showerror("실행 오류", str(e), parent=self)

    def _connect_ssh(self, server: dict, username: str, password: str,
                     key_path: str = "", key_passphrase: str = "", auth_method: str = "password"):
        sid      = server.get("id", "")
        host     = server.get("host", "")
        port     = server.get("port", 22)
        init_dir = server.get("init_dir", "").strip()

        LOG.ssh(host, port, username, auth=auth_method, key_path=key_path)
        if init_dir:
            LOG.info(f"SSH 연결 후 init_dir 이동: {init_dir}")

        def connect():
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

                if auth_method in ("ppk", "pem") and key_path:
                    key_pw = key_passphrase if key_passphrase else None
                    try:
                        pkey = None
                        for key_cls in (paramiko.RSAKey, paramiko.DSSKey,
                                        paramiko.ECDSAKey, paramiko.Ed25519Key):
                            try:
                                pkey = key_cls.from_private_key_file(key_path, password=key_pw)
                                break
                            except Exception:
                                continue
                        if pkey is None:
                            if auth_method == "ppk":
                                raise ValueError("PPK 파일을 읽을 수 없습니다. 형식을 확인하세요.")
                            else:
                                raise ValueError("PEM 파일을 읽을 수 없습니다. 형식이나 패스프레이즈를 확인하세요.")
                        LOG.info(f"키 파일 로드 성공: {key_cls.__name__}  {key_path}")
                        ssh.connect(host, port=port, username=username, pkey=pkey, timeout=10)
                    except paramiko.AuthenticationException:
                        raise Exception("키 파일 인증 실패. 사용자명이나 키 파일을 확인하세요.")
                else:
                    LOG.info(f"paramiko.connect({host}, port={port}, username={username}, password=****)")
                    ssh.connect(host, port=port, username=username, password=password, timeout=10)

                sftp = ssh.open_sftp()
                LOG.info(f"SFTP 세션 열림  →  {host}")

                if init_dir:
                    try:
                        sftp.chdir(init_dir)
                        LOG.info(f"SFTP chdir: {init_dir}")
                    except Exception as e:
                        LOG.warn(f"SFTP chdir 실패 ({init_dir}): {e}")

                self.ssh_sessions[sid] = (ssh, sftp)
                key_info = f" [{auth_method.upper()} 키]" if auth_method in ("ppk", "pem") else ""
                LOG.info(f"SSH 연결 완료: {server['name']}{key_info}")
                self.status_label.config(
                    text=f"● {server['name']} 연결됨{key_info}", fg=THEME["success"])
                self.after(0, self._load_server_grid)

            except Exception as e:
                LOG.error(f"SSH 연결 실패 ({host}:{port}): {e}")
                self.after(0, lambda: messagebox.showerror(
                    "SSH 연결 오류", f"{host}: {e}", parent=self))

        threading.Thread(target=connect, daemon=True).start()

    def _disconnect_selected(self):
        server, _ = self._get_selected_server()
        if not server:
            return
        sid = server.get("id", "")
        if sid in self.ssh_sessions:
            ssh, sftp = self.ssh_sessions.pop(sid)
            try:
                sftp.close()
                ssh.close()
            except Exception:
                pass
            self._load_server_grid()
            self.status_label.config(text="● 준비", fg=THEME["success"])

    def _open_filetransfer(self):
        server, _ = self._get_selected_server()
        if not server:
            messagebox.showwarning("선택 없음", "서버를 선택하세요.", parent=self)
            return
        sid = server.get("id", "")
        if sid not in self.ssh_sessions:
            messagebox.showwarning("미연결", "먼저 서버에 SSH 접속하세요.", parent=self)
            return
        _, sftp = self.ssh_sessions[sid]
        FileTransferDialog(self, sftp, server, self.config_mgr)

    def _delete_server(self):
        server, idx = self._get_selected_server()
        if not server:
            return
        if messagebox.askyesno("삭제 확인", f"'{server['name']}' 을 삭제하시겠습니까?", parent=self):
            self.config_mgr.delete_server(idx)
            self._load_server_grid()

    def _show_servers(self, edit=False):
        self._clear_content()

        # ── 상단 헤더 ──────────────────────────────────────────────
        top = tk.Frame(self.content, bg=THEME["panel"], pady=12, padx=20)
        top.pack(fill="x")
        tk.Label(top, text="🖥️  서버 관리", bg=THEME["panel"],
                 fg=THEME["text"], font=("Malgun Gothic", 14, "bold")).pack(side="left")
        styled_btn(top, "서버 추가", command=lambda: self._open_server_add(),
                   color=THEME["accent"], icon="➕").pack(side="right")

        # ── Command 선택 콤보박스 행 ──────────────────────────────
        cmd_bar = tk.Frame(self.content, bg=THEME["bg"], pady=8, padx=16)
        cmd_bar.pack(fill="x")

        # 아이콘 + 레이블
        tk.Label(cmd_bar, text="🖥️", bg=THEME["bg"],
                 fg=THEME["accent2"], font=("Malgun Gothic", 12)).pack(side="left", padx=(0, 4))
        tk.Label(cmd_bar, text="CLI 실행 파일:", bg=THEME["bg"],
                 fg=THEME["text2"], font=("Malgun Gothic", 9, "bold")).pack(side="left", padx=(0, 8))

        cmds = self.config_mgr.get_commands()
        srv_cmd_names = [f"{c.get('icon','')} {c.get('name','')}" for c in cmds]

        # cmd_var / cmd_combo 를 인스턴스 변수로 공유 (홈과 동일 변수 사용)
        if not hasattr(self, "cmd_var"):
            self.cmd_var = tk.StringVar(value=srv_cmd_names[0] if srv_cmd_names else "")
        self.srv_cmd_combo = ttk.Combobox(
            cmd_bar, textvariable=self.cmd_var,
            values=srv_cmd_names, state="readonly",
            width=32, font=("Malgun Gothic", 9),
        )
        self.srv_cmd_combo.pack(side="left", padx=(0, 10))
        if srv_cmd_names and not self.cmd_var.get():
            self.cmd_var.set(srv_cmd_names[0])

        styled_btn(cmd_bar, "커맨드 설정", command=self._show_cmd_settings,
                   color=THEME["accent3"], icon="⚙️").pack(side="left", padx=4)

        # 안내 텍스트
        tk.Label(cmd_bar,
                 text="  ←  선택 후 서버 더블클릭 → 접속 / 우클릭 → 메뉴",
                 bg=THEME["bg"], fg=THEME["text3"],
                 font=("Malgun Gothic", 8)).pack(side="left")

        # ── 구분선 ───────────────────────────────────────────────
        tk.Frame(self.content, bg=THEME["border"], height=1).pack(fill="x", padx=16)

        # ── Treeview ─────────────────────────────────────────────
        tree_wrap = tk.Frame(self.content, bg=THEME["bg"])
        tree_wrap.pack(fill="both", expand=True, padx=16, pady=(8, 0))

        # 연결 상태 표시 레이블
        self.srv_status_bar = tk.Label(
            tree_wrap, text="서버를 선택하세요  ·  더블클릭: CLI 접속  ·  우클릭: 메뉴",
            bg=THEME["bg"], fg=THEME["text3"], font=("Malgun Gothic", 8), anchor="w",
        )
        self.srv_status_bar.pack(anchor="w", pady=(0, 4))

        tree_frame = tk.Frame(tree_wrap, bg=THEME["border"], padx=1, pady=1)
        tree_frame.pack(fill="both", expand=True)

        cols = ("icon", "name", "host", "port", "username", "auth",
                "key_file", "init_dir", "description", "conn")
        self._srv_tree = ttk.Treeview(tree_frame, columns=cols,
                                      show="headings", selectmode="browse")
        t = self._srv_tree
        t.heading("icon",        text="")
        t.heading("name",        text="이름")
        t.heading("host",        text="호스트")
        t.heading("port",        text="포트")
        t.heading("username",    text="사용자")
        t.heading("auth",        text="인증")
        t.heading("key_file",    text="키 파일")
        t.heading("init_dir",    text="접속 디렉토리")
        t.heading("description", text="설명")
        t.heading("conn",        text="상태")

        t.column("icon",        width=28,  anchor="center")
        t.column("name",        width=130)
        t.column("host",        width=150)
        t.column("port",        width=50,  anchor="center")
        t.column("username",    width=90)
        t.column("auth",        width=90,  anchor="center")
        t.column("key_file",    width=120)
        t.column("init_dir",    width=130)
        t.column("description", width=160)
        t.column("conn",        width=80,  anchor="center")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",   command=t.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal",  command=t.xview)
        t.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        t.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        t.tag_configure("connected",    foreground=THEME["success"])
        t.tag_configure("disconnected", foreground=THEME["text"])

        # 데이터 로드
        self._reload_srv_tree()

        # ── 이벤트 바인딩 ─────────────────────────────────────────
        t.bind("<Double-Button-1>", self._srv_tree_dbl_click)
        t.bind("<Button-3>",        self._srv_tree_right_click)
        t.bind("<<TreeviewSelect>>", self._srv_tree_select)

        # ── 우클릭 컨텍스트 메뉴 ─────────────────────────────────
        self._srv_ctx = tk.Menu(self, tearoff=0,
                                bg=THEME["card"], fg=THEME["text"],
                                activebackground=THEME["accent2"],
                                activeforeground="white",
                                font=("Malgun Gothic", 9))
        self._srv_ctx.add_command(
            label="🔐  CLI 접속  (더블클릭)",
            command=lambda: self._srv_connect_action())
        self._srv_ctx.add_separator()
        self._srv_ctx.add_command(
            label="📁  파일 탐색기  (로컬 ↔ 원격)",
            command=lambda: self._srv_open_explorer_action())
        self._srv_ctx.add_separator()
        self._srv_ctx.add_command(
            label="✏️  서버 편집",
            command=lambda: self._srv_edit_action())
        self._srv_ctx.add_command(
            label="🗑️  서버 삭제",
            command=lambda: self._srv_delete_action())

        # ── 하단 액션 바 ──────────────────────────────────────────
        act = tk.Frame(self.content, bg=THEME["panel"], pady=8, padx=16)
        act.pack(fill="x", side="bottom")

        styled_btn(act, "CLI 접속",   self._srv_connect_action,
                   color=THEME["accent"],  icon="🔐").pack(side="left", padx=4)
        styled_btn(act, "파일 탐색기", self._srv_open_explorer_action,
                   color=THEME["accent2"], icon="📁").pack(side="left", padx=4)
        styled_btn(act, "편집",       self._srv_edit_action,
                   color=THEME["card"],    icon="✏️").pack(side="left", padx=4)
        styled_btn(act, "삭제",       self._srv_delete_action,
                   color=THEME["danger"],  icon="🗑️").pack(side="left", padx=4)
        styled_btn(act, "홈으로",     self._show_home,
                   color=THEME["card"],    icon="🏠").pack(side="right", padx=4)

    # ── 서버 관리 내부 헬퍼 ───────────────────────────────────────────────

    def _reload_srv_tree(self):
        """서버 관리 Treeview 데이터 새로고침"""
        if not hasattr(self, "_srv_tree"):
            return
        self._srv_tree.delete(*self._srv_tree.get_children())
        AUTH_ICONS = {"password": "🔑 비밀번호", "ppk": "🗝️ PPK", "pem": "🗝️ PEM"}
        for srv in self.config_mgr.get_servers():
            am     = srv.get("auth_method", "password")
            auth_d = AUTH_ICONS.get(am, "🔑 비밀번호")
            key_f  = (Path(srv.get("ppk_path", "")).name if am == "ppk"
                      else Path(srv.get("pem_path", "")).name if am == "pem"
                      else "")
            sid       = srv.get("id", "")
            connected = sid in self.ssh_sessions
            conn_txt  = "● 연결됨" if connected else "○ 미연결"
            tag       = "connected" if connected else "disconnected"
            self._srv_tree.insert("", tk.END, values=(
                "🖥️",
                srv.get("name", ""),
                srv.get("host", ""),
                srv.get("port", 22),
                srv.get("username", ""),
                auth_d,
                key_f,
                srv.get("init_dir", ""),
                srv.get("description", ""),
                conn_txt,
            ), tags=(tag,))

    def _get_srv_tree_server(self):
        """서버 관리 Treeview 에서 선택된 서버·인덱스 반환"""
        if not hasattr(self, "_srv_tree"):
            return None, None
        sel = self._srv_tree.selection()
        if not sel:
            return None, None
        idx     = self._srv_tree.index(sel[0])
        servers = self.config_mgr.get_servers()
        if idx >= len(servers):
            return None, None
        return servers[idx], idx

    def _srv_tree_select(self, _=None):
        srv, _ = self._get_srv_tree_server()
        if srv and hasattr(self, "srv_status_bar"):
            sid  = srv.get("id", "")
            conn = "● SSH 연결됨" if sid in self.ssh_sessions else "○ 미연결"
            self.srv_status_bar.config(
                text=f"{srv.get('name','')}  ·  {srv.get('host','')}:{srv.get('port',22)}"
                     f"  ·  {conn}  ·  더블클릭: CLI 접속  ·  우클릭: 메뉴")

    def _srv_tree_dbl_click(self, event):
        item = self._srv_tree.identify_row(event.y)
        if item:
            self._srv_connect_action()

    def _srv_tree_right_click(self, event):
        item = self._srv_tree.identify_row(event.y)
        if item:
            self._srv_tree.selection_set(item)
            self._srv_tree_select()
            self._srv_ctx.post(event.x_root, event.y_root)

    def _srv_connect_action(self):
        """서버 관리 화면에서 CLI 접속"""
        server, idx = self._get_srv_tree_server()
        if not server:
            messagebox.showwarning("선택 없음", "서버를 선택하세요.", parent=self)
            return

        sid = server.get("id", "")

        # CLI 커맨드: srv_cmd_combo 우선, 없으면 cmd_combo
        cmds    = self.config_mgr.get_commands()
        cli_idx = 0
        if hasattr(self, "srv_cmd_combo"):
            cli_idx = self.srv_cmd_combo.current()
        elif hasattr(self, "cmd_combo"):
            cli_idx = self.cmd_combo.current()
        if cli_idx < 0:
            cli_idx = 0
        cli = cmds[cli_idx] if cmds else None

        # 이미 SSH 연결된 경우에도 CLI 는 새로 띄움 (SSH 세션은 파일전송용)
        dlg = LoginDialog(self, server, self.config_mgr)
        self.wait_window(dlg)
        if not dlg.result:
            return

        creds        = dlg.result
        username     = creds["username"]
        password     = creds["password"]
        auth_method  = creds.get("auth_method", "password")
        key_path     = creds.get("key_path", "")
        key_pw       = creds.get("key_passphrase", "")

        if creds["save"]:
            server["username"] = username
            if auth_method == "password":
                server["password_enc"] = self.config_mgr.encrypt_password(password)
            elif key_pw:
                server["key_passphrase_enc"] = self.config_mgr.encrypt_password(key_pw)
            self.config_mgr.update_server(idx, server)

        # CLI 실행
        self._launch_cli(cli, server, username, password, key_path, key_pw, auth_method)

        # SSH 백그라운드 연결 (파일전송용)
        if sid not in self.ssh_sessions:
            self._connect_ssh(server, username, password, key_path, key_pw, auth_method)

        self._reload_srv_tree()

    def _srv_open_explorer_action(self):
        """서버 관리 화면 → 파일 탐색기 팝업"""
        server, idx = self._get_srv_tree_server()
        if not server:
            messagebox.showwarning("선택 없음", "서버를 선택하세요.", parent=self)
            return

        sid = server.get("id", "")

        # SSH 미연결이면 먼저 로그인
        if sid not in self.ssh_sessions:
            dlg = LoginDialog(self, server, self.config_mgr)
            self.wait_window(dlg)
            if not dlg.result:
                return

            creds       = dlg.result
            username    = creds["username"]
            password    = creds["password"]
            auth_method = creds.get("auth_method", "password")
            key_path    = creds.get("key_path", "")
            key_pw      = creds.get("key_passphrase", "")

            if creds["save"]:
                server["username"] = username
                if auth_method == "password":
                    server["password_enc"] = self.config_mgr.encrypt_password(password)
                elif key_pw:
                    server["key_passphrase_enc"] = self.config_mgr.encrypt_password(key_pw)
                self.config_mgr.update_server(idx, server)

            # SSH 연결 후 탐색기 열기 (연결 완료 대기)
            self._connect_ssh_sync(server, username, password,
                                   key_path, key_pw, auth_method,
                                   on_success=lambda: self._open_explorer_popup(server))
        else:
            self._open_explorer_popup(server)

    def _connect_ssh_sync(self, server, username, password,
                          key_path="", key_passphrase="", auth_method="password",
                          on_success=None):
        """SSH 연결 후 on_success 콜백 실행 (진행 팝업 포함)"""
        sid  = server.get("id", "")
        host = server.get("host", "")
        port = server.get("port", 22)
        init_dir = server.get("init_dir", "").strip()

        # 진행 팝업
        prog = tk.Toplevel(self)
        prog.title("SSH 연결 중…")
        prog.configure(bg=THEME["panel"])
        prog.resizable(False, False)
        prog.transient(self)
        prog.geometry("320x100")
        prog.update_idletasks()
        px = self.winfo_rootx() + (self.winfo_width() - 320) // 2
        py = self.winfo_rooty() + (self.winfo_height() - 100) // 2
        prog.geometry(f"320x100+{px}+{py}")
        tk.Label(prog, text=f"🔗  {host}:{port} 연결 중…",
                 bg=THEME["panel"], fg=THEME["text"],
                 font=("Malgun Gothic", 10)).pack(expand=True)

        def connect():
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

                if auth_method in ("ppk", "pem") and key_path:
                    kpw  = key_passphrase if key_passphrase else None
                    pkey = None
                    for cls in (paramiko.RSAKey, paramiko.DSSKey,
                                paramiko.ECDSAKey, paramiko.Ed25519Key):
                        try:
                            pkey = cls.from_private_key_file(key_path, password=kpw)
                            break
                        except Exception:
                            continue
                    if pkey is None:
                        raise ValueError("키 파일 인증 실패")
                    ssh.connect(host, port=port, username=username, pkey=pkey, timeout=10)
                else:
                    ssh.connect(host, port=port, username=username,
                                password=password, timeout=10)

                sftp = ssh.open_sftp()
                if init_dir:
                    try:
                        sftp.chdir(init_dir)
                    except Exception:
                        pass

                self.ssh_sessions[sid] = (ssh, sftp)
                self.status_label.config(
                    text=f"● {server['name']} 연결됨", fg=THEME["success"])
                self.after(0, prog.destroy)
                self.after(0, self._reload_srv_tree)
                if on_success:
                    self.after(50, on_success)

            except Exception as e:
                self.after(0, prog.destroy)
                self.after(0, lambda: messagebox.showerror(
                    "SSH 연결 오류", f"{host}: {e}", parent=self))

        threading.Thread(target=connect, daemon=True).start()

    def _open_explorer_popup(self, server: dict):
        """FileTransferDialog 를 탐색기 모드로 오픈"""
        sid = server.get("id", "")
        if sid not in self.ssh_sessions:
            messagebox.showwarning("미연결",
                "SSH 연결이 완료되지 않았습니다.\n잠시 후 다시 시도하세요.", parent=self)
            return
        _, sftp = self.ssh_sessions[sid]
        FileTransferDialog(self, sftp, server, self.config_mgr)

    def _srv_edit_action(self):
        server, idx = self._get_srv_tree_server()
        if not server:
            messagebox.showwarning("선택 없음", "서버를 선택하세요.", parent=self)
            return
        dlg = ServerDialog(self, self.config_mgr, server, idx)
        self.wait_window(dlg)
        self._reload_srv_tree()

    def _srv_delete_action(self):
        server, idx = self._get_srv_tree_server()
        if not server:
            messagebox.showwarning("선택 없음", "서버를 선택하세요.", parent=self)
            return
        if messagebox.askyesno("삭제 확인",
                               f"'{server['name']}' 을 삭제하시겠습니까?",
                               parent=self):
            self.config_mgr.delete_server(idx)
            self._reload_srv_tree()

    def _open_server_add(self):
        dlg = ServerDialog(self, self.config_mgr)
        self.wait_window(dlg)
        self._reload_srv_tree()

    # ── COMMAND SETTINGS ─────────────────────
    def _show_cmd_settings(self):
        dlg = CommandSettingsDialog(self, self.config_mgr)
        self.wait_window(dlg)
        # Refresh home combo
        if hasattr(self, "cmd_combo"):
            cmds = self.config_mgr.get_commands()
            names = [f"{c.get('icon','')} {c.get('name','')}" for c in cmds]
            self.cmd_combo["values"] = names
            if names:
                self.cmd_var.set(names[0])

    # ── LOGS ─────────────────────────────────
    def _show_logs(self):
        self._clear_content()
        top = tk.Frame(self.content, bg=THEME["panel"], pady=12, padx=20)
        top.pack(fill="x")
        tk.Label(top, text="📋  세션 로그", bg=THEME["panel"],
                 fg=THEME["text"], font=("Malgun Gothic", 14, "bold")).pack(side="left")

        body = tk.Frame(self.content, bg=THEME["bg"], padx=16, pady=16)
        body.pack(fill="both", expand=True)

        tk.Label(body, text="현재 활성 SSH 세션:", bg=THEME["bg"],
                 fg=THEME["text2"], font=("Malgun Gothic", 9, "bold")).pack(anchor="w", pady=(0, 8))

        for sid, (ssh, sftp) in self.ssh_sessions.items():
            t = ssh.get_transport()
            active = t and t.is_active()
            color = THEME["success"] if active else THEME["danger"]
            status = "● 활성" if active else "✖ 비활성"
            servers = {s["id"]: s for s in self.config_mgr.get_servers()}
            srv = servers.get(sid, {})
            txt = f"{status}   {srv.get('name', sid)}   ({srv.get('host', '')}:{srv.get('port', 22)})"
            tk.Label(body, text=txt, bg=THEME["bg"], fg=color,
                     font=("Consolas", 10)).pack(anchor="w", pady=2)

        if not self.ssh_sessions:
            tk.Label(body, text="활성 세션 없음", bg=THEME["bg"],
                     fg=THEME["text3"], font=("Malgun Gothic", 10)).pack(anchor="w", pady=8)

    def _on_close(self):
        self.config_mgr.data["window_geometry"] = self.geometry()
        self.config_mgr.save()
        LOG.sep("앱 종료")
        for sid, (ssh, sftp) in list(self.ssh_sessions.items()):
            LOG.info(f"SSH 세션 종료: {sid}")
            try:
                sftp.close()
                ssh.close()
            except Exception:
                pass
        self.destroy()


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    # LOG 배너는 CmdLogger.__init__ 에서 이미 출력됨
    LOG.info(f"Python {sys.version.split()[0]}  /  tkinter {tk.TkVersion}")
    app = CLIManagerApp()
    app.mainloop()
    LOG.sep("mainloop 종료")

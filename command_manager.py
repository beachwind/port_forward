from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from crypto_store import (
    command_id,
    decrypt_text,
    encrypt_text,
    load_commands,
    load_editors,
    load_favorites,
    load_profiles,
    profile_id,
    save_commands,
    save_editors,
    save_favorites,
    save_profiles,
)


ROOT = Path(__file__).resolve().parent
DOCKER_LOG_DIR = ROOT / "docker_logs"


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def cygwin_path(value: str) -> str:
    path = Path(value)
    text = str(path).replace("\\", "/")
    if len(text) >= 2 and text[1] == ":":
        drive = text[0].lower()
        return f"/cygdrive/{drive}{text[2:]}"
    return text


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def human_size(size: int | None) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def format_mtime(timestamp: float | int | None) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


class ToolTip:
    def __init__(self, widget: tk.Widget, text: str):
        self.widget = widget
        self.text = text
        self.window: tk.Toplevel | None = None
        self.after_id: str | None = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, _event=None) -> None:
        self.cancel()
        self.after_id = self.widget.after(450, self.show)

    def cancel(self) -> None:
        if self.after_id:
            self.widget.after_cancel(self.after_id)
            self.after_id = None

    def show(self) -> None:
        self.after_id = None
        if self.window or not self.text:
            return
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{x}+{y}")
        label = ttk.Label(self.window, text=self.text, padding=(6, 3), relief="solid", borderwidth=1)
        label.grid(row=0, column=0)

    def hide(self, _event=None) -> None:
        self.cancel()
        if self.window:
            self.window.destroy()
            self.window = None


class TransferNotification(tk.Toplevel):
    """Bottom-right toast notification shown after a file transfer completes.

    Clicking the toast expands it into three action buttons that operate on
    the transferred file: opening it in Windows Explorer, granting execute
    permission on the remote (Linux) copy, and running it remotely.
    """

    DISPLAY_MS = 20000
    WIDTH = 300

    def __init__(
        self,
        master: tk.Widget,
        message: str,
        dialog: "FileTransferDialog",
        local_path: str | None,
        remote_path: str | None,
    ):
        super().__init__(master)
        self.dialog = dialog
        self.local_path = local_path
        self.remote_path = remote_path
        self.auto_close_id: str | None = None

        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.configure(bg="#1a202c")

        self._build_message(message)
        self._reposition()
        self._schedule_auto_close()

    def _schedule_auto_close(self) -> None:
        self.auto_close_id = self.after(self.DISPLAY_MS, self._safe_destroy)

    def _cancel_auto_close(self) -> None:
        if self.auto_close_id:
            self.after_cancel(self.auto_close_id)
            self.auto_close_id = None

    def _clear(self) -> None:
        for child in self.winfo_children():
            child.destroy()

    def _build_message(self, message: str) -> None:
        self._clear()
        frame = tk.Frame(self, bg="#1a202c", padx=14, pady=12)
        frame.pack(fill="both", expand=True)
        label = tk.Label(
            frame,
            text=message,
            bg="#1a202c",
            fg="#f7fafc",
            wraplength=260,
            justify="left",
            font=("Segoe UI", 10),
        )
        label.pack(anchor="w")

        clickable = [frame, label]
        if self.remote_path:
            hint = tk.Label(
                frame,
                text="클릭하여 작업 선택",
                bg="#1a202c",
                fg="#a0aec0",
                font=("Segoe UI", 8),
            )
            hint.pack(anchor="w", pady=(4, 0))
            clickable.append(hint)
            for widget in clickable:
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>", self._show_actions)

        close_button = tk.Label(self, text="\u2715", bg="#1a202c", fg="#a0aec0", cursor="hand2")
        close_button.place(x=self.WIDTH - 22, y=6)
        close_button.bind("<Button-1>", lambda _event: self._safe_destroy())

    def _show_actions(self, _event=None) -> None:
        self._cancel_auto_close()
        self._clear()
        frame = tk.Frame(self, bg="#1a202c", padx=12, pady=10)
        frame.pack(fill="both", expand=True)
        name = posixpath.basename(self.remote_path or self.local_path or "")
        tk.Label(
            frame, text=name, bg="#1a202c", fg="#f7fafc", font=("Segoe UI", 9, "bold"), wraplength=260, justify="left"
        ).pack(anchor="w", pady=(0, 6))
        ttk.Button(frame, text="파일 탐색기", command=self._open_explorer).pack(fill="x", pady=2)
        ttk.Button(frame, text="실행 권한 주기", command=self._grant_execute).pack(fill="x", pady=2)
        ttk.Button(frame, text="실행", command=self._execute).pack(fill="x", pady=2)
        ttk.Button(frame, text="닫기", command=self._safe_destroy).pack(fill="x", pady=(6, 0))

        close_button = tk.Label(self, text="\u2715", bg="#1a202c", fg="#a0aec0", cursor="hand2")
        close_button.place(x=self.WIDTH - 22, y=6)
        close_button.bind("<Button-1>", lambda _event: self._safe_destroy())
        self._reposition()

    def _reposition(self) -> None:
        self.update_idletasks()
        height = max(self.winfo_reqheight(), 70)
        x = self.winfo_screenwidth() - self.WIDTH - 24
        y = self.winfo_screenheight() - height - 60
        self.geometry(f"{self.WIDTH}x{height}+{x}+{y}")

    def _open_explorer(self) -> None:
        if self.local_path:
            self.dialog.open_transfer_location(self.local_path)
        self._safe_destroy()

    def _grant_execute(self) -> None:
        if self.remote_path:
            self.dialog.grant_remote_execute_permission(self.remote_path)
        self._safe_destroy()

    def _execute(self) -> None:
        if self.remote_path:
            self.dialog.run_remote_executable(self.remote_path)
        self._safe_destroy()

    def _safe_destroy(self) -> None:
        self._cancel_auto_close()
        if self.winfo_exists():
            self.destroy()


class ProfileDialog(tk.Toplevel):
    def __init__(self, master: tk.Tk, profile: dict | None = None, title: str = "로그인 정보"):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.result: dict | None = None
        self.profile = profile or {}

        self.name_var = tk.StringVar(value=self.profile.get("name", ""))
        self.host_var = tk.StringVar(value=self.profile.get("host", ""))
        self.port_var = tk.StringVar(value=str(self.profile.get("port", 22)))
        self.user_var = tk.StringVar(value=self.profile.get("username", ""))
        self.pass_var = tk.StringVar()
        self.key_path_var = tk.StringVar(value=self.profile.get("key_path", ""))
        self.save_var = tk.StringVar(value="save" if self.profile.get("password") else "nosave")

        if self.profile.get("password"):
            try:
                self.pass_var.set(decrypt_text(self.profile["password"]))
            except Exception:
                self.pass_var.set("")

        self._build()
        self.transient(master)
        self.grab_set()
        self.wait_visibility()
        self.name_entry.focus_set()

    def _build(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")

        fields = [
            ("이름", self.name_var),
            ("서버", self.host_var),
            ("포트", self.port_var),
            ("계정", self.user_var),
            ("암호", self.pass_var),
        ]
        for row, (label, var) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
            show = "*" if label == "암호" else ""
            entry = ttk.Entry(frame, textvariable=var, width=34, show=show)
            entry.grid(row=row, column=1, sticky="ew", pady=5)
            if row == 0:
                self.name_entry = entry

        ttk.Label(frame, text="PEM 키").grid(row=5, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.key_path_var, width=34).grid(row=5, column=1, sticky="ew", pady=5)
        ttk.Button(frame, text="찾기", command=self.browse_key_file).grid(row=5, column=2, padx=(6, 0), pady=5)

        save_frame = ttk.Frame(frame)
        save_frame.grid(row=6, column=1, sticky="w", pady=(6, 12))
        ttk.Radiobutton(save_frame, text="저장", value="save", variable=self.save_var).grid(
            row=0, column=0, padx=(0, 12)
        )
        ttk.Radiobutton(save_frame, text="저장 안 함", value="nosave", variable=self.save_var).grid(
            row=0, column=1
        )

        actions = ttk.Frame(frame)
        actions.grid(row=7, column=0, columnspan=3, sticky="e")
        ttk.Button(actions, text="취소", command=self.destroy).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(actions, text="로그인", command=self._submit).grid(row=0, column=1)

    def browse_key_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="PEM 키 파일 선택",
            filetypes=[("PEM 키", "*.pem"), ("키 파일", "*.pem *.key *.ppk"), ("모든 파일", "*.*")],
        )
        if path:
            self.key_path_var.set(path)

    def _submit(self) -> None:
        name = self.name_var.get().strip()
        host = self.host_var.get().strip()
        username = self.user_var.get().strip()
        password = self.pass_var.get()
        key_path = self.key_path_var.get().strip()
        try:
            port = int(self.port_var.get().strip() or "22")
        except ValueError:
            messagebox.showerror("입력 오류", "포트는 숫자여야 합니다.", parent=self)
            return
        if not name or not host or not username:
            messagebox.showerror("입력 오류", "이름, 서버, 계정은 필수입니다.", parent=self)
            return
        if key_path and not Path(key_path).exists():
            messagebox.showerror("입력 오류", "PEM 키 파일 경로가 존재하지 않습니다.", parent=self)
            return

        saved_password = encrypt_text(password) if self.save_var.get() == "save" and password else ""
        self.result = {
            "id": self.profile.get("id") or profile_id(name, host, username),
            "name": name,
            "host": host,
            "port": port,
            "username": username,
            "password": saved_password,
            "key_path": key_path,
            "runtime_password": "" if saved_password else password,
        }
        self.destroy()


class FileTransferDialog(tk.Toplevel):
    def __init__(self, master: tk.Tk, profile: dict, password: str, key_path: str = ""):
        super().__init__(master)
        self.manager = master
        self.title(f"파일 전송 - {profile.get('name', profile.get('host', 'server'))}")
        self.geometry("1050x620")
        self.minsize(900, 500)
        self.profile = profile
        self.password = password
        self.key_path = key_path
        self.client = None
        self.sftp = None
        self.sftp_lock = threading.Lock()
        self.local_cwd = Path.home()
        self.remote_cwd = "."
        self.drag_data: dict | None = None
        self.favorites = load_favorites()
        self.local_favorite_var = tk.StringVar()
        self.remote_favorite_var = tk.StringVar()
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text_var = tk.StringVar(value="0%")
        self.selected_terminal_command: dict | None = None
        self.selected_terminal_var = tk.StringVar(value="터미널: 미선택")

        self.folder_icon = self._icon("#d9a441")
        self.file_icon = self._icon("#6f93c8")
        self.action_icons = self._build_action_icons()
        self._build_extra_icons()

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self._connect_remote)

    def _icon(self, color: str) -> tk.PhotoImage:
        image = tk.PhotoImage(width=16, height=16)
        image.put(color, to=(2, 5, 15, 14))
        image.put("#f0c76c" if color == "#d9a441" else "#b8c9e6", to=(2, 3, 8, 6))
        image.put("#5c5c5c", to=(2, 5, 15, 6))
        image.put("#5c5c5c", to=(2, 14, 15, 15))
        image.put("#5c5c5c", to=(2, 5, 3, 15))
        image.put("#5c5c5c", to=(14, 5, 15, 15))
        return image

    def _build_extra_icons(self) -> None:
        """Hook for subclasses (e.g. DockerTransferDialog) to prepare extra icons
        before the dialog UI is built. No-op by default."""
        return

    def _build_action_icons(self) -> dict[str, tk.PhotoImage]:
        specs = {
            "refresh": ("#4f81bd", "loop"),
            "register": ("#2f855a", "plus"),
            "go": ("#4f81bd", "arrow"),
            "folder": ("#d9a441", "folder_plus"),
            "empty": ("#6f93c8", "file_plus"),
            "view": ("#5b6f8f", "eye"),
            "log": ("#4a5568", "lines"),
            "today_log": ("#2b6cb0", "calendar"),
            "log_color": ("#b7791f", "bars"),
            "edit": ("#2f855a", "pen"),
            "rename": ("#805ad5", "pen"),
            "delete": ("#c53030", "x"),
            "terminal": ("#2d3748", "prompt"),
            "clipboard": ("#718096", "clip"),
            "explorer": ("#d69e2e", "folder"),
            "screenshot": ("#2b6cb0", "camera"),
        }
        return {name: self._action_icon(color, shape) for name, (color, shape) in specs.items()}

    def _action_icon(self, color: str, shape: str) -> tk.PhotoImage:
        image = tk.PhotoImage(width=16, height=16)
        image.put("#f7f7f7", to=(0, 0, 16, 16))
        image.put("#d0d0d0", to=(0, 0, 16, 1))
        image.put("#d0d0d0", to=(0, 15, 16, 16))
        image.put("#d0d0d0", to=(0, 0, 1, 16))
        image.put("#d0d0d0", to=(15, 0, 16, 16))
        if shape in {"folder", "folder_plus"}:
            image.put("#f0c76c", to=(3, 4, 8, 6))
            image.put(color, to=(3, 6, 13, 12))
        elif shape in {"file_plus", "lines"}:
            image.put("#ffffff", to=(4, 3, 12, 13))
            image.put(color, to=(4, 3, 12, 4))
            image.put(color, to=(4, 12, 12, 13))
            image.put(color, to=(4, 3, 5, 13))
            image.put(color, to=(11, 3, 12, 13))
            if shape == "lines":
                image.put(color, to=(6, 6, 10, 7))
                image.put(color, to=(6, 9, 10, 10))
        elif shape == "eye":
            image.put(color, to=(3, 7, 13, 9))
            image.put(color, to=(5, 5, 11, 11))
            image.put("#ffffff", to=(7, 7, 9, 9))
        elif shape == "bars":
            image.put("#38a169", to=(3, 4, 13, 6))
            image.put("#3182ce", to=(3, 7, 13, 9))
            image.put("#e53e3e", to=(3, 10, 13, 12))
        elif shape == "calendar":
            image.put(color, to=(3, 4, 13, 13))
            image.put("#ffffff", to=(4, 6, 12, 12))
            image.put(color, to=(5, 8, 7, 10))
            image.put(color, to=(9, 8, 11, 10))
        elif shape == "pen":
            image.put(color, to=(4, 10, 12, 12))
            image.put(color, to=(9, 4, 12, 7))
        elif shape == "x":
            for offset in range(5):
                image.put(color, to=(4 + offset, 4 + offset, 5 + offset, 5 + offset))
                image.put(color, to=(11 - offset, 4 + offset, 12 - offset, 5 + offset))
        elif shape == "prompt":
            image.put("#1a202c", to=(3, 4, 13, 12))
            image.put("#ffffff", to=(5, 6, 8, 7))
            image.put("#ffffff", to=(8, 9, 11, 10))
        elif shape == "clip":
            image.put(color, to=(5, 3, 11, 5))
            image.put(color, to=(4, 5, 12, 13))
            image.put("#ffffff", to=(5, 6, 11, 12))
        elif shape == "camera":
            image.put(color, to=(3, 6, 13, 12))
            image.put(color, to=(5, 4, 9, 6))
            image.put("#ffffff", to=(7, 8, 10, 11))
        elif shape == "loop":
            image.put(color, to=(4, 4, 12, 6))
            image.put(color, to=(10, 6, 12, 10))
            image.put(color, to=(4, 10, 12, 12))
        elif shape == "arrow":
            image.put(color, to=(4, 7, 11, 9))
            image.put(color, to=(9, 5, 12, 11))
        if shape in {"plus", "folder_plus", "file_plus"}:
            image.put("#ffffff", to=(7, 5, 9, 12))
            image.put("#ffffff", to=(5, 7, 11, 9))
            image.put("#2f855a", to=(7, 6, 9, 11))
            image.put("#2f855a", to=(6, 7, 10, 9))
        return image

    def _icon_button(
        self,
        parent: tk.Widget,
        icon_name: str,
        tooltip: str,
        command,
        row: int,
        column: int,
    ) -> ttk.Button:
        button = ttk.Button(parent, image=self.action_icons[icon_name], command=command, width=3)
        button.grid(row=row, column=column, padx=(2, 0), pady=2)
        ToolTip(button, tooltip)
        return button

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self, padding=(12, 10))
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(toolbar, text="로컬 파일과 원격 파일을 반대쪽 목록으로 드래그하여 전송합니다.").grid(
            row=0, column=0, sticky="w"
        )
        self._icon_button(toolbar, "refresh", "새로 고침", self.refresh_all, 0, 1).grid(
            row=0, column=1, padx=(12, 0)
        )
        self._build_toolbar_buttons(toolbar, start_column=2)
        toolbar.columnconfigure(40, weight=1)
        self.selected_terminal_label = ttk.Label(
            toolbar, textvariable=self.selected_terminal_var, foreground="#2b6cb0"
        )
        self.selected_terminal_label.grid(row=0, column=41, sticky="e")
        ToolTip(self.selected_terminal_label, "터미널 아이콘 버튼 클릭 시 사용할 CLI 명령창")

        panes = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        panes.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))

        local = ttk.Frame(panes, padding=8)
        remote = ttk.Frame(panes, padding=8)
        panes.add(local, weight=1)
        panes.add(remote, weight=1)
        self._build_local_pane(local)
        self._build_remote_pane(remote)

        self.status = tk.StringVar(value="원격 서버에 연결 중...")
        status_bar = ttk.Frame(self, padding=(12, 0, 12, 10))
        status_bar.grid(row=2, column=0, sticky="ew")
        status_bar.columnconfigure(0, weight=1)
        ttk.Label(status_bar, textvariable=self.status).grid(row=0, column=0, sticky="ew")
        self.progress_bar = ttk.Progressbar(status_bar, variable=self.progress_var, maximum=100, mode="determinate")
        self.progress_bar.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        status_bar.columnconfigure(1, weight=1)
        ttk.Label(status_bar, textvariable=self.progress_text_var, width=6, anchor="e").grid(
            row=0, column=2, sticky="e", padx=(6, 0)
        )

    def _build_toolbar_buttons(self, toolbar: ttk.Frame, start_column: int = 2) -> None:
        """Hook for specialized transfer dialogs to add top toolbar buttons."""
        return

    def _build_local_pane(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(4, weight=1)
        ttk.Label(parent, text="로컬 파일 탐색기", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        self.local_path_var = tk.StringVar()
        favorite_group = ttk.LabelFrame(parent, text="즐겨찾기")
        favorite_group.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        favorite_group.columnconfigure(0, weight=1)
        self.local_favorite_combo = ttk.Combobox(
            favorite_group,
            textvariable=self.local_favorite_var,
            state="readonly",
            values=self.favorites.get("local_paths", []),
        )
        self.local_favorite_combo.grid(row=0, column=0, sticky="ew", padx=(3, 0), pady=3)
        self.local_favorite_combo.bind("<<ComboboxSelected>>", self.use_local_favorite)

        path_group = ttk.LabelFrame(parent, text="경로 이동")
        path_group.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        path_group.columnconfigure(0, weight=1)
        self.local_path_entry = ttk.Entry(path_group, textvariable=self.local_path_var)
        self.local_path_entry.grid(row=0, column=0, sticky="ew", padx=(3, 0), pady=3)
        self.local_path_entry.bind("<Return>", lambda _event: self.goto_local_path())
        self._icon_button(path_group, "go", "이동", self.goto_local_path, 0, 1)
        self._icon_button(path_group, "register", "등록", self.add_local_favorite, 0, 2)

        action_group = ttk.LabelFrame(parent, text="작업")
        action_group.grid(row=3, column=0, sticky="ew", pady=3)
        local_actions = [
            ("folder", "새 폴더", self.create_local_folder),
            ("empty", "빈 파일", self.create_local_empty_file),
            ("view", "파일 보기", self.view_local_selected),
            ("log", "로그 보기", self.view_local_log),
            ("log_color", "로그 보기(색상)", self.view_local_log_color),
            ("rename", "파일명 변경", self.rename_local_selected),
            ("delete", "삭제", self.delete_local_selected),
            ("terminal", "터미널", self.open_local_terminal),
            ("clipboard", "클립보드 보기", self.view_clipboard),
            ("explorer", "Explorer", self.open_windows_explorer),
        ]
        for column, (icon, tooltip, command) in enumerate(local_actions):
            self._icon_button(action_group, icon, tooltip, command, 0, column)

        self.local_tree = self._make_tree(parent)
        self.local_tree.grid(row=4, column=0, sticky="nsew")
        self._bind_tree_events(self.local_tree, "local")

    def _build_remote_pane(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(4, weight=1)
        ttk.Label(parent, text="원격 서버 파일 탐색기", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        self.remote_path_var = tk.StringVar()
        favorite_group = ttk.LabelFrame(parent, text="즐겨찾기")
        favorite_group.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        favorite_group.columnconfigure(0, weight=1)
        self.remote_favorite_combo = ttk.Combobox(
            favorite_group,
            textvariable=self.remote_favorite_var,
            state="readonly",
            values=self._remote_favorites_sorted(),
        )
        self.remote_favorite_combo.grid(row=0, column=0, sticky="ew", padx=(3, 0), pady=3)
        self.remote_favorite_combo.bind("<<ComboboxSelected>>", self.use_remote_favorite)

        path_group = ttk.LabelFrame(parent, text="경로 이동")
        path_group.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        path_group.columnconfigure(0, weight=1)
        self.remote_path_entry = ttk.Entry(path_group, textvariable=self.remote_path_var)
        self.remote_path_entry.grid(row=0, column=0, sticky="ew", padx=(3, 0), pady=3)
        self.remote_path_entry.bind("<Return>", lambda _event: self.goto_remote_path())
        self._icon_button(path_group, "go", "이동", self.goto_remote_path, 0, 1)
        self._icon_button(path_group, "register", "등록", self.add_remote_favorite, 0, 2)

        action_group = ttk.LabelFrame(parent, text="작업")
        action_group.grid(row=3, column=0, sticky="ew", pady=3)
        remote_actions = [
            ("folder", "새 폴더", self.create_remote_folder),
            ("empty", "빈 파일", self.create_remote_empty_file),
            ("view", "파일 보기", self.view_remote_selected),
            ("log", "로그 보기", self.view_remote_log),
            ("today_log", "금일 로그 보기", self.view_today_remote_log),
            ("log_color", "로그 보기(색상)", self.view_remote_log_color),
            ("edit", "편집", self.edit_remote_with_gvim),
            ("rename", "파일명 변경", self.rename_remote_selected),
            ("delete", "삭제", self.delete_remote_selected),
            ("terminal", "터미널", self.open_remote_terminal),
            ("clipboard", "클립보드 보기", self.view_remote_clipboard),
            ("screenshot", "스크린샷", self.capture_remote_screenshot),
        ]
        for column, (icon, tooltip, command) in enumerate(remote_actions):
            self._icon_button(action_group, icon, tooltip, command, 0, column)

        self.remote_tree = self._make_tree(parent)
        self.remote_tree.grid(row=4, column=0, sticky="nsew")
        self._bind_tree_events(self.remote_tree, "remote")

    def _make_tree(self, parent: ttk.Frame) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=("type", "size", "modified"), show="tree headings", selectmode="extended")
        tree.heading("#0", text="이름")
        tree.heading("type", text="종류")
        tree.heading("size", text="크기")
        tree.heading("modified", text="수정 날짜")
        tree.column("#0", width=280, minwidth=160)
        tree.column("type", width=70, anchor="center")
        tree.column("size", width=90, anchor="e")
        tree.column("modified", width=150, anchor="center")
        yscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=yscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        return frame

    def _tree_widget(self, container: ttk.Frame) -> ttk.Treeview:
        return container.winfo_children()[0]

    def _bind_tree_events(self, container: ttk.Frame, source: str) -> None:
        tree = self._tree_widget(container)
        tree.bind("<ButtonPress-1>", lambda event: self.start_drag(event, source))
        tree.bind("<ButtonRelease-1>", self.finish_drag)
        tree.bind("<Double-1>", lambda event: self.open_clicked_folder(event, source))
        tree.bind("<Button-3>", lambda event: self.show_tree_menu(event, source))

    def _add_terminal_menu(self, menu: tk.Menu, source: str) -> None:
        """Add a '터미널' cascade listing saved command launchers. Selecting one
        opens that CLI window, auto-connects (remote) and jumps to the current
        '경로 이동' path, then remembers the choice for the 터미널 icon button
        and shows it in the dialog's top bar."""
        term_menu = tk.Menu(menu, tearoff=0)
        commands = list(getattr(self.manager, "commands", []) or [])
        if not commands:
            term_menu.add_command(label="(등록된 command 없음)", state=tk.DISABLED)
        else:
            for command in commands:
                name = command.get("name") or "(이름 없음)"
                term_menu.add_command(
                    label=name,
                    command=lambda cmd=command, src=source: self.launch_terminal_with_command(cmd, src),
                )
        menu.add_cascade(label="터미널", menu=term_menu)

    def _find_plink_command(self) -> dict | None:
        """CLI 창을 서브메뉴로 선택하지 않은 경우 사용할 기본 launcher.
        저장된 command 목록에서 'plink'가 포함된 항목(이름 또는 실행 파일명)을 찾는다."""
        for command in getattr(self.manager, "commands", []) or []:
            name = str(command.get("name", "")).lower()
            path = str(command.get("path", "")).lower()
            if "plink" in name or "plink" in Path(path).name:
                return command
        return None

    def _resolve_terminal_launcher(self) -> dict | None:
        if self.selected_terminal_command:
            return self.selected_terminal_command
        plink_command = self._find_plink_command()
        if plink_command:
            return plink_command
        return self.manager.selected_command() if hasattr(self.manager, "selected_command") else None

    def launch_terminal_with_command(self, command: dict, source: str) -> None:
        self.selected_terminal_command = command
        self.selected_terminal_var.set(f"터미널: {command.get('name', '')}")
        if source == "remote":
            self.manager.launch_remote(self.profile, command, cwd=self.remote_cwd)
        elif hasattr(self.manager, "launch_command"):
            self.manager.launch_command(command, cwd_override=str(self.local_cwd))

    def show_tree_menu(self, event, source: str) -> None:
        tree = event.widget
        row = tree.identify_row(event.y)
        if row and row not in tree.selection():
            tree.selection_set(row)
        elif not row:
            tree.selection_remove(tree.selection())

        menu = tk.Menu(self, tearoff=0)
        if source == "local":
            view_menu = tk.Menu(menu, tearoff=0)
            view_menu.add_command(label="(default)", command=self.view_local_selected)
            self._add_editor_view_menu_items(view_menu, "local")
            menu.add_command(label="새 폴더", command=self.create_local_folder)
            menu.add_command(label="빈 파일", command=self.create_local_empty_file)
            menu.add_cascade(label="파일 보기", menu=view_menu)
            menu.add_command(label="비교", command=self.compare_selected_files)
            menu.add_command(label="로그 보기", command=self.view_local_log)
            menu.add_command(label="로그 보기(색상)", command=self.view_local_log_color)
            menu.add_command(label="파일명 변경", command=self.rename_local_selected)
            menu.add_command(label="삭제", command=self.delete_local_selected)
            menu.add_separator()
            self._add_terminal_menu(menu, "local")
        else:
            view_menu = tk.Menu(menu, tearoff=0)
            view_menu.add_command(label="(default)", command=self.view_remote_selected)
            self._add_editor_view_menu_items(view_menu, "remote")
            menu.add_command(label="새 폴더", command=self.create_remote_folder)
            menu.add_command(label="빈 파일", command=self.create_remote_empty_file)
            menu.add_cascade(label="파일 보기", menu=view_menu)
            menu.add_command(label="비교", command=self.compare_selected_files)
            menu.add_command(label="편집", command=self.edit_remote_with_gvim)
            menu.add_command(label="로그 보기", command=self.view_remote_log)
            menu.add_command(label="로그 보기(색상)", command=self.view_remote_log_color)
            menu.add_command(label="파일명 변경", command=self.rename_remote_selected)
            menu.add_command(label="삭제", command=self.delete_remote_selected)
            menu.add_separator()
            self._add_terminal_menu(menu, "remote")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _add_editor_view_menu_items(self, view_menu: tk.Menu, source: str) -> None:
        editors = list(getattr(self.manager, "editors", []))
        if not editors:
            return
        view_menu.add_separator()
        for editor in editors:
            name = editor.get("name") or Path(editor.get("path", "")).stem or "Editor"
            if source == "local":
                view_menu.add_command(
                    label=name,
                    command=lambda item=dict(editor): self.view_local_with_editor(item),
                )
            else:
                view_menu.add_command(
                    label=name,
                    command=lambda item=dict(editor): self.view_remote_with_editor(item),
                )

    def _connect_remote(self) -> None:
        def worker() -> None:
            try:
                import paramiko

                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    hostname=self.profile["host"],
                    port=int(self.profile.get("port") or 22),
                    username=self.profile["username"],
                    password=self.password or None,
                    key_filename=self.key_path or None,
                    look_for_keys=False,
                    allow_agent=False,
                    timeout=15,
                )
                sftp = client.open_sftp()
                with self.sftp_lock:
                    self.client = client
                    self.sftp = sftp
                    self.remote_cwd = sftp.normalize(".")
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: self._remote_connect_failed(detail))
                return
            self.after(0, self.refresh_all)

        threading.Thread(target=worker, daemon=True).start()

    def _remote_connect_failed(self, detail: str) -> None:
        self.status.set(f"원격 서버 연결 실패: {detail}")
        messagebox.showerror("연결 실패", f"원격 서버에 연결할 수 없습니다.\n{detail}", parent=self)

    def refresh_all(self) -> None:
        self.refresh_local()
        self.refresh_remote()

    def refresh_local(self) -> None:
        tree = self._tree_widget(self.local_tree)
        tree.delete(*tree.get_children())
        self.local_path_var.set(str(self.local_cwd))
        parent = self.local_cwd.parent
        if parent != self.local_cwd:
            tree.insert("", "end", iid="local:..", text="..", image=self.folder_icon, values=("폴더", "", ""))
        try:
            entries = sorted(self.local_cwd.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except Exception as exc:
            self.status.set(f"로컬 폴더를 읽을 수 없습니다: {exc}")
            return
        for entry in entries:
            try:
                is_dir = entry.is_dir()
                entry_stat = entry.stat()
                size = None if is_dir else entry_stat.st_size
                modified = format_mtime(entry_stat.st_mtime)
            except OSError:
                continue
            iid = f"local:{entry}"
            tree.insert(
                "",
                "end",
                iid=iid,
                text=entry.name,
                image=self.folder_icon if is_dir else self.file_icon,
                values=("폴더" if is_dir else "파일", human_size(size), modified),
            )

    def refresh_remote(self) -> None:
        if not self.sftp:
            return
        tree = self._tree_widget(self.remote_tree)
        tree.delete(*tree.get_children())
        self.remote_path_var.set(self.remote_cwd)
        if self.remote_cwd not in ("", "/"):
            tree.insert("", "end", iid="remote:..", text="..", image=self.folder_icon, values=("폴더", "", ""))

        def worker() -> None:
            try:
                with self.sftp_lock:
                    entries = self.sftp.listdir_attr(self.remote_cwd)
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: self.status.set(f"원격 폴더를 읽을 수 없습니다: {detail}"))
                return
            rows = []
            for item in sorted(entries, key=lambda e: (not stat.S_ISDIR(e.st_mode), e.filename.lower())):
                is_dir = stat.S_ISDIR(item.st_mode)
                remote_path = posixpath.join(self.remote_cwd, item.filename)
                rows.append((remote_path, item.filename, is_dir, None if is_dir else item.st_size, item.st_mtime))
            self.after(0, lambda: self._fill_remote_rows(rows))

        threading.Thread(target=worker, daemon=True).start()

    def _fill_remote_rows(self, rows: list[tuple[str, str, bool, int | None, int | None]]) -> None:
        tree = self._tree_widget(self.remote_tree)
        for remote_path, name, is_dir, size, modified in rows:
            tree.insert(
                "",
                "end",
                iid=f"remote:{remote_path}",
                text=name,
                image=self.folder_icon if is_dir else self.file_icon,
                values=("폴더" if is_dir else "파일", human_size(size), format_mtime(modified)),
            )
        self.status.set("탐색기 준비 완료. 파일을 반대쪽 목록으로 드래그하세요.")

    def goto_local_path(self) -> None:
        target = Path(self.local_path_var.get()).expanduser()
        if not target.exists() or not target.is_dir():
            messagebox.showerror("이동 실패", "존재하는 로컬 폴더 경로를 입력하세요.", parent=self)
            return
        self.local_cwd = target
        self.refresh_local()

    def add_local_favorite(self) -> None:
        path = str(self.local_cwd)
        self._add_favorite("local_paths", path)
        self.local_favorite_var.set(path)
        self.status.set("로컬 즐겨찾기 경로를 등록했습니다.")

    def use_local_favorite(self, _event=None) -> None:
        path = self.local_favorite_var.get()
        if not path:
            return
        target = Path(path).expanduser()
        if not target.exists() or not target.is_dir():
            messagebox.showerror("이동 실패", "등록된 로컬 경로가 존재하지 않습니다.", parent=self)
            return
        self.local_cwd = target
        self.refresh_local()

    def add_remote_favorite(self) -> None:
        path = self.remote_cwd
        self._add_favorite("remote_paths", path)
        self.remote_favorite_var.set(path)
        self.status.set("원격 즐겨찾기 경로를 등록했습니다.")

    def use_remote_favorite(self, _event=None) -> None:
        path = self.remote_favorite_var.get()
        if path:
            self._set_remote_cwd(path)

    def _add_favorite(self, key: str, path: str) -> None:
        values = self.favorites.setdefault(key, [])
        if path not in values:
            values.append(path)
            values.sort(key=str.lower)
            save_favorites(self.favorites)
        self.local_favorite_combo.configure(values=self.favorites.get("local_paths", []))
        self.remote_favorite_combo.configure(values=self._remote_favorites_sorted())

    def _remote_favorites_sorted(self) -> list[str]:
        """Sort remote favorite paths so the connected account's home comes
        first and other accounts' /home/[user] paths are pushed to the back.
        """
        paths = list(self.favorites.get("remote_paths", []))
        username = self.profile.get("username", "")
        own_home = f"/home/{username}"

        def sort_key(path: str) -> tuple[int, str]:
            normalized = path.rstrip("/") or "/"
            if username and (normalized == own_home or normalized.startswith(own_home + "/")):
                return (0, path.lower())
            if normalized == "/home" or normalized.startswith("/home/"):
                return (2, path.lower())
            return (1, path.lower())

        return sorted(paths, key=sort_key)

    def open_windows_explorer(self) -> None:
        try:
            subprocess.Popen(["explorer.exe", str(self.local_cwd)])
        except Exception as exc:
            messagebox.showerror("Explorer 실행 실패", f"Windows 탐색기를 열 수 없습니다.\n{exc}", parent=self)

    def create_local_folder(self) -> None:
        name = simpledialog.askstring("새 폴더", "생성할 로컬 폴더 이름을 입력하세요.", parent=self)
        if not name:
            return
        target = self.local_cwd / name
        try:
            target.mkdir()
        except Exception as exc:
            messagebox.showerror("생성 실패", f"로컬 폴더를 생성할 수 없습니다.\n{exc}", parent=self)
            return
        self.refresh_local()
        self.status.set("로컬 새 폴더를 생성했습니다.")

    def create_local_empty_file(self) -> None:
        name = simpledialog.askstring("빈 파일", "생성할 로컬 파일 이름을 입력하세요.", parent=self)
        if not name:
            return
        target = self.local_cwd / name
        try:
            target.touch(exist_ok=False)
        except Exception as exc:
            messagebox.showerror("생성 실패", f"로컬 빈 파일을 생성할 수 없습니다.\n{exc}", parent=self)
            return
        self.refresh_local()
        self.status.set("로컬 빈 파일을 생성했습니다.")

    def selected_local_path(self) -> Path | None:
        tree = self._tree_widget(self.local_tree)
        selected = tree.selection()
        if not selected:
            return None
        item = selected[0]
        if item == "local:..":
            return None
        return Path(item.removeprefix("local:"))

    def delete_local_selected(self) -> None:
        target = self.selected_local_path()
        if not target:
            messagebox.showinfo("선택 필요", "삭제할 로컬 항목을 선택하세요.", parent=self)
            return
        if not messagebox.askyesno("삭제 확인", f"{target.name} 항목을 삭제할까요?", parent=self):
            return
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except Exception as exc:
            messagebox.showerror("삭제 실패", f"로컬 항목을 삭제할 수 없습니다.\n{exc}", parent=self)
            return
        self.refresh_local()
        self.status.set("로컬 항목을 삭제했습니다.")

    def view_local_selected(self) -> None:
        target = self.selected_local_path()
        if not target:
            messagebox.showinfo("선택 필요", "볼 로컬 파일을 선택하세요.", parent=self)
            return
        if target.is_dir():
            messagebox.showinfo("파일 보기", "폴더는 파일 보기로 열 수 없습니다.", parent=self)
            return
        try:
            data = target.read_bytes()
        except Exception as exc:
            messagebox.showerror("파일 보기 실패", f"로컬 파일을 읽을 수 없습니다.\n{exc}", parent=self)
            return
        self.show_file_viewer(str(target), data)

    def view_local_with_editor(self, editor: dict) -> None:
        target = self.selected_local_path()
        if not target:
            messagebox.showinfo("선택 필요", "볼 로컬 파일을 선택하세요.", parent=self)
            return
        if target.is_dir():
            messagebox.showinfo("파일 보기", "폴더는 Editor로 열 수 없습니다.", parent=self)
            return
        self.launch_editor(editor, str(target))

    def view_local_log(self) -> None:
        self._view_local_log(color=False)

    def view_local_log_color(self) -> None:
        self._view_local_log(color=True)

    def _view_local_log(self, color: bool) -> None:
        target = self.selected_local_path()
        if not target:
            messagebox.showinfo("선택 필요", "로그를 볼 로컬 파일을 선택하세요.", parent=self)
            return
        if target.is_dir():
            messagebox.showinfo("로그 보기", "폴더는 로그 보기로 열 수 없습니다.", parent=self)
            return
        stop_event, run_event, append_text = self.show_log_viewer(str(target), color=color)

        def worker() -> None:
            try:
                with target.open("rb") as file:
                    file.seek(0, os.SEEK_END)
                    size = file.tell()
                    file.seek(max(0, size - 65536))
                    initial = file.read().decode("utf-8", errors="replace")
                    if initial:
                        append_text(initial)
                    while not stop_event.is_set():
                        if not run_event.is_set():
                            time.sleep(0.2)
                            continue
                        chunk = file.read()
                        if chunk:
                            append_text(chunk.decode("utf-8", errors="replace"))
                        else:
                            time.sleep(0.5)
            except Exception as exc:
                append_text(f"\n[로그 보기 종료: {exc}]\n")

        threading.Thread(target=worker, daemon=True).start()

    def rename_local_selected(self) -> None:
        target = self.selected_local_path()
        if not target:
            messagebox.showinfo("선택 필요", "이름을 변경할 로컬 항목을 선택하세요.", parent=self)
            return
        new_name = simpledialog.askstring("파일명 변경", "새 파일명/폴더명을 입력하세요.", initialvalue=target.name, parent=self)
        if not new_name or new_name == target.name:
            return
        destination = target.with_name(new_name)
        try:
            target.rename(destination)
        except Exception as exc:
            messagebox.showerror("변경 실패", f"로컬 항목 이름을 변경할 수 없습니다.\n{exc}", parent=self)
            return
        self.refresh_local()
        self.status.set("로컬 항목 이름을 변경했습니다.")

    def open_local_terminal(self) -> None:
        launcher = self._resolve_terminal_launcher()
        if launcher:
            self.manager.launch_command(launcher, cwd_override=str(self.local_cwd))
            return
        try:
            subprocess.Popen(["cmd.exe", "/k"], cwd=str(self.local_cwd), creationflags=subprocess.CREATE_NEW_CONSOLE)
        except Exception as exc:
            messagebox.showerror("터미널 실행 실패", f"로컬 터미널을 열 수 없습니다.\n{exc}", parent=self)

    def goto_remote_path(self) -> None:
        if not self.sftp:
            return
        target = self.remote_path_var.get().strip() or "."
        self._set_remote_cwd(target)

    def create_remote_folder(self) -> None:
        name = simpledialog.askstring("새 폴더", "생성할 원격 폴더 이름을 입력하세요.", parent=self)
        if not name:
            return
        remote_path = posixpath.join(self.remote_cwd, name)

        def worker() -> None:
            try:
                with self.sftp_lock:
                    self.sftp.mkdir(remote_path)
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("생성 실패", f"원격 폴더를 생성할 수 없습니다.\n{detail}", parent=self))
                return
            self.after(0, lambda: (self.refresh_remote(), self.status.set("원격 새 폴더를 생성했습니다.")))

        threading.Thread(target=worker, daemon=True).start()

    def create_remote_empty_file(self) -> None:
        name = simpledialog.askstring("빈 파일", "생성할 원격 파일 이름을 입력하세요.", parent=self)
        if not name:
            return
        remote_path = posixpath.join(self.remote_cwd, name)

        def worker() -> None:
            try:
                with self.sftp_lock:
                    with self.sftp.open(remote_path, "ab"):
                        pass
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("생성 실패", f"원격 빈 파일을 생성할 수 없습니다.\n{detail}", parent=self))
                return
            self.after(0, lambda: (self.refresh_remote(), self.status.set("원격 빈 파일을 생성했습니다.")))

        threading.Thread(target=worker, daemon=True).start()

    def selected_remote_item(self) -> tuple[str, bool] | None:
        tree = self._tree_widget(self.remote_tree)
        selected = tree.selection()
        if not selected:
            return None
        item = selected[0]
        if item == "remote:..":
            return None
        kind = tree.set(item, "type")
        if kind not in ("폴더", "파일"):
            # Docker 컨테이너 등, 실제 원격 경로가 아닌 항목은 파일 작업 대상에서 제외한다.
            return None
        return item.removeprefix("remote:"), kind == "폴더"

    def delete_remote_selected(self) -> None:
        selected = self.selected_remote_item()
        if not selected:
            messagebox.showinfo("선택 필요", "삭제할 원격 항목을 선택하세요.", parent=self)
            return
        remote_path, is_dir = selected
        if not messagebox.askyesno("삭제 확인", f"{posixpath.basename(remote_path)} 항목을 삭제할까요?", parent=self):
            return

        def worker() -> None:
            try:
                with self.sftp_lock:
                    if is_dir:
                        self._remove_remote_tree(remote_path)
                    else:
                        self.sftp.remove(remote_path)
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("삭제 실패", f"원격 항목을 삭제할 수 없습니다.\n{detail}", parent=self))
                return
            self.after(0, lambda: (self.refresh_remote(), self.status.set("원격 항목을 삭제했습니다.")))

        threading.Thread(target=worker, daemon=True).start()

    def view_remote_selected(self) -> None:
        selected = self.selected_remote_item()
        if not selected:
            messagebox.showinfo("선택 필요", "볼 원격 파일을 선택하세요.", parent=self)
            return
        remote_path, is_dir = selected
        if is_dir:
            messagebox.showinfo("파일 보기", "폴더는 파일 보기로 열 수 없습니다.", parent=self)
            return

        def worker() -> None:
            try:
                with self.sftp_lock:
                    with self.sftp.open(remote_path, "rb") as remote_file:
                        data = remote_file.read()
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("파일 보기 실패", f"원격 파일을 읽을 수 없습니다.\n{detail}", parent=self))
                return
            self.after(0, lambda: self.show_file_viewer(remote_path, data))

        threading.Thread(target=worker, daemon=True).start()

    def view_remote_with_editor(self, editor: dict) -> None:
        selected = self.selected_remote_item()
        if not selected:
            messagebox.showinfo("선택 필요", "볼 원격 파일을 선택하세요.", parent=self)
            return
        remote_path, is_dir = selected
        if is_dir:
            messagebox.showinfo("파일 보기", "폴더는 Editor로 열 수 없습니다.", parent=self)
            return

        temp_dir = Path(tempfile.gettempdir()) / "CommandManager" / "remote_view" / self.profile["id"]
        local_path = temp_dir / posixpath.basename(remote_path)

        def worker() -> None:
            try:
                temp_dir.mkdir(parents=True, exist_ok=True)
                with self.sftp_lock:
                    self.sftp.get(remote_path, str(local_path))
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("파일 보기 실패", f"원격 파일을 내려받을 수 없습니다.\n{detail}", parent=self))
                return
            self.after(0, lambda: self.launch_editor(editor, str(local_path)))

        threading.Thread(target=worker, daemon=True).start()

    def edit_remote_with_gvim(self) -> None:
        selected = self.selected_remote_item()
        if not selected:
            messagebox.showinfo("선택 필요", "편집할 원격 파일을 선택하세요.", parent=self)
            return
        remote_path, is_dir = selected
        if is_dir:
            messagebox.showinfo("편집", "폴더는 편집으로 열 수 없습니다.", parent=self)
            return
        bash_path = Path(r"C:\Util\cygwin\bin\bash.exe")
        if not bash_path.exists():
            messagebox.showerror(
                "편집 실패",
                f"Cygwin bash 실행 파일을 찾을 수 없습니다.\n{bash_path}",
                parent=self,
            )
            return

        temp_dir = Path(tempfile.gettempdir()) / "CommandManager" / "remote_edit" / self.profile["id"]
        local_path = temp_dir / posixpath.basename(remote_path)
        self.status.set("원격 파일을 편집용으로 내려받는 중...")

        def worker() -> None:
            try:
                temp_dir.mkdir(parents=True, exist_ok=True)
                with self.sftp_lock:
                    remote_size = max(1, self.sftp.stat(remote_path).st_size)

                def download_callback(transferred: int, _total: int) -> None:
                    percent = min(100, int((transferred / remote_size) * 100))
                    self.after(0, lambda value=percent: self.status.set(f"원격 파일을 편집용으로 내려받는 중... ({value}%)"))

                with self.sftp_lock:
                    self.sftp.get(remote_path, str(local_path), callback=download_callback)
                before_stat = local_path.stat()
                before_signature = (before_stat.st_mtime_ns, before_stat.st_size)
                self.after(0, lambda: self.status.set("vim 편집 중... 저장 후 터미널 창을 닫으면 원격에 업로드합니다."))
                vim_command = f"vim -- {shell_quote(cygwin_path(str(local_path)))}"
                process = subprocess.Popen(
                    [str(bash_path), "--login", "-lc", vim_command],
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
                exit_code = process.wait()
                self.after(0, lambda code=exit_code: self.status.set(f"vim 종료 감지됨. 변경 사항 확인 중... (종료 코드 {code})"))
                if not local_path.exists():
                    self.after(0, lambda: self.status.set("편집 파일이 삭제되어 업로드하지 않았습니다."))
                    return
                after_stat = local_path.stat()
                after_signature = (after_stat.st_mtime_ns, after_stat.st_size)
                if after_signature == before_signature:
                    self.after(0, lambda: self.status.set("변경 사항이 없어 업로드하지 않았습니다."))
                    return
                self.after(0, lambda: self.status.set("수정된 파일을 원격 서버에 업로드하는 중..."))
                with self.sftp_lock:
                    self.sftp.put(str(local_path), remote_path)
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("편집 실패", f"원격 파일 편집/업로드 중 오류가 발생했습니다.\n{detail}", parent=self))
                return
            self.after(0, lambda: (self.refresh_remote(), self.status.set("vim 편집 내용을 원격 파일에 업로드했습니다.")))

        threading.Thread(target=worker, daemon=True).start()

    def compare_selected_files(self) -> None:
        local_path = self.selected_local_path()
        if not local_path or local_path.is_dir():
            messagebox.showinfo("파일 비교", "로컬 파일 탐색기에서 비교할 파일 하나를 선택하세요.", parent=self)
            return
        remote_selected = self.selected_remote_item()
        if not remote_selected or remote_selected[1]:
            messagebox.showinfo("파일 비교", "원격 파일 탐색기에서 비교할 파일 하나를 선택하세요.", parent=self)
            return
        remote_path, _is_dir = remote_selected

        bash_path = Path(r"C:\Util\cygwin\bin\bash.exe")
        if not bash_path.exists():
            messagebox.showerror(
                "비교 실패",
                f"Cygwin bash 실행 파일을 찾을 수 없습니다.\n{bash_path}",
                parent=self,
            )
            return

        temp_dir = Path(tempfile.gettempdir()) / "CommandManager" / "remote_compare" / self.profile["id"]
        remote_local_copy = temp_dir / posixpath.basename(remote_path)
        self.status.set("원격 파일을 비교용으로 내려받는 중...")

        def worker() -> None:
            try:
                temp_dir.mkdir(parents=True, exist_ok=True)
                with self.sftp_lock:
                    self.sftp.get(remote_path, str(remote_local_copy))
            except Exception as exc:
                detail = str(exc)
                self.after(
                    0,
                    lambda: messagebox.showerror("비교 실패", f"원격 파일을 내려받을 수 없습니다.\n{detail}", parent=self),
                )
                return
            self.after(0, lambda: self._launch_vimdiff(bash_path, local_path, remote_local_copy))

        threading.Thread(target=worker, daemon=True).start()

    def _launch_vimdiff(self, bash_path: Path, local_path: Path, remote_local_copy: Path) -> None:
        vimdiff_command = (
            f"vimdiff -- {shell_quote(cygwin_path(str(local_path)))} "
            f"{shell_quote(cygwin_path(str(remote_local_copy)))}"
        )
        try:
            subprocess.Popen(
                [str(bash_path), "--login", "-lc", vimdiff_command],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        except Exception as exc:
            messagebox.showerror("비교 실패", f"vimdiff를 실행할 수 없습니다.\n{exc}", parent=self)
            return
        self.status.set(f"{local_path.name} \u2194 {remote_local_copy.name} vimdiff 비교 창을 열었습니다.")

    def view_remote_log(self) -> None:
        self._view_remote_log(color=False)

    def view_remote_log_color(self) -> None:
        self._view_remote_log(color=True)

    def view_today_remote_log(self) -> None:
        selected = self.today_remote_log_item()
        if not selected:
            messagebox.showinfo("선택 필요", "로그를 볼 원격 파일을 선택하세요.", parent=self)
            return
        self.open_remote_log(selected[0], color=False)

    def _view_remote_log(self, color: bool) -> None:
        selected = self.selected_remote_item()
        if not selected:
            selected = self.today_remote_log_item()
            if not selected:
                messagebox.showinfo("선택 필요", "로그를 볼 원격 파일을 선택하세요.", parent=self)
                return
        remote_path, is_dir = selected
        if is_dir:
            messagebox.showinfo("로그 보기", "폴더는 로그 보기로 열 수 없습니다.", parent=self)
            return
        self.open_remote_log(remote_path, color=color)

    def open_remote_log(self, remote_path: str, color: bool = False) -> None:
        stop_event, run_event, append_text = self.show_log_viewer(remote_path, color=color)

        def worker() -> None:
            channel = None
            try:
                command = f"tail -n 200 -F -- {shlex.quote(remote_path)}"
                stdin, stdout, stderr = self.client.exec_command(command, get_pty=True)
                stdin.close()
                channel = stdout.channel
                while not stop_event.is_set():
                    if not run_event.is_set():
                        time.sleep(0.2)
                        continue
                    if channel.recv_ready():
                        data = channel.recv(4096)
                        if not data:
                            break
                        append_text(data.decode("utf-8", errors="replace"))
                    elif channel.exit_status_ready():
                        break
                    else:
                        time.sleep(0.2)
                if not stop_event.is_set() and stderr.channel.recv_stderr_ready():
                    error_data = stderr.read().decode("utf-8", errors="replace")
                    if error_data:
                        append_text(f"\n{error_data}\n")
            except Exception as exc:
                append_text(f"\n[로그 보기 종료: {exc}]\n")
            finally:
                if channel:
                    channel.close()

        threading.Thread(target=worker, daemon=True).start()

    def today_remote_log_item(self) -> tuple[str, bool] | None:
        if not self.sftp:
            return None
        today_name = datetime.now().strftime("%Y-%m-%d.log")
        remote_path = posixpath.join(self.remote_cwd, today_name)
        try:
            with self.sftp_lock:
                attrs = self.sftp.stat(remote_path)
        except Exception:
            return None
        if stat.S_ISDIR(attrs.st_mode):
            return None
        return remote_path, False

    def rename_remote_selected(self) -> None:
        selected = self.selected_remote_item()
        if not selected:
            messagebox.showinfo("선택 필요", "이름을 변경할 원격 항목을 선택하세요.", parent=self)
            return
        remote_path, _is_dir = selected
        current_name = posixpath.basename(remote_path)
        new_name = simpledialog.askstring("파일명 변경", "새 파일명/폴더명을 입력하세요.", initialvalue=current_name, parent=self)
        if not new_name or new_name == current_name:
            return
        destination = posixpath.join(posixpath.dirname(remote_path), new_name)

        def worker() -> None:
            try:
                with self.sftp_lock:
                    self.sftp.rename(remote_path, destination)
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("변경 실패", f"원격 항목 이름을 변경할 수 없습니다.\n{detail}", parent=self))
                return
            self.after(0, lambda: (self.refresh_remote(), self.status.set("원격 항목 이름을 변경했습니다.")))

        threading.Thread(target=worker, daemon=True).start()

    def _remove_remote_tree(self, remote_path: str) -> None:
        for item in self.sftp.listdir_attr(remote_path):
            child = posixpath.join(remote_path, item.filename)
            if stat.S_ISDIR(item.st_mode):
                self._remove_remote_tree(child)
            else:
                self.sftp.remove(child)
        self.sftp.rmdir(remote_path)

    def open_remote_terminal(self) -> None:
        launcher = self._resolve_terminal_launcher()
        self.manager.launch_remote(self.profile, launcher, cwd=self.remote_cwd)

    def capture_remote_screenshot(self) -> None:
        default_name = f"remote_screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        local_path = filedialog.asksaveasfilename(
            parent=self,
            title="원격 스크린샷 저장",
            initialdir=str(self.local_cwd),
            initialfile=default_name,
            defaultextension=".png",
            filetypes=[("PNG 이미지", "*.png"), ("모든 파일", "*.*")],
        )
        if not local_path:
            return
        self.status.set("원격 스크린샷을 캡처하는 중...")

        def worker() -> None:
            try:
                with self.sftp_lock:
                    stdin, stdout, stderr = self.client.exec_command("DISPLAY=:0 scrot -o /dev/stdout")
                    stdin.close()
                    image_data = stdout.read()
                    error_data = stderr.read().decode("utf-8", errors="replace").strip()
                    exit_code = stdout.channel.recv_exit_status()
                if exit_code != 0 or not image_data:
                    raise RuntimeError(error_data or f"scrot 종료 코드: {exit_code}")
                Path(local_path).write_bytes(image_data)
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: self._screenshot_done(False, local_path, detail))
                return
            self.after(0, lambda: self._screenshot_done(True, local_path, ""))

        threading.Thread(target=worker, daemon=True).start()

    def _screenshot_done(self, success: bool, local_path: str, detail: str) -> None:
        if not success:
            self.status.set(f"스크린샷 실패: {detail}")
            messagebox.showerror("스크린샷 실패", f"원격 스크린샷을 저장할 수 없습니다.\n{detail}", parent=self)
            return
        saved_path = Path(local_path)
        if saved_path.parent == self.local_cwd:
            self.refresh_local()
        self.status.set(f"스크린샷 저장 완료: {saved_path}")
        try:
            os.startfile(saved_path)
        except Exception as exc:
            messagebox.showinfo("스크린샷 저장 완료", f"저장 위치:\n{saved_path}\n\n이미지를 열 수 없습니다.\n{exc}", parent=self)

    def show_file_viewer(self, title: str, data: bytes) -> None:
        max_bytes = 1024 * 1024
        truncated = len(data) > max_bytes
        content = data[:max_bytes]
        if b"\x00" in content:
            messagebox.showinfo("파일 보기", "바이너리 파일로 보여 텍스트 보기로 열 수 없습니다.", parent=self)
            return
        text = content.decode("utf-8", errors="replace")
        if truncated:
            text += "\n\n[파일이 커서 처음 1MB만 표시합니다.]"

        window = tk.Toplevel(self)
        window.title(f"파일 보기 - {title}")
        window.geometry("900x640")
        window.minsize(640, 420)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)

        frame = ttk.Frame(window, padding=8)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        text_widget = tk.Text(frame, wrap="none", font=("Consolas", 10))
        yscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text_widget.yview)
        xscroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=text_widget.xview)
        text_widget.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        text_widget.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        text_widget.insert("1.0", text)
        text_widget.configure(state="disabled")

    def launch_editor(self, editor: dict, file_path: str) -> None:
        path = editor.get("path", "")
        if not path or not Path(path).exists():
            messagebox.showerror("Editor 실행 실패", "설정한 Editor 실행 파일 경로가 존재하지 않습니다.", parent=self)
            return
        workdir = editor.get("workdir") or str(Path(path).parent)
        if workdir and not Path(workdir).is_dir():
            messagebox.showerror("Editor 실행 실패", "설정한 Editor 작업 폴더가 존재하지 않습니다.", parent=self)
            return
        try:
            args = shlex.split(editor.get("args", ""), posix=False)
            subprocess.Popen([path, *args, file_path], cwd=workdir or None)
        except Exception as exc:
            messagebox.showerror("Editor 실행 실패", f"Editor로 파일을 열 수 없습니다.\n{exc}", parent=self)
            return
        self.status.set(f"{editor.get('name', Path(path).name)} Editor로 파일을 열었습니다.")

    def view_clipboard(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("클립보드 보기", "클립보드에 표시할 텍스트가 없습니다.", parent=self)
            return
        if not text:
            messagebox.showinfo("클립보드 보기", "클립보드가 비어 있습니다.", parent=self)
            return
        self.show_file_viewer("클립보드", text.encode("utf-8", errors="replace"))

    def view_remote_clipboard(self) -> None:
        self.status.set("원격 클립보드를 읽는 중...")

        def worker() -> None:
            command = (
                "if command -v xclip >/dev/null 2>&1; then "
                "DISPLAY=${DISPLAY:-:0} XAUTHORITY=${XAUTHORITY:-$HOME/.Xauthority} "
                "xclip -selection clipboard -o; "
                "elif command -v xsel >/dev/null 2>&1; then "
                "DISPLAY=${DISPLAY:-:0} XAUTHORITY=${XAUTHORITY:-$HOME/.Xauthority} "
                "xsel -b -o; "
                "elif command -v wl-paste >/dev/null 2>&1; then "
                "XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)} "
                "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-0} "
                "wl-paste; "
                "else "
                "echo '원격 클립보드 도구(xclip, xsel, wl-paste)를 찾을 수 없습니다.' >&2; exit 127; "
                "fi"
            )
            try:
                stdin, stdout, stderr = self.client.exec_command(command)
                stdin.close()
                data = stdout.read()
                error = stderr.read().decode("utf-8", errors="replace").strip()
                exit_code = stdout.channel.recv_exit_status()
                if exit_code != 0:
                    raise RuntimeError(error or f"원격 클립보드 명령 종료 코드: {exit_code}")
                text = data.decode("utf-8", errors="replace")
                if not text:
                    raise RuntimeError("원격 클립보드가 비어 있습니다.")
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: self._remote_clipboard_done(False, "", detail))
                return
            self.after(0, lambda: self._remote_clipboard_done(True, text, ""))

        threading.Thread(target=worker, daemon=True).start()

    def _remote_clipboard_done(self, success: bool, text: str, detail: str) -> None:
        if not success:
            self.status.set(f"원격 클립보드 읽기 실패: {detail}")
            messagebox.showerror("원격 클립보드 보기 실패", detail, parent=self)
            return
        self.status.set("원격 클립보드를 읽었습니다.")
        self.show_file_viewer("원격 클립보드", text.encode("utf-8", errors="replace"))

    def show_log_viewer(self, title: str, color: bool = False):
        stop_event = threading.Event()
        run_event = threading.Event()
        run_event.set()
        window = tk.Toplevel(self)
        window.title(f"로그 보기{'(색상)' if color else ''} - {title}")
        window.geometry("900x640")
        window.minsize(640, 420)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(window, padding=(8, 8, 8, 0))
        toolbar.grid(row=0, column=0, sticky="ew")
        state_var = tk.StringVar(value="실행 중")

        def start_log() -> None:
            run_event.set()
            state_var.set("실행 중")
            start_button.configure(state=tk.DISABLED)
            stop_button.configure(state=tk.NORMAL)

        def stop_log() -> None:
            run_event.clear()
            state_var.set("중지")
            start_button.configure(state=tk.NORMAL)
            stop_button.configure(state=tk.DISABLED)

        start_button = ttk.Button(toolbar, text="실행", command=start_log, state=tk.DISABLED)
        start_button.grid(row=0, column=0, padx=(0, 6))
        stop_button = ttk.Button(toolbar, text="중지", command=stop_log)
        stop_button.grid(row=0, column=1, padx=(0, 10))

        def clear_log() -> None:
            text_widget.delete("1.0", "end")
            if hasattr(text_widget, "_ansi_buffer"):
                text_widget._ansi_buffer = ""
            if hasattr(text_widget, "_ansi_fg"):
                text_widget._ansi_fg = None
            if hasattr(text_widget, "_ansi_bg"):
                text_widget._ansi_bg = None

        clear_button = ttk.Button(toolbar, text="지우기", command=clear_log)
        clear_button.grid(row=0, column=2, padx=(0, 10))
        ttk.Label(toolbar, textvariable=state_var).grid(row=0, column=3, sticky="w")

        frame = ttk.Frame(window, padding=8)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        text_widget = tk.Text(frame, wrap="none", font=("Consolas", 10))
        if color:
            self.configure_ansi_tags(text_widget)
        yscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text_widget.yview)
        xscroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=text_widget.xview)
        text_widget.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        text_widget.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")

        def append_text(value: str) -> None:
            def update() -> None:
                if not text_widget.winfo_exists():
                    return
                if color:
                    self.insert_ansi_text(text_widget, value)
                else:
                    text_widget.insert("end", value)
                text_widget.see("end")

            self.after(0, update)

        def close() -> None:
            stop_event.set()
            run_event.set()
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", close)
        return stop_event, run_event, append_text

    def configure_ansi_tags(self, text_widget: tk.Text) -> None:
        colors = {
            "black": "#111111",
            "red": "#d70000",
            "green": "#008700",
            "yellow": "#af8700",
            "blue": "#005faf",
            "magenta": "#af00af",
            "cyan": "#0087af",
            "white": "#e4e4e4",
            "bright_black": "#666666",
            "bright_red": "#ff5f5f",
            "bright_green": "#5fff5f",
            "bright_yellow": "#ffff5f",
            "bright_blue": "#5fafff",
            "bright_magenta": "#ff5fff",
            "bright_cyan": "#5fffff",
            "bright_white": "#ffffff",
        }
        for name, value in colors.items():
            text_widget.tag_configure(f"fg_{name}", foreground=value)
            text_widget.tag_configure(f"bg_{name}", background=value)

    def insert_ansi_text(self, text_widget: tk.Text, value: str) -> None:
        fg_names = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]
        value = getattr(text_widget, "_ansi_buffer", "") + value
        text_widget._ansi_buffer = ""
        tail = re.search(r"\x1b\[[0-9;]*$", value)
        if tail:
            text_widget._ansi_buffer = value[tail.start():]
            value = value[:tail.start()]

        current_fg: str | None = getattr(text_widget, "_ansi_fg", None)
        current_bg: str | None = getattr(text_widget, "_ansi_bg", None)
        pattern = re.compile(r"\x1b\[([0-9;]*)m")
        position = 0
        for match in pattern.finditer(value):
            if match.start() > position:
                tags = tuple(tag for tag in (current_fg, current_bg) if tag)
                text_widget.insert("end", value[position:match.start()], tags)
            codes = [0] if not match.group(1) else [int(code or 0) for code in match.group(1).split(";")]
            for code in codes:
                if code == 0:
                    current_fg = None
                    current_bg = None
                elif code == 39:
                    current_fg = None
                elif code == 49:
                    current_bg = None
                elif 30 <= code <= 37:
                    current_fg = f"fg_{fg_names[code - 30]}"
                elif 40 <= code <= 47:
                    current_bg = f"bg_{fg_names[code - 40]}"
                elif 90 <= code <= 97:
                    current_fg = f"fg_bright_{fg_names[code - 90]}"
                elif 100 <= code <= 107:
                    current_bg = f"bg_bright_{fg_names[code - 100]}"
            position = match.end()
        if position < len(value):
            tags = tuple(tag for tag in (current_fg, current_bg) if tag)
            text_widget.insert("end", value[position:], tags)
        text_widget._ansi_fg = current_fg
        text_widget._ansi_bg = current_bg

    def open_clicked_folder(self, event, source: str) -> None:
        tree = event.widget
        item = tree.identify_row(event.y)
        if not item:
            return
        if source == "local":
            self.open_local_item(item)
        else:
            self.open_remote_item(item)

    def open_local_item(self, item: str | None = None) -> None:
        tree = self._tree_widget(self.local_tree)
        if item is None:
            selected = tree.selection()
            if not selected:
                return
            item = selected[0]
        if item == "local:..":
            self.local_cwd = self.local_cwd.parent
            self.refresh_local()
            return
        path = Path(item.removeprefix("local:"))
        if path.is_dir():
            self.local_cwd = path
            self.refresh_local()

    def open_remote_item(self, item: str | None = None) -> None:
        tree = self._tree_widget(self.remote_tree)
        if item is None:
            selected = tree.selection()
            if not selected:
                return
            item = selected[0]
        if item == "remote:..":
            parent = posixpath.dirname(self.remote_cwd.rstrip("/")) or "/"
            self._set_remote_cwd(parent)
            return
        remote_path = item.removeprefix("remote:")
        if tree.set(item, "type") == "폴더":
            self._set_remote_cwd(remote_path)

    def _set_remote_cwd(self, target: str) -> None:
        def worker() -> None:
            try:
                with self.sftp_lock:
                    normalized = self.sftp.normalize(target)
                    attrs = self.sftp.stat(normalized)
                if not stat.S_ISDIR(attrs.st_mode):
                    raise NotADirectoryError("원격 경로가 폴더가 아닙니다.")
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("이동 실패", detail, parent=self))
                return
            self.remote_cwd = normalized
            self.after(0, self.refresh_remote)

        threading.Thread(target=worker, daemon=True).start()

    def start_drag(self, event, source: str) -> None:
        tree = event.widget
        item = tree.identify_row(event.y)
        if not item or item.endswith(":..") or tree.set(item, "type") != "파일":
            self.drag_data = None
            return
        selected = list(tree.selection())
        if item not in selected:
            tree.selection_set(item)
            selected = [item]
        file_items = [
            selected_item
            for selected_item in selected
            if not selected_item.endswith(":..") and tree.set(selected_item, "type") == "파일"
        ]
        self.drag_data = {"source": source, "items": file_items}

    def finish_drag(self, event) -> None:
        if not self.drag_data:
            return
        source = self.drag_data["source"]
        local_tree = self._tree_widget(self.local_tree)
        remote_tree = self._tree_widget(self.remote_tree)
        target_tree = self._drop_tree_at_pointer(event.x_root, event.y_root)
        if source == "local" and target_tree == remote_tree:
            remote_folder = self._drop_remote_folder(remote_tree, event.x_root, event.y_root) or self.remote_cwd
            transfers = []
            for item in self.drag_data["items"]:
                local_path = item.removeprefix("local:")
                remote_path = posixpath.join(remote_folder, Path(local_path).name)
                transfers.append((local_path, remote_path))
            self.confirm_transfers("upload", transfers)
        elif source == "remote" and target_tree == local_tree:
            local_folder = self._drop_local_folder(local_tree, event.x_root, event.y_root) or self.local_cwd
            transfers = []
            for item in self.drag_data["items"]:
                remote_path = item.removeprefix("remote:")
                local_path = str(local_folder / posixpath.basename(remote_path))
                transfers.append((local_path, remote_path))
            self.confirm_transfers("download", transfers)
        elif source == "local" and target_tree == local_tree:
            local_folder = self._drop_local_folder(local_tree, event.x_root, event.y_root)
            if local_folder:
                self.move_local_files_to_folder(local_folder)
        elif source == "remote" and target_tree == remote_tree:
            remote_folder = self._drop_remote_folder(remote_tree, event.x_root, event.y_root)
            if remote_folder:
                self.move_remote_files_to_folder(remote_folder)
        self.drag_data = None

    def _drop_tree_at_pointer(self, x_root: int, y_root: int) -> ttk.Treeview | None:
        widget = self.winfo_containing(x_root, y_root)
        local_tree = self._tree_widget(self.local_tree)
        remote_tree = self._tree_widget(self.remote_tree)
        while widget:
            if widget in (local_tree, remote_tree):
                return widget
            widget = widget.master
        return None

    def _drop_local_folder(self, tree: ttk.Treeview, x_root: int, y_root: int) -> Path | None:
        row = tree.identify_row(y_root - tree.winfo_rooty())
        if not row or row == "local:.." or tree.set(row, "type") != "폴더":
            return None
        return Path(row.removeprefix("local:"))

    def _drop_remote_folder(self, tree: ttk.Treeview, x_root: int, y_root: int) -> str | None:
        row = tree.identify_row(y_root - tree.winfo_rooty())
        if not row or row == "remote:.." or tree.set(row, "type") != "폴더":
            return None
        return row.removeprefix("remote:")

    def move_local_files_to_folder(self, target_folder: Path) -> None:
        if not self.drag_data:
            return
        files = [Path(item.removeprefix("local:")) for item in self.drag_data["items"]]
        files = [path for path in files if path.parent != target_folder and target_folder != path]
        if not files:
            return
        if not messagebox.askyesno("파일 이동", f"선택한 파일 {len(files)}개를 {target_folder} 폴더로 이동할까요?", parent=self):
            return
        try:
            for path in files:
                shutil.move(str(path), str(target_folder / path.name))
        except Exception as exc:
            messagebox.showerror("이동 실패", f"로컬 파일을 이동할 수 없습니다.\n{exc}", parent=self)
            return
        self.refresh_local()
        self.status.set("로컬 파일 이동 완료.")

    def move_remote_files_to_folder(self, target_folder: str) -> None:
        if not self.drag_data:
            return
        files = [item.removeprefix("remote:") for item in self.drag_data["items"]]
        files = [path for path in files if posixpath.dirname(path) != target_folder and target_folder != path]
        if not files:
            return
        if not messagebox.askyesno("파일 이동", f"선택한 파일 {len(files)}개를 {target_folder} 폴더로 이동할까요?", parent=self):
            return

        def worker() -> None:
            try:
                with self.sftp_lock:
                    for remote_path in files:
                        self.sftp.rename(remote_path, posixpath.join(target_folder, posixpath.basename(remote_path)))
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("이동 실패", f"원격 파일을 이동할 수 없습니다.\n{detail}", parent=self))
                return
            self.after(0, lambda: (self.refresh_remote(), self.status.set("원격 파일 이동 완료.")))

        threading.Thread(target=worker, daemon=True).start()

    def confirm_transfer(self, mode: str, local_path: str, remote_path: str) -> None:
        self.confirm_transfers(mode, [(local_path, remote_path)])

    def confirm_transfers(self, mode: str, transfers: list[tuple[str, str]]) -> None:
        if not transfers:
            return
        verb = "업로드" if mode == "upload" else "다운로드"
        if len(transfers) == 1:
            local_path, remote_path = transfers[0]
            message = (
                f"{Path(local_path).name} 파일을 업로드할까요?\n\n로컬: {local_path}\n원격: {remote_path}"
                if mode == "upload"
                else f"{posixpath.basename(remote_path)} 파일을 다운로드할까요?\n\n원격: {remote_path}\n로컬: {local_path}"
            )
        else:
            message = f"선택한 파일 {len(transfers)}개를 {verb}할까요?"
        if not messagebox.askyesno(f"파일 {verb}", message, parent=self):
            return
        self.run_transfers(mode, transfers)

    def run_transfer(self, mode: str, local_path: str, remote_path: str) -> None:
        self.run_transfers(mode, [(local_path, remote_path)])

    def run_transfers(self, mode: str, transfers: list[tuple[str, str]]) -> None:
        verb = "업로드" if mode == "upload" else "다운로드"
        self.status.set(f"파일 {verb} 중... ({len(transfers)}개, 0%)")
        self.progress_var.set(0)
        self.progress_text_var.set("0%")

        def update_progress(percent: float) -> None:
            rounded = int(round(percent))
            self.progress_var.set(percent)
            self.progress_text_var.set(f"{rounded}%")
            self.status.set(f"파일 {verb} 중... ({len(transfers)}개, {rounded}%)")

        def worker() -> None:
            try:
                with self.sftp_lock:
                    file_sizes = []
                    for local_path, remote_path in transfers:
                        if mode == "upload":
                            file_sizes.append(Path(local_path).stat().st_size)
                        else:
                            file_sizes.append(self.sftp.stat(remote_path).st_size)
                    total_size = max(1, sum(file_sizes))
                    completed_size = 0
                    for file_index, (local_path, remote_path) in enumerate(transfers):

                        def callback(transferred: int, _total: int, base: int = completed_size) -> None:
                            percent = min(100, ((base + transferred) / total_size) * 100)
                            self.after(0, lambda value=percent: update_progress(value))

                        if mode == "upload":
                            self.sftp.put(local_path, remote_path, callback=callback)
                        else:
                            self.sftp.get(remote_path, local_path, callback=callback)
                        completed_size += file_sizes[file_index]
                        percent = min(100, (completed_size / total_size) * 100)
                        self.after(0, lambda value=percent: update_progress(value))
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: self._transfer_done(False, verb, detail, mode, transfers))
                return
            self.after(0, lambda: self._transfer_done(True, verb, "", mode, transfers))

        threading.Thread(target=worker, daemon=True).start()

    def _transfer_done(
        self,
        success: bool,
        verb: str,
        detail: str,
        mode: str = "",
        transfers: list[tuple[str, str]] | None = None,
    ) -> None:
        if success:
            self.progress_var.set(100)
            self.progress_text_var.set("100%")
            self.status.set(f"파일 {verb} 완료. (100%)")
            self.refresh_all()
            if transfers:
                self._show_transfer_notification(mode, transfers)
        else:
            self.progress_var.set(0)
            self.progress_text_var.set("0%")
            self.status.set(f"파일 {verb} 실패: {detail}")
            messagebox.showerror("전송 실패", f"파일 {verb}에 실패했습니다.\n{detail}", parent=self)

    def open_transfer_location(self, local_path: str) -> None:
        try:
            subprocess.Popen(["explorer.exe", f"/select,{local_path}"])
        except Exception as exc:
            messagebox.showerror("탐색기 실행 실패", f"Windows 탐색기를 열 수 없습니다.\n{exc}", parent=self)

    def grant_remote_execute_permission(self, remote_path: str) -> None:
        if not self.client:
            messagebox.showerror("실행 권한 부여 실패", "원격 서버에 연결되어 있지 않습니다.", parent=self)
            return
        name = posixpath.basename(remote_path)
        self.status.set(f"{name} 실행 권한을 부여하는 중...")

        def worker() -> None:
            try:
                command = f"chmod +x {shlex.quote(remote_path)}"
                stdin, stdout, stderr = self.client.exec_command(command)
                stdin.close()
                error = stderr.read().decode("utf-8", errors="replace").strip()
                exit_code = stdout.channel.recv_exit_status()
                if exit_code != 0:
                    raise RuntimeError(error or f"chmod 종료 코드: {exit_code}")
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: self._grant_execute_done(False, name, detail))
                return
            self.after(0, lambda: self._grant_execute_done(True, name, ""))

        threading.Thread(target=worker, daemon=True).start()

    def _grant_execute_done(self, success: bool, name: str, detail: str) -> None:
        if success:
            self.status.set(f"{name} 실행 권한을 부여했습니다. (chmod +x)")
            self.refresh_remote()
        else:
            self.status.set(f"실행 권한 부여 실패: {detail}")
            messagebox.showerror("실행 권한 부여 실패", f"chmod +x 실행에 실패했습니다.\n{detail}", parent=self)

    def run_remote_executable(self, remote_path: str) -> None:
        if not self.client:
            messagebox.showerror("실행 실패", "원격 서버에 연결되어 있지 않습니다.", parent=self)
            return
        remote_dir = posixpath.dirname(remote_path) or "."
        filename = posixpath.basename(remote_path)
        stop_event, run_event, append_text = self.show_log_viewer(f"실행 - {filename}", color=False)

        def worker() -> None:
            channel = None
            try:
                command = f"cd {shlex.quote(remote_dir)} && ./{shlex.quote(filename)}"
                stdin, stdout, stderr = self.client.exec_command(command, get_pty=True)
                stdin.close()
                channel = stdout.channel
                while not stop_event.is_set():
                    if not run_event.is_set():
                        time.sleep(0.2)
                        continue
                    if channel.recv_ready():
                        data = channel.recv(4096)
                        if not data:
                            break
                        append_text(data.decode("utf-8", errors="replace"))
                    elif channel.exit_status_ready():
                        break
                    else:
                        time.sleep(0.2)
                if not stop_event.is_set() and channel.exit_status_ready():
                    append_text(f"\n[종료 코드: {channel.recv_exit_status()}]\n")
            except Exception as exc:
                append_text(f"\n[실행 종료: {exc}]\n")
            finally:
                if channel:
                    channel.close()

        threading.Thread(target=worker, daemon=True).start()

    def _show_transfer_notification(self, mode: str, transfers: list[tuple[str, str]]) -> None:
        verb = "업로드" if mode == "upload" else "다운로드"
        if len(transfers) == 1:
            local_path, remote_path = transfers[0]
            message = f"{Path(local_path).name} 파일 {verb}가 완료되었습니다."
            TransferNotification(self, message, self, local_path, remote_path)
        else:
            message = f"{len(transfers)}개 파일 {verb}가 완료되었습니다."
            TransferNotification(self, message, self, None, None)

    def close(self) -> None:
        try:
            with self.sftp_lock:
                if self.sftp:
                    self.sftp.close()
                if self.client:
                    self.client.close()
        finally:
            self.destroy()


class DockerRunDialog(tk.Toplevel):
    """`docker run -d --name ... -p ... <image>` 실행에 필요한 값을 입력받는 대화상자."""

    def __init__(self, master: tk.Widget, initial: dict | None = None):
        super().__init__(master)
        self.title("Docker 컨테이너 Run")
        self.resizable(False, False)
        self.result: dict | None = None
        initial = initial or {}
        auto_filled = bool(initial.get("auto_filled"))

        self.name_var = tk.StringVar(value=initial.get("name", ""))
        self.image_var = tk.StringVar(value=initial.get("image", ""))
        self.ports_var = tk.StringVar(value=initial.get("ports", ""))
        self.extra_var = tk.StringVar(value=initial.get("extra", ""))
        self.remove_existing_var = tk.BooleanVar(value=True)

        frame = ttk.Frame(self, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)

        row = 0
        if auto_filled:
            hint = ttk.Label(
                frame,
                text=(
                    "아래 값은 선택한 컨테이너의 현재 실행 설정(docker inspect)에서\n"
                    "자동으로 채워졌습니다. 내용을 확인하고 필요하면 직접 수정한 뒤 실행하세요."
                ),
                foreground="#2b6cb0",
                justify="left",
            )
            hint.grid(row=row, column=0, sticky="w", pady=(0, 10))
            row += 1

        ttk.Label(frame, text="컨테이너 이름").grid(row=row, column=0, sticky="w", pady=(0, 2))
        row += 1
        ttk.Entry(frame, textvariable=self.name_var, width=48).grid(row=row, column=0, sticky="ew", pady=(0, 8))
        row += 1

        ttk.Label(frame, text="이미지 (예: blitz-admin-api:v18.6)").grid(row=row, column=0, sticky="w", pady=(0, 2))
        row += 1
        ttk.Entry(frame, textvariable=self.image_var, width=48).grid(row=row, column=0, sticky="ew", pady=(0, 8))
        row += 1

        ttk.Label(
            frame, text="포트 매핑 (쉼표로 구분, 예: 21391:3300, 62000-62003:62000-62003)"
        ).grid(row=row, column=0, sticky="w", pady=(0, 2))
        row += 1
        ttk.Entry(frame, textvariable=self.ports_var, width=48).grid(row=row, column=0, sticky="ew", pady=(0, 8))
        row += 1

        ttk.Label(frame, text="추가 docker run 옵션 (선택, 예: -v /data:/data -e KEY=VALUE)").grid(
            row=row, column=0, sticky="w", pady=(0, 2)
        )
        row += 1
        ttk.Entry(frame, textvariable=self.extra_var, width=48).grid(row=row, column=0, sticky="ew", pady=(0, 8))
        row += 1

        ttk.Checkbutton(
            frame,
            text="동일한 이름의 기존 컨테이너가 있으면 먼저 중지 후 삭제",
            variable=self.remove_existing_var,
        ).grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1

        button_row = ttk.Frame(frame)
        button_row.grid(row=row, column=0, sticky="e")
        ttk.Button(button_row, text="취소", command=self._cancel).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(button_row, text="실행", command=self._confirm).grid(row=0, column=1)

        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Return>", lambda _event: self._confirm())
        self.bind("<Escape>", lambda _event: self._cancel())

    def _confirm(self) -> None:
        name = self.name_var.get().strip()
        image = self.image_var.get().strip()
        if not name or not image:
            messagebox.showerror("입력 필요", "컨테이너 이름과 이미지는 필수 입력값입니다.", parent=self)
            return
        self.result = {
            "name": name,
            "image": image,
            "ports": self.ports_var.get().strip(),
            "extra": self.extra_var.get().strip(),
            "remove_existing": self.remove_existing_var.get(),
        }
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class DockerTransferDialog(FileTransferDialog):
    """파일 전송 탐색기와 동일한 UI를 사용하되, 원격 그리드 목록에 원격 서버의
    Docker 컨테이너 목록(docker ps)을 디렉토리/파일과 함께 표시하는 탐색기."""

    def __init__(self, master: tk.Tk, profile: dict, password: str, key_path: str = ""):
        super().__init__(master, profile, password, key_path)
        self.title(f"Docker 파일 전송 - {profile.get('name', profile.get('host', 'server'))}")
        # None이면 호스트(서버) 목록, dict이면 특정 컨테이너 내부를 탐색 중임을 의미한다.
        # {"id": 컨테이너ID, "name": 표시용 이름, "path": 컨테이너 내부 현재 경로}
        self.container_context: dict | None = None
        self.last_docker_image_tag: str = ""

    def _build_extra_icons(self) -> None:
        self.docker_icon = self._docker_icon()

    def _docker_icon(self) -> tk.PhotoImage:
        image = tk.PhotoImage(width=16, height=16)
        image.put("#f7f7f7", to=(0, 0, 16, 16))
        image.put("#d0d0d0", to=(0, 0, 16, 1))
        image.put("#d0d0d0", to=(0, 15, 16, 16))
        image.put("#d0d0d0", to=(0, 0, 1, 16))
        image.put("#d0d0d0", to=(15, 0, 16, 16))
        # 고래 몸체
        image.put("#2496ed", to=(2, 8, 14, 12))
        image.put("#2496ed", to=(1, 9, 15, 11))
        # 컨테이너 박스들
        image.put("#66c2ff", to=(3, 5, 6, 8))
        image.put("#66c2ff", to=(7, 5, 10, 8))
        image.put("#66c2ff", to=(11, 5, 14, 8))
        image.put("#66c2ff", to=(7, 2, 10, 5))
        # 물결/파도
        image.put("#ffffff", to=(1, 12, 15, 13))
        return image

    def _build_action_icons(self) -> dict[str, tk.PhotoImage]:
        icons = super()._build_action_icons()
        icons["docker_stop"] = self._docker_action_icon("stop")
        icons["docker_build"] = self._docker_action_icon("build")
        icons["docker_run"] = self._docker_action_icon("run")
        icons["docker_history"] = self._docker_action_icon("history")
        return icons

    def _docker_action_icon(self, shape: str) -> tk.PhotoImage:
        image = tk.PhotoImage(width=16, height=16)
        image.put("#f7f7f7", to=(0, 0, 16, 16))
        image.put("#d0d0d0", to=(0, 0, 16, 1))
        image.put("#d0d0d0", to=(0, 15, 16, 16))
        image.put("#d0d0d0", to=(0, 0, 1, 16))
        image.put("#d0d0d0", to=(15, 0, 16, 16))
        if shape == "stop":
            image.put("#c53030", to=(5, 5, 11, 11))
        elif shape == "build":
            image.put("#4a5568", to=(3, 4, 13, 7))  # 망치 머리
            image.put("#8a5a2b", to=(7, 7, 9, 13))  # 망치 손잡이
        elif shape == "run":
            image.put("#2f855a", to=(5, 4, 7, 12))
            image.put("#2f855a", to=(7, 5, 9, 11))
            image.put("#2f855a", to=(9, 6, 11, 10))
            image.put("#2f855a", to=(11, 7, 12, 9))
        elif shape == "history":
            # 시계 모양(테두리 + 시침/분침)으로 탐색 기록(로그) 버튼을 표현한다.
            image.put("#2b6cb0", to=(3, 3, 13, 13))
            image.put("#f7fafc", to=(4, 4, 12, 12))
            image.put("#2b6cb0", to=(7, 5, 9, 8))
            image.put("#2b6cb0", to=(8, 7, 11, 9))
        return image

    def _build_toolbar_buttons(self, toolbar: ttk.Frame, start_column: int = 2) -> None:
        self._icon_button(toolbar, "docker_build", "Docker Build", self.build_docker_image, 0, start_column).grid(
            row=0, column=start_column, padx=(6, 0)
        )
        self._icon_button(
            toolbar,
            "docker_run",
            "Docker 실행/교체",
            self.run_docker_container,
            0,
            start_column + 1,
        ).grid(row=0, column=start_column + 1, padx=(4, 0))

    def _build_remote_pane(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(4, weight=1)
        ttk.Label(parent, text="원격 서버 파일 탐색기", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        self.remote_path_var = tk.StringVar()
        favorite_group = ttk.LabelFrame(parent, text="즐겨찾기")
        favorite_group.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        favorite_group.columnconfigure(0, weight=1)
        self.remote_favorite_combo = ttk.Combobox(
            favorite_group,
            textvariable=self.remote_favorite_var,
            state="readonly",
            values=self._remote_favorites_sorted(),
        )
        self.remote_favorite_combo.grid(row=0, column=0, sticky="ew", padx=(3, 0), pady=3)
        self.remote_favorite_combo.bind("<<ComboboxSelected>>", self.use_remote_favorite)

        path_group = ttk.LabelFrame(parent, text="경로 이동")
        path_group.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        path_group.columnconfigure(0, weight=1)
        self.remote_path_entry = ttk.Entry(path_group, textvariable=self.remote_path_var)
        self.remote_path_entry.grid(row=0, column=0, sticky="ew", padx=(3, 0), pady=3)
        self.remote_path_entry.bind("<Return>", lambda _event: self.goto_remote_path())
        self._icon_button(path_group, "go", "이동", self.goto_remote_path, 0, 1)
        self._icon_button(path_group, "register", "등록", self.add_remote_favorite, 0, 2)

        action_group = ttk.LabelFrame(parent, text="작업")
        action_group.grid(row=3, column=0, sticky="ew", pady=3)
        remote_actions = [
            ("folder", "새 폴더", self.create_remote_folder),
            ("empty", "빈 파일", self.create_remote_empty_file),
            ("view", "파일 보기", self.view_remote_selected),
            ("log", "로그 보기", self.view_remote_log),
            ("today_log", "금일 로그 보기", self.view_today_remote_log),
            ("log_color", "로그 보기(색상)", self.view_remote_log_color),
            ("edit", "편집", self.edit_remote_with_gvim),
            ("rename", "파일명 변경", self.rename_remote_selected),
            ("delete", "삭제", self.delete_remote_selected),
            ("terminal", "터미널", self.open_remote_terminal),
            ("clipboard", "클립보드 보기", self.view_remote_clipboard),
            ("screenshot", "스크린샷", self.capture_remote_screenshot),
            ("docker_stop", "Docker 컨테이너 중지", self.stop_selected_container),
            ("docker_build", "Docker 컨테이너 빌드", self.build_docker_image),
            ("docker_run", "Docker 컨테이너 run", self.run_docker_container),
            ("docker_history", "컨테이너 탐색 기록 보기", lambda: self.view_container_explore_log()),
        ]
        for column, (icon, tooltip, command) in enumerate(remote_actions):
            self._icon_button(action_group, icon, tooltip, command, 0, column)

        self.remote_tree = self._make_tree(parent)
        self.remote_tree.grid(row=4, column=0, sticky="nsew")
        self._bind_tree_events(self.remote_tree, "remote")

    def refresh_remote(self) -> None:
        if not self.client:
            return
        if self.container_context is not None:
            self._refresh_container_listing()
            return
        self._refresh_host_listing()

    def _refresh_host_listing(self) -> None:
        if not self.sftp:
            return
        tree = self._tree_widget(self.remote_tree)
        tree.delete(*tree.get_children())
        self.remote_path_var.set(self.remote_cwd)
        if self.remote_cwd not in ("", "/"):
            tree.insert("", "end", iid="remote:..", text="..", image=self.folder_icon, values=("폴더", "", ""))

        def worker() -> None:
            try:
                with self.sftp_lock:
                    entries = self.sftp.listdir_attr(self.remote_cwd)
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: self.status.set(f"원격 폴더를 읽을 수 없습니다: {detail}"))
                return
            rows = []
            for item in sorted(entries, key=lambda e: (not stat.S_ISDIR(e.st_mode), e.filename.lower())):
                is_dir = stat.S_ISDIR(item.st_mode)
                remote_path = posixpath.join(self.remote_cwd, item.filename)
                rows.append((remote_path, item.filename, is_dir, None if is_dir else item.st_size, item.st_mtime))
            containers, docker_error = self._fetch_docker_ps()
            self.after(0, lambda: self._fill_remote_rows_with_docker(rows, containers, docker_error))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_container_listing(self) -> None:
        tree = self._tree_widget(self.remote_tree)
        tree.delete(*tree.get_children())
        container_id = self.container_context["id"]
        name = self.container_context["name"]
        path = self.container_context["path"]
        self.remote_path_var.set(f"[Docker:{name}] {path}")
        tree.insert("", "end", iid="remote:ctrback:..", text="..", image=self.folder_icon, values=("폴더", "", ""))
        self.status.set(f"{name} 컨테이너 내부 {path} 를 읽는 중...")

        def worker() -> None:
            entries, error = self._list_container_directory(container_id, path)
            self.after(0, lambda: self._fill_container_rows(entries, error))

        threading.Thread(target=worker, daemon=True).start()

    def _list_container_directory(self, container_id: str, path: str) -> tuple[list[dict], str]:
        """컨테이너 내부 디렉터리 목록을 가져온다.

        `ls -la` 출력 텍스트를 정규식으로 파싱하는 방식은 베이스 이미지(alpine/busybox,
        debian 등)마다 컬럼 구성이나 옵션 지원 여부가 달라 일부 컨테이너에서 목록이
        전혀 표시되지 않는 문제가 있었다. 대신 POSIX 셸의 글롭(`.* *`)과 `[ -d ]` 테스트로
        직접 순회하여, 어떤 셸/베이스 이미지에서도 동일하게 동작하도록 만든다.
        각 줄은 "타입\\t크기\\t수정시각(epoch)\\t이름" 형식(탭 구분)으로 출력된다.
        """
        if not self.client:
            return [], ""
        script = (
            'cd -- "$1" 2>/dev/null || exit 1; '
            'for f in .* *; do '
            'case "$f" in .|..) continue;; esac; '
            '[ -e "$f" ] || [ -L "$f" ] || continue; '
            'if [ -d "$f" ]; then t=D; s=""; else t=F; s=$(wc -c < "$f" 2>/dev/null | tr -d " "); fi; '
            'm=$(stat -c %Y -- "$f" 2>/dev/null); '
            'printf "%s\\t%s\\t%s\\t%s\\n" "$t" "$s" "$m" "$f"; '
            'done'
        )
        inner_cmd = f"sh -c {shlex.quote(script)} sh {shlex.quote(path)}"
        command = f"docker exec {shlex.quote(container_id)} {inner_cmd}"
        try:
            data, err, exit_code = self._exec_with_sudo_fallback(command, timeout=15)
        except Exception as exc:
            return [], str(exc)
        if exit_code != 0:
            return [], err or f"디렉터리 조회 실패 (종료 코드 {exit_code})"
        output = data.decode("utf-8", errors="replace")
        entries: list[dict] = []
        for line in output.splitlines():
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            type_flag, size_str, mtime_str, name = parts[0], parts[1], parts[2], "\t".join(parts[3:])
            is_dir = type_flag == "D"
            size = None
            if not is_dir and size_str.strip().isdigit():
                size = int(size_str.strip())
            modified = ""
            if mtime_str.strip().isdigit():
                modified = format_mtime(int(mtime_str.strip()))
            entries.append({"name": name, "is_dir": is_dir, "size": size, "modified": modified})
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return entries, ""

    def _fill_container_rows(self, entries: list[dict], error: str) -> None:
        tree = self._tree_widget(self.remote_tree)
        container_id = self.container_context["id"]
        base_path = self.container_context["path"]
        container_name = self.container_context.get("name", container_id)
        self._append_docker_explore_log(container_id, container_name, base_path, entries, error)
        for entry in entries:
            full_path = posixpath.join(base_path, entry["name"])
            tree.insert(
                "",
                "end",
                iid=f"remote:ctrpath:{container_id}\x1f{full_path}",
                text=entry["name"],
                image=self.folder_icon if entry["is_dir"] else self.file_icon,
                values=(
                    "컨테이너 폴더" if entry["is_dir"] else "컨테이너 파일",
                    "" if entry["is_dir"] else human_size(entry["size"]),
                    entry["modified"],
                ),
            )
        if error:
            self.status.set(f"컨테이너 내부 목록을 가져오지 못했습니다: {error}")
        else:
            self.status.set(f"{self.container_context['name']} 컨테이너 내부: {self.container_context['path']}")

    def _exec_with_sudo_fallback(self, command: str, timeout: float | None = None) -> tuple[bytes, str, int]:
        """주어진 원격 명령을 실행하고, 'permission denied'로 실패하면
        `sudo -n`(비밀번호 없는 sudo)으로 한 번 더 시도한다.
        서버에 비밀번호 없는 sudo 권한이 설정되어 있지 않으면 sudo 재시도도
        실패하며, 이 경우 최초 오류 메시지를 그대로 반환한다.
        Returns (stdout_bytes, stderr_text, exit_code)."""
        with self.sftp_lock:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
            stdin.close()
            data = stdout.read()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0 and "permission denied" in err.lower():
            sudo_command = f"sudo -n {command}"
            with self.sftp_lock:
                stdin, stdout, stderr = self.client.exec_command(sudo_command, timeout=timeout)
                stdin.close()
                sudo_data = stdout.read()
                sudo_err = stderr.read().decode("utf-8", errors="replace").strip()
                sudo_exit_code = stdout.channel.recv_exit_status()
            if sudo_exit_code == 0:
                return sudo_data, "", 0
            combined_err = err
            if sudo_err:
                combined_err = f"{err} (sudo 재시도 실패: {sudo_err})"
            return data, combined_err, exit_code
        return data, err, exit_code

    def _fetch_docker_ps(self) -> tuple[list[dict], str]:
        """원격 서버에서 `docker ps` 결과를 가져온다. 실패해도 파일/디렉토리
        목록 표시는 계속되도록 예외를 삼키고 오류 메시지만 반환한다."""
        if not self.client:
            return [], ""
        command = (
            "docker ps --format '{{.ID}}\\t{{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}'"
        )
        try:
            data, error_output, exit_code = self._exec_with_sudo_fallback(command, timeout=10)
        except Exception as exc:
            return [], str(exc)
        if exit_code != 0:
            return [], error_output or f"docker ps 종료 코드: {exit_code}"
        output = data.decode("utf-8", errors="replace")
        containers = []
        for line in output.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            parts += [""] * (5 - len(parts))
            container_id, name, image, status, ports = parts[:5]
            containers.append(
                {
                    "id": container_id,
                    "name": name or container_id,
                    "image": image,
                    "status": status,
                    "ports": ports,
                }
            )
        return containers, ""

    def _fill_remote_rows_with_docker(
        self,
        rows: list[tuple[str, str, bool, int | None, int | None]],
        containers: list[dict],
        docker_error: str,
    ) -> None:
        tree = self._tree_widget(self.remote_tree)
        for container in containers:
            tree.insert(
                "",
                "end",
                iid=f"remote:docker:{container['id']}",
                text=container["name"],
                image=self.docker_icon,
                values=("컨테이너", container["image"], container["status"]),
            )
        for remote_path, name, is_dir, size, modified in rows:
            tree.insert(
                "",
                "end",
                iid=f"remote:{remote_path}",
                text=name,
                image=self.folder_icon if is_dir else self.file_icon,
                values=("폴더" if is_dir else "파일", human_size(size), format_mtime(modified)),
            )
        if docker_error:
            self.status.set(f"탐색기 준비 완료 (Docker 목록을 가져오지 못했습니다: {docker_error})")
        else:
            self.status.set("탐색기 준비 완료. 파일을 반대쪽 목록으로 드래그하세요.")

    def _container_id_from_item(self, item: str) -> str | None:
        prefix = "remote:docker:"
        if not item.startswith(prefix):
            return None
        return item[len(prefix):]

    def open_remote_item(self, item: str | None = None) -> None:
        tree = self._tree_widget(self.remote_tree)
        if item is None:
            selected = tree.selection()
            if not selected:
                return
            item = selected[0]
        if not tree.exists(item):
            return
        if item == "remote:ctrback:..":
            self._exit_or_go_up_container()
            return
        kind = tree.set(item, "type")
        if kind == "컨테이너":
            self._enter_container(item)
            return
        if kind == "컨테이너 폴더":
            self._enter_container_path(item)
            return
        if kind == "컨테이너 파일":
            self._view_container_file(item)
            return
        super().open_remote_item(item)

    def goto_remote_path(self) -> None:
        if self.container_context is not None:
            typed = self.remote_path_var.get().strip()
            if not typed:
                return
            # "[Docker:이름] /path" 형태로 표시되므로 접두어가 있으면 제거하고 경로만 사용한다.
            marker = "] "
            marker_index = typed.find(marker)
            path = typed[marker_index + len(marker):] if typed.startswith("[Docker:") and marker_index != -1 else typed
            if not path.startswith("/"):
                path = "/" + path
            self.container_context["path"] = posixpath.normpath(path) or "/"
            self.refresh_remote()
            return
        super().goto_remote_path()

    def _enter_container(self, item: str) -> None:
        container_id = self._container_id_from_item(item)
        if not container_id:
            return
        name = self._tree_widget(self.remote_tree).item(item, "text")
        self.status.set(f"{name} 컨테이너 진입 준비 중 (WORKDIR 확인)...")

        def worker() -> None:
            workdir = self._fetch_container_workdir(container_id)
            self.after(0, lambda: self._enter_container_at(container_id, name, workdir))

        threading.Thread(target=worker, daemon=True).start()

    def _fetch_container_workdir(self, container_id: str) -> str:
        """컨테이너 이미지에 설정된 WORKDIR을 조회한다.
        (docker exec -it <id> /bin/sh 로 접속했을 때 셸이 시작되는 경로와 동일)
        조회에 실패하거나 값이 비어 있으면 컨테이너 루트('/')를 기본값으로 사용한다."""
        if not self.client:
            return "/"
        command = "docker inspect --format '{{.Config.WorkingDir}}' " + shlex.quote(container_id)
        try:
            data, _err, exit_code = self._exec_with_sudo_fallback(command, timeout=15)
        except Exception:
            return "/"
        if exit_code != 0:
            return "/"
        workdir = data.decode("utf-8", errors="replace").strip()
        if not workdir:
            return "/"
        if not workdir.startswith("/"):
            workdir = "/" + workdir
        return posixpath.normpath(workdir) or "/"

    def _enter_container_at(self, container_id: str, name: str, path: str) -> None:
        self.container_context = {"id": container_id, "name": name, "path": path}
        self.refresh_remote()

    def _enter_container_path(self, item: str) -> None:
        full_path = self._container_full_path_from_item(item)
        if full_path is None or not self.container_context:
            return
        self.container_context["path"] = full_path
        self.refresh_remote()

    def _exit_or_go_up_container(self) -> None:
        if not self.container_context:
            self.refresh_remote()
            return
        current = self.container_context["path"]
        if current in ("/", ""):
            # 컨테이너 최상위 경로에서 다시 ".."을 누르면 컨테이너 목록(호스트 화면)으로 돌아간다.
            self.container_context = None
        else:
            parent = posixpath.dirname(current.rstrip("/")) or "/"
            self.container_context["path"] = parent
        self.refresh_remote()

    def _view_container_file(self, item: str) -> None:
        full_path = self._container_full_path_from_item(item)
        if full_path is None or not self.container_context:
            return
        container_id = self.container_context["id"]
        name = self._tree_widget(self.remote_tree).item(item, "text")
        self.status.set(f"{name} 파일을 읽는 중...")

        def worker() -> None:
            try:
                inner_cmd = f"cat -- {shlex.quote(full_path)}"
                command = f"docker exec {shlex.quote(container_id)} sh -c {shlex.quote(inner_cmd)}"
                data, err, exit_code = self._exec_with_sudo_fallback(command, timeout=15)
                if exit_code != 0:
                    raise RuntimeError(err or f"cat 종료 코드: {exit_code}")
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: self._container_action_failed("컨테이너 파일 보기", detail))
                return
            self.after(
                0,
                lambda: (
                    self.status.set(f"{self.container_context['name']} 컨테이너 내부: {self.container_context['path']}"),
                    self.show_file_viewer(f"{name} (컨테이너 내부)", data),
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _container_full_path_from_item(self, item: str) -> str | None:
        prefix = "remote:ctrpath:"
        if not item.startswith(prefix):
            return None
        remainder = item[len(prefix):]
        try:
            _container_id, path = remainder.split("\x1f", 1)
        except ValueError:
            return None
        return path

    def _selected_container_item(self) -> str | None:
        tree = self._tree_widget(self.remote_tree)
        selected = tree.selection()
        if not selected:
            return None
        item = selected[0]
        if self._container_full_path_from_item(item) is None:
            return None
        return item

    def delete_container_selected(self, item: str | None = None) -> None:
        """컨테이너 내부 파일/폴더를 docker exec 로 삭제한다. (rm -rf)"""
        if item is None:
            item = self._selected_container_item()
        if not item:
            messagebox.showinfo("선택 필요", "삭제할 컨테이너 내부 항목을 선택하세요.", parent=self)
            return
        full_path = self._container_full_path_from_item(item)
        if full_path is None or not self.container_context:
            return
        container_id = self.container_context["id"]
        tree = self._tree_widget(self.remote_tree)
        name = tree.item(item, "text")
        if not messagebox.askyesno(
            "삭제 확인",
            f"'{name}' 항목을 컨테이너 내부에서 삭제할까요?\n(docker exec ... rm -rf {full_path})",
            parent=self,
        ):
            return
        self.status.set(f"{name} 항목을 컨테이너에서 삭제하는 중...")

        def worker() -> None:
            try:
                inner_cmd = f"rm -rf -- {shlex.quote(full_path)}"
                command = f"docker exec {shlex.quote(container_id)} sh -c {shlex.quote(inner_cmd)}"
                self._run_remote_ok(command, timeout=30)
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: self._container_action_failed("컨테이너 항목 삭제", detail))
                return
            self.after(0, lambda: (self.refresh_remote(), self.status.set(f"{name} 항목을 삭제했습니다.")))

        threading.Thread(target=worker, daemon=True).start()

    def rename_container_selected(self, item: str | None = None) -> None:
        """컨테이너 내부 파일/폴더 이름을 docker exec 로 변경한다. (mv)"""
        if item is None:
            item = self._selected_container_item()
        if not item:
            messagebox.showinfo("선택 필요", "이름을 변경할 컨테이너 내부 항목을 선택하세요.", parent=self)
            return
        full_path = self._container_full_path_from_item(item)
        if full_path is None or not self.container_context:
            return
        container_id = self.container_context["id"]
        tree = self._tree_widget(self.remote_tree)
        current_name = tree.item(item, "text")
        new_name = simpledialog.askstring(
            "이름 변경", "새 파일명/폴더명을 입력하세요.", initialvalue=current_name, parent=self
        )
        if not new_name or new_name == current_name:
            return
        if "/" in new_name:
            messagebox.showerror("이름 변경 실패", "파일명/폴더명에는 '/' 를 포함할 수 없습니다.", parent=self)
            return
        dest_path = posixpath.join(posixpath.dirname(full_path), new_name)
        self.status.set(f"{current_name} 이름을 변경하는 중...")

        def worker() -> None:
            try:
                inner_cmd = f"mv -- {shlex.quote(full_path)} {shlex.quote(dest_path)}"
                command = f"docker exec {shlex.quote(container_id)} sh -c {shlex.quote(inner_cmd)}"
                self._run_remote_ok(command, timeout=30)
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: self._container_action_failed("컨테이너 항목 이름 변경", detail))
                return
            self.after(
                0,
                lambda: (self.refresh_remote(), self.status.set(f"'{current_name}' 이름을 '{new_name}'(으)로 변경했습니다.")),
            )

        threading.Thread(target=worker, daemon=True).start()

    def show_tree_menu(self, event, source: str) -> None:
        if source == "remote":
            tree = event.widget
            row = tree.identify_row(event.y)
            kind = tree.set(row, "type") if row else ""
            if row and kind == "컨테이너":
                if row not in tree.selection():
                    tree.selection_set(row)
                menu = tk.Menu(self, tearoff=0)
                menu.add_command(label="폴더 열기", command=lambda: self.open_remote_item(row))
                menu.add_command(label="컨테이너 정보 보기", command=lambda: self.show_container_details(row))
                menu.add_command(label="로그 보기(docker logs)", command=lambda: self.view_container_logs(row))
                menu.add_command(label="탐색 기록 보기(폴더/파일 목록)", command=lambda: self.view_container_explore_log(row))
                menu.add_separator()
                menu.add_command(label="Docker 컨테이너 중지", command=self.stop_selected_container)
                menu.add_command(label="Docker 컨테이너 빌드", command=self.build_docker_image)
                menu.add_command(label="Docker 컨테이너 run", command=self.run_docker_container)
                menu.add_separator()
                menu.add_command(label="새로 고침", command=self.refresh_remote)
                try:
                    menu.tk_popup(event.x_root, event.y_root)
                finally:
                    menu.grab_release()
                return
            if row and kind in ("컨테이너 폴더", "컨테이너 파일"):
                if row not in tree.selection():
                    tree.selection_set(row)
                menu = tk.Menu(self, tearoff=0)
                label = "열기" if kind == "컨테이너 폴더" else "파일 보기"
                menu.add_command(label=label, command=lambda: self.open_remote_item(row))
                menu.add_command(label="탐색 기록 보기(폴더/파일 목록)", command=lambda: self.view_container_explore_log())
                menu.add_separator()
                menu.add_command(label="이름 변경", command=lambda: self.rename_container_selected(row))
                menu.add_command(label="삭제", command=lambda: self.delete_container_selected(row))
                menu.add_separator()
                menu.add_command(label="새로 고침", command=self.refresh_remote)
                try:
                    menu.tk_popup(event.x_root, event.y_root)
                finally:
                    menu.grab_release()
                return
        super().show_tree_menu(event, source)

    def show_container_details(self, item: str) -> None:
        container_id = self._container_id_from_item(item)
        if not container_id or not self.client:
            return
        name = self._tree_widget(self.remote_tree).item(item, "text")
        self.status.set(f"{name} 컨테이너 정보를 가져오는 중...")

        def worker() -> None:
            try:
                command = f"docker inspect {shlex.quote(container_id)}"
                data, err, exit_code = self._exec_with_sudo_fallback(command)
                if exit_code != 0:
                    raise RuntimeError(err or f"docker inspect 종료 코드: {exit_code}")
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: self._container_action_failed("컨테이너 정보 조회", detail))
                return
            self.after(0, lambda: (self.status.set("탐색기 준비 완료."), self.show_file_viewer(f"{name} (docker inspect)", data)))

        threading.Thread(target=worker, daemon=True).start()

    def view_container_logs(self, item: str) -> None:
        container_id = self._container_id_from_item(item)
        if not container_id or not self.client:
            return
        name = self._tree_widget(self.remote_tree).item(item, "text")
        self.status.set(f"{name} 컨테이너 로그를 가져오는 중...")

        def worker() -> None:
            try:
                command = f"docker logs --tail 300 {shlex.quote(container_id)}"
                data, err, exit_code = self._exec_with_sudo_fallback(command)
                if exit_code != 0:
                    raise RuntimeError(err or f"docker logs 종료 코드: {exit_code}")
                if err:
                    # docker logs는 컨테이너의 stderr 출력도 함께 전달하므로 이어서 표시한다.
                    data = data + b"\n" + err.encode("utf-8", errors="replace")
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: self._container_action_failed("컨테이너 로그 조회", detail))
                return
            self.after(0, lambda: (self.status.set("탐색기 준비 완료."), self.show_file_viewer(f"{name} (docker logs)", data)))

        threading.Thread(target=worker, daemon=True).start()

    def _container_action_failed(self, action: str, detail: str) -> None:
        self.status.set(f"{action} 실패: {detail}")
        messagebox.showerror(action, detail, parent=self)

    # ------------------------------------------------------------------
    # 컨테이너 탐색(폴더/파일 목록) 로컬 로그 기록 / 보기
    #
    # 컨테이너를 클릭해 진입하거나 컨테이너 내부에서 폴더를 이동할 때마다
    # (내부적으로 docker exec 로 목록을 조회할 때마다) 조회 결과를
    # 로컬 파일(docker_logs/<컨테이너>.log)에 이력으로 남긴다.
    # ------------------------------------------------------------------
    def _docker_explore_log_path(self, container_id: str, container_name: str) -> Path:
        DOCKER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", container_name or container_id).strip() or container_id
        short_id = (container_id or "unknown")[:12]
        return DOCKER_LOG_DIR / f"{safe_name}_{short_id}.log"

    def _docker_build_log_path(self, service_name: str) -> Path:
        DOCKER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", service_name or "docker_build").strip() or "docker_build"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return DOCKER_LOG_DIR / f"docker_build_{safe_name}_{timestamp}.log"

    def _append_docker_explore_log(
        self,
        container_id: str,
        container_name: str,
        path: str,
        entries: list[dict],
        error: str,
    ) -> None:
        """컨테이너 내부 폴더/파일 목록 조회 결과를 로컬 로그 파일에 추가 기록한다.
        로그 기록 자체가 실패하더라도 탐색 기능에는 영향을 주지 않도록 예외를 삼킨다."""
        try:
            log_path = self._docker_explore_log_path(container_id, container_name)
            server_label = self.profile.get("name") or self.profile.get("host", "")
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            lines = [f"[{timestamp}] 서버: {server_label}  컨테이너: {container_name} ({container_id})  경로: {path}"]
            if error:
                lines.append(f"  (목록 조회 실패: {error})")
            elif not entries:
                lines.append("  (빈 폴더)")
            else:
                for entry in entries:
                    if entry.get("is_dir"):
                        lines.append(f"  [D] {entry['name']}")
                    else:
                        size = human_size(entry.get("size"))
                        modified = entry.get("modified") or ""
                        lines.append(f"  [F] {entry['name']}  {size}  {modified}".rstrip())
            lines.append("-" * 60)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except Exception:
            pass

    def view_container_explore_log(self, item: str | None = None) -> None:
        """선택/탐색 중인 컨테이너의 폴더·파일 목록 조회 이력을 보여준다.
        item이 주어지면 해당 컨테이너 행 기준, 아니면 현재 탐색 중이거나
        선택된 컨테이너 기준으로 로그를 찾는다."""
        container_id = None
        container_name = None
        if item:
            container_id = self._container_id_from_item(item)
            if container_id:
                container_name = self._tree_widget(self.remote_tree).item(item, "text")
        if not container_id:
            if self.container_context is not None:
                container_id = self.container_context["id"]
                container_name = self.container_context.get("name", container_id)
            else:
                current = self._selected_or_current_container()
                if current:
                    container_id = current["id"]
                    container_name = current["name"]
        if not container_id:
            messagebox.showinfo(
                "탐색 기록 보기",
                "기록을 볼 컨테이너를 원격 목록에서 선택하거나, 해당 컨테이너 내부를 탐색 중이어야 합니다.",
                parent=self,
            )
            return
        log_path = self._docker_explore_log_path(container_id, container_name or container_id)
        if not log_path.exists():
            messagebox.showinfo("탐색 기록 보기", "아직 기록된 탐색 로그가 없습니다.", parent=self)
            return
        data = log_path.read_bytes()
        self.show_file_viewer(f"{container_name or container_id} 컨테이너 탐색 기록", data)

    # ------------------------------------------------------------------
    # Docker 컨테이너 중지 / 빌드 / run
    # ------------------------------------------------------------------
    def _selected_or_current_container(self) -> dict | None:
        """현재 컨테이너 내부를 탐색 중이면 그 컨테이너를, 아니면 원격 목록에서
        선택된 컨테이너 행을 반환한다."""
        if self.container_context is not None:
            return {"id": self.container_context["id"], "name": self.container_context["name"]}
        tree = self._tree_widget(self.remote_tree)
        selected = tree.selection()
        if selected:
            item = selected[0]
            if tree.exists(item) and tree.set(item, "type") == "컨테이너":
                container_id = self._container_id_from_item(item)
                if container_id:
                    return {"id": container_id, "name": tree.item(item, "text")}
        return None

    def stop_selected_container(self) -> None:
        container = self._selected_or_current_container()
        if not container:
            messagebox.showinfo(
                "선택 필요",
                "중지할 컨테이너를 원격 목록에서 선택하거나, 해당 컨테이너 내부를 탐색 중이어야 합니다.",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "컨테이너 중지",
            f"'{container['name']}' 컨테이너를 중지할까요?\n(docker stop {container['id']})",
            parent=self,
        ):
            return
        self.status.set(f"{container['name']} 컨테이너를 중지하는 중...")

        def worker() -> None:
            try:
                self._run_remote_ok(f"docker stop {shlex.quote(container['id'])}", timeout=60)
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: self._container_action_failed("컨테이너 중지", detail))
                return
            self.after(0, lambda: self._container_stopped(container))

        threading.Thread(target=worker, daemon=True).start()

    def _container_stopped(self, container: dict) -> None:
        self.status.set(f"{container['name']} 컨테이너를 중지했습니다.")
        messagebox.showinfo("컨테이너 중지 완료", f"'{container['name']}' 컨테이너를 중지했습니다.", parent=self)
        if self.container_context and self.container_context["id"] == container["id"]:
            self.container_context = None
        self.refresh_remote()

    def _docker_build_context_from_path_entry(self) -> dict | None:
        typed = self.remote_path_var.get().strip()
        if self.container_context is None or not typed.startswith("[Docker:"):
            return None
        marker = "] "
        marker_index = typed.find(marker)
        if marker_index == -1:
            return None
        container_path = typed[marker_index + len(marker):].strip()
        if not container_path.startswith("/"):
            return None
        container_path = posixpath.normpath(container_path) or "/"
        return {
            "container_id": self.container_context["id"],
            "container_name": self.container_context.get("name", self.container_context["id"]),
            "path": container_path,
        }

    def _docker_image_hint_from_path_entry(self) -> str:
        build_context = self._docker_build_context_from_path_entry()
        if not build_context:
            return ""
        service = posixpath.basename(build_context["path"].rstrip("/")) or "image"
        return f"{service}:latest"

    def _docker_build_image_tag(self, build_context: dict) -> str:
        service = posixpath.basename(build_context["path"].rstrip("/")) or "image"
        container_parent = posixpath.dirname(build_context["path"].rstrip("/")) or "/"
        version_path = posixpath.join(container_parent, f"{service}.version")
        command = (
            "docker exec "
            + shlex.quote(build_context["container_id"])
            + " /bin/sh -lc "
            + shlex.quote(f"cat {shlex.quote(version_path)} 2>/dev/null || true")
        )
        try:
            data, _err, exit_code = self._exec_with_sudo_fallback(command, timeout=10)
        except Exception:
            return f"{service}:latest"
        if exit_code != 0:
            return f"{service}:latest"
        version_text = data.decode("utf-8", errors="replace").strip()
        if not version_text:
            return f"{service}:latest"
        try:
            next_version = float(version_text) + 0.1
        except ValueError:
            return f"{service}:latest"
        return f"{service}:v{next_version:.1f}"

    def build_docker_image(self) -> None:
        """서비스 폴더명과 상위 version 파일을 기준으로 Docker 이미지를 빌드한다."""
        build_context = self._docker_build_context_from_path_entry()
        if not build_context:
            messagebox.showinfo(
                "Docker Build",
                "Docker Build는 Docker 컨테이너 내부 경로에서만 실행할 수 있습니다.\n"
                "Docker 컨테이너를 더블클릭하여 내부 폴더로 이동한 뒤 다시 실행하세요.",
                parent=self,
            )
            return
        script = """
set -e
container_id="$1"
container_path="$2"
service=$(basename "$container_path")
container_parent=$(dirname "$container_path")
work_root=$(mktemp -d /tmp/command_manager_docker_build.XXXXXX)
trap 'rm -rf "$work_root"' EXIT
context="$work_root/$service"
version_file="$work_root/$service.version"
echo "container=$container_id"
echo "container_path=$container_path"
echo "copy_context=$context"
docker cp "$container_id:$container_path" "$context"
docker cp "$container_id:$container_parent/$service.version" "$version_file" >/dev/null 2>&1 || true
if [ -f "$context/Dockerfile" ]; then
  echo "prebuild_cleanup=scan Dockerfile symlink targets"
  sed -n -E 's/^[[:space:]]*RUN[[:space:]]+ln[[:space:]]+-s[[:space:]]+[^[:space:]]+[[:space:]]+([^[:space:];]+).*$/\\1/p' "$context/Dockerfile" |
  while IFS= read -r link_target; do
    case "$link_target" in
      /app/*)
        rel=${link_target#/app/}
        if [ -e "$context/$rel" ] || [ -L "$context/$rel" ]; then
          echo "remove_existing_link_target=$context/$rel"
          rm -rf -- "$context/$rel"
        fi
        ;;
    esac
  done
  if grep -Eq '^[[:space:]]*RUN[[:space:]]+ln[[:space:]]+-s[[:space:]]+' "$context/Dockerfile"; then
    echo "prebuild_cleanup=patch Dockerfile ln -s targets"
    sed -i.bak -E 's#^([[:space:]]*RUN[[:space:]]+)ln[[:space:]]+-s[[:space:]]+([^[:space:]]+)[[:space:]]+([^[:space:];]+)(.*)$#\\1rm -rf \\3 \\&\\& ln -s \\2 \\3\\4#' "$context/Dockerfile"
  fi
fi
service=$(basename "$context")
version="latest"
if [ -e "$version_file" ]; then
  version=$(cat "$version_file")
  version=$(awk -v ver="$version" 'BEGIN { printf "%.1f", ver + 0.1 }')
  echo "$version" > "$version_file"
  docker cp "$version_file" "$container_id:$container_parent/$service.version" >/dev/null 2>&1 || true
  version="v$version"
fi
echo "service=$service"
echo "version=$version"
echo "context=$context"
echo "container_version_file=$container_parent/$service.version"
docker build --progress=plain --tag "$service:$version" "$context"
""".strip()
        context_path = build_context["path"]
        service_hint = posixpath.basename(context_path.rstrip("/")) or "image"
        image_tag = self._docker_build_image_tag(build_context)
        self.last_docker_image_tag = image_tag
        command = (
            "bash -lc "
            + shlex.quote(script)
            + " -- "
            + shlex.quote(build_context["container_id"])
            + " "
            + shlex.quote(context_path)
        )
        if not messagebox.askyesno(
            "Docker Build",
            f"[Docker:{build_context['container_name']}] {context_path} 경로를 기준으로 Docker 이미지를 빌드할까요?\n"
            f"예상 이미지 태그: {image_tag}",
            parent=self,
        ):
            return
        log_path = self._docker_build_log_path(service_hint)
        self.run_streaming_command(f"Docker Build: {service_hint}", command, log_path=log_path)

    def run_docker_container(self) -> None:
        """docker run -d --name ... -p ... <이미지> 를 원격 서버에서 실행하고 로그를 실시간으로 보여준다.
        선택했거나 현재 탐색 중인 컨테이너가 있으면 docker inspect 결과를 참조해
        이름/이미지/포트/추가 옵션을 자동으로 채워 넣고, 입력창은 그 값을
        확인하거나 필요한 부분만 수정하는 용도로 사용한다."""
        container = self._selected_or_current_container()
        if not container:
            initial = {}
            image_hint = self.last_docker_image_tag or self._docker_image_hint_from_path_entry()
            if image_hint:
                initial["image"] = image_hint
            self._open_docker_run_dialog(initial)
            return

        self.status.set(f"{container['name']} 컨테이너의 현재 설정을 확인하는 중 (docker inspect)...")

        def worker() -> None:
            config = self._fetch_container_run_config(container["id"])
            initial = {"name": container["name"], "auto_filled": bool(config)}
            if config:
                if config.get("image"):
                    initial["image"] = config["image"]
                if config.get("ports"):
                    initial["ports"] = config["ports"]
                if config.get("extra"):
                    initial["extra"] = config["extra"]
            if "image" not in initial:
                image_hint = self.last_docker_image_tag or self._docker_image_hint_from_path_entry()
                if image_hint:
                    initial["image"] = image_hint

            def show() -> None:
                self.status.set("")
                self._open_docker_run_dialog(initial)

            self.after(0, show)

        threading.Thread(target=worker, daemon=True).start()

    def _fetch_container_run_config(self, container_id: str) -> dict | None:
        """`docker inspect <id>` 결과에서 docker run 재현에 필요한 값
        (이미지, 포트 매핑, 볼륨/환경변수 등 추가 옵션)을 추출한다.
        조회에 실패하면 None을 반환하며, 이 경우 호출부는 입력창을 빈 값으로 연다."""
        if not self.client:
            return None
        command = "docker inspect " + shlex.quote(container_id)
        try:
            data, _err, exit_code = self._exec_with_sudo_fallback(command, timeout=15)
        except Exception:
            return None
        if exit_code != 0:
            return None
        try:
            parsed = json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            return None
        if not parsed:
            return None
        info = parsed[0]
        config = info.get("Config") or {}
        host_config = info.get("HostConfig") or {}

        image = config.get("Image", "") or ""

        ports: list[str] = []
        for container_port_proto, bindings in (host_config.get("PortBindings") or {}).items():
            if not bindings:
                continue
            host_port = bindings[0].get("HostPort", "")
            if not host_port:
                continue
            container_port, _sep, proto = container_port_proto.partition("/")
            entry = f"{host_port}:{container_port}"
            if proto and proto != "tcp":
                entry += f"/{proto}"
            ports.append(entry)

        extra_tokens: list[str] = []
        for bind in host_config.get("Binds") or []:
            extra_tokens.append("-v " + shlex.quote(bind))
        for env in config.get("Env") or []:
            extra_tokens.append("-e " + shlex.quote(env))
        restart_policy_info = host_config.get("RestartPolicy") or {}
        restart_name = restart_policy_info.get("Name", "")
        if restart_name and restart_name != "no":
            if restart_name == "on-failure" and restart_policy_info.get("MaximumRetryCount"):
                restart_name = f"on-failure:{restart_policy_info['MaximumRetryCount']}"
            extra_tokens.append("--restart " + shlex.quote(restart_name))
        network_mode = host_config.get("NetworkMode", "")
        if network_mode and network_mode not in ("default", "bridge"):
            extra_tokens.append("--network " + shlex.quote(network_mode))
        if host_config.get("Privileged"):
            extra_tokens.append("--privileged")

        return {
            "image": image,
            "ports": ", ".join(ports),
            "extra": " ".join(extra_tokens),
        }

    def _open_docker_run_dialog(self, initial: dict) -> None:
        dialog = DockerRunDialog(self, initial)
        self.wait_window(dialog)
        if not dialog.result:
            return
        result = dialog.result
        port_args = [f"-p {shlex.quote(part.strip())}" for part in result["ports"].split(",") if part.strip()]
        command_parts = ["docker", "run", "-d", "--name", shlex.quote(result["name"]), *port_args]
        if result["extra"]:
            command_parts.append(result["extra"])
        command_parts.append(shlex.quote(result["image"]))
        run_command = " ".join(command_parts)

        def prepare() -> None:
            if result["remove_existing"]:
                # 기존에 같은 이름의 컨테이너가 없을 수도 있으므로 실패는 무시한다.
                self._run_remote_best_effort(f"docker stop {shlex.quote(result['name'])}")
                self._run_remote_best_effort(f"docker rm {shlex.quote(result['name'])}")
            self.after(0, lambda: self.run_streaming_command(f"Docker Run: {result['name']}", run_command))

        self.status.set(f"{result['name']} 컨테이너 실행을 준비하는 중...")
        threading.Thread(target=prepare, daemon=True).start()

    def run_streaming_command(self, title: str, command: str, log_path: Path | None = None) -> None:
        """원격 명령을 실행하고 실시간 출력을 로그 뷰어 창에 스트리밍한다.
        (docker build / docker run 등 시간이 걸리는 명령의 진행 상황 확인용)"""
        stop_event, run_event, append_text = self.show_log_viewer(title)
        if log_path:
            self.status.set(f"{title} 실행 중... 로그: {log_path}")

        def worker() -> None:
            channel = None
            log_file = None

            def write_log(text: str) -> None:
                if log_file:
                    log_file.write(text)
                    log_file.flush()

            try:
                if log_path:
                    log_file = open(log_path, "a", encoding="utf-8", errors="replace")
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    server_label = self.profile.get("name") or self.profile.get("host", "")
                    log_file.write(f"[{timestamp}] {title}\n")
                    log_file.write(f"서버: {server_label}\n")
                    log_file.write(f"명령: {command}\n")
                    log_file.write("-" * 80 + "\n")
                    log_file.flush()
                stdin, stdout, stderr = self.client.exec_command(command, get_pty=True)
                stdin.close()
                channel = stdout.channel
                while not stop_event.is_set():
                    if not run_event.is_set():
                        time.sleep(0.2)
                        continue
                    if channel.recv_ready():
                        data = channel.recv(4096)
                        if not data:
                            break
                        text = data.decode("utf-8", errors="replace")
                        append_text(text)
                        write_log(text)
                    elif channel.exit_status_ready():
                        while channel.recv_ready():
                            data = channel.recv(4096)
                            if not data:
                                break
                            text = data.decode("utf-8", errors="replace")
                            append_text(text)
                            write_log(text)
                        break
                    else:
                        time.sleep(0.2)
                if not stop_event.is_set():
                    exit_status = channel.recv_exit_status() if channel else None
                    finish_text = f"\n\n[명령 종료: 코드 {exit_status}]\n"
                    append_text(finish_text)
                    write_log(finish_text)
                    self.after(0, lambda: self._streaming_command_finished(title, exit_status, log_path))
            except Exception as exc:
                detail = str(exc)
                error_text = f"\n[실행 오류: {detail}]\n"
                append_text(error_text)
                write_log(error_text)
            finally:
                if log_file:
                    try:
                        log_file.write("-" * 80 + "\n")
                        log_file.close()
                    except Exception:
                        pass
                if channel:
                    try:
                        channel.close()
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def _streaming_command_finished(self, title: str, exit_status: int | None, log_path: Path | None = None) -> None:
        if log_path:
            self.status.set(f"{title} 완료 (종료 코드: {exit_status}) / 로그: {log_path}")
        else:
            self.status.set(f"{title} 완료 (종료 코드: {exit_status})")
        self.refresh_remote()

    # ------------------------------------------------------------------
    # Docker 컨테이너 / 컨테이너 내부 폴더·파일을 로컬로 드래그하여 다운로드
    # ------------------------------------------------------------------
    def start_drag(self, event, source: str) -> None:
        if source == "remote":
            tree = event.widget
            item = tree.identify_row(event.y)
            if item and self._docker_drag_source(item) is not None:
                selected = list(tree.selection())
                if item not in selected:
                    tree.selection_set(item)
                    selected = [item]
                docker_items = [sel for sel in selected if self._docker_drag_source(sel) is not None]
                self.drag_data = {"source": "remote_docker", "items": docker_items}
                return
        super().start_drag(event, source)

    def finish_drag(self, event) -> None:
        if self.drag_data and self.drag_data.get("source") == "remote_docker":
            items = self.drag_data["items"]
            self.drag_data = None
            local_tree = self._tree_widget(self.local_tree)
            target_tree = self._drop_tree_at_pointer(event.x_root, event.y_root)
            if target_tree != local_tree:
                return
            local_folder = self._drop_local_folder(local_tree, event.x_root, event.y_root) or self.local_cwd
            self._confirm_and_download_docker_items(items, local_folder)
            return
        if self.drag_data and self.drag_data.get("source") == "local":
            remote_tree = self._tree_widget(self.remote_tree)
            target_tree = self._drop_tree_at_pointer(event.x_root, event.y_root)
            if target_tree == remote_tree:
                docker_target = self._docker_drop_target(remote_tree, event.x_root, event.y_root)
                if docker_target is not None:
                    items = self.drag_data["items"]
                    self.drag_data = None
                    self._confirm_and_upload_to_container(items, docker_target)
                    return
                if self.container_context is not None:
                    self.drag_data = None
                    return
        super().finish_drag(event)

    def _docker_drop_target(self, tree: ttk.Treeview, x_root: int, y_root: int) -> dict | None:
        """로컬 파일을 드롭한 위치가 Docker 컨테이너 관련 대상(컨테이너 자체,
        컨테이너 내부 폴더, 또는 현재 탐색 중인 컨테이너 경로)이면 업로드에
        필요한 정보를 담은 dict를, 아니면 None을 반환한다."""
        row = tree.identify_row(y_root - tree.winfo_rooty())
        if row:
            if row in ("remote:..", "remote:ctrback:.."):
                return None
            kind = tree.set(row, "type")
            if kind == "컨테이너":
                container_id = self._container_id_from_item(row)
                if not container_id:
                    return None
                return {"container_id": container_id, "path": "/", "name": tree.item(row, "text")}
            if kind == "컨테이너 폴더":
                prefix = "remote:ctrpath:"
                full_path = self._container_full_path_from_item(row)
                if full_path is None or not row.startswith(prefix):
                    return None
                container_id = row[len(prefix):].split("\x1f", 1)[0]
                return {
                    "container_id": container_id,
                    "path": full_path,
                    "name": posixpath.basename(full_path) or full_path,
                }
            if kind == "컨테이너 파일":
                prefix = "remote:ctrpath:"
                full_path = self._container_full_path_from_item(row)
                if full_path is None or not row.startswith(prefix):
                    return None
                container_id = row[len(prefix):].split("\x1f", 1)[0]
                parent_path = posixpath.dirname(full_path.rstrip("/")) or "/"
                return {
                    "container_id": container_id,
                    "path": parent_path,
                    "name": posixpath.basename(parent_path) or parent_path,
                }
            return None
        # 특정 행이 아니라 빈 영역에 드롭한 경우: 현재 컨테이너 내부를 탐색 중이면
        # 그 경로를 업로드 대상으로 사용한다.
        if self.container_context is not None:
            return {
                "container_id": self.container_context["id"],
                "path": self.container_context["path"],
                "name": self.container_context["name"],
            }
        return None

    def _confirm_and_upload_to_container(self, items: list[str], target: dict) -> None:
        local_paths = [item.removeprefix("local:") for item in items]
        local_paths = [p for p in local_paths if Path(p).is_file()]
        if not local_paths:
            messagebox.showinfo("업로드 불가", "폴더는 지원하지 않으며, 파일만 컨테이너로 업로드할 수 있습니다.", parent=self)
            return
        names = ", ".join(Path(p).name for p in local_paths)
        message = f"{names} 파일을 '{target['name']}' 컨테이너의 {target['path']} 경로로 업로드할까요?\n(docker cp)"
        if not messagebox.askyesno("Docker 업로드", message, parent=self):
            return
        self._upload_to_container(local_paths, target)

    def _upload_to_container(self, local_paths: list[str], target: dict) -> None:
        total_items = len(local_paths)
        self.status.set(f"컨테이너로 업로드 준비 중... (0/{total_items})")
        self._set_progress_mode("determinate")

        def worker() -> None:
            results = []
            for index, local_path in enumerate(local_paths, start=1):
                try:
                    self._upload_one_file_to_container(local_path, target, index, total_items)
                    results.append((Path(local_path).name, True, ""))
                except Exception as exc:
                    results.append((Path(local_path).name, False, str(exc)))
            self.after(0, lambda: self._docker_upload_done(results, target))

        threading.Thread(target=worker, daemon=True).start()

    def _upload_one_file_to_container(self, local_path: str, target: dict, index: int, total: int) -> None:
        container_id = target["container_id"]
        dest_dir = target["path"]
        filename = Path(local_path).name
        label = f"({index}/{total}) {filename}"

        self.after(0, lambda: self.status.set(f"{label}: 서버로 업로드 준비 중..."))
        tmp_dir = self._make_remote_tempdir()
        try:
            remote_temp_path = posixpath.join(tmp_dir, filename)
            file_size = max(1, Path(local_path).stat().st_size)

            def callback(transferred: int, _total: int) -> None:
                percent = min(100, (transferred / file_size) * 100)
                self.after(0, lambda value=percent: self._update_progress(value, f"{label}: 서버로 전송 중"))

            with self.sftp_lock:
                self.sftp.put(local_path, remote_temp_path, callback=callback)

            self.after(
                0,
                lambda: (
                    self._set_progress_mode("indeterminate"),
                    self.status.set(f"{label}: 컨테이너에 반영 중 (docker cp)..."),
                ),
            )
            dest_path = posixpath.join(dest_dir, filename)
            self._run_remote_ok(
                f"docker cp {shlex.quote(remote_temp_path)} {shlex.quote(container_id + ':' + dest_path)}",
                timeout=None,
            )
        finally:
            self._run_remote_best_effort(f"rm -rf {shlex.quote(tmp_dir)}")
        self.after(0, lambda: self._set_progress_mode("determinate"))

    def _docker_upload_done(self, results: list[tuple[str, bool, str]], target: dict) -> None:
        self._set_progress_mode("determinate")
        success = [r for r in results if r[1]]
        failed = [r for r in results if not r[1]]
        if success and not failed:
            self.progress_var.set(100)
            self.progress_text_var.set("100%")
            names = ", ".join(r[0] for r in success)
            self.status.set(f"컨테이너 업로드 완료: {names}")
            messagebox.showinfo(
                "업로드 완료",
                f"{names} 파일을 '{target['name']}' 컨테이너의 {target['path']} 경로로 업로드했습니다.",
                parent=self,
            )
        elif success and failed:
            ok_names = ", ".join(r[0] for r in success)
            fail_detail = "\n".join(f"- {r[0]}: {r[2]}" for r in failed)
            self.status.set(f"일부 파일 업로드 실패 ({len(failed)}건)")
            messagebox.showwarning("일부 실패", f"성공: {ok_names}\n\n실패:\n{fail_detail}", parent=self)
        else:
            fail_detail = "\n".join(f"- {r[0]}: {r[2]}" for r in failed)
            self.status.set("컨테이너 업로드 실패")
            messagebox.showerror("업로드 실패", fail_detail, parent=self)
        if (
            self.container_context
            and self.container_context["id"] == target["container_id"]
            and self.container_context["path"] == target["path"]
        ):
            self.refresh_remote()

    def _docker_drag_source(self, item: str) -> dict | None:
        """드래그 중인 원격 항목이 Docker 컨테이너/컨테이너 폴더/파일이면
        다운로드에 필요한 정보를 담은 dict를, 아니면 None을 반환한다."""
        tree = self._tree_widget(self.remote_tree)
        if not tree.exists(item) or item in ("remote:..", "remote:ctrback:.."):
            return None
        kind = tree.set(item, "type")
        name = tree.item(item, "text")
        if kind == "컨테이너":
            container_id = self._container_id_from_item(item)
            if not container_id:
                return None
            return {"container_id": container_id, "path": "/", "name": name, "whole_container": True}
        if kind in ("컨테이너 폴더", "컨테이너 파일"):
            prefix = "remote:ctrpath:"
            full_path = self._container_full_path_from_item(item)
            if full_path is None or not item.startswith(prefix):
                return None
            container_id = item[len(prefix):].split("\x1f", 1)[0]
            return {"container_id": container_id, "path": full_path, "name": name, "whole_container": False}
        return None

    def _confirm_and_download_docker_items(self, items: list[str], local_folder: Path) -> None:
        sources = [info for item in items if (info := self._docker_drag_source(item))]
        if not sources:
            return
        names = ", ".join(info["name"] for info in sources)
        if any(info["whole_container"] for info in sources):
            message = (
                f"{names} 항목을 {local_folder} 폴더로 다운로드할까요?\n\n"
                "컨테이너 전체를 내려받는 경우 tar 압축 파일(docker export)로 저장되며,\n"
                "컨테이너 크기에 따라 시간이 오래 걸릴 수 있습니다."
            )
        else:
            message = f"{names} 항목을 {local_folder} 폴더로 다운로드할까요?"
        if not messagebox.askyesno("Docker 다운로드", message, parent=self):
            return
        self._download_docker_items(sources, local_folder)

    def _download_docker_items(self, sources: list[dict], local_folder: Path) -> None:
        total_items = len(sources)
        self.status.set(f"Docker 항목 다운로드 준비 중... (0/{total_items})")
        self._set_progress_mode("indeterminate")

        def worker() -> None:
            results = []
            for index, info in enumerate(sources, start=1):
                try:
                    saved_path = self._download_one_docker_item(info, local_folder, index, total_items)
                    results.append((info["name"], True, str(saved_path)))
                except Exception as exc:
                    results.append((info["name"], False, str(exc)))
            self.after(0, lambda: self._docker_download_done(results))

        threading.Thread(target=worker, daemon=True).start()

    def _download_one_docker_item(self, info: dict, local_folder: Path, index: int, total: int) -> Path:
        container_id = info["container_id"]
        remote_source_path = info["path"]
        name = info["name"] or container_id
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", name)
        label = f"({index}/{total}) {name}"

        self.after(0, lambda: (self._set_progress_mode("indeterminate"), self.status.set(f"{label}: 원격 서버에서 준비 중 (docker export/cp 실행)...")))

        tmp_dir = self._make_remote_tempdir()
        try:
            if info["whole_container"]:
                archive_name = f"{safe_name}.tar"
                remote_archive = posixpath.join(tmp_dir, archive_name)
                self._run_remote_ok(
                    f"docker export {shlex.quote(container_id)} -o {shlex.quote(remote_archive)}",
                    timeout=None,
                )
                with self.sftp_lock:
                    remote_size = max(1, self.sftp.stat(remote_archive).st_size)
                local_target = self._unique_local_path(local_folder / archive_name)
                self.after(0, lambda: self._set_progress_mode("determinate"))

                def callback(transferred: int, _total: int) -> None:
                    percent = min(100, (transferred / remote_size) * 100)
                    self.after(0, lambda value=percent: self._update_progress(value, f"{label}: 다운로드 중"))

                with self.sftp_lock:
                    self.sftp.get(remote_archive, str(local_target), callback=callback)
            else:
                remote_dest = posixpath.join(tmp_dir, safe_name)
                self._run_remote_ok(
                    f"docker cp {shlex.quote(container_id + ':' + remote_source_path)} {shlex.quote(remote_dest)}",
                    timeout=None,
                )
                with self.sftp_lock:
                    total_size = max(1, self._sftp_total_size(self.sftp, remote_dest))
                local_target = self._unique_local_path(local_folder / safe_name)
                self.after(0, lambda: self._set_progress_mode("determinate"))
                progress_state = {"done": 0, "label": f"{label}: 다운로드 중"}
                with self.sftp_lock:
                    self._sftp_download_recursive(self.sftp, remote_dest, local_target, total_size, progress_state)
        finally:
            self._run_remote_best_effort(f"rm -rf {shlex.quote(tmp_dir)}")
        self.after(0, lambda: self._update_progress(100, f"{label}: 완료"))
        return local_target

    def _make_remote_tempdir(self) -> str:
        data, err, exit_code = self._exec_with_sudo_fallback("mktemp -d /tmp/cmgr_docker_XXXXXX", timeout=15)
        if exit_code != 0:
            raise RuntimeError(err or "임시 디렉터리를 만들 수 없습니다.")
        path = data.decode("utf-8", errors="replace").strip()
        if not path:
            raise RuntimeError("임시 디렉터리 경로를 확인할 수 없습니다.")
        return path

    def _run_remote_ok(self, command: str, timeout: float | None = None) -> None:
        data, err, exit_code = self._exec_with_sudo_fallback(command, timeout=timeout)
        if exit_code != 0:
            raise RuntimeError(err or f"명령 실행에 실패했습니다 (종료 코드 {exit_code}).")

    def _run_remote_best_effort(self, command: str) -> None:
        """실패는 무시하되(best-effort), 원격 명령이 실제로 종료될 때까지 대기한다.
        exec_command()는 명령을 비동기로 실행하고 즉시 반환하므로, 종료 상태를
        확인하지 않으면 (예: run_docker_container의 '기존 컨테이너 중지 후 삭제')
        뒤이어 실행되는 명령이 앞선 명령의 완료보다 먼저 실행되는 경쟁 조건이
        발생할 수 있다."""
        try:
            with self.sftp_lock:
                stdin, stdout, stderr = self.client.exec_command(command)
                stdin.close()
                stdout.read()
                stderr.read()
                stdout.channel.recv_exit_status()
        except Exception:
            pass

    def _set_progress_mode(self, mode: str) -> None:
        """진행률 바를 '준비 중'(원격 docker 명령 실행 동안, 바이트 단위 진행을 알 수 없음)을
        나타내는 indeterminate 모드와, 실제 SFTP 전송량을 %로 보여주는 determinate 모드
        사이에서 전환한다."""
        if mode == "indeterminate":
            self.progress_bar.configure(mode="indeterminate")
            self.progress_bar.start(12)
            self.progress_text_var.set("...")
        else:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            self.progress_var.set(0)
            self.progress_text_var.set("0%")

    def _update_progress(self, percent: float, label: str) -> None:
        rounded = int(round(percent))
        self.progress_var.set(percent)
        self.progress_text_var.set(f"{rounded}%")
        self.status.set(f"{label} ({rounded}%)")

    def _sftp_total_size(self, sftp, remote_path: str) -> int:
        attrs = sftp.stat(remote_path)
        if stat.S_ISDIR(attrs.st_mode):
            total = 0
            for entry in sftp.listdir_attr(remote_path):
                child = posixpath.join(remote_path, entry.filename)
                if stat.S_ISDIR(entry.st_mode):
                    total += self._sftp_total_size(sftp, child)
                else:
                    total += entry.st_size
            return total
        return attrs.st_size

    def _sftp_download_recursive(
        self,
        sftp,
        remote_path: str,
        local_path: Path,
        total_size: int,
        progress_state: dict,
    ) -> None:
        attrs = sftp.stat(remote_path)
        if stat.S_ISDIR(attrs.st_mode):
            local_path.mkdir(parents=True, exist_ok=True)
            for entry in sftp.listdir_attr(remote_path):
                child_remote = posixpath.join(remote_path, entry.filename)
                child_local = local_path / entry.filename
                if stat.S_ISDIR(entry.st_mode):
                    self._sftp_download_recursive(sftp, child_remote, child_local, total_size, progress_state)
                else:
                    base = progress_state["done"]

                    def callback(transferred: int, _total: int, base: int = base) -> None:
                        percent = min(100, ((base + transferred) / total_size) * 100)
                        self.after(
                            0,
                            lambda value=percent: self._update_progress(value, progress_state.get("label", "다운로드 중")),
                        )

                    sftp.get(child_remote, str(child_local), callback=callback)
                    progress_state["done"] = base + entry.st_size
        else:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            base = progress_state["done"]

            def callback(transferred: int, _total: int, base: int = base) -> None:
                percent = min(100, ((base + transferred) / total_size) * 100)
                self.after(
                    0,
                    lambda value=percent: self._update_progress(value, progress_state.get("label", "다운로드 중")),
                )

            sftp.get(remote_path, str(local_path), callback=callback)
            progress_state["done"] = base + attrs.st_size

    def _unique_local_path(self, path: Path) -> Path:
        if not path.exists():
            return path
        stem, suffix = path.stem, path.suffix
        counter = 1
        while True:
            candidate = path.with_name(f"{stem} ({counter}){suffix}")
            if not candidate.exists():
                return candidate
            counter += 1

    def _docker_download_done(self, results: list[tuple[str, bool, str]]) -> None:
        self._set_progress_mode("determinate")
        self.refresh_local()
        success = [r for r in results if r[1]]
        failed = [r for r in results if not r[1]]
        if success and not failed:
            self.progress_var.set(100)
            self.progress_text_var.set("100%")
            names = ", ".join(r[0] for r in success)
            self.status.set(f"Docker 항목 다운로드 완료: {names}")
            messagebox.showinfo("다운로드 완료", f"{names} 다운로드가 완료되었습니다.", parent=self)
        elif success and failed:
            ok_names = ", ".join(r[0] for r in success)
            fail_detail = "\n".join(f"- {r[0]}: {r[2]}" for r in failed)
            self.status.set(f"일부 항목 다운로드 실패 ({len(failed)}건)")
            messagebox.showwarning(
                "일부 실패", f"성공: {ok_names}\n\n실패:\n{fail_detail}", parent=self
            )
        else:
            fail_detail = "\n".join(f"- {r[0]}: {r[2]}" for r in failed)
            self.status.set("Docker 항목 다운로드 실패")
            messagebox.showerror("다운로드 실패", fail_detail, parent=self)


class CommandSettingsDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Tk,
        commands: list[dict],
        title: str = "Command 설정",
        heading: str = "Command 실행 파일 설정",
        browse_title: str = "Command 실행 파일 선택",
    ):
        super().__init__(master)
        self.title(title)
        self.geometry("860x500")
        self.minsize(760, 440)
        self.heading = heading
        self.browse_title = browse_title
        self.commands = [dict(command) for command in commands]
        self.result: list[dict] | None = None
        self.selected_id: str | None = None

        self.name_var = tk.StringVar()
        self.path_var = tk.StringVar()
        self.args_var = tk.StringVar()
        self.workdir_var = tk.StringVar()
        self.init_cmd_var = tk.StringVar()

        self._build()
        self._refresh()
        self.transient(master)
        self.grab_set()

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=(14, 12))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text=self.heading, font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, sticky="w"
        )

        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        body.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 10))

        list_frame = ttk.Frame(body)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(list_frame, columns=("path", "args"), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="이름")
        self.tree.heading("path", text="실행 파일")
        self.tree.heading("args", text="인자")
        self.tree.column("#0", width=130)
        self.tree.column("path", width=330)
        self.tree.column("args", width=160)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        yscroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=yscroll.set)
        body.add(list_frame, weight=2)

        form = ttk.Frame(body, padding=(14, 0, 0, 0))
        form.columnconfigure(1, weight=1)
        body.add(form, weight=1)

        fields = [
            ("이름", self.name_var, None),
            ("실행 파일", self.path_var, self.browse_executable),
            ("실행 인자", self.args_var, None),
            ("작업 폴더", self.workdir_var, self.browse_workdir),
            ("초기화 명령어", self.init_cmd_var, None),
        ]
        last_row = 0
        for row, (label, var, browse) in enumerate(fields):
            last_row = row
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(form, textvariable=var).grid(row=row, column=1, sticky="ew", pady=5)
            if browse:
                ttk.Button(form, text="찾기", command=browse).grid(row=row, column=2, padx=(6, 0), pady=5)
        ttk.Label(
            form,
            text="원격 서버 CLI 접속 직후 자동 실행됩니다. 여러 명령은 ';'로 구분하세요.\n예: export DISPLAY=:0",
            foreground="#718096",
            justify="left",
        ).grid(row=last_row + 1, column=0, columnspan=3, sticky="w", pady=(0, 5))

        actions = ttk.Frame(form)
        actions.grid(row=last_row + 2, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        ttk.Button(actions, text="추가/저장", command=self.save_current).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(actions, text="새 항목", command=self.clear_form).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(actions, text="삭제", command=self.delete_current).grid(row=0, column=2)

        footer = ttk.Frame(self, padding=(14, 0, 14, 14))
        footer.grid(row=2, column=0, sticky="e")
        ttk.Button(footer, text="취소", command=self.destroy).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(footer, text="확인", command=self.apply_and_close).grid(row=0, column=1)

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for command in self.commands:
            self.tree.insert(
                "",
                "end",
                iid=command["id"],
                values=(command.get("path", ""), command.get("args", "")),
                text=command.get("name", ""),
            )

    def _on_select(self, _event=None) -> None:
        selected = self.tree.selection()
        self.selected_id = selected[0] if selected else None
        command = next((item for item in self.commands if item.get("id") == self.selected_id), None)
        if not command:
            return
        self.name_var.set(command.get("name", ""))
        self.path_var.set(command.get("path", ""))
        self.args_var.set(command.get("args", ""))
        self.workdir_var.set(command.get("workdir", ""))
        self.init_cmd_var.set(command.get("init_cmd", ""))

    def browse_executable(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title=self.browse_title,
            filetypes=[("실행 파일", "*.exe *.bat *.cmd *.ps1"), ("모든 파일", "*.*")],
        )
        if path:
            self.path_var.set(path)
            if not self.name_var.get().strip():
                self.name_var.set(Path(path).stem)
            if not self.workdir_var.get().strip():
                self.workdir_var.set(str(Path(path).parent))

    def browse_workdir(self) -> None:
        path = filedialog.askdirectory(parent=self, title="작업 폴더 선택")
        if path:
            self.workdir_var.set(path)

    def clear_form(self) -> None:
        self.selected_id = None
        self.tree.selection_remove(self.tree.selection())
        self.name_var.set("")
        self.path_var.set("")
        self.args_var.set("")
        self.workdir_var.set("")
        self.init_cmd_var.set("")

    def save_current(self) -> bool:
        name = self.name_var.get().strip()
        path = self.path_var.get().strip()
        args = self.args_var.get().strip()
        workdir = self.workdir_var.get().strip()
        init_cmd = self.init_cmd_var.get().strip()
        if not name or not path:
            messagebox.showerror("입력 오류", "이름과 실행 파일은 필수입니다.", parent=self)
            return False
        if not Path(path).exists():
            messagebox.showerror("입력 오류", "실행 파일 경로가 존재하지 않습니다.", parent=self)
            return False
        if workdir and not Path(workdir).is_dir():
            messagebox.showerror("입력 오류", "작업 폴더 경로가 존재하지 않습니다.", parent=self)
            return False

        command = {
            "id": self.selected_id or command_id(name, path),
            "name": name,
            "path": path,
            "args": args,
            "workdir": workdir,
            "init_cmd": init_cmd,
        }
        for index, existing in enumerate(self.commands):
            if existing.get("id") == command["id"]:
                self.commands[index] = command
                break
        else:
            self.commands.append(command)
        self.selected_id = command["id"]
        self._refresh()
        self.tree.selection_set(self.selected_id)
        return True

    def delete_current(self) -> None:
        if not self.selected_id:
            messagebox.showinfo("선택 필요", "삭제할 command를 선택하세요.", parent=self)
            return
        if not messagebox.askyesno("삭제 확인", "선택한 command 설정을 삭제할까요?", parent=self):
            return
        self.commands = [item for item in self.commands if item.get("id") != self.selected_id]
        self.clear_form()
        self._refresh()

    def apply_and_close(self) -> None:
        if self.name_var.get().strip() or self.path_var.get().strip():
            if not self.save_current():
                return
        self.result = self.commands
        self.destroy()


class CommandManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Command Manager")
        self.geometry("900x560")
        self.minsize(760, 460)
        self.profiles = load_profiles()
        self.commands = load_commands()
        self.editors = load_editors()
        self._ensure_default_commands()
        self.selected_id: str | None = None
        self.selected_command_id: str | None = None
        self.command_combo_var = tk.StringVar()
        self.command_combo_names: list[str] = []
        self.server_click_after_id: str | None = None
        self._build()
        self._refresh_commands()
        self._refresh_profiles()

    def _ensure_default_commands(self) -> None:
        defaults = [
            {
                "name": "PowerShell",
                "path": shutil.which("powershell") or r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe",
                "args": "",
                "workdir": "",
            },
            {
                "name": "Cygwin",
                "path": r"C:\Util\cygwin\cygwin64\Cygwin.bat",
                "args": "",
                "workdir": r"C:\Util\cygwin\cygwin64",
            },
            {
                "name": "cygwin_mintty",
                "path": r"C:\Util\cygwin\cygwin64\bin\mintty.exe",
                "args": "",
                "workdir": r"C:\Util\cygwin\cygwin64\bin",
            },
            {
                "name": "cygwin_bash",
                "path": r"C:\Util\cygwin\cygwin64\bin\bash.exe",
                "args": "",
                "workdir": r"C:\Util\cygwin\cygwin64\bin",
            },
            {
                "name": "PuTTY",
                "path": r"C:\Util\putty\putty.exe",
                "args": "",
                "workdir": r"C:\Util\putty",
            },
        ]
        changed = False
        existing_names = {command.get("name") for command in self.commands}
        existing_paths = {str(command.get("path", "")).lower() for command in self.commands}
        for item in defaults:
            if item["name"] in existing_names or item["path"].lower() in existing_paths:
                continue
            item["id"] = command_id(item["name"], item["path"])
            self.commands.append(item)
            changed = True
        if changed:
            save_commands(self.commands)

    def _build(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", font=("Segoe UI", 8), padding=(4, 2))
        style.configure("TLabel", font=("Segoe UI", 9))
        style.configure("TLabelframe.Label", font=("Segoe UI", 8))
        style.configure("Treeview", font=("Segoe UI", 9), rowheight=20)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(self, padding=(12, 12))
        sidebar.grid(row=0, column=0, sticky="ns")
        ttk.Label(sidebar, text="Command", font=("Segoe UI", 16, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 12)
        )
        ttk.Button(sidebar, text="로컬 CMD", command=self.open_local_cmd).grid(
            row=1, column=0, sticky="ew", pady=3
        )
        ttk.Button(sidebar, text="로컬 PowerShell", command=self.open_powershell).grid(
            row=2, column=0, sticky="ew", pady=3
        )
        ttk.Button(sidebar, text="Command 설정", command=self.open_command_settings).grid(
            row=3, column=0, sticky="ew", pady=3
        )
        ttk.Button(sidebar, text="Editor 설정", command=self.open_editor_settings).grid(
            row=4, column=0, sticky="ew", pady=3
        )
        ttk.Separator(sidebar).grid(row=5, column=0, sticky="ew", pady=12)
        ttk.Button(sidebar, text="서버 추가/로그인", command=self.add_profile).grid(
            row=6, column=0, sticky="ew", pady=3
        )
        ttk.Button(sidebar, text="선택 서버 로그인", command=self.login_selected).grid(
            row=7, column=0, sticky="ew", pady=3
        )
        ttk.Button(sidebar, text="선택 서버 수정", command=self.edit_selected).grid(
            row=8, column=0, sticky="ew", pady=3
        )
        ttk.Button(sidebar, text="파일 전송", command=self.open_transfer_explorer).grid(
            row=9, column=0, sticky="ew", pady=3
        )
        ttk.Button(sidebar, text="Docker", command=self.open_docker_explorer).grid(
            row=10, column=0, sticky="ew", pady=3
        )
        ttk.Separator(sidebar).grid(row=11, column=0, sticky="ew", pady=12)
        ttk.Button(sidebar, text="수정", command=self.edit_selected).grid(row=12, column=0, sticky="ew", pady=3)
        ttk.Button(sidebar, text="삭제", command=self.delete_selected).grid(row=13, column=0, sticky="ew", pady=3)

        main = ttk.Frame(self, padding=16)
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)

        ttk.Label(main, text="Command 실행 파일", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        command_bar = ttk.Frame(main)
        command_bar.grid(row=1, column=0, sticky="ew", pady=(10, 18))
        command_bar.columnconfigure(0, weight=1)
        self.command_combo = ttk.Combobox(
            command_bar,
            textvariable=self.command_combo_var,
            state="readonly",
            values=[],
        )
        self.command_combo.grid(row=0, column=0, sticky="ew")
        self.command_combo.bind("<<ComboboxSelected>>", self._on_command_combo_select)
        ttk.Button(command_bar, text="설정", command=self.open_command_settings).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(command_bar, text="선택 해제", command=self.clear_command_combo).grid(
            row=0, column=2, padx=(6, 0)
        )

        ttk.Label(main, text="원격 서버", font=("Segoe UI", 14, "bold")).grid(row=2, column=0, sticky="w")
        columns = ("name", "host", "port", "username", "saved")
        self.tree = ttk.Treeview(main, columns=columns, show="headings", selectmode="browse")
        headers = {
            "name": "이름",
            "host": "서버",
            "port": "포트",
            "username": "계정",
            "saved": "저장",
        }
        widths = {"name": 170, "host": 230, "port": 70, "username": 150, "saved": 80}
        for col in columns:
            self.tree.heading(col, text=headers[col])
            self.tree.column(col, width=widths[col], anchor="w")
        self.tree.grid(row=3, column=0, sticky="nsew", pady=(10, 8))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<ButtonRelease-1>", self.on_server_grid_click)
        self.tree.bind("<Double-1>", self.on_server_grid_double_click)
        self.tree.bind("<Button-3>", self.show_server_menu)

        self.status = tk.StringVar(value="Command 콤보를 선택한 뒤 원격 서버 행을 더블클릭하세요.")
        ttk.Label(main, textvariable=self.status).grid(row=4, column=0, sticky="w")

    def _refresh_commands(self) -> None:
        current_id = self.selected_command_id
        self.command_combo_names = [command.get("name", "") for command in self.commands]
        self.command_combo.configure(values=self.command_combo_names)
        if current_id and any(command.get("id") == current_id for command in self.commands):
            command = next(command for command in self.commands if command.get("id") == current_id)
            self.command_combo_var.set(command.get("name", ""))
        else:
            self.selected_command_id = None
            self.command_combo_var.set("")

    def _on_command_combo_select(self, _event=None) -> None:
        selected_name = self.command_combo_var.get()
        command = next((item for item in self.commands if item.get("name") == selected_name), None)
        self.selected_command_id = command.get("id") if command else None
        if command:
            self.status.set(f"{command.get('name')} command 선택됨. 원격 서버 행을 더블클릭하면 CLI가 실행됩니다.")

    def clear_command_combo(self) -> None:
        self.selected_command_id = None
        self.command_combo_var.set("")
        self.status.set("Command 선택을 해제했습니다. 서버 행 클릭은 선택만 합니다.")

    def open_command_settings(self) -> None:
        dialog = CommandSettingsDialog(self, self.commands)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        self.commands = dialog.result
        save_commands(self.commands)
        self._refresh_commands()
        self.status.set("Command 설정이 저장되었습니다.")

    def open_editor_settings(self) -> None:
        dialog = CommandSettingsDialog(
            self,
            self.editors,
            title="Editor 설정",
            heading="Editor 실행 파일 설정",
            browse_title="Editor 실행 파일 선택",
        )
        self.wait_window(dialog)
        if dialog.result is None:
            return
        self.editors = dialog.result
        save_editors(self.editors)
        self.status.set("Editor 설정이 저장되었습니다.")

    def selected_command(self) -> dict | None:
        if not self.selected_command_id:
            return None
        return next((item for item in self.commands if item.get("id") == self.selected_command_id), None)

    def launch_command(self, command: dict, cwd_override: str | None = None) -> None:
        path = command.get("path", "")
        if not path or not Path(path).exists():
            messagebox.showerror("실행 실패", "등록된 실행 파일 경로가 존재하지 않습니다.", parent=self)
            return
        workdir = cwd_override or command.get("workdir") or str(Path(path).parent)
        if workdir and not Path(workdir).is_dir():
            messagebox.showerror("실행 실패", "등록된 작업 폴더 경로가 존재하지 않습니다.", parent=self)
            return
        try:
            args = shlex.split(command.get("args", ""), posix=False)
            subprocess.Popen(
                [path, *args],
                cwd=workdir or None,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        except Exception as exc:
            messagebox.showerror("실행 실패", f"Command를 실행할 수 없습니다.\n{exc}", parent=self)
            return
        self.status.set(f"{command.get('name', Path(path).name)} command를 실행했습니다.")

    def _refresh_profiles(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for profile in self.profiles:
            self.tree.insert(
                "",
                "end",
                iid=profile["id"],
                values=(
                    profile.get("name", ""),
                    profile.get("host", ""),
                    profile.get("port", 22),
                    profile.get("username", ""),
                    "예" if profile.get("password") or profile.get("key_path") else "아니오",
                ),
            )

    def _on_select(self, _event=None) -> None:
        selected = self.tree.selection()
        self.selected_id = selected[0] if selected else None

    def on_server_grid_click(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        self.selected_id = row_id

    def on_server_grid_double_click(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        if self.server_click_after_id:
            self.after_cancel(self.server_click_after_id)
            self.server_click_after_id = None
        self.selected_id = row_id
        self.tree.selection_set(row_id)
        command = self.selected_command()
        profile = self._selected_profile()
        if profile and command:
            self.launch_remote(profile, command)
        else:
            self.login_selected()

    def show_server_menu(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.selected_id = row_id
            self.tree.selection_set(row_id)
        else:
            self.selected_id = None
            self.tree.selection_remove(self.tree.selection())

        has_selection = bool(row_id)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="서버 추가", command=lambda: self.add_profile(login_after_save=False))
        menu.add_command(label="서버 설정 수정", command=self.edit_selected, state=tk.NORMAL if has_selection else tk.DISABLED)
        menu.add_command(label="서버 삭제", command=self.delete_selected, state=tk.NORMAL if has_selection else tk.DISABLED)
        menu.add_separator()
        menu.add_command(label="파일 전송", command=self.open_transfer_explorer, state=tk.NORMAL if has_selection else tk.DISABLED)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def launch_server_with_selected_command(self, row_id: str) -> None:
        self.server_click_after_id = None
        self.selected_id = row_id
        profile = self._selected_profile()
        command = self.selected_command()
        if profile and command:
            self.launch_remote(profile, command)

    def _selected_profile(self) -> dict | None:
        if not self.selected_id:
            messagebox.showinfo("선택 필요", "먼저 서버를 선택하세요.", parent=self)
            return None
        return next((p for p in self.profiles if p.get("id") == self.selected_id), None)

    def _save_profile(self, profile: dict) -> None:
        profile = {key: value for key, value in profile.items() if key != "runtime_password"}
        for index, existing in enumerate(self.profiles):
            if existing.get("id") == profile.get("id"):
                self.profiles[index] = profile
                break
        else:
            self.profiles.append(profile)
        save_profiles(self.profiles)
        self._refresh_profiles()

    def add_profile(self, login_after_save: bool = True) -> None:
        dialog = ProfileDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self._save_profile(dialog.result)
            self.selected_id = dialog.result["id"]
            self.tree.selection_set(self.selected_id)
            self.status.set("서버 프로필이 저장되었습니다.")
            if login_after_save:
                self.launch_remote(dialog.result)

    def edit_selected(self) -> None:
        profile = self._selected_profile()
        if not profile:
            return
        dialog = ProfileDialog(self, profile, title="서버 설정 수정")
        self.wait_window(dialog)
        if dialog.result:
            self._save_profile(dialog.result)
            self.status.set("프로필이 저장되었습니다.")

    def delete_selected(self) -> None:
        profile = self._selected_profile()
        if not profile:
            return
        if not messagebox.askyesno("삭제 확인", f"{profile.get('name')} 프로필을 삭제할까요?", parent=self):
            return
        self.profiles = [p for p in self.profiles if p.get("id") != profile.get("id")]
        save_profiles(self.profiles)
        self.selected_id = None
        self._refresh_profiles()
        self.status.set("프로필이 삭제되었습니다.")

    def login_selected(self) -> None:
        profile = self._selected_profile()
        if profile:
            if not profile.get("password") and not profile.get("key_path"):
                dialog = ProfileDialog(self, profile)
                self.wait_window(dialog)
                if not dialog.result:
                    return
                profile = dialog.result
                self._save_profile(profile)
            self.launch_remote(profile)

    def _profile_for_transfer(self) -> tuple[dict, str, str] | None:
        profile = self._selected_profile()
        if not profile:
            return None

        key_path = profile.get("key_path", "")
        if profile.get("password"):
            try:
                return profile, decrypt_text(profile["password"]), key_path
            except Exception as exc:
                messagebox.showerror("암호 오류", f"저장된 암호를 읽을 수 없습니다.\n{exc}", parent=self)
                return None
        if key_path:
            return profile, "", key_path

        dialog = ProfileDialog(self, profile)
        self.wait_window(dialog)
        if not dialog.result:
            return None

        password = dialog.result.get("runtime_password", "")
        key_path = dialog.result.get("key_path", "")
        if not password:
            if dialog.result.get("password"):
                password = decrypt_text(dialog.result["password"])
            elif not key_path:
                messagebox.showerror("인증 정보 필요", "파일 전송에는 서버 암호 또는 PEM 키 파일이 필요합니다.", parent=self)
                return None
        self._save_profile(dialog.result)
        return dialog.result, password, key_path

    def _password_for_profile(self, profile: dict) -> tuple[dict, str, str] | None:
        key_path = profile.get("key_path", "")
        if profile.get("password"):
            try:
                return profile, decrypt_text(profile["password"]), key_path
            except Exception as exc:
                messagebox.showerror("암호 오류", f"저장된 암호를 읽을 수 없습니다.\n{exc}", parent=self)
                return None
        if profile.get("runtime_password"):
            return profile, profile["runtime_password"], key_path
        if key_path:
            return profile, "", key_path

        dialog = ProfileDialog(self, profile)
        self.wait_window(dialog)
        if not dialog.result:
            return None
        password = dialog.result.get("runtime_password", "")
        key_path = dialog.result.get("key_path", "")
        if not password and dialog.result.get("password"):
            password = decrypt_text(dialog.result["password"])
        if not password and not key_path:
            messagebox.showerror("인증 정보 필요", "자동 로그인에는 서버 암호 또는 PEM 키 파일이 필요합니다.", parent=self)
            return None
        self._save_profile(dialog.result)
        return dialog.result, password, key_path

    def upload_selected(self) -> None:
        self.open_transfer_explorer()

    def download_selected(self) -> None:
        self.open_transfer_explorer()

    def open_transfer_explorer(self) -> None:
        auth = self._profile_for_transfer()
        if not auth:
            return
        profile, password, key_path = auth
        FileTransferDialog(self, profile, password, key_path)

    def open_docker_explorer(self) -> None:
        auth = self._profile_for_transfer()
        if not auth:
            return
        profile, password, key_path = auth
        DockerTransferDialog(self, profile, password, key_path)

    def _run_transfer(
        self,
        mode: str,
        profile: dict,
        password: str,
        local_path: str,
        remote_path: str,
    ) -> None:
        verb = "업로드" if mode == "upload" else "다운로드"
        self.status.set(f"{profile.get('name')} 서버 파일 {verb} 중...")

        def worker() -> None:
            try:
                import paramiko

                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    hostname=profile["host"],
                    port=int(profile.get("port") or 22),
                    username=profile["username"],
                    password=password or None,
                    key_filename=profile.get("key_path") or None,
                    look_for_keys=False,
                    allow_agent=False,
                    timeout=15,
                )
                try:
                    with client.open_sftp() as sftp:
                        if mode == "upload":
                            sftp.put(local_path, remote_path)
                        else:
                            sftp.get(remote_path, local_path)
                finally:
                    client.close()
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: self._transfer_done(False, verb, detail))
                return
            self.after(0, lambda: self._transfer_done(True, verb, "완료되었습니다."))

        threading.Thread(target=worker, daemon=True).start()

    def _transfer_done(self, success: bool, verb: str, detail: str) -> None:
        if success:
            self.status.set(f"파일 {verb}이 완료되었습니다.")
            messagebox.showinfo("전송 완료", f"파일 {verb}이 완료되었습니다.", parent=self)
        else:
            self.status.set(f"파일 {verb} 실패: {detail}")
            messagebox.showerror("전송 실패", f"파일 {verb}에 실패했습니다.\n{detail}", parent=self)

    def open_local_cmd(self) -> None:
        subprocess.Popen(["cmd.exe"], creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.status.set("로컬 CMD 창을 열었습니다.")

    def open_powershell(self) -> None:
        executable = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        subprocess.Popen([executable], creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.status.set("로컬 PowerShell 창을 열었습니다.")

    def launch_remote(self, profile: dict, launcher: dict | None = None, cwd: str = "") -> None:
        init_cmd = (launcher.get("init_cmd") or "").strip() if launcher else ""
        cli_command = [sys.executable, str(ROOT / "ssh_cli.py"), "--profile-id", profile["id"]]
        if cwd:
            cli_command.extend(["--cwd", cwd])
        if init_cmd:
            cli_command.extend(["--init-cmd", init_cmd])
        cli_command_line = subprocess.list2cmdline(cli_command)
        if launcher is None:
            subprocess.Popen(cli_command, creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=str(ROOT))
            self.status.set(f"{profile.get('name')} 서버 로그인 창을 열었습니다.")
            return

        path = launcher.get("path", "")
        if not path or not Path(path).exists():
            messagebox.showerror("실행 실패", "선택한 command 실행 파일 경로가 존재하지 않습니다.", parent=self)
            return
        workdir = launcher.get("workdir") or str(Path(path).parent)
        if workdir and not Path(workdir).is_dir():
            messagebox.showerror("실행 실패", "선택한 command 작업 폴더가 존재하지 않습니다.", parent=self)
            return

        def build_remote_shell_command() -> str:
            # export 등으로 지정한 환경변수는 뒤에 실행되는 bash(대화형 쉘)에 그대로
            # 상속되므로 초기화 명령어를 cd보다 앞에 실행한다.
            segments = []
            if init_cmd:
                segments.append(init_cmd)
            if cwd:
                segments.append(f"cd {shell_quote(cwd)}")
            segments.append("bash")
            return " ; ".join(segments)

        try:
            launcher_args = shlex.split(launcher.get("args", ""), posix=False)
            executable_name = Path(path).name.lower()
            if executable_name == "putty.exe":
                auth = self._password_for_profile(profile)
                if not auth:
                    return
                profile, password, key_path = auth
                command = [
                    path,
                    *launcher_args,
                    "-ssh",
                    profile["host"],
                    "-P",
                    str(int(profile.get("port") or 22)),
                    "-l",
                    profile["username"],
                ]
                if cwd or init_cmd:
                    # -t 로 pty를 강제 할당해 원격 쉘 명령(초기화 명령어/경로 이동)을
                    # 실행한 뒤에도 대화형 bash 세션이 유지되게 한다.
                    command.extend(["-t", build_remote_shell_command()])

                def _insert_before_remote_command(opts: list[str]) -> None:
                    if "-t" in command:
                        idx = command.index("-t")
                        command[idx:idx] = opts
                    else:
                        command.extend(opts)

                if password:
                    _insert_before_remote_command(["-pw", password])
                if key_path:
                    _insert_before_remote_command(["-i", key_path])
            elif executable_name == "plink.exe":
                auth = self._password_for_profile(profile)
                if not auth:
                    return
                profile, password, key_path = auth
                if "-no-antispoof" not in {arg.lower() for arg in launcher_args}:
                    launcher_args.append("-no-antispoof")
                remote_shell_command = build_remote_shell_command()
                command = [
                    path,
                    *launcher_args,
                    "-ssh",
                    f"{profile['username']}@{profile['host']}",
                    "-P",
                    str(int(profile.get("port") or 22)),
                    "-t",
                    remote_shell_command,
                ]
                if password:
                    command[command.index("-t"):command.index("-t")] = ["-pw", password]
                if key_path:
                    command[command.index("-t"):command.index("-t")] = ["-i", key_path]
            elif executable_name in {"cygwin.bat", "mintty.exe", "bash.exe"}:
                cygwin_root = Path(path).parent
                if executable_name in {"mintty.exe", "bash.exe"}:
                    cygwin_root = cygwin_root.parent
                mintty = cygwin_root / "bin" / "mintty.exe"
                bash = cygwin_root / "bin" / "bash.exe"
                cygwin_cli_command = " ".join(
                    [shell_quote(cygwin_path(cli_command[0])), *[shell_quote(part) for part in cli_command[1:]]]
                )
                bash_command = (
                    f"cd {shell_quote(cygwin_path(str(ROOT)))}; "
                    f"{cygwin_cli_command}; "
                    "exec bash"
                )
                if executable_name in {"cygwin.bat", "mintty.exe"} and mintty.exists() and bash.exists():
                    command = [str(mintty), *launcher_args, "-e", str(bash), "--login", "-lc", bash_command]
                elif executable_name == "bash.exe":
                    command = [path, *launcher_args, "--login", "-lc", bash_command]
                else:
                    command = [path, *launcher_args]
            elif executable_name in {"cmd.exe", "cmd"}:
                command = [path, *launcher_args, "/k", cli_command_line]
            elif executable_name in {"powershell.exe", "powershell", "pwsh.exe", "pwsh"}:
                ps_command = "& " + " ".join(powershell_quote(part) for part in cli_command)
                command = [path, *launcher_args, "-NoExit", "-Command", ps_command]
            elif executable_name in {"wt.exe", "wt"}:
                command = [path, *launcher_args, "new-tab", "cmd.exe", "/k", cli_command_line]
            else:
                command = [path, *launcher_args, *cli_command]
            subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=workdir or str(ROOT))
        except Exception as exc:
            messagebox.showerror("실행 실패", f"선택한 command로 CLI를 실행할 수 없습니다.\n{exc}", parent=self)
            return

        self.status.set(f"{launcher.get('name')} command로 {profile.get('name')} 서버 CLI를 실행했습니다.")


if __name__ == "__main__":
    app = CommandManager()
    app.mainloop()


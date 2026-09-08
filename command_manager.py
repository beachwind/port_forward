from __future__ import annotations

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
    def __init__(self, master: tk.Tk, profile: dict, password: str, key_path: str = "", docker_mode: bool = False):
        super().__init__(master)
        self.manager = master
        self.docker_mode = docker_mode
        self.title(f"{'Docker' if docker_mode else '파일 전송'} - {profile.get('name', profile.get('host', 'server'))}")
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
        self.docker_container: dict | None = None
        self.docker_cwd = "/"
        self.drag_data: dict | None = None
        self.favorites = load_favorites()
        self.local_favorite_var = tk.StringVar()
        self.remote_favorite_var = tk.StringVar()
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text_var = tk.StringVar(value="0%")

        self.folder_icon = self._icon("#d9a441")
        self.file_icon = self._icon("#6f93c8")
        self.action_icons = self._build_action_icons()

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

    def _build_action_icons(self) -> dict[str, tk.PhotoImage]:
        specs = {
            "refresh": ("#4f81bd", "loop"),
            "register": ("#2f855a", "plus"),
            "go": ("#4f81bd", "arrow"),
            "folder": ("#d9a441", "folder_plus"),
            "empty": ("#6f93c8", "file_plus"),
            "view": ("#5b6f8f", "eye"),
            "compare": ("#2b6cb0", "split"),
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
        elif shape == "split":
            image.put(color, to=(3, 4, 7, 12))
            image.put(color, to=(9, 4, 13, 12))
            image.put("#ffffff", to=(5, 6, 6, 10))
            image.put("#ffffff", to=(11, 6, 12, 10))
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
            # ("compare", "비교", self.compare_selected_files),
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
        ttk.Label(parent, text="Docker 탐색기" if self.docker_mode else "원격 서버 파일 탐색기", font=("Segoe UI", 11, "bold")).grid(
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
            values=self.sorted_remote_favorites(),
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
            ("compare", "비교", self.compare_selected_files),
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
        if self.docker_mode:
            remote_actions.extend(
                [
                    ("delete", "Docker 컨테이너 중지", self.stop_selected_docker_container),
                    ("terminal", "Docker 컨테이너 빌드", self.build_selected_docker_container),
                    ("go", "Docker 컨테이너 run", self.run_selected_docker_container),
                ]
            )
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
        else:
            view_menu = tk.Menu(menu, tearoff=0)
            view_menu.add_command(label="(default)", command=self.view_remote_selected)
            self._add_editor_view_menu_items(view_menu, "remote")
            menu.add_command(label="새 폴더", command=self.create_remote_folder)
            menu.add_command(label="빈 파일", command=self.create_remote_empty_file)
            menu.add_cascade(label="파일 보기", menu=view_menu)
            menu.add_command(label="편집", command=self.edit_remote_with_gvim)
            menu.add_command(label="비교", command=self.compare_selected_files)
            menu.add_command(label="로그 보기", command=self.view_remote_log)
            menu.add_command(label="로그 보기(색상)", command=self.view_remote_log_color)
            menu.add_command(label="파일명 변경", command=self.rename_remote_selected)
            menu.add_command(label="삭제", command=self.delete_remote_selected)
            if self.docker_mode:
                menu.add_separator()
                menu.add_command(label="Docker 컨테이너 중지", command=self.stop_selected_docker_container)
                menu.add_command(label="Docker 컨테이너 빌드", command=self.build_selected_docker_container)
                menu.add_command(label="Docker 컨테이너 run", command=self.run_selected_docker_container)
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
        if self.docker_mode:
            self.refresh_docker_remote()
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

    def refresh_docker_remote(self) -> None:
        tree = self._tree_widget(self.remote_tree)
        tree.delete(*tree.get_children())
        if not self.docker_container:
            self.remote_path_var.set("Docker containers")
            threading.Thread(target=self._load_docker_containers, daemon=True).start()
            return
        self.remote_path_var.set(f"[Docker:{self.docker_container['name']}] {self.docker_cwd}")
        if self.docker_cwd != "/":
            tree.insert("", "end", iid="docker:..", text="..", image=self.folder_icon, values=("폴더", "", ""))
        threading.Thread(target=self._load_docker_path, daemon=True).start()

    def _remote_exec(self, command: str, get_pty: bool = False) -> tuple[int, str, str]:
        stdin, stdout, stderr = self.client.exec_command(command, get_pty=get_pty)
        stdin.close()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return stdout.channel.recv_exit_status(), out, err

    def _load_docker_containers(self) -> None:
        try:
            code, out, err = self._remote_exec("docker ps --format '{{.ID}}\\t{{.Image}}\\t{{.Names}}\\t{{.Status}}'")
            if code != 0:
                raise RuntimeError(err.strip() or f"docker ps 종료 코드: {code}")
            rows = []
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) < 4:
                    continue
                container_id, image, name, status_text = parts[:4]
                rows.append((container_id, image, name, status_text))
            self.after(0, lambda: self._fill_docker_containers(rows))
        except Exception as exc:
            detail = str(exc)
            self.after(0, lambda: self.status.set(f"Docker 목록을 읽을 수 없습니다: {detail}"))

    def _fill_docker_containers(self, rows: list[tuple[str, str, str, str]]) -> None:
        tree = self._tree_widget(self.remote_tree)
        tree.delete(*tree.get_children())
        for container_id, image, name, status_text in rows:
            tree.insert(
                "",
                "end",
                iid=f"docker_container:{container_id}",
                text=f"[Docker:{name}]",
                image=self.folder_icon,
                values=("컨테이너", image, status_text),
            )
        self.status.set("Docker 컨테이너 목록을 표시했습니다. 컨테이너를 더블클릭하면 내부 파일을 봅니다.")

    def _load_docker_path(self) -> None:
        container_id = self.docker_container["id"]
        list_script = (
            f"cd {shlex.quote(self.docker_cwd)} && "
            "for f in .* *; do "
            "[ \"$f\" = \".\" ] || [ \"$f\" = \"..\" ] && continue; "
            "[ -e \"$f\" ] || continue; "
            "if [ -d \"$f\" ]; then printf 'D\\t%s\\t0\\n' \"$f\"; "
            "else printf 'F\\t%s\\t%s\\n' \"$f\" \"$(wc -c < \"$f\" 2>/dev/null || echo 0)\"; fi; "
            "done"
        )
        try:
            code, out, err = self._remote_exec(f"docker exec {shlex.quote(container_id)} /bin/sh -lc {shlex.quote(list_script)}")
            if code != 0:
                raise RuntimeError(err.strip() or f"docker exec 종료 코드: {code}")
            rows = []
            for line in out.splitlines():
                kind, name, size_text = (line.split("\t") + ["", "", ""])[:3]
                if not name:
                    continue
                is_dir = kind == "D"
                size = None if is_dir else int(size_text.strip() or "0")
                rows.append((posixpath.join(self.docker_cwd, name), name, is_dir, size))
            self.after(0, lambda: self._fill_docker_path(rows))
        except Exception as exc:
            detail = str(exc)
            self.after(0, lambda: self.status.set(f"Docker 경로를 읽을 수 없습니다: {detail}"))

    def _fill_docker_path(self, rows: list[tuple[str, str, bool, int | None]]) -> None:
        tree = self._tree_widget(self.remote_tree)
        for path, name, is_dir, size in sorted(rows, key=lambda row: (not row[2], row[1].lower())):
            tree.insert(
                "",
                "end",
                iid=f"docker:{path}",
                text=name,
                image=self.folder_icon if is_dir else self.file_icon,
                values=("폴더" if is_dir else "파일", human_size(size), ""),
            )
        self.status.set(f"Docker 컨테이너 내부 경로 표시: {self.docker_cwd}")

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
        self.remote_favorite_combo.configure(values=self.sorted_remote_favorites())

    def sorted_remote_favorites(self) -> list[str]:
        username = str(self.profile.get("username", "")).strip().strip("/")
        own_home = f"/home/{username}/" if username else ""
        own_home_root = f"/home/{username}" if username else ""

        def sort_key(path: str) -> tuple[int, str]:
            normalized = path.replace("\\", "/").rstrip("/")
            if username and (normalized == own_home_root or normalized.startswith(own_home)):
                return 0, normalized.lower()
            if normalized.startswith("/home/"):
                return 1, normalized.lower()
            return 2, normalized.lower()

        return sorted(self.favorites.get("remote_paths", []), key=sort_key)

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
        launcher = self.manager.selected_command() if hasattr(self.manager, "selected_command") else None
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
        if item in {"remote:..", "docker:.."} or item.startswith("docker_container:"):
            return None
        if self.docker_mode and item.startswith("docker:"):
            return item.removeprefix("docker:"), tree.set(item, "type") == "폴더"
        return item.removeprefix("remote:"), tree.set(item, "type") == "폴더"

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

    def compare_selected_files(self) -> None:
        local_path = self.selected_local_path()
        remote_selected = self.selected_remote_item()
        if not local_path or not remote_selected:
            messagebox.showinfo("선택 필요", "로컬 파일과 원격 파일을 각각 하나씩 선택하세요.", parent=self)
            return
        remote_path, is_dir = remote_selected
        if local_path.is_dir() or is_dir:
            messagebox.showinfo("비교", "폴더는 비교할 수 없습니다. 파일을 선택하세요.", parent=self)
            return
        bash_path = Path(r"C:\Util\cygwin\bin\bash.exe")
        if not bash_path.exists():
            messagebox.showerror("비교 실패", f"Cygwin bash 실행 파일을 찾을 수 없습니다.\n{bash_path}", parent=self)
            return
        temp_dir = Path(tempfile.gettempdir()) / "CommandManager" / "remote_compare" / self.profile["id"]
        remote_local_path = temp_dir / posixpath.basename(remote_path)
        self.status.set("비교할 원격 파일을 내려받는 중...")

        def worker() -> None:
            try:
                temp_dir.mkdir(parents=True, exist_ok=True)
                if self.docker_mode and self.docker_container:
                    self._docker_copy_to_remote_temp(remote_path, str(remote_local_path))
                else:
                    with self.sftp_lock:
                        self.sftp.get(remote_path, str(remote_local_path))
                diff_command = (
                    "vimdiff -- "
                    f"{shell_quote(cygwin_path(str(local_path)))} "
                    f"{shell_quote(cygwin_path(str(remote_local_path)))}"
                )
                self.after(0, lambda: self.status.set("vimdiff 비교 창을 열었습니다."))
                subprocess.Popen([str(bash_path), "--login", "-lc", diff_command], creationflags=subprocess.CREATE_NEW_CONSOLE)
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("비교 실패", f"파일 비교를 실행할 수 없습니다.\n{detail}", parent=self))

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
            messagebox.showerror("편집 실패", f"Cygwin bash 실행 파일을 찾을 수 없습니다.\n{bash_path}", parent=self)
            return
        temp_dir = Path(tempfile.gettempdir()) / "CommandManager" / "remote_edit" / self.profile["id"]
        local_path = temp_dir / posixpath.basename(remote_path)
        self.status.set("원격 파일을 편집용으로 내려받는 중...")

        def worker() -> None:
            try:
                temp_dir.mkdir(parents=True, exist_ok=True)
                if self.docker_mode and self.docker_container:
                    self._docker_copy_to_remote_temp(remote_path, str(local_path))
                else:
                    with self.sftp_lock:
                        self.sftp.get(remote_path, str(local_path))
                before = local_path.stat()
                before_signature = (before.st_mtime_ns, before.st_size)
                vim_command = f"vim -- {shell_quote(cygwin_path(str(local_path)))}"
                self.after(0, lambda: self.status.set("vim 편집 중... 저장 후 터미널 창을 닫으면 원격에 업로드합니다."))
                process = subprocess.Popen([str(bash_path), "--login", "-lc", vim_command], creationflags=subprocess.CREATE_NEW_CONSOLE)
                process.wait()
                if not local_path.exists():
                    self.after(0, lambda: self.status.set("편집 파일이 삭제되어 업로드하지 않았습니다."))
                    return
                after = local_path.stat()
                if (after.st_mtime_ns, after.st_size) == before_signature:
                    self.after(0, lambda: self.status.set("변경 사항이 없어 업로드하지 않았습니다."))
                    return
                self.after(0, lambda: self.status.set("수정된 파일을 원격 서버에 업로드하는 중..."))
                if self.docker_mode and self.docker_container:
                    self._docker_copy_local_to_container(str(local_path), remote_path)
                else:
                    with self.sftp_lock:
                        self.sftp.put(str(local_path), remote_path)
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("편집 실패", f"원격 파일 편집/업로드 중 오류가 발생했습니다.\n{detail}", parent=self))
                return
            self.after(0, lambda: (self.refresh_remote(), self.status.set("vim 편집 내용을 원격 파일에 업로드했습니다.")))

        threading.Thread(target=worker, daemon=True).start()

    def view_today_remote_log(self) -> None:
        selected = self.today_remote_log_item()
        if not selected:
            messagebox.showinfo("선택 필요", "로그를 볼 원격 파일을 선택하세요.", parent=self)
            return
        self.open_remote_log(selected[0], color=False)

    def today_remote_log_item(self) -> tuple[str, bool] | None:
        if self.docker_mode:
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
            except Exception as exc:
                append_text(f"\n[로그 보기 종료: {exc}]\n")
            finally:
                if channel:
                    channel.close()

        threading.Thread(target=worker, daemon=True).start()

    def view_remote_log(self) -> None:
        self._view_remote_log(color=False)

    def view_remote_log_color(self) -> None:
        self._view_remote_log(color=True)

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
        launcher = self.manager.selected_command() if hasattr(self.manager, "selected_command") else None
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
        ttk.Label(toolbar, textvariable=state_var).grid(row=0, column=2, sticky="w")

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
        if self.docker_mode:
            if item == "docker:..":
                parent = posixpath.dirname(self.docker_cwd.rstrip("/")) or "/"
                self.docker_cwd = parent
                self.refresh_remote()
                return
            if item.startswith("docker_container:"):
                container_id = item.removeprefix("docker_container:")
                name = tree.item(item, "text").removeprefix("[Docker:").removesuffix("]")
                image = tree.set(item, "size")
                self.docker_container = {"id": container_id, "name": name, "image": image}
                self.docker_cwd = "/"
                self.refresh_remote()
                return
            if item.startswith("docker:") and tree.set(item, "type") == "폴더":
                self.docker_cwd = item.removeprefix("docker:")
                self.refresh_remote()
                return
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
        if not item or item.endswith(":.."):
            self.drag_data = None
            return
        if not (self.docker_mode and source == "remote") and tree.set(item, "type") != "파일":
            self.drag_data = None
            return
        selected = list(tree.selection())
        if item not in selected:
            tree.selection_set(item)
            selected = [item]
        file_items = []
        for selected_item in selected:
            if selected_item.endswith(":.."):
                continue
            if self.docker_mode and source == "remote":
                file_items.append(selected_item)
            elif tree.set(selected_item, "type") == "파일":
                file_items.append(selected_item)
        self.drag_data = {"source": source, "items": file_items}

    def finish_drag(self, event) -> None:
        if not self.drag_data:
            return
        source = self.drag_data["source"]
        local_tree = self._tree_widget(self.local_tree)
        remote_tree = self._tree_widget(self.remote_tree)
        target_tree = self._drop_tree_at_pointer(event.x_root, event.y_root)
        if source == "local" and target_tree == remote_tree:
            if self.docker_mode:
                self.confirm_docker_uploads(self.drag_data["items"], self._drop_docker_folder(remote_tree, event.x_root, event.y_root))
                self.drag_data = None
                return
            remote_folder = self._drop_remote_folder(remote_tree, event.x_root, event.y_root) or self.remote_cwd
            transfers = []
            for item in self.drag_data["items"]:
                local_path = item.removeprefix("local:")
                remote_path = posixpath.join(remote_folder, Path(local_path).name)
                transfers.append((local_path, remote_path))
            self.confirm_transfers("upload", transfers)
        elif source == "remote" and target_tree == local_tree:
            if self.docker_mode:
                self.confirm_docker_downloads(self.drag_data["items"], self._drop_local_folder(local_tree, event.x_root, event.y_root) or self.local_cwd)
                self.drag_data = None
                return
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
        if self.docker_mode and row.startswith("docker:"):
            return row.removeprefix("docker:")
        return row.removeprefix("remote:")

    def _drop_docker_folder(self, tree: ttk.Treeview, x_root: int, y_root: int) -> str:
        row = tree.identify_row(y_root - tree.winfo_rooty())
        if row and row.startswith("docker:") and tree.set(row, "type") == "폴더":
            return row.removeprefix("docker:")
        return self.docker_cwd

    def _docker_copy_to_remote_temp(self, docker_path: str, local_path: str) -> None:
        if not self.docker_container:
            raise RuntimeError("Docker 컨테이너가 선택되지 않았습니다.")
        temp_remote = f"/tmp/command_manager_{int(time.time() * 1000)}_{posixpath.basename(docker_path)}"
        code, _out, err = self._remote_exec(
            f"docker cp {shlex.quote(self.docker_container['id'] + ':' + docker_path)} {shlex.quote(temp_remote)}"
        )
        if code != 0:
            raise RuntimeError(err.strip() or "docker cp 다운로드 준비 실패")
        try:
            with self.sftp_lock:
                self._download_remote_path_recursive(temp_remote, Path(local_path))
        finally:
            self._remote_exec(f"rm -rf -- {shlex.quote(temp_remote)}")

    def _download_remote_path_recursive(self, remote_path: str, local_path: Path) -> None:
        attrs = self.sftp.stat(remote_path)
        if stat.S_ISDIR(attrs.st_mode):
            local_path.mkdir(parents=True, exist_ok=True)
            for item in self.sftp.listdir_attr(remote_path):
                self._download_remote_path_recursive(
                    posixpath.join(remote_path, item.filename),
                    local_path / item.filename,
                )
            return
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self.sftp.get(remote_path, str(local_path))

    def _docker_copy_local_to_container(self, local_path: str, docker_path: str) -> None:
        if not self.docker_container:
            raise RuntimeError("Docker 컨테이너가 선택되지 않았습니다.")
        temp_remote = f"/tmp/command_manager_upload_{int(time.time() * 1000)}_{Path(local_path).name}"
        try:
            with self.sftp_lock:
                self.sftp.put(local_path, temp_remote)
            code, _out, err = self._remote_exec(
                f"docker cp {shlex.quote(temp_remote)} {shlex.quote(self.docker_container['id'] + ':' + docker_path)}"
            )
            if code != 0:
                raise RuntimeError(err.strip() or "docker cp 업로드 실패")
        finally:
            self._remote_exec(f"rm -f -- {shlex.quote(temp_remote)}")

    def confirm_docker_downloads(self, items: list[str], local_folder: Path) -> None:
        if not self.docker_container:
            messagebox.showinfo("선택 필요", "Docker 컨테이너를 선택하세요.", parent=self)
            return
        docker_paths = []
        for item in items:
            if item.startswith("docker_container:"):
                docker_paths.append("/")
            elif item.startswith("docker:"):
                docker_paths.append(item.removeprefix("docker:"))
        if not docker_paths:
            return
        if not messagebox.askyesno("Docker 다운로드", f"선택한 Docker 항목 {len(docker_paths)}개를 다운로드할까요?", parent=self):
            return

        def worker() -> None:
            try:
                for index, docker_path in enumerate(docker_paths, start=1):
                    name = self.docker_container["name"] if docker_path == "/" else posixpath.basename(docker_path)
                    self.after(0, lambda i=index: self.status.set(f"Docker 항목 다운로드 중... ({i}/{len(docker_paths)})"))
                    self._docker_copy_to_remote_temp(docker_path, str(local_folder / name))
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("Docker 다운로드 실패", detail, parent=self))
                return
            self.after(0, lambda: (self.refresh_local(), self.status.set("Docker 항목 다운로드 완료.")))

        threading.Thread(target=worker, daemon=True).start()

    def confirm_docker_uploads(self, items: list[str], docker_folder: str) -> None:
        if not self.docker_container:
            messagebox.showinfo("선택 필요", "업로드할 Docker 컨테이너를 먼저 더블클릭하세요.", parent=self)
            return
        local_paths = [Path(item.removeprefix("local:")) for item in items if item.startswith("local:")]
        local_paths = [path for path in local_paths if path.is_file()]
        if not local_paths:
            return
        if not messagebox.askyesno("Docker 업로드", f"선택한 로컬 파일 {len(local_paths)}개를 Docker 컨테이너에 업로드할까요?", parent=self):
            return

        def worker() -> None:
            try:
                for index, local_path in enumerate(local_paths, start=1):
                    self.after(0, lambda i=index: self.status.set(f"Docker 컨테이너 소스 업데이트 중... ({i}/{len(local_paths)})"))
                    self._docker_copy_local_to_container(str(local_path), posixpath.join(docker_folder, local_path.name))
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("Docker 업로드 실패", detail, parent=self))
                return
            self.after(0, lambda: (self.refresh_remote(), self.status.set("Docker 컨테이너 소스 업데이트 완료.")))

        threading.Thread(target=worker, daemon=True).start()

    def selected_docker_container(self) -> dict | None:
        if not self.docker_mode:
            return None
        tree = self._tree_widget(self.remote_tree)
        selected = tree.selection()
        if selected and selected[0].startswith("docker_container:"):
            item = selected[0]
            return {
                "id": item.removeprefix("docker_container:"),
                "name": tree.item(item, "text").removeprefix("[Docker:").removesuffix("]"),
                "image": tree.set(item, "size"),
            }
        return self.docker_container

    def stop_selected_docker_container(self) -> None:
        container = self.selected_docker_container()
        if not container:
            messagebox.showinfo("선택 필요", "Docker 컨테이너를 선택하세요.", parent=self)
            return
        if not messagebox.askyesno("Docker 컨테이너 중지", f"{container['name']} 컨테이너를 중지할까요?", parent=self):
            return
        self._run_docker_host_command(
            f"docker stop {shlex.quote(container['id'])}",
            "Docker 컨테이너를 중지했습니다.",
        )

    def build_selected_docker_container(self) -> None:
        container = self.selected_docker_container()
        default_image = container.get("image", "") if container else ""
        image = simpledialog.askstring("Docker 컨테이너 빌드", "빌드할 이미지 태그를 입력하세요.", initialvalue=default_image, parent=self)
        if not image:
            return
        context = simpledialog.askstring("Docker 컨테이너 빌드", "원격 서버의 build context 경로를 입력하세요.", initialvalue=self.remote_cwd if self.remote_cwd != "." else "~", parent=self)
        if not context:
            return
        self._run_docker_host_command(
            f"cd {shlex.quote(context)} && docker build -t {shlex.quote(image)} .",
            "Docker 이미지 빌드를 시작했습니다.",
        )

    def run_selected_docker_container(self) -> None:
        container = self.selected_docker_container()
        default_image = container.get("image", "") if container else ""
        command = simpledialog.askstring(
            "Docker 컨테이너 run",
            "실행할 docker run 명령을 입력하세요.",
            initialvalue=f"docker run -d {default_image}".strip(),
            parent=self,
        )
        if not command:
            return
        self._run_docker_host_command(command, "Docker run 명령을 실행했습니다.")

    def _run_docker_host_command(self, command: str, success_message: str) -> None:
        self.status.set("Docker 명령 실행 중...")

        def worker() -> None:
            try:
                code, out, err = self._remote_exec(command, get_pty=True)
                if code != 0:
                    raise RuntimeError(err.strip() or out.strip() or f"Docker 명령 종료 코드: {code}")
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("Docker 명령 실패", detail, parent=self))
                return
            self.after(0, lambda: (self.refresh_remote(), self.status.set(success_message)))

        threading.Thread(target=worker, daemon=True).start()

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

    def _transfer_done(self, success: bool, verb: str, detail: str, mode: str = "", transfers: list[tuple[str, str]] | None = None) -> None:
        if success:
            self.progress_var.set(100)
            self.progress_text_var.set("100%")
            self.status.set(f"파일 {verb} 완료. (100%)")
            self.refresh_all()
            if transfers:
                self.show_transfer_notification(mode, transfers)
        else:
            self.progress_var.set(0)
            self.progress_text_var.set("0%")
            self.status.set(f"파일 {verb} 실패: {detail}")
            messagebox.showerror("전송 실패", f"파일 {verb}에 실패했습니다.\n{detail}", parent=self)

    def show_transfer_notification(self, mode: str, transfers: list[tuple[str, str]]) -> None:
        target_local, target_remote = transfers[-1]
        verb = "업로드" if mode == "upload" else "다운로드"
        target_name = Path(target_local).name if mode == "download" else posixpath.basename(target_remote)
        popup = tk.Toplevel(self)
        popup.title("파일 전송 완료")
        popup.resizable(False, False)
        popup.attributes("-topmost", True)
        popup.after(20000, lambda: popup.winfo_exists() and popup.destroy())
        frame = ttk.Frame(popup, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(frame, text=f"파일 {verb} 완료", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text=target_name).grid(row=1, column=0, sticky="w", pady=(4, 0))
        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        actions.grid_remove()
        ttk.Button(actions, text="파일 탐색기", command=lambda: self.open_transfer_target(mode, target_local, target_remote)).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(actions, text="실행 권한 주기", command=lambda: self.grant_transfer_execute_permission(mode, target_local, target_remote)).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(actions, text="실행", command=lambda: self.run_transfer_target(mode, target_local, target_remote)).grid(row=0, column=2)

        def reveal(_event=None) -> None:
            actions.grid()

        popup.bind("<Button-1>", reveal)
        frame.bind("<Button-1>", reveal)
        for child in frame.winfo_children():
            child.bind("<Button-1>", reveal)
        popup.update_idletasks()
        x = max(0, popup.winfo_screenwidth() - popup.winfo_width() - 24)
        y = max(0, popup.winfo_screenheight() - popup.winfo_height() - 64)
        popup.geometry(f"+{x}+{y}")

    def open_transfer_target(self, mode: str, local_path: str, remote_path: str) -> None:
        if mode == "download":
            subprocess.Popen(["explorer.exe", "/select,", str(Path(local_path))])
            return
        self._set_remote_cwd(posixpath.dirname(remote_path) or ".")

    def grant_transfer_execute_permission(self, mode: str, local_path: str, remote_path: str) -> None:
        if mode == "download":
            messagebox.showinfo("실행 권한", "Windows 로컬 파일에는 chmod +x를 적용하지 않습니다.", parent=self)
            return
        self._run_remote_background(f"chmod +x -- {shlex.quote(remote_path)}", "원격 파일에 실행 권한을 부여했습니다.")

    def run_transfer_target(self, mode: str, local_path: str, remote_path: str) -> None:
        if mode == "download":
            os.startfile(local_path)
            return
        remote_dir = posixpath.dirname(remote_path) or "."
        remote_name = posixpath.basename(remote_path)
        self._run_remote_background(f"cd {shlex.quote(remote_dir)} && ./{shlex.quote(remote_name)}", f"원격 실행 명령을 보냈습니다: ./{remote_name}")

    def _run_remote_background(self, command: str, success_message: str) -> None:
        def worker() -> None:
            try:
                self.client.exec_command(command)
            except Exception as exc:
                detail = str(exc)
                self.after(0, lambda: messagebox.showerror("실행 실패", detail, parent=self))
                return
            self.after(0, lambda: self.status.set(success_message))

        threading.Thread(target=worker, daemon=True).start()

    def close(self) -> None:
        try:
            with self.sftp_lock:
                if self.sftp:
                    self.sftp.close()
                if self.client:
                    self.client.close()
        finally:
            self.destroy()


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
        self.geometry("860x460")
        self.minsize(760, 400)
        self.heading = heading
        self.browse_title = browse_title
        self.commands = [dict(command) for command in commands]
        self.result: list[dict] | None = None
        self.selected_id: str | None = None

        self.name_var = tk.StringVar()
        self.path_var = tk.StringVar()
        self.args_var = tk.StringVar()
        self.workdir_var = tk.StringVar()

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
        ]
        for row, (label, var, browse) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(form, textvariable=var).grid(row=row, column=1, sticky="ew", pady=5)
            if browse:
                ttk.Button(form, text="찾기", command=browse).grid(row=row, column=2, padx=(6, 0), pady=5)

        actions = ttk.Frame(form)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 0))
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

    def save_current(self) -> bool:
        name = self.name_var.get().strip()
        path = self.path_var.get().strip()
        args = self.args_var.get().strip()
        workdir = self.workdir_var.get().strip()
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
                    "예" if profile.get("password") else "아니오",
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
        FileTransferDialog(self, profile, password, key_path, docker_mode=True)

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
                    password=password,
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
        cli_command = [sys.executable, str(ROOT / "ssh_cli.py"), "--profile-id", profile["id"]]
        if cwd:
            cli_command.extend(["--cwd", cwd])
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

        try:
            launcher_args = shlex.split(launcher.get("args", ""), posix=False)
            executable_name = Path(path).name.lower()
            if executable_name == "putty.exe":
                auth = self._password_for_profile(profile)
                if not auth:
                    return
                profile, password = auth
                command = [
                    path,
                    *launcher_args,
                    "-ssh",
                    profile["host"],
                    "-P",
                    str(int(profile.get("port") or 22)),
                    "-l",
                    profile["username"],
                    "-pw",
                    password,
                ]
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


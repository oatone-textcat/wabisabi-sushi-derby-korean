from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import traceback
import winreg
from datetime import datetime
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    X,
    Button,
    Entry,
    Frame,
    Label,
    StringVar,
    Text,
    Tk,
    filedialog,
    messagebox,
)
from tkinter.scrolledtext import ScrolledText


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_ID = "4113050"
GAME_NAME = "Wabisabi SushiDerby"

# Used only as a fallback if Steam's manifest cannot be parsed.
EXPECTED_INSTALL_DIR_NAMES = (
    "Wabisabi SushiDerby",
    "Wabisabi SushiDerby Demo",
)

# A file or directory that should normally exist in the game directory.
# Add more entries here if you want stricter validation.
GAME_DIRECTORY_MARKERS = (
    "Wabisabi SushiDerby.exe",
    "Wabisabi_Data",
)

PATCH_FOLDER_NAME = "patch_files"
BACKUP_FOLDER_NAME = "KoreanPatch_Backup"

WINDOW_TITLE = "와비사비 스시 더비 한글 패치 (제작: 팀 글냥이)"
WINDOW_SIZE = "680x430"


# ---------------------------------------------------------------------------
# Runtime path helpers
# ---------------------------------------------------------------------------

def application_base_directory() -> Path:
    """
    Return the directory containing bundled resources.

    When running normally:
        directory containing patch_installer.py

    When running as a PyInstaller executable:
        PyInstaller's temporary extraction directory
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)

    return Path(__file__).resolve().parent


def patch_source_directory() -> Path:
    return application_base_directory() / PATCH_FOLDER_NAME


# ---------------------------------------------------------------------------
# Steam detection
# ---------------------------------------------------------------------------

def read_steam_path_from_registry() -> Path | None:
    """
    Try common Steam registry keys.

    Steam may be registered under either the native registry path or
    WOW6432Node depending on the Windows installation.
    """
    candidates = (
        (
            winreg.HKEY_CURRENT_USER,
            r"Software\Valve\Steam",
            "SteamPath",
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Valve\Steam",
            "InstallPath",
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Valve\Steam",
            "InstallPath",
        ),
    )

    for root, key_path, value_name in candidates:
        try:
            with winreg.OpenKey(root, key_path) as key:
                value, _ = winreg.QueryValueEx(key, value_name)

            steam_path = Path(value).expanduser()

            if steam_path.is_dir():
                return steam_path

        except (FileNotFoundError, OSError):
            continue

    return None


def parse_libraryfolders_vdf(vdf_path: Path) -> list[Path]:
    """
    Extract Steam library paths from libraryfolders.vdf.

    This intentionally uses a small parser instead of requiring an external
    VDF package. It supports the modern and older common VDF formats.
    """
    if not vdf_path.is_file():
        return []

    try:
        content = vdf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    paths: list[Path] = []

    # Modern format:
    # "path"    "D:\\SteamLibrary"
    modern_matches = re.findall(
        r'"path"\s+"([^"]+)"',
        content,
        flags=re.IGNORECASE,
    )

    for raw_path in modern_matches:
        normalized = raw_path.replace("\\\\", "\\")
        paths.append(Path(normalized))

    # Older format:
    # "1"    "D:\\SteamLibrary"
    old_matches = re.findall(
        r'"\d+"\s+"([^"]+)"',
        content,
    )

    for raw_path in old_matches:
        normalized = raw_path.replace("\\\\", "\\")
        paths.append(Path(normalized))

    return unique_existing_paths(paths)


def unique_existing_paths(paths: list[Path]) -> list[Path]:
    results: list[Path] = []
    seen: set[str] = set()

    for path in paths:
        try:
            normalized = str(path.resolve()).lower()
        except OSError:
            normalized = str(path.absolute()).lower()

        if normalized in seen:
            continue

        seen.add(normalized)

        if path.is_dir():
            results.append(path)

    return results


def get_steam_library_directories() -> list[Path]:
    """
    Return all detected Steam library roots.

    A library root is the directory containing steamapps, such as:
        C:\\Program Files (x86)\\Steam
        D:\\SteamLibrary
    """
    steam_root = read_steam_path_from_registry()

    if steam_root is None:
        return []

    libraries = [steam_root]

    library_file = steam_root / "steamapps" / "libraryfolders.vdf"
    libraries.extend(parse_libraryfolders_vdf(library_file))

    return unique_existing_paths(libraries)


def parse_install_dir_from_manifest(manifest_path: Path) -> str | None:
    """
    Read the installdir value from a Steam app manifest.
    """
    if not manifest_path.is_file():
        return None

    try:
        content = manifest_path.read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None

    match = re.search(
        r'"installdir"\s+"([^"]+)"',
        content,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    return match.group(1)


def detect_game_directory() -> Path | None:
    """
    Locate the game using appmanifest_4113050.acf.

    Expected path:
        <Steam library>\\steamapps\\common\\<installdir>
    """
    libraries = get_steam_library_directories()

    for library in libraries:
        steamapps = library / "steamapps"
        manifest = steamapps / f"appmanifest_{APP_ID}.acf"

        install_dir_name = parse_install_dir_from_manifest(manifest)

        if install_dir_name:
            candidate = steamapps / "common" / install_dir_name

            if candidate.is_dir():
                return candidate

    # Fallback for cases where the manifest is absent or unreadable.
    for library in libraries:
        common_directory = library / "steamapps" / "common"

        for folder_name in EXPECTED_INSTALL_DIR_NAMES:
            candidate = common_directory / folder_name

            if candidate.is_dir():
                return candidate

    return None


# ---------------------------------------------------------------------------
# Directory validation
# ---------------------------------------------------------------------------

def looks_like_game_directory(directory: Path) -> bool:
    if not directory.is_dir():
        return False

    return any(
        (directory / marker).exists()
        for marker in GAME_DIRECTORY_MARKERS
    )


def count_patch_files(source_directory: Path) -> int:
    if not source_directory.is_dir():
        return 0

    return sum(
        1
        for path in source_directory.rglob("*")
        if path.is_file()
    )


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

def generate_backup_directory(game_directory: Path) -> Path:
    """
    Keep backups inside the game directory.

    Example:
        Wabisabi SushiDerby\\KoreanPatch_Backup\\2026-07-18_113045
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    return (
        game_directory
        / BACKUP_FOLDER_NAME
        / timestamp
    )


def install_patch(
    game_directory: Path,
    source_directory: Path,
    log_callback,
) -> tuple[int, int, Path | None]:
    """
    Copy all files from source_directory into game_directory.

    Existing files are backed up before being overwritten.

    Returns:
        copied_count,
        backed_up_count,
        backup_directory
    """
    if not source_directory.is_dir():
        raise FileNotFoundError(
            f"Embedded patch folder was not found:\n{source_directory}"
        )

    patch_files = sorted(
        path
        for path in source_directory.rglob("*")
        if path.is_file()
    )

    if not patch_files:
        raise RuntimeError("The patch contains no files.")

    backup_directory = generate_backup_directory(game_directory)

    copied_count = 0
    backed_up_count = 0
    backup_created = False

    for source_file in patch_files:
        relative_path = source_file.relative_to(source_directory)
        destination_root = game_directory / "Wabisabi_Data"
        destination_file = destination_root / relative_path

        log_callback(f"처리 중: {relative_path}")

        destination_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        if destination_file.exists():
            backup_file = backup_directory / relative_path
            backup_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            shutil.copy2(destination_file, backup_file)

            backed_up_count += 1
            backup_created = True

            log_callback(f"  원본 백업 완료: {relative_path}")

        shutil.copy2(source_file, destination_file)
        copied_count += 1

        log_callback(f"  설치 완료: {relative_path}")

    if not backup_created:
        # Avoid returning a backup path that was never created.
        backup_directory = None

    return copied_count, backed_up_count, backup_directory


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


class PatchInstallerGUI:
    BG = "#F4F6F8"
    CARD_BG = "#FFFFFF"
    TEXT = "#18212B"
    MUTED = "#68727D"
    ACCENT = "#2F6FED"
    ACCENT_ACTIVE = "#2459BF"
    BORDER = "#D9DEE5"
    LOG_BG = "#10151C"
    LOG_FG = "#E7EDF5"
    SUCCESS = "#1B7F4B"
    WARNING = "#A15C00"
    ERROR = "#B42318"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(WINDOW_TITLE)
        self.root.geometry("760x540")
        self.root.minsize(680, 500)
        self.root.configure(bg=self.BG)

        self.game_directory_var = tk.StringVar()
        self.status_var = tk.StringVar(value="준비됨")
        self.patch_count_var = tk.StringVar(value="패치 파일 확인 중...")
        self.busy = False

        self._configure_style()
        self.create_widgets()
        self._center_window()

        # Tkinter가 창을 완전히 그린 뒤 자동 감지를 실행합니다.
        self.root.after(180, lambda: self.auto_detect_directory(show_failure_dialog=False))

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("App.TFrame", background=self.BG)
        style.configure("Card.TFrame", background=self.CARD_BG)
        style.configure(
            "Title.TLabel",
            background=self.CARD_BG,
            foreground=self.TEXT,
            font=("맑은 고딕", 18, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=self.CARD_BG,
            foreground=self.MUTED,
            font=("맑은 고딕", 9),
        )
        style.configure(
            "Section.TLabel",
            background=self.CARD_BG,
            foreground=self.TEXT,
            font=("맑은 고딕", 10, "bold"),
        )
        style.configure(
            "Body.TLabel",
            background=self.CARD_BG,
            foreground=self.MUTED,
            font=("맑은 고딕", 9),
        )
        style.configure(
            "Status.TLabel",
            background=self.CARD_BG,
            foreground=self.MUTED,
            font=("맑은 고딕", 9, "bold"),
        )
        style.configure(
            "Modern.TEntry",
            fieldbackground="#FFFFFF",
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            lightcolor=self.BORDER,
            darkcolor=self.BORDER,
            padding=8,
            font=("맑은 고딕", 9),
        )
        style.map(
            "Modern.TEntry",
            bordercolor=[("focus", self.ACCENT)],
            lightcolor=[("focus", self.ACCENT)],
            darkcolor=[("focus", self.ACCENT)],
        )
        style.configure(
            "Secondary.TButton",
            background="#FFFFFF",
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            padding=(12, 8),
            font=("맑은 고딕", 9),
        )
        style.map(
            "Secondary.TButton",
            background=[("active", "#EEF2F6"), ("disabled", "#F0F2F5")],
            foreground=[("disabled", "#9AA2AB")],
        )
        style.configure(
            "Accent.TButton",
            background=self.ACCENT,
            foreground="#FFFFFF",
            bordercolor=self.ACCENT,
            padding=(18, 9),
            font=("맑은 고딕", 10, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[("active", self.ACCENT_ACTIVE), ("disabled", "#AABCE8")],
            foreground=[("disabled", "#EEF2FA")],
        )

    def create_widgets(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=0)
        outer.pack(fill="both", expand=True)

        card = ttk.Frame(outer, style="Card.TFrame", padding=22)
        card.pack(fill="both", expand=True)

        ttk.Label(
            card,
            text="와비사비 스시 더비 한글 패치",
            style="Title.TLabel",
        ).pack(anchor="w")

        ttk.Label(
            card,
            text="제작: 팀 글냥이  ·  교체되는 기존 파일은 설치 전에 자동으로 백업됩니다.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 20))

        ttk.Label(card, text="게임 설치 폴더", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            card,
            text="Steam 설치 경로를 자동으로 찾거나 직접 선택할 수 있습니다.",
            style="Body.TLabel",
        ).pack(anchor="w", pady=(3, 8))

        path_row = ttk.Frame(card, style="Card.TFrame")
        path_row.pack(fill="x")
        path_row.columnconfigure(0, weight=1)

        self.directory_entry = ttk.Entry(
            path_row,
            textvariable=self.game_directory_var,
            style="Modern.TEntry",
        )
        self.directory_entry.grid(row=0, column=0, sticky="ew")

        self.detect_button = ttk.Button(
            path_row,
            text="자동 감지",
            style="Secondary.TButton",
            command=lambda: self.auto_detect_directory(show_failure_dialog=True),
        )
        self.detect_button.grid(row=0, column=1, padx=(8, 0))

        self.browse_button = ttk.Button(
            path_row,
            text="찾아보기",
            style="Secondary.TButton",
            command=self.browse_for_directory,
        )
        self.browse_button.grid(row=0, column=2, padx=(8, 0))

        info_row = ttk.Frame(card, style="Card.TFrame")
        info_row.pack(fill="x", pady=(10, 18))

        self.status_label = ttk.Label(
            info_row,
            textvariable=self.status_var,
            style="Status.TLabel",
        )
        self.status_label.pack(side="left")

        ttk.Label(
            info_row,
            textvariable=self.patch_count_var,
            style="Body.TLabel",
        ).pack(side="right")

        ttk.Label(card, text="설치 로그", style="Section.TLabel").pack(anchor="w")

        log_frame = tk.Frame(
            card,
            bg=self.LOG_BG,
            highlightbackground=self.BORDER,
            highlightthickness=1,
            bd=0,
        )
        log_frame.pack(fill="both", expand=True, pady=(8, 0))

        self.log_box = ScrolledText(
            log_frame,
            height=7,
            wrap="word",
            state="disabled",
            relief="flat",
            borderwidth=0,
            background=self.LOG_BG,
            foreground=self.LOG_FG,
            insertbackground=self.LOG_FG,
            selectbackground="#31598D",
            font=("맑은 고딕", 9),
            padx=12,
            pady=10,
        )
        self.log_box.pack(fill="both", expand=True)

        bottom_row = ttk.Frame(card, style="Card.TFrame")
        bottom_row.pack(fill="x", pady=(16, 0))

        ttk.Label(
            bottom_row,
            text="게임을 종료한 상태에서 설치해 주세요.",
            style="Body.TLabel",
        ).pack(side="left")

        self.install_button = ttk.Button(
            bottom_row,
            text="한글 패치 설치",
            style="Accent.TButton",
            command=self.begin_installation,
        )
        self.install_button.pack(side="right")

    def _center_window(self) -> None:
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def log(self, message: str) -> None:
        self.root.after(0, self._append_log, message)

    def _append_log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def set_status(self, message: str, *, tone: str = "normal") -> None:
        colors = {
            "normal": self.MUTED,
            "success": self.SUCCESS,
            "warning": self.WARNING,
            "error": self.ERROR,
        }

        def update() -> None:
            self.status_var.set(message)
            ttk.Style(self.root).configure(
                "Status.TLabel",
                foreground=colors.get(tone, self.MUTED),
            )

        self.root.after(0, update)

    def set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"

        def update() -> None:
            self.install_button.configure(state=state)
            self.browse_button.configure(state=state)
            self.detect_button.configure(state=state)
            self.directory_entry.configure(state=state)

        self.root.after(0, update)

    def auto_detect_directory(self, *, show_failure_dialog: bool = False) -> None:
        if self.busy:
            return

        self.busy = True
        self.set_controls_enabled(False)
        self.set_status("Steam 게임 설치 폴더를 찾는 중...")
        self.log("Steam 게임 설치 폴더를 찾는 중입니다...")

        threading.Thread(
            target=self._auto_detect_worker,
            args=(show_failure_dialog,),
            daemon=True,
        ).start()

    def _auto_detect_worker(self, show_failure_dialog: bool) -> None:
        try:
            detected = detect_game_directory()
            self.root.after(
                0,
                self._finish_auto_detection,
                detected,
                show_failure_dialog,
            )
        except Exception as exc:
            self.log(f"자동 감지 중 오류가 발생했습니다: {exc}")
            self.root.after(
                0,
                self._finish_auto_detection,
                None,
                show_failure_dialog,
            )

    def _finish_auto_detection(
        self,
        detected: Path | None,
        show_failure_dialog: bool,
    ) -> None:
        self.busy = False
        self.set_controls_enabled(True)

        embedded_count = count_patch_files(patch_source_directory())
        self.patch_count_var.set(f"포함된 패치 파일: {embedded_count}개")

        if detected:
            self.game_directory_var.set(str(detected))
            self.log(f"게임 설치 폴더를 찾았습니다: {detected}")

            if looks_like_game_directory(detected):
                self.set_status("게임 설치 폴더 감지 완료", tone="success")
            else:
                self.log("주의: 감지된 폴더에서 예상한 게임 파일을 확인하지 못했습니다.")
                self.set_status("설치 폴더 확인 필요", tone="warning")
            return

        self.log("게임 설치 폴더를 자동으로 찾지 못했습니다.")
        self.log("'찾아보기' 버튼으로 게임 폴더를 직접 선택해 주세요.")
        self.set_status("게임 설치 폴더를 찾지 못함", tone="warning")

        if show_failure_dialog:
            messagebox.showwarning(
                "자동 감지 실패",
                "Steam에서 게임 설치 폴더를 찾지 못했습니다.\n\n"
                "'찾아보기' 버튼을 눌러 게임 폴더를 직접 선택해 주세요.",
                parent=self.root,
            )

    def browse_for_directory(self) -> None:
        initial_directory = self.game_directory_var.get().strip()
        if not os.path.isdir(initial_directory):
            initial_directory = str(Path.home())

        selected = filedialog.askdirectory(
            title="와비사비 스시 더비 설치 폴더 선택",
            initialdir=initial_directory,
            mustexist=True,
            parent=self.root,
        )

        if selected:
            selected_path = Path(selected)
            self.game_directory_var.set(selected)
            self.log(f"선택한 폴더: {selected}")
            if looks_like_game_directory(selected_path):
                self.set_status("게임 설치 폴더가 선택되었습니다.", tone="success")
            else:
                self.set_status("선택한 폴더를 확인해 주세요.", tone="warning")

    def begin_installation(self) -> None:
        raw_directory = self.game_directory_var.get().strip()

        if not raw_directory:
            messagebox.showerror(
                "게임 폴더가 선택되지 않음",
                "먼저 게임 설치 폴더를 선택해 주세요.",
                parent=self.root,
            )
            return

        game_directory = Path(raw_directory)
        if not game_directory.is_dir():
            messagebox.showerror(
                "잘못된 폴더",
                "선택한 게임 폴더가 존재하지 않습니다.",
                parent=self.root,
            )
            return

        source_directory = patch_source_directory()
        if not source_directory.is_dir():
            messagebox.showerror(
                "패치 파일 없음",
                "프로그램에 포함된 patch_files 폴더를 찾을 수 없습니다.\n\n"
                "PyInstaller 빌드 시 --add-data 옵션을 확인해 주세요.",
                parent=self.root,
            )
            return

        patch_file_count = count_patch_files(source_directory)
        if patch_file_count == 0:
            messagebox.showerror(
                "패치 파일 없음",
                "프로그램에 포함된 패치 파일이 없습니다.",
                parent=self.root,
            )
            return

        if not looks_like_game_directory(game_directory):
            proceed = messagebox.askyesno(
                "게임 폴더를 확인할 수 없음",
                "선택한 폴더에서 와비사비 스시 더비의 게임 파일을 확인하지 못했습니다.\n\n"
                "잘못된 폴더에 설치하면 관련 없는 파일을 덮어쓸 수 있습니다.\n\n"
                "그래도 계속하시겠습니까?",
                icon="warning",
                parent=self.root,
            )
            if not proceed:
                return

        destination_root = game_directory / "Wabisabi_Data"
        confirmed = messagebox.askyesno(
            "한글 패치 설치",
            f"다음 폴더에 패치 파일 {patch_file_count}개를 설치합니다.\n\n"
            f"{destination_root}\n\n"
            "교체되는 기존 파일은 자동으로 백업됩니다.\n\n"
            "계속하시겠습니까?",
            parent=self.root,
        )
        if not confirmed:
            return

        self.busy = True
        self.set_controls_enabled(False)
        self.set_status("한글 패치 설치 중...")
        self.log("")
        self.log("한글 패치 설치를 시작합니다.")
        self.log(f"게임 폴더: {game_directory}")
        self.log(f"설치 대상: {destination_root}")

        threading.Thread(
            target=self.installation_worker,
            args=(game_directory, source_directory),
            daemon=True,
        ).start()

    def installation_worker(
        self,
        game_directory: Path,
        source_directory: Path,
    ) -> None:
        try:
            copied, backed_up, backup_directory = install_patch(
                game_directory=game_directory,
                source_directory=source_directory,
                log_callback=self.log,
            )

            self.log("")
            self.log("한글 패치 설치가 완료되었습니다.")
            self.log(f"설치된 파일: {copied}개")
            self.log(f"백업된 파일: {backed_up}개")
            if backup_directory:
                self.log(f"백업 폴더: {backup_directory}")

            self.set_status("설치 완료", tone="success")
            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "설치 완료",
                    (
                        "한글 패치가 정상적으로 설치되었습니다.\n"
                        "게임을 실행한 후 게임 언어를 일본어로 설정해 주세요!\n\n"
                        f"설치된 파일: {copied}개\n"
                        f"백업된 파일: {backed_up}개"
                    ),
                    parent=self.root,
                ),
            )

        except PermissionError:
            self.log("")
            self.log("설치 실패: 게임 폴더에 파일을 쓸 권한이 없습니다.")
            self.log("게임과 Steam을 종료한 뒤 다시 시도해 주세요.")
            self.set_status("설치 실패", tone="error")
            self.root.after(
                0,
                lambda: messagebox.showerror(
                    "접근 권한 오류",
                    (
                        "게임 폴더에 파일을 쓸 수 없습니다.\n\n"
                        "게임과 Steam을 종료한 뒤 다시 시도해 주세요. "
                        "필요한 경우 설치 프로그램을 관리자 권한으로 실행해 주세요."
                    ),
                    parent=self.root,
                ),
            )

        except Exception as exc:
            self.log("")
            self.log(f"설치 중 오류가 발생했습니다: {exc}")
            self.log(traceback.format_exc())
            self.set_status("설치 실패", tone="error")
            self.root.after(
                0,
                messagebox.showerror,
                "설치 실패",
                f"패치 설치 중 오류가 발생했습니다.\n\n{exc}",
                parent=self.root,
            )

        finally:
            self.busy = False
            self.set_controls_enabled(True)


def main() -> None:
    root = tk.Tk()
    PatchInstallerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

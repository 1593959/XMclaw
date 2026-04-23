"""Fast build script for XMclaw v2 executable using current Python environment.

Phase 4.7 deliverable. Builds a standalone ``XMclaw.exe`` that bundles
the v2 daemon, CLI, and Web UI static files. No desktop app — v2 is
terminal + WebUI only.

Usage::

    python scripts/build_exe_fast.py [--skip-shortcut] [--skip-inno]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent.resolve()
DIST_DIR = BASE_DIR / "dist" / "XMclaw"
BUILD_DIR = BASE_DIR / "build"


def clean_dist() -> None:
    """Remove old dist and build directories."""
    if DIST_DIR.exists():
        print(f"Removing old dist at {DIST_DIR}")
        shutil.rmtree(DIST_DIR)
    if BUILD_DIR.exists():
        print(f"Removing old build at {BUILD_DIR}")
        shutil.rmtree(BUILD_DIR)


def copy_missing_dlls() -> None:
    """Copy missing DLLs and .pyd files from the Python environment."""
    print("Copying missing DLLs and .pyd files...")
    python_prefix = Path(sys.prefix)

    dll_dirs = [
        python_prefix / "Library" / "bin",
        python_prefix / "DLLs",
    ]

    dll_files = [
        "sqlite3.dll",
        "liblzma.dll",
        "LIBBZ2.dll",
        "ffi.dll",
        "libexpat.dll",
        "pyexpat.pyd",
        "_bz2.pyd",
        "_ctypes.pyd",
        "_elementtree.pyd",
        "_lzma.pyd",
        "_sqlite3.pyd",
    ]

    copied: set[str] = set()
    for dll_dir in dll_dirs:
        if not dll_dir.exists():
            continue
        for dll_file in dll_files:
            src = dll_dir / dll_file
            if src.exists() and dll_file not in copied:
                dst1 = DIST_DIR / dll_file
                dst2 = DIST_DIR.parent / dll_file
                shutil.copy2(src, dst1)
                if dst1 != dst2:
                    shutil.copy2(src, dst2)
                print(f"  Copied {dll_file}")
                copied.add(dll_file)
    print(f"  Total DLLs copied: {len(copied)}")


def build_executable() -> None:
    """Build executable using PyInstaller."""
    print("Building executable with PyInstaller...")

    # Hidden imports for v2 architecture
    hiddenimports = [
        # CLI & daemon
        "xmclaw.__main__",
        "xmclaw.cli.main",
        "xmclaw.cli.chat",
        "xmclaw.cli.doctor",
        "xmclaw.daemon.app",
        "xmclaw.daemon.agent_loop",
        "xmclaw.daemon.factory",
        "xmclaw.daemon.lifecycle",
        "xmclaw.daemon.pairing",
        # Core
        "xmclaw.core.bus.memory",
        "xmclaw.core.bus.events",
        "xmclaw.core.bus.sqlite",
        "xmclaw.core.bus.replay",
        "xmclaw.core.grader.verdict",
        "xmclaw.core.grader.checks",
        "xmclaw.core.scheduler.online",
        "xmclaw.core.scheduler.policy",
        "xmclaw.core.evolution.controller",
        "xmclaw.core.ir",
        # Providers
        "xmclaw.providers.llm.anthropic",
        "xmclaw.providers.llm.openai",
        "xmclaw.providers.llm.base",
        "xmclaw.providers.tool.builtin",
        "xmclaw.providers.tool.composite",
        "xmclaw.providers.tool.browser",
        "xmclaw.providers.tool.lsp",
        # Skills
        "xmclaw.skills.base",
        "xmclaw.skills.manifest",
        "xmclaw.skills.registry",
        "xmclaw.skills.versioning",
        # Utils
        "xmclaw.utils.cost",
        "xmclaw.utils.redact",
        # Third-party
        "fastapi",
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "websockets",
        "starlette",
        "starlette.staticfiles",
        "rich",
        "rich.console",
        "rich.panel",
        "rich.live",
        "rich.table",
        "rich.syntax",
        "rich.progress",
        "prompt_toolkit",
        "typer",
        "openai",
        "anthropic",
        "playwright",
        "playwright.sync_api",
        "sqlite_vec",
        "httpx",
        "watchfiles",
        "cryptography",
        "cryptography.hazmat.primitives.asymmetric.ed25519",
    ]

    spec_path = BUILD_DIR / "XMclaw.spec"
    spec_path.parent.mkdir(parents=True, exist_ok=True)

    entry_script = BASE_DIR / "xmclaw" / "__main__.py"

    spec_content = f"""# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT

a = Analysis(
    [r'{entry_script}'],
    pathex=[r'{BASE_DIR}', r'{Path(sys.executable).parent / "Lib" / "site-packages"}'],
    binaries=[],
    datas=[
        (r'{BASE_DIR / "xmclaw" / "daemon" / "static"}', 'xmclaw/daemon/static'),
    ],
    hiddenimports={hiddenimports!r},
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=["PySide6", "PyQt6", "PyQt5", "pyside2"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=None)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='XMclaw',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
"""
    spec_path.write_text(spec_content, encoding="utf-8")

    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--distpath", str(DIST_DIR.parent),
            "--workpath", str(BUILD_DIR / "work"),
            "--noconfirm",
            str(spec_path),
        ],
        check=True,
    )
    print(f"Executable built at {DIST_DIR / 'XMclaw.exe'}")


def copy_data_to_dist() -> None:
    """Copy project data next to the built executable."""
    print("Copying project data into dist...")
    exe_dir = DIST_DIR / "XMclaw"
    if not exe_dir.exists():
        exe_dir = DIST_DIR

    dirs_to_copy = [
        "daemon",
        "docs",
        "scripts",
        "xmclaw",
    ]
    files_to_copy = [
        "README.md",
        "pyproject.toml",
        "requirements-lock.txt",
        "requirements-dev-lock.txt",
    ]

    for d in dirs_to_copy:
        src = BASE_DIR / d
        dst = exe_dir / d
        if src.exists():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            print(f"  Copied {d}")

    for f in files_to_copy:
        src = BASE_DIR / f
        if src.exists():
            shutil.copy2(src, exe_dir / f)
            print(f"  Copied {f}")


def create_shortcut() -> None:
    """Create Windows Desktop shortcut."""
    print("Creating shortcuts...")
    try:
        import winshell
        from win32com.client import Dispatch
    except ImportError:
        print("  winshell not available, skipping shortcuts")
        return

    exe_path = DIST_DIR / "XMclaw.exe"
    if not exe_path.exists():
        print(f"  Executable not found at {exe_path}, skipping shortcuts")
        return

    desktop = Path(winshell.desktop())
    shortcut = desktop / "XMclaw.lnk"
    shell = Dispatch("WScript.Shell")
    sc = shell.CreateShortCut(str(shortcut))
    sc.TargetPath = str(exe_path)
    sc.WorkingDirectory = str(DIST_DIR)
    sc.IconLocation = str(exe_path)
    sc.save()
    print(f"  Desktop shortcut: {shortcut}")


def build_inno_setup() -> None:
    """Build Inno Setup installer if ISCC is available."""
    iscc_paths = [
        Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
        Path("C:/Program Files/Inno Setup 6/ISCC.exe"),
    ]
    iscc = None
    for p in iscc_paths:
        if p.exists():
            iscc = p
            break

    iss_path = BASE_DIR / "scripts" / "xmclaw_setup.iss"
    if not iscc:
        print("  Inno Setup not found, skipping installer build")
        return
    if not iss_path.exists():
        print("  xmclaw_setup.iss not found, skipping installer build")
        return

    print("Building Inno Setup installer...")
    subprocess.run([str(iscc), str(iss_path)], check=True)
    print("  Installer built at dist/XMclaw_Setup.exe")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build XMclaw v2 executable")
    parser.add_argument("--skip-dll", action="store_true", help="Skip DLL copying")
    parser.add_argument("--skip-shortcut", action="store_true", help="Skip shortcut creation")
    parser.add_argument("--skip-inno", action="store_true", help="Skip Inno Setup installer")
    args = parser.parse_args()

    print("=" * 60)
    print("XMclaw v2 Build Script")
    print("=" * 60)

    clean_dist()
    build_executable()
    copy_data_to_dist()
    if not args.skip_dll:
        copy_missing_dlls()
    if not args.skip_shortcut:
        create_shortcut()
    if not args.skip_inno:
        build_inno_setup()

    print("=" * 60)
    print("Build complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()

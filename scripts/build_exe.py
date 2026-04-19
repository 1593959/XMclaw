"""One-click build script for XMclaw executable with bundled Python."""
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

BASE_DIR = Path(r"C:\Users\15978\Desktop\XMclaw")
SPEC_FILE = BASE_DIR / "build.spec"
DIST_DIR = BASE_DIR / "dist"
PYTHON_DIR = BASE_DIR / "python"

PYTHON_EMBED_URL = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip"


def ensure_bundled_python():
    """Download Windows embeddable Python if not present."""
    if PYTHON_DIR.exists():
        print(f"Bundled Python already exists at {PYTHON_DIR}")
        return

    print("Downloading Windows embeddable Python...")
    PYTHON_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = BASE_DIR / "tmp" / "python-embed.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    urllib.request.urlretrieve(PYTHON_EMBED_URL, zip_path)
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(PYTHON_DIR)
    zip_path.unlink()

    # Enable site packages so pip works
    pth_file = next(PYTHON_DIR.glob("python*._pth"), None)
    if pth_file:
        content = pth_file.read_text(encoding="utf-8")
        content = content.replace("#import site", "import site")
        pth_file.write_text(content, encoding="utf-8")

    # Install pip
    get_pip = BASE_DIR / "tmp" / "get-pip.py"
    urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", get_pip)
    subprocess.run([str(PYTHON_DIR / "python.exe"), str(get_pip)], check=True)
    get_pip.unlink()

    # Install dependencies
    subprocess.run(
        [str(PYTHON_DIR / "python.exe"), "-m", "pip", "install", "-r", str(BASE_DIR / "pyproject.toml")],
        cwd=str(BASE_DIR),
        check=True,
    )
    print(f"Bundled Python ready at {PYTHON_DIR}")


def main():
    print("=" * 50)
    print("XMclaw Build Script")
    print("=" * 50)

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found. Installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    ensure_bundled_python()

    print(f"\nBuilding from: {SPEC_FILE}")
    print(f"Output to: {DIST_DIR}\n")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC_FILE),
        "--distpath", str(DIST_DIR),
        "--workpath", str(BASE_DIR / "build"),
        "--noconfirm",
    ]

    result = subprocess.run(cmd, cwd=str(BASE_DIR))
    if result.returncode == 0:
        print(f"\nBuild successful! Executable at: {DIST_DIR / 'xmclaw.exe'}")
        print(f"Bundled Python at: {DIST_DIR / 'python'}")
    else:
        print("\nBuild failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()

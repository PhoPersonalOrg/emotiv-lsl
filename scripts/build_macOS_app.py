"""Build a single-file macOS emotiv_lsl binary and prepare it for local use."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import PyInstaller.__main__


def _post_process(binary: Path) -> Path:
    """Ad-hoc codesign, clear quarantine, and install into ~/bin."""
    if not binary.is_file():
        raise FileNotFoundError(f"Expected onefile binary at {binary}")

    binary.chmod(binary.stat().st_mode | 0o111)

    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(binary)],
        check=True,
    )
    subprocess.run(["xattr", "-cr", str(binary)], check=False)

    bin_dir = Path.home() / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    installed = bin_dir / "emotiv_lsl"
    shutil.copy2(binary, installed)
    installed.chmod(installed.stat().st_mode | 0o111)
    # Re-sign the copy so Gatekeeper sees a consistent signature at the install path.
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(installed)],
        check=True,
    )
    subprocess.run(["xattr", "-cr", str(installed)], check=False)
    return installed


def main() -> None:
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent.resolve()
    spec_file = repo_root / "main.spec"
    dist_binary = repo_root / "dist" / "emotiv_lsl"

    if spec_file.exists():
        PyInstaller.__main__.run([
            str(spec_file),
            f"--distpath={repo_root}/dist",
            f"--workpath={repo_root}/build",
            "--noconfirm",
            "--clean",
        ])
    else:
        # Fallback: mirror macOS onefile settings from main.spec when it is absent.
        PyInstaller.__main__.run([
            "main.py",
            "--onefile",
            "--name=emotiv_lsl",
            "--icon=images/icons/emotiv_lsl_icon_design.ico",
            "--hidden-import=pylsl",
            "--hidden-import=hid",
            "--hidden-import=mne",
            "--collect-all=mne",
            "--collect-binaries=pylsl",
            f"--distpath={repo_root}/dist",
            f"--workpath={repo_root}/build",
            f"--specpath={repo_root}",
            "--noconfirm",
            "--clean",
        ])

    installed = _post_process(dist_binary)
    print(f"Built:     {dist_binary}")
    print(f"Installed: {installed}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"Post-build step failed: {exc}", file=sys.stderr)
        sys.exit(exc.returncode or 1)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

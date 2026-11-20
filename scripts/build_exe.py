import PyInstaller.__main__
from pathlib import Path


def main() -> None:
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent.resolve()
    spec_file = repo_root / "main.spec"

    if spec_file.exists():
        PyInstaller.__main__.run([
            str(spec_file),
            f"--distpath={repo_root}/dist",
            f"--workpath={repo_root}/build",
            "--noconfirm",
            "--clean",
        ])
        return

    # Fallback: mirror Windows onefile settings from main.spec when it is absent.
    PyInstaller.__main__.run([
        "main.py",
        "--onefile",
        "--console",
        "--name=emotiv_lsl",
        "--icon=images/icons/emotiv_lsl_icon_design.ico",
        "--add-data=images/icons/emotiv_lsl_icon_design.ico;images/icons",
        "--hidden-import=pylsl",
        "--hidden-import=hid",
        "--collect-binaries=pylsl",
        f"--distpath={repo_root}/dist",
        f"--workpath={repo_root}/build",
        f"--specpath={repo_root}",
        "--noconfirm",
        "--clean",
    ])


if __name__ == "__main__":
    main()

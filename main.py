import logging
import os
import sys
import platform

is_windows: bool = False

# Add the directory containing the HIDAPI DLL on Windows only
if sys.platform == "win32":
    arch_dir = "x64" if platform.architecture()[0] == "64bit" else "x86"
    project_root = os.path.dirname(os.path.abspath(__file__))
    dll_dir = os.path.join(project_root, "hidapi-win", arch_dir)
    if os.path.isdir(dll_dir):
        os.add_dll_directory(dll_dir)
    is_windows = True

# Hint the loader about bundled hidapi on macOS when frozen
if sys.platform == "darwin" and getattr(sys, 'frozen', False):
    exe_dir = os.path.dirname(sys.executable)
    # Prefer Frameworks inside the .app bundle
    frameworks_dir = os.path.normpath(os.path.join(exe_dir, '..', 'Frameworks'))
    candidates = [
        frameworks_dir,
        exe_dir,
    ]
    for candidate in candidates:
        if os.path.isdir(candidate):
            current = os.environ.get('DYLD_FALLBACK_LIBRARY_PATH', '')
            paths = [candidate] + ([current] if current else [])
            os.environ['DYLD_FALLBACK_LIBRARY_PATH'] = ':'.join(paths)
            break

from emotiv_lsl.emotiv_epoc_x import EmotivEpocX

import socket

def enforce_single_instance():
    """Ensure only one instance of the application is running."""
    if sys.platform == "win32":
        import ctypes
        mutex_name = "Global\\EmotivLSLSingleInstanceMutex"
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            print("Another instance is already running. Exiting.")
            sys.exit(0)
        # We need to keep a reference to the mutex so it isn't garbage collected
        return mutex
    else:
        # On Unix, bind a socket to a specific local port
        # Or use an abstract namespace socket on Linux
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        socket_name = "\0emotiv_lsl_single_instance" if sys.platform.startswith("linux") else "/tmp/emotiv_lsl.sock"
        try:
            s.bind(socket_name)
        except socket.error as e:
            print("Another instance is already running. Exiting.")
            sys.exit(0)
        return s

import threading
import pystray
from PIL import Image

console_visible = True
_console_wndproc = None
_old_console_wndproc = None


def _get_console_hwnd():
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    ctypes.windll.kernel32.GetConsoleWindow.restype = wintypes.HWND
    return ctypes.windll.kernel32.GetConsoleWindow()


def hide_console():
    global console_visible
    if sys.platform != "win32":
        return
    import ctypes

    hwnd = _get_console_hwnd()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        console_visible = False


def show_console():
    global console_visible
    if sys.platform != "win32":
        return
    import ctypes

    hwnd = _get_console_hwnd()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        console_visible = True


def toggle_console():
    if console_visible:
        hide_console()
    else:
        show_console()


def install_console_minimize_hook():
    """Intercept the console minimize button so it hides to the tray."""
    global _console_wndproc, _old_console_wndproc
    if sys.platform != "win32":
        return

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    GWLP_WNDPROC = -4
    WM_SYSCOMMAND = 0x0112
    SC_MINIMIZE = 0xF020

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    hwnd = _get_console_hwnd()
    if not hwnd:
        return

    @WNDPROC
    def console_wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_SYSCOMMAND and (wparam & 0xFFF0) == SC_MINIMIZE:
            hide_console()
            return 0
        return user32.CallWindowProcW(_old_console_wndproc, hwnd, msg, wparam, lparam)

    _console_wndproc = console_wndproc
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        _old_console_wndproc = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, _console_wndproc)
    else:
        _old_console_wndproc = user32.SetWindowLongW(hwnd, GWLP_WNDPROC, _console_wndproc)

def run_emotiv_loop():
    # Configure logging for debugging data packets
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    enable_electrode_quality_stream: bool = True
    enable_motion_data: bool = True

    if is_windows:
        print(f'platform is_windows, enabling motion and quality stream...')
        enable_electrode_quality_stream: bool = True
        enable_motion_data: bool = True

    emotiv_epoc_x = EmotivEpocX(enable_motion_data=enable_motion_data, enable_electrode_quality_stream=enable_electrode_quality_stream)
    crypto_key = emotiv_epoc_x.get_crypto_key()
    print(f'crypto_key: {crypto_key}')
    emotiv_epoc_x.main_loop()

def on_toggle_clicked(icon, item):
    toggle_console()

def on_exit_clicked(icon, item):
    icon.stop()
    sys.exit(0)

if __name__ == "__main__":
    _instance_lock = enforce_single_instance()
    install_console_minimize_hook()

    # Create tray icon
    try:
        icon_image = Image.open("images/icons/emotiv_lsl_icon_design.ico")
    except Exception:
        # Fallback if image is missing
        icon_image = Image.new('RGB', (64, 64), color='blue')

    menu = pystray.Menu(
        pystray.MenuItem("Toggle Console", on_toggle_clicked, default=True),
        pystray.MenuItem("Exit", on_exit_clicked)
    )
    tray_icon = pystray.Icon("emotiv_lsl", icon_image, "Emotiv LSL Server", menu)

    # Start Emotiv in a background thread so the main thread can run the system tray
    emotiv_thread = threading.Thread(target=run_emotiv_loop, daemon=True)
    emotiv_thread.start()

    # Start the tray icon loop (blocks the main thread)
    tray_icon.run()

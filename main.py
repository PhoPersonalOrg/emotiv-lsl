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

if __name__ == "__main__":
    # Configure logging for debugging data packets
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # logger = logging.getLogger("emotiv_lsl")
    # logger.setLevel(logging.DEBUG)

    # file_handler = logging.FileHandler("logs_and_notes/logs/decode_tracing.log")
    # file_handler.setLevel(logging.WARN)

    # console_handler = logging.StreamHandler()
    # console_handler.setLevel(logging.INFO)

    # formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    # file_handler.setFormatter(formatter)
    # console_handler.setFormatter(formatter)

    # logger.addHandler(file_handler)
    # logger.addHandler(console_handler)

    # logger.info("info → console only")
    # logger.error("error → console + file")

    # logging.basicConfig(filename="logs_and_notes/logs/decode_tracing.log", level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    enable_electrode_quality_stream: bool = False
    enable_motion_data: bool = False

    if is_windows:
        print(f'platform is_windows, enabling motion and quality stream...')
        enable_electrode_quality_stream: bool = True
        enable_motion_data: bool = True

    emotiv_epoc_x = EmotivEpocX(enable_motion_data=enable_motion_data, enable_electrode_quality_stream=enable_electrode_quality_stream)
    crypto_key = emotiv_epoc_x.get_crypto_key()
    print(f'crypto_key: {crypto_key}')
    emotiv_epoc_x.main_loop()

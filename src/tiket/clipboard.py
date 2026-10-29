import shutil
import subprocess


def copy_to_clipboard(text: str) -> bool:
    """Copy with the system clipboard tool; False if none is installed."""
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text.encode(), check=True)
            return True
    return False


def paste_from_clipboard() -> str | None:
    """Clipboard text from the system tool; None if none is installed."""
    for cmd in (["pbpaste"], ["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"]):
        if shutil.which(cmd[0]):
            return subprocess.run(cmd, capture_output=True, check=True).stdout.decode()
    return None

import shutil
import subprocess


def copy_to_clipboard(text: str) -> bool:
    """Copy with the system clipboard tool; False if none is installed."""
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text.encode(), check=True)
            return True
    return False

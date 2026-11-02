"""Double-click launcher for the desktop app.

The .pyw extension makes Windows run this with pythonw.exe, so no console
window appears. Right-click > 'Send to' > 'Desktop (create shortcut)' to put it
on your desktop.
"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    try:
        from psi.gui import launch
    except ImportError:
        # Without a console there is nowhere to print, so use a dialog.
        try:
            import tkinter
            from tkinter import messagebox

            root = tkinter.Tk()
            root.withdraw()
            messagebox.showerror(
                "PageSpeed Analyzer",
                "Could not start:\n\n" + traceback.format_exc(),
            )
        except Exception:
            raise
        return
    launch()


if __name__ == "__main__":
    main()

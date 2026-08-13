"""Deterministic Windows-only Tkinter fixture for Milestone 45 P1's real
UIA integration tests (see ../test_desktop_windows_integration.py). Owned
entirely by the test suite - no user data, no network access, no
filesystem mutation beyond its own throwaway status file, deterministic
exit via SIGTERM/process.terminate() from the driving test.

Empirical finding this fixture encodes (see docs/architecture.md's
Milestone 45 section): stock Tkinter widgets expose an EMPTY
AutomationId and IDENTICAL class_name/control_type for every sibling
control of the same type - class/type alone cannot distinguish them. This
fixture explicitly assigns its own stable native Win32 control IDs (via
ctypes SetWindowLongPtrW(GWL_ID, ...)) to its two buttons so integration
tests can prove BOTH that class/type-only resolution is genuinely
ambiguous (by deliberately probing without automation_id) AND that
automation_id-based resolution is exact and stable (the whole reason
Milestone 45 P1 REQUIRES automation_id on every approved_desktop_controls
entry - see kernel/tools/desktop_safety.py's own module docstring).

Usage: `python desktop_fixture_app.py <status_file_path> [--hide|--minimize]`
"""

import ctypes
import sys
import pathlib
import tkinter as tk

FIXED_TITLE = "AIOS-M45-Fixture-Window"

REFRESH_BUTTON_AUTOMATION_ID = "5001"
QUIT_BUTTON_AUTOMATION_ID = "5002"
_REFRESH_BUTTON_CONTROL_ID = 5001
_QUIT_BUTTON_CONTROL_ID = 5002
_GWL_ID = -12


def _set_control_id(widget, control_id: int) -> None:
    hwnd = widget.winfo_id()
    ctypes.windll.user32.SetWindowLongPtrW(hwnd, _GWL_ID, control_id)


def main() -> None:
    status_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(
        "fixture_status.txt"
    )

    root = tk.Tk()
    root.title(FIXED_TITLE)
    root.geometry("320x160+100+100")

    counter = {"n": 0}

    label = tk.Label(root, text="idle", name="statuslabel")
    label.pack(pady=8)

    def on_refresh():
        counter["n"] += 1
        label.config(text=f"refreshed:{counter['n']}")
        status_path.write_text(f"refreshed:{counter['n']}", encoding="utf-8")

    def on_quit():
        root.destroy()

    refresh_btn = tk.Button(root, text="Refresh", name="refreshbutton", command=on_refresh)
    refresh_btn.pack(pady=4)
    _set_control_id(refresh_btn, _REFRESH_BUTTON_CONTROL_ID)

    quit_btn = tk.Button(root, text="Quit", name="quitbutton", command=on_quit)
    quit_btn.pack(pady=4)
    _set_control_id(quit_btn, _QUIT_BUTTON_CONTROL_ID)

    entry = tk.Entry(root, name="noteentry")
    entry.pack(pady=4)

    status_path.write_text("idle", encoding="utf-8")

    if "--hide" in sys.argv:
        root.withdraw()
    elif "--minimize" in sys.argv:
        root.update_idletasks()
        root.iconify()

    root.mainloop()


if __name__ == "__main__":
    main()

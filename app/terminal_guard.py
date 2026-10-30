"""Recognize an interactive approval terminal, including Windows NUL handling."""
from __future__ import annotations

import sys
import unittest
from unittest import mock

WINDOWS = sys.platform == "win32"


def windows_console_input(stream):
    # Windows isatty() also accepts character devices such as NUL. Use the
    # handle of this Python input stream; never open CONIN$ to bypass redirection.
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetConsoleMode.restype = wintypes.BOOL
    mode = wintypes.DWORD()
    return bool(kernel.GetConsoleMode(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(mode)))


def interactive_input(stream=None):
    stream = sys.stdin if stream is None else stream
    try:
        if stream is None or not stream.isatty():
            return False
        return windows_console_input(stream) if WINDOWS else True
    except (OSError, ValueError, AttributeError, ImportError):
        return False


class TerminalTests(unittest.TestCase):
    def test_redirected_input_is_rejected_without_opening_a_console(self):
        stream = mock.Mock()
        stream.isatty.return_value = False
        with mock.patch(__name__ + ".windows_console_input") as check:
            self.assertFalse(interactive_input(stream))
        check.assert_not_called()

    def test_windows_nul_can_pass_isatty_but_is_not_a_console(self):
        stream = mock.Mock()
        stream.isatty.return_value = True
        with mock.patch(__name__ + ".WINDOWS", True), mock.patch(__name__ + ".windows_console_input", return_value=False) as check:
            self.assertFalse(interactive_input(stream))
        check.assert_called_once_with(stream)

    def test_windows_console_input_is_accepted(self):
        stream = mock.Mock()
        stream.isatty.return_value = True
        with mock.patch(__name__ + ".WINDOWS", True), mock.patch(__name__ + ".windows_console_input", return_value=True):
            self.assertTrue(interactive_input(stream))

    def test_unavailable_or_closed_console_fails_closed(self):
        stream = mock.Mock()
        stream.isatty.return_value = True
        for error in (OSError("closed"), ValueError("closed"), AttributeError("no handle")):
            with self.subTest(error=type(error).__name__), mock.patch(__name__ + ".WINDOWS", True), mock.patch(__name__ + ".windows_console_input", side_effect=error):
                self.assertFalse(interactive_input(stream))

    def test_posix_terminal_needs_no_windows_api(self):
        stream = mock.Mock()
        stream.isatty.return_value = True
        with mock.patch(__name__ + ".WINDOWS", False), mock.patch(__name__ + ".windows_console_input") as check:
            self.assertTrue(interactive_input(stream))
        check.assert_not_called()

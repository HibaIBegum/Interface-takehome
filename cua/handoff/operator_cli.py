"""Operator console (mock): shows intervention requests and reads take / resume / approve / deny / abort.

stdin is read on a background thread so the main thread can keep pumping browser events while the
human works in the window; otherwise their clicks would only be delivered after they typed a command.
"""

from __future__ import annotations

import getpass
import queue
import sys
import threading

from .intervention import InterventionRequest


class ConsoleOperator:
    def __init__(self) -> None:
        self.name = getpass.getuser()
        self._lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in sys.stdin:
            self._lines.put(line)
        self._lines.put("abort")  # stdin closed: nobody can answer

    def show(self, request: InterventionRequest) -> None:
        out = sys.stderr
        print("\n" + "=" * 72, file=out)
        print(f"HUMAN NEEDED ({request.trigger}) - run {request.run_id}", file=out)
        print(f"  subject:    {request.subject}" + (f"   step: {request.step_id}" if request.step_id else ""), file=out)
        print(f"  reason:     {request.reason}", file=out)
        print(f"  page:       {request.current_url}", file=out)
        for name, url in request.frames.items():
            print(f"    frame {name}: {url}", file=out)
        print(f"  screenshot: {request.screenshot}   (request saved to intervention.json)", file=out)
        print(f"  commands:   {', '.join(request.allowed_commands)}", file=out)
        print("=" * 72, file=out)

    def say(self, message: str) -> None:
        print(f"  >> {message}", file=sys.stderr)

    def next_command(self, pump) -> str:
        print("operator> ", end="", file=sys.stderr, flush=True)
        while True:
            try:
                return self._lines.get_nowait()
            except queue.Empty:
                pump()


class UnattendedOperator:
    """For headless / non-interactive runs: nobody is there, so every request is aborted."""

    name = "unattended"

    def show(self, request: InterventionRequest) -> None:
        print(f"HUMAN NEEDED ({request.trigger}) but running unattended: {request.reason}", file=sys.stderr)

    def say(self, message: str) -> None:
        pass

    def next_command(self, pump) -> str:
        return "abort"

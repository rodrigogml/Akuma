"""Optional Windows Service adapter.

The scheduler remains usable without pywin32, which keeps development and
foreground execution dependency-free.
"""
from __future__ import annotations

import threading

from .service import run


try:
    import win32service
    import win32serviceutil
    import servicemanager
except ImportError:  # pragma: no cover - only exercised on machines without pywin32
    win32service = None
    win32serviceutil = None
    servicemanager = None


if win32serviceutil is not None:
    class AkumaDaemonService(win32serviceutil.ServiceFramework):
        _svc_name_ = "AkumaDaemon"
        _svc_display_name_ = "Akuma Daemon"
        _svc_description_ = "Akuma resident scheduler and event service"

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = threading.Event()

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self.stop_event.set()

        def SvcDoRun(self):
            servicemanager.LogInfoMsg("Akuma Daemon started")
            run(stop_event=self.stop_event)


def main() -> None:
    if win32serviceutil is None:
        raise RuntimeError("Windows Service mode requires pywin32; use `python -m akuma_daemon run` otherwise")
    win32serviceutil.HandleCommandLine(AkumaDaemonService)


if __name__ == "__main__":
    main()

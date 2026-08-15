from __future__ import annotations

import json
import socket
import socketserver
import threading
from typing import Any, Callable


Handler = Callable[[str, dict[str, Any]], Any]


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: "RpcServer" = self.server.rpc_server  # type: ignore[attr-defined]
        for raw in self.rfile:
            try:
                request = json.loads(raw.decode("utf-8"))
                if request.get("token") != server.token:
                    raise PermissionError("invalid control token")
                result = server.handler(request["method"], request.get("params", {}))
                response = {"ok": True, "result": result}
            except Exception as exc:  # protocol boundary: return errors to the client
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.wfile.write((json.dumps(response, default=str) + "\n").encode("utf-8"))
            self.wfile.flush()


class _ThreadingTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class RpcServer:
    def __init__(self, host: str, port: int, token: str, handler: Handler):
        self.host, self.port, self.token, self.handler = host, port, token, handler
        self._server = _ThreadingTcpServer((host, port), _RequestHandler)
        self._server.rpc_server = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]

    def serve_forever(self, stop_event: threading.Event | None = None) -> None:
        if stop_event is None:
            self._server.serve_forever()
            return
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        stop_event.wait()
        self.close()
        thread.join(timeout=5)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def rpc_call(host: str, port: int, token: str, method: str,
             params: dict[str, Any] | None = None, timeout: float = 10) -> Any:
    with socket.create_connection((host, port), timeout=timeout) as connection:
        request = {"token": token, "method": method, "params": params or {}}
        connection.sendall((json.dumps(request) + "\n").encode("utf-8"))
        file = connection.makefile("rb")
        response = json.loads(file.readline().decode("utf-8"))
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "RPC request failed"))
    return response.get("result")

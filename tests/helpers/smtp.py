"""A minimal SMTP sink for harmless send/invite fixtures (spec section 22: no real accounts)."""

from __future__ import annotations

import socketserver
import threading


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        sink: SmtpSink = self.server.sink  # type: ignore[attr-defined]
        self.wfile.write(b"220 fixture-smtp ready\r\n")
        mail_from, rcpts = None, []
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode("utf-8", "replace").strip()
            up = cmd.upper()
            if up.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250-fixture-smtp\r\n250 OK\r\n")
            elif up.startswith("MAIL FROM"):
                mail_from, rcpts = cmd[10:].strip(), []
                self.wfile.write(b"250 OK\r\n")
            elif up.startswith("RCPT TO"):
                rcpts.append(cmd[8:].strip().strip("<>"))
                self.wfile.write(b"250 OK\r\n")
            elif up == "DATA":
                self.wfile.write(b"354 end with .\r\n")
                data = []
                while True:
                    ln = self.rfile.readline()
                    if ln in (b".\r\n", b".\n", b""):
                        break
                    data.append(ln)
                sink.messages.append({"from": mail_from, "to": list(rcpts), "data": b"".join(data)})
                self.wfile.write(b"250 queued\r\n")
            elif up == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 OK\r\n")


class SmtpSink:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.sink = self  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

"""Fail-closed health probe for the runtime's VNC/noVNC stack."""

from __future__ import annotations

import base64
import socket
import sys
import urllib.error
import urllib.request


TIMEOUT_SECONDS = 2
NOVNC_URL = "http://127.0.0.1:6080/vnc_lite.html"
VNC_ADDRESS = ("127.0.0.1", 5900)
WEBSOCKET_ADDRESS = ("127.0.0.1", 6080)


def _read_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("runtime viewer returned an incomplete response")
        data.extend(chunk)
    return bytes(data)


def _read_until(sock: socket.socket, marker: bytes) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > 16_384:
            raise RuntimeError("runtime viewer returned an oversized handshake")
    return bytes(data)


def _read_websocket_frame(sock: socket.socket) -> bytes:
    header = _read_exact(sock, 2)
    first, second = header
    if first & 0x0F != 0x02 or not first & 0x80:
        raise RuntimeError("websockify did not return a final binary frame")
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(_read_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(_read_exact(sock, 8), "big")
    if second & 0x80:
        raise RuntimeError("unexpected masked WebSocket server frame")
    if length > 1_048_576:
        raise RuntimeError("websockify returned an oversized WebSocket frame")
    return _read_exact(sock, length)


def _check_novnc_asset() -> None:
    with urllib.request.urlopen(NOVNC_URL, timeout=TIMEOUT_SECONDS) as response:
        if response.status != 200 or not response.read(256):
            raise RuntimeError("noVNC asset is unavailable")


def _check_vnc_handshake() -> None:
    with socket.create_connection(VNC_ADDRESS, timeout=TIMEOUT_SECONDS) as sock:
        if not sock.recv(12).startswith(b"RFB "):
            raise RuntimeError("x11vnc did not return an RFB greeting")


def _check_websocket_bridge() -> None:
    key = base64.b64encode(b"job-applier-health").decode("ascii")
    request = (
        "GET /websockify HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    with socket.create_connection(WEBSOCKET_ADDRESS, timeout=TIMEOUT_SECONDS) as sock:
        sock.sendall(request)
        response = _read_until(sock, b"\r\n\r\n")
        if not response.startswith(b"HTTP/1.1 101"):
            raise RuntimeError("websockify WebSocket upgrade failed")
        if not _read_websocket_frame(sock).startswith(b"RFB "):
            raise RuntimeError("websockify did not bridge an RFB greeting")


def main() -> int:
    try:
        _check_novnc_asset()
        _check_vnc_handshake()
        _check_websocket_bridge()
    except (OSError, RuntimeError, TimeoutError, urllib.error.URLError) as exc:
        print(f"runtime viewer health check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

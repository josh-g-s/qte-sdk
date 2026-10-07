"""Temporary diagnostic for issue 167: which actions from another thread wake a thread
blocked reading a socket, on this system. Removed before the PR is finished."""

import http.client
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def listener() -> socket.socket:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    return srv


def certificate() -> tuple[str, str, ssl.SSLContext, ssl.SSLContext]:
    d = Path(tempfile.mkdtemp())
    cert, key = str(d / "c.pem"), str(d / "k.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout",
         key, "-out", cert, "-subj", "/CN=t", "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True,
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    client = ssl.create_default_context(cafile=cert)
    return cert, key, server, client


def run(name: str, setup, action, timeout: float | None = 30.0) -> None:
    srv = listener()
    held: list[socket.socket] = []
    ready = threading.Event()
    result: dict = {}

    def accept() -> None:
        conn, _ = srv.accept()
        held.append(conn)

    acceptor = threading.Thread(target=accept, daemon=True)
    acceptor.start()
    sock = socket.create_connection(srv.getsockname(), timeout=timeout)
    acceptor.join()
    reader = setup(sock, held)

    def blocked() -> None:
        ready.set()
        start = time.monotonic()
        try:
            data = reader()
            result["outcome"] = f"returned {data!r}"
        except BaseException as error:  # noqa: BLE001
            result["outcome"] = f"raised {type(error).__name__}: {error}"
        result["after"] = time.monotonic() - start

    thread = threading.Thread(target=blocked, daemon=True)
    thread.start()
    ready.wait()
    time.sleep(0.5)
    acted = time.monotonic()
    try:
        action(sock)
        acting = f"action ok in {time.monotonic() - acted:.2f}s"
    except BaseException as error:  # noqa: BLE001
        acting = f"action raised {type(error).__name__}: {error}"
    thread.join(5)
    woke = "WOKE" if not thread.is_alive() else "STILL BLOCKED after 5s"
    print(f"{name:60s} {woke:24s} {acting:30s} {result.get('outcome', '')[:80]} "
          f"{result.get('after', 0):.2f}s", flush=True)
    for conn in held:
        try:
            conn.close()
        except OSError:
            pass
    srv.close()


def plain_recv(sock, held):
    return lambda: sock.recv(10)


def plain_makefile(sock, held):
    f = sock.makefile("rb")
    return lambda: f.read1(10)


def shutdown(sock):
    socket.socket.shutdown(sock, socket.SHUT_RDWR)


def shutdown_rd(sock):
    socket.socket.shutdown(sock, socket.SHUT_RD)


def close(sock):
    sock.close()


def real_close(sock):
    socket.socket._real_close(sock)  # the C close, ignoring makefile references


def linger_close(sock):
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0))
    socket.socket._real_close(sock)


def shutdown_then_close(sock):
    shutdown(sock)
    real_close(sock)


def tls_setup(server_ctx, client_ctx, handshake=True):
    def setup(sock, held):
        if handshake:
            srv_tls = server_ctx.wrap_socket(held[0], server_side=True, do_handshake_on_connect=False)
            t = threading.Thread(target=srv_tls.do_handshake, daemon=True)
            t.start()
        tls = client_ctx.wrap_socket(sock, server_hostname="127.0.0.1", do_handshake_on_connect=False)
        if handshake:
            tls.do_handshake()
            t.join()
            held[0] = srv_tls
            run.tls = tls
            return lambda: tls.recv(10)
        run.tls = tls
        return lambda: tls.do_handshake()

    return setup


def on_tls(action):
    def act(sock):
        action(run.tls)

    return act


def http_setup(sock, held):
    conn = http.client.HTTPConnection("127.0.0.1", 1)
    conn.sock = sock
    conn.putrequest("GET", "/")
    conn.endheaders()
    run.conn = conn
    return lambda: conn.getresponse()


def main() -> None:
    print(sys.version, sys.platform, flush=True)
    _, _, sctx, cctx = certificate()
    actions = {
        "shutdown RDWR": shutdown,
        "shutdown RD": shutdown_rd,
        "close()": close,
        "C close": real_close,
        "linger0 + C close": linger_close,
        "shutdown + C close": shutdown_then_close,
    }
    for timeout in (30.0, None):
        for aname, action in actions.items():
            run(f"plain recv, timeout={timeout}, {aname}", plain_recv, action, timeout)
        for aname, action in actions.items():
            run(f"makefile read1, timeout={timeout}, {aname}", plain_makefile, action, timeout)
        for aname, action in actions.items():
            run(f"http getresponse, timeout={timeout}, {aname}", http_setup, action, timeout)
        for aname in ("shutdown RDWR", "C close", "shutdown + C close", "linger0 + C close"):
            run(f"TLS recv, timeout={timeout}, {aname}", tls_setup(sctx, cctx), on_tls(actions[aname]), timeout)
            run(f"TLS handshake, timeout={timeout}, {aname}", tls_setup(sctx, cctx, False), on_tls(actions[aname]), timeout)


if __name__ == "__main__":
    main()

"""Proxy-aware SMTP/IMAP for the OpenShell sandbox.

The sandbox has no direct DNS/egress: every connection goes through the OpenShell policy proxy (HTTPS_PROXY). The
NemoClaw `gmail` preset lets python3 open HTTP CONNECT tunnels to smtp.gmail.com:465 and imap.gmail.com:993 only;
TLS is end-to-end through the tunnel. Outside a sandbox (no proxy env) these fall back to direct connections.
"""
import imaplib
import os
import smtplib
import socket
import ssl
import urllib.parse


def _proxy():
    url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("ALL_PROXY")
    if not url:
        return None
    u = urllib.parse.urlparse(url)
    return u.hostname, u.port or 3128


def tunnel(host, port, timeout=30):
    """TCP socket to host:port, via HTTP CONNECT when a proxy is configured."""
    px = _proxy()
    if not px:
        return socket.create_connection((host, port), timeout=timeout)
    s = socket.create_connection(px, timeout=timeout)
    s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            break
        resp += chunk
    status = resp.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 200" not in status:
        s.close()
        raise ConnectionRefusedError(f"proxy refused CONNECT {host}:{port}: {status} (is the gmail policy preset applied?)")
    return s


class SMTP_SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        raw = tunnel(host, port, timeout)
        return self.context.wrap_socket(raw, server_hostname=host)


class IMAP4_SSL(imaplib.IMAP4_SSL):
    def _create_socket(self, timeout):
        raw = tunnel(self.host, self.port, timeout or 30)
        return self.ssl_context.wrap_socket(raw, server_hostname=self.host)


def tls_probe(host, port):
    with tunnel(host, port, 10) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as t:
            return t.recv(80).decode(errors="replace").strip()

from __future__ import annotations

import http.server
import ipaddress
import select
import socket
import socketserver
import threading
import urllib.parse
from collections.abc import Callable
from typing import Any

from job_applier.logger import log_event

# Explicitly denied hostnames and domains
RESTRICTED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "broadcasthost",
        "local",
        "internal",
        "metadata.google.internal",
        "instance-data",
        "docker.for.mac.localhost",
        "docker.for.win.localhost",
    }
)

# Explicitly denied internal ports (even on public IPs)
RESTRICTED_PORTS: frozenset[int] = frozenset(
    {
        22,  # SSH
        25,  # SMTP
        5900,  # VNC default
        5901,  # VNC alternate
        6080,  # Websockify default
        8000,  # Web app internal port
        8899,  # Outbound proxy internal port
        9222,  # Chromium DevTools Protocol (CDP)
        2375,  # Docker daemon unencrypted
        2376,  # Docker daemon encrypted
        3306,  # MySQL
        5432,  # PostgreSQL
        6379,  # Redis
        27017,  # MongoDB
    }
)

# RFC-defined restricted IPv4 networks
RESTRICTED_IPV4_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("0.0.0.0/8"),  # "This" network
    ipaddress.IPv4Network("10.0.0.0/8"),  # Private RFC 1918
    ipaddress.IPv4Network("100.64.0.0/10"),  # Shared Address Space (CGNAT)
    ipaddress.IPv4Network("127.0.0.0/8"),  # Loopback
    ipaddress.IPv4Network("169.254.0.0/16"),  # Link-local / Cloud metadata
    ipaddress.IPv4Network("172.16.0.0/12"),  # Private RFC 1918
    ipaddress.IPv4Network("192.0.0.0/24"),  # IETF Protocol Assignments
    ipaddress.IPv4Network("192.0.2.0/24"),  # Documentation (TEST-NET-1)
    ipaddress.IPv4Network("192.168.0.0/16"),  # Private RFC 1918
    ipaddress.IPv4Network("198.18.0.0/15"),  # Benchmarking
    ipaddress.IPv4Network("198.51.100.0/24"),  # Documentation (TEST-NET-2)
    ipaddress.IPv4Network("203.0.113.0/24"),  # Documentation (TEST-NET-3)
    ipaddress.IPv4Network("224.0.0.0/4"),  # Multicast
    ipaddress.IPv4Network("240.0.0.0/4"),  # Reserved for Future Use
    ipaddress.IPv4Network("255.255.255.255/32"),  # Limited Broadcast
)

# RFC-defined restricted IPv6 networks
RESTRICTED_IPV6_NETWORKS: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("::/128"),  # Unspecified
    ipaddress.IPv6Network("::1/128"),  # Loopback
    ipaddress.IPv6Network("::ffff:0:0/96"),  # IPv4-mapped IPv6
    ipaddress.IPv6Network("64:ff9b::/96"),  # IPv4/IPv6 translation
    ipaddress.IPv6Network("100::/64"),  # Discard prefix
    ipaddress.IPv6Network("2001:db8::/32"),  # Documentation
    ipaddress.IPv6Network("fc00::/7"),  # Unique Local Addresses (ULA / RFC 4193)
    ipaddress.IPv6Network("fe80::/10"),  # Link-local
    ipaddress.IPv6Network("ff00::/8"),  # Multicast
    ipaddress.IPv6Network("fd00:ec2::254/128"),  # AWS IPv6 metadata
)


def is_ip_denied(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> tuple[bool, str]:
    """
    Checks if an IP address belongs to loopback, private, link-local, cloud metadata,
    or other non-routable/restricted address spaces.
    """
    # Check standard attributes
    if ip_obj.is_loopback:
        return True, f"Loopback address ({ip_obj})"
    if ip_obj.is_private:
        return True, f"Private address space ({ip_obj})"
    if ip_obj.is_link_local:
        return True, f"Link-local / Cloud metadata ({ip_obj})"
    if ip_obj.is_multicast:
        return True, f"Multicast address ({ip_obj})"
    if ip_obj.is_reserved:
        return True, f"Reserved address ({ip_obj})"
    if ip_obj.is_unspecified:
        return True, f"Unspecified address ({ip_obj})"

    # Handle IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped:
        return is_ip_denied(ip_obj.ipv4_mapped)

    if isinstance(ip_obj, ipaddress.IPv4Address):
        for net4 in RESTRICTED_IPV4_NETWORKS:
            if ip_obj in net4:
                return True, f"Restricted IPv4 range {net4} ({ip_obj})"
    elif isinstance(ip_obj, ipaddress.IPv6Address):
        for net6 in RESTRICTED_IPV6_NETWORKS:
            if ip_obj in net6:
                return True, f"Restricted IPv6 range {net6} ({ip_obj})"

    return False, "Public IP"


def resolve_and_validate_host(
    hostname: str,
    dns_resolver: Callable[[str], list[str]] | None = None,
) -> tuple[bool, str, list[str]]:
    """
    Validates hostname, resolves DNS, and checks EVERY resolved IP against SSRF/rebinding.
    Returns (allowed: bool, reason: str, validated_ips: list[str]).
    """
    cleaned = hostname.strip().lower().rstrip(".")
    if not cleaned:
        return False, "Empty hostname", []

    # Strip bracket notation for IPv6 literals
    raw_host = cleaned.strip("[]")

    # Static hostname check
    if raw_host in RESTRICTED_HOSTNAMES or any(
        raw_host.endswith(f".{r}") for r in RESTRICTED_HOSTNAMES
    ):
        return False, f"Restricted hostname: {hostname}", []

    # Check direct IP literals
    try:
        ip_obj = ipaddress.ip_address(raw_host)
        denied, reason = is_ip_denied(ip_obj)
        if denied:
            return False, f"Direct IP denied: {reason}", []
        return True, "Valid direct IP", [str(ip_obj)]
    except ValueError:
        pass  # It is a domain name, proceed to DNS resolution

    # Resolve hostname via standard DNS or provided custom resolver
    resolved_ips: list[str] = []
    if dns_resolver is not None:
        try:
            resolved_ips = dns_resolver(raw_host)
        except Exception as e:
            return False, f"DNS resolution failed for {hostname}: {e}", []
    else:
        try:
            addr_info = socket.getaddrinfo(raw_host, None)
            for item in addr_info:
                sockaddr = item[4]
                if sockaddr and isinstance(sockaddr, tuple):
                    resolved_ips.append(str(sockaddr[0]))
        except socket.gaierror as e:
            return False, f"DNS resolution failed for {hostname}: {e}", []
        except Exception as e:
            return False, f"Unexpected DNS resolution error for {hostname}: {e}", []

    if not resolved_ips:
        return False, f"No IP addresses resolved for {hostname}", []

    # Anti-DNS-Rebinding: Validate EVERY resolved address
    for ip_str in resolved_ips:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            denied, reason = is_ip_denied(ip_obj)
            if denied:
                return (
                    False,
                    f"DNS rebinding detected: Host '{hostname}' resolved to restricted IP {ip_str} ({reason})",
                    [],
                )
        except ValueError:
            return False, f"Invalid resolved IP format: {ip_str}", []

    return True, "Host and DNS verified", resolved_ips


def validate_host_and_dns(
    hostname: str,
    dns_resolver: Callable[[str], list[str]] | None = None,
) -> tuple[bool, str]:
    """
    Validates hostname and checks DNS resolution against DNS rebinding attacks.
    If the hostname resolves to ANY private, loopback, or cloud metadata IP, it fails closed.
    """
    allowed, reason, _ = resolve_and_validate_host(hostname, dns_resolver=dns_resolver)
    return allowed, reason


def validate_target_url(
    url: str,
    dns_resolver: Callable[[str], list[str]] | None = None,
    allow_file_scheme: bool = False,
) -> tuple[bool, str]:
    """
    Strict URL validation for browser navigation, redirects, and subresources.
    Enforces allowed schemes (http, https), blocked internal ports, and anti-SSRF/DNS rebinding checks.
    """
    if not url or not isinstance(url, str):
        return False, "Invalid URL input"

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as e:
        return False, f"URL parsing error: {e}"

    scheme = parsed.scheme.lower()
    if scheme == "file":
        if allow_file_scheme:
            return True, "File scheme permitted for local testing"
        return (
            False,
            "Unsupported or dangerous scheme 'file': only http and https allowed",
        )

    # Allowed schemes only
    if scheme not in ("http", "https"):
        return (
            False,
            f"Unsupported or dangerous scheme '{parsed.scheme}': only http and https allowed",
        )

    hostname = parsed.hostname
    if not hostname:
        return False, "URL has no hostname"

    port = parsed.port
    if port is not None and port in RESTRICTED_PORTS:
        return False, f"Access to restricted internal port {port} is denied"

    return validate_host_and_dns(hostname, dns_resolver=dns_resolver)


def attach_security_routes(
    context: Any,
    allow_file_scheme: bool = False,
) -> None:
    """
    Attaches security route filtering to a Playwright BrowserContext.
    Intercepts and blocks requests to private IPs, metadata, internal services,
    and DNS-rebound targets across all navigations, redirects, and subresources.
    """

    def _security_route_handler(route: Any, request: Any) -> None:
        target_url = request.url
        allowed, reason = validate_target_url(
            target_url, allow_file_scheme=allow_file_scheme
        )

        if allowed:
            try:
                route.continue_()
            except Exception:
                pass
        else:
            log_event(
                f"Blocked dangerous request to {target_url}: {reason}",
                level="WARN",
                category="Security",
            )
            try:
                route.abort("blockedbyclient")
            except Exception:
                pass

    try:
        context.route("**/*", _security_route_handler)
    except Exception as e:
        print(f"Notice: Failed to attach security route interceptor: {e}")


class OutboundSecurityProxyHandler(http.server.BaseHTTPRequestHandler):
    """
    HTTP/CONNECT proxy handler enforcing fail-closed egress security policy.
    Blocks private IP ranges, cloud metadata, loopback, and DNS rebinding at the socket layer.
    """

    def do_CONNECT(self) -> None:  # noqa: N802
        """Handles HTTPS CONNECT tunnel requests."""
        raw_address = self.path
        if ":" in raw_address:
            host, port_str = raw_address.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                port = 443
        else:
            host = raw_address
            port = 443

        if port in RESTRICTED_PORTS:
            self.send_error(403, f"Direct egress denied: Restricted port {port}")
            return

        allowed, reason, validated_ips = resolve_and_validate_host(host)
        if not allowed or not validated_ips:
            self.send_error(403, f"Direct egress denied: {reason}")
            return

        try:
            # Connect directly to the validated IP to prevent TOCTOU DNS rebinding
            remote_sock = socket.create_connection((validated_ips[0], port), timeout=10)
        except Exception as e:
            self.send_error(502, f"Proxy connection failed: {e}")
            return

        self.send_response(200, "Connection Established")
        self.end_headers()

        # Bidirectional streaming between client and remote destination
        conns = [self.connection, remote_sock]
        try:
            while True:
                rlist, _, xlist = select.select(conns, [], conns, 30)
                if xlist or not rlist:
                    break
                for s in rlist:
                    other = remote_sock if s is self.connection else self.connection
                    data = s.recv(8192)
                    if not data:
                        return
                    other.sendall(data)
        except Exception:
            pass
        finally:
            remote_sock.close()

    def do_GET(self) -> None:  # noqa: N802
        """Handles standard HTTP requests through the proxy."""
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.scheme or not parsed.hostname:
            self.send_error(400, "Bad Request: Absolute URL required for proxy GET")
            return

        if parsed.scheme.lower() != "http":
            self.send_error(
                403, f"Direct egress denied: Unsupported proxy scheme '{parsed.scheme}'"
            )
            return

        host = parsed.hostname
        port = parsed.port or 80

        if port in RESTRICTED_PORTS:
            self.send_error(403, f"Direct egress denied: Restricted port {port}")
            return

        allowed, reason, validated_ips = resolve_and_validate_host(host)
        if not allowed or not validated_ips:
            self.send_error(403, f"Direct egress denied: {reason}")
            return

        try:
            remote_sock = socket.create_connection((validated_ips[0], port), timeout=10)
        except Exception as e:
            self.send_error(502, f"Proxy connection failed: {e}")
            return

        try:
            req_path = parsed.path or "/"
            if parsed.query:
                req_path += f"?{parsed.query}"
            out_headers = [f"GET {req_path} HTTP/1.1\r\n".encode()]
            out_headers.append(f"Host: {parsed.netloc}\r\n".encode())
            for key, val in self.headers.items():
                if key.lower() not in ("host", "proxy-connection", "connection"):
                    out_headers.append(f"{key}: {val}\r\n".encode())
            out_headers.append(b"Connection: close\r\n\r\n")

            remote_sock.sendall(b"".join(out_headers))

            while True:
                chunk = remote_sock.recv(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            pass
        finally:
            remote_sock.close()

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress verbose default proxy request logging to console
        pass


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class OutboundSecurityProxy:
    """
    Enforceable outbound forward proxy server ensuring no browser direct-egress
    bypasses private/loopback/metadata/DNS-rebinding denial policies.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8899):
        self.host = host
        self.port = port
        self._server: ThreadedTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._server = ThreadedTCPServer(
            (self.host, self.port), OutboundSecurityProxyHandler
        )
        self._running = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._running or self._server is None:
            return
        self._running = False
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass
        self._server = None
        self._thread = None

    @property
    def proxy_url(self) -> str:
        return f"http://{self.host}:{self.port}"

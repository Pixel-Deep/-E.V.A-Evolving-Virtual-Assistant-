"""Bounded external URL reachability checks with SSRF-resistant redirect handling."""
from __future__ import annotations

import errno
import http.client
import ipaddress
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPHandler, HTTPSHandler, HTTPRedirectHandler, Request, build_opener
from typing import Any

from eva.automation.engine.models import AutomationError, ExecutionContext, ToolResult, ToolSpec
from eva.automation.engine.registry import ToolRegistry
from eva.automation.tools.browser import validate_browser_url


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _validate_host_and_resolve_ip(host: str, port: int) -> tuple[str, list[ipaddress.IPv4Address | ipaddress.IPv6Address]]:
    hostname = (host or "").rstrip(".").lower()
    if not hostname or hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".local", ".internal", ".test", ".lan", ".home")):
        raise AutomationError("URL_BLOCKED", "Local, internal, and test hostnames are blocked by the URL checker.")
    try:
        addresses = [ipaddress.ip_address(hostname)] if _is_ip(hostname) else [
            ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        ]
    except (OSError, ValueError) as exc:
        raise AutomationError("DNS_LOOKUP_FAILED", f"Could not resolve the URL host: {exc}") from exc
    if not addresses or any(not addr.is_global for addr in addresses):
        raise AutomationError("URL_BLOCKED", "URL host resolves to a non-public or reserved IP address; the request was blocked.")
    return hostname, addresses


def _validate_public_target(url: str) -> str:
    clean = validate_browser_url(url)
    parsed = urlsplit(clean)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    _validate_host_and_resolve_ip(host, port)
    return clean


class SafeHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        if hasattr(sys, "audit"):
            sys.audit("http.client.connect", self, self.host, self.port)
        _, addresses = _validate_host_and_resolve_ip(self.host, self.port or 80)
        pinned_ip = str(addresses[0])
        self.sock = socket.create_connection(
            (pinned_ip, self.port), self.timeout, self.source_address
        )
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            if getattr(e, "errno", None) != getattr(errno, "ENOPROTOOPT", None):
                raise
        if self._tunnel_host:
            self._tunnel()


class SafeHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        if hasattr(sys, "audit"):
            sys.audit("http.client.connect", self, self.host, self.port)
        _, addresses = _validate_host_and_resolve_ip(self.host, self.port or 443)
        pinned_ip = str(addresses[0])
        self.sock = socket.create_connection(
            (pinned_ip, self.port), self.timeout, self.source_address
        )
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            if getattr(e, "errno", None) != getattr(errno, "ENOPROTOOPT", None):
                raise
        if self._tunnel_host:
            server_hostname = self._tunnel_host
            self._tunnel()
        else:
            server_hostname = self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


class SafeHTTPHandler(HTTPHandler):
    def http_open(self, req):
        return self.do_open(SafeHTTPConnection, req)


class SafeHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        return self.do_open(SafeHTTPSConnection, req, context=self._context)


def _url_check(context: ExecutionContext, args: dict[str, Any]) -> ToolResult:
    current = _validate_public_target(args["url"])
    started = time.monotonic()
    redirects = []
    timeout = min(max(args.get("timeout_seconds", 8), 1), 15)
    opener = build_opener(_NoRedirect(), SafeHTTPHandler(), SafeHTTPSHandler())
    status = None
    content_type = None
    method_used = "HEAD"
    for hop in range(6):
        context.check_cancelled()
        current = _validate_public_target(current)
        request = Request(current, headers={"User-Agent": "EVA-URL-Check/1.0"}, method="HEAD")
        try:
            response = opener.open(request, timeout=timeout)
            status = response.status
            content_type = response.headers.get("Content-Type")
            response.close()
        except AutomationError:
            raise
        except HTTPError as error:
            status = error.code
            location = error.headers.get("Location") if error.headers else None
            content_type = error.headers.get("Content-Type") if error.headers else None
            error.close()
            if status in {301, 302, 303, 307, 308} and location:
                if hop >= 5:
                    raise AutomationError("TOO_MANY_REDIRECTS", "URL exceeded the 5-redirect limit.")
                next_url = urljoin(current, location)
                _validate_public_target(next_url)
                redirects.append({"status": status, "from": current, "to": next_url})
                current = next_url
                continue
            if status in {405, 501} and method_used == "HEAD":
                method_used = "GET_RANGE"
                get_request = Request(current, headers={"User-Agent": "EVA-URL-Check/1.0", "Range": "bytes=0-0"}, method="GET")
                try:
                    response = opener.open(get_request, timeout=timeout)
                    status = response.status
                    content_type = response.headers.get("Content-Type")
                    response.close()
                except AutomationError:
                    raise
                except HTTPError as get_error:
                    status = get_error.code
                    location = get_error.headers.get("Location") if get_error.headers else None
                    content_type = get_error.headers.get("Content-Type") if get_error.headers else None
                    get_error.close()
                    if status in {301, 302, 303, 307, 308} and location:
                        if hop >= 5:
                            raise AutomationError("TOO_MANY_REDIRECTS", "URL exceeded the 5-redirect limit.")
                        next_url = urljoin(current, location)
                        _validate_public_target(next_url)
                        redirects.append({"status": status, "from": current, "to": next_url})
                        current = next_url
                        continue
                except (URLError, TimeoutError, OSError) as get_error:
                    if isinstance(getattr(get_error, "reason", None), AutomationError):
                        raise get_error.reason
                    raise AutomationError("URL_CHECK_FAILED", f"GET range request failed: {get_error}") from get_error
            break
        except (URLError, TimeoutError, OSError) as exc:
            if isinstance(getattr(exc, "reason", None), AutomationError):
                raise exc.reason
            raise AutomationError("URL_CHECK_FAILED", f"Could not reach the URL: {exc}") from exc
        break
    elapsed = int((time.monotonic() - started) * 1000)
    reachable = status is not None and 200 <= status < 400
    return ToolResult(True, "network.url_check", {
        "requested_url": args["url"], "final_url": current, "http_status": status,
        "reachable": reachable,
        "content_type": content_type, "redirects": redirects, "elapsed_ms": elapsed,
        "method": method_used,
    }, "HTTP reachability check completed; result reflects the actual server response.", verified=reachable)


def register_network_tools(registry: ToolRegistry) -> None:
    registry.register(ToolSpec("network.url_check", "Check external HTTP/HTTPS reachability and actual status without downloading a page body; local/private targets and unsafe redirects are blocked.", {
        "type": "object", "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 4096},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 15}},
        "required": ["url"], "additionalProperties": False}, ("network.http",), "network",
        timeout_seconds=20, aliases=("check URL reachability", "HTTP status")), _url_check)

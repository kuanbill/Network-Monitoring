"""Bounded HTTPS checks for the two explicitly configured website targets."""
import http.client
import ipaddress
import socket
import ssl
import threading
import time


WEBSITE_DOMAINS = {
    '192.168.6.23': 'urc.landagent.com.tw',
    '192.168.6.44': 'ai.tailan.com.tw',
}
DEADLINE = 5
_SLOTS = {domain: threading.Lock() for domain in WEBSITE_DOMAINS.values()}
_GLOBAL_IPV6 = ipaddress.ip_network('2000::/3')
# Be conservative for special-purpose allocations whose classification varies
# across the supported Python versions.
_SPECIAL_NETWORKS = tuple(map(ipaddress.ip_network, (
    '192.0.0.0/24', '192.88.99.0/24', '2001::/23', '2002::/16', '3fff::/20',
)))


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return remaining


def _public_endpoints(domain):
    answers = socket.getaddrinfo(domain, 443, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    seen = set()
    for family, _, _, _, endpoint in answers:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        ip = ipaddress.ip_address(endpoint[0])
        if not ip.is_global or ip.is_multicast or ip.is_reserved or any(ip in net for net in _SPECIAL_NETWORKS):
            continue
        if family == socket.AF_INET6:
            if ip.version != 6 or ip.scope_id is not None or ip not in _GLOBAL_IPV6 or endpoint[3]:
                continue
            numeric = (str(ip), 443, 0, 0)
        else:
            if ip.version != 4:
                continue
            numeric = (str(ip), 443)
        if numeric not in seen:
            seen.add(numeric)
            yield family, numeric


def _request_status(domain, family, endpoint, deadline):
    context = ssl.create_default_context()
    with socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP) as raw:
        raw.settimeout(_remaining(deadline))
        raw.connect(endpoint)
        _remaining(deadline)
        with context.wrap_socket(raw, server_hostname=domain, do_handshake_on_connect=False) as transport:
            def expire():
                # shutdown also wakes a reader holding a socket file reference.
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                transport.close()

            timer = threading.Timer(_remaining(deadline), expire)
            timer.daemon = True
            timer.start()
            try:
                transport.settimeout(_remaining(deadline))
                transport.do_handshake()
                transport.settimeout(_remaining(deadline))
                request = f'GET / HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n'
                transport.sendall(request.encode('ascii'))
                transport.settimeout(_remaining(deadline))
                response = http.client.HTTPResponse(transport)
                try:
                    response.begin()
                    _remaining(deadline)
                    return response.status
                finally:
                    response.close()
            finally:
                timer.cancel()


def _failure(code, ok=False):
    return {'code': code, 'ok': ok, 'latency_ms': None}


def _probe(domain, started, deadline):
    try:
        endpoints = list(_public_endpoints(domain))
    except (OSError, ValueError):
        return _failure('dns')
    _remaining(deadline)
    code = 'dns'
    for family, endpoint in endpoints:
        try:
            _remaining(deadline)
            status = _request_status(domain, family, endpoint, deadline)
        except ssl.SSLCertVerificationError:
            code = 'certificate'
        except TimeoutError:
            code = 'timeout'
        except (OSError, http.client.HTTPException):
            code = 'connection'
        else:
            _remaining(deadline)
            return {'code': 'http', 'ok': 200 <= status < 300, 'http_status': status,
                    'latency_ms': round((time.monotonic() - started) * 1000, 1)}
    _remaining(deadline)
    return _failure(code)


def probe_website(host_ip: str) -> dict:
    domain = WEBSITE_DOMAINS.get(host_ip)
    if domain is None:
        raise ValueError('此主機不支援固定網站檢查')
    slot = _SLOTS[domain]
    if not slot.acquire(blocking=False):
        return _failure('busy', None)
    started = time.monotonic()
    deadline = started + DEADLINE
    done = threading.Event()
    result = _failure('timeout')

    def work():
        nonlocal result
        try:
            result = _probe(domain, started, deadline)
        except TimeoutError:
            pass
        finally:
            slot.release()
            done.set()

    try:
        threading.Thread(target=work, daemon=True, name=f'website-probe:{domain}').start()
    except RuntimeError:
        slot.release()
        return _failure('busy', None)
    if done.wait(max(0, deadline - time.monotonic())) and time.monotonic() < deadline:
        return result
    return _failure('timeout')

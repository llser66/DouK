"""LAN access policy for the business app; the collection tools stay loopback-only."""
from functools import lru_cache
import ipaddress
import json
from pathlib import Path
import socket
import time

ROOT = Path(__file__).resolve().parent
NETWORK_CONFIG = ROOT / 'data' / 'network.json'
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '127.0.0.0/8'))


def local_ipv4():
    """Discover interface addresses locally, without contacting an Internet service."""
    return list(_addresses(int(time.monotonic() // 30)))


@lru_cache(maxsize=2)
def _addresses(_period):
    addresses = {'127.0.0.1'}
    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = entry[4][0]
            if _private_client(address):
                addresses.add(address)
    except OSError:
        pass
    return tuple(sorted(addresses, key=lambda value: (value.startswith('127.'),
                                                     not value.startswith('192.168.'), value)))


def _private_client(value):
    try:
        address = ipaddress.ip_address(value)
        if address == ipaddress.ip_address('::1'):
            return True
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return isinstance(address, ipaddress.IPv4Address) and any(address in net for net in PRIVATE_NETWORKS)
    except ValueError:
        return False


def bind_host(config_path=None):
    path = Path(config_path) if config_path is not None else NETWORK_CONFIG
    if not path.exists():
        return '0.0.0.0'
    try:
        config = json.loads(path.read_text(encoding='utf-8-sig'))
        host = config.get('listen_host', '0.0.0.0')
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError('无法读取局域网配置 data/network.json') from exc
    if host != '0.0.0.0' and host not in local_ipv4():
        raise ValueError('监听地址必须为本机 IPv4 或 0.0.0.0')
    return host


def trusted_request(headers, port, client_ip, tools_mode=False, allow_document_navigation=False):
    """Do not accept forwarded IP headers or arbitrary private-address Host names."""
    if not _private_client(client_ip):
        return False
    if tools_mode:
        try:
            if not ipaddress.ip_address(client_ip).is_loopback:
                return False
        except ValueError:
            return False
        addresses = {'127.0.0.1', 'localhost'}
    else:
        addresses = set(local_ipv4()) | {'localhost', '127.0.0.1'}
    expected = {f'{address}:{port}' for address in addresses}
    host = str(headers.get('Host', '')).lower()
    if host not in expected:
        return False
    origin = headers.get('Origin')
    # A valid origin must match this request's Host, not another allowed host.
    if origin and origin != f'http://{host}':
        return False
    if headers.get('Sec-Fetch-Site', 'same-origin') != 'cross-site':
        return True
    # A link from chat/email may navigate to an entry page. Cross-site API calls
    # and embedded documents remain blocked, even with a valid local Host.
    return (allow_document_navigation and headers.get('Sec-Fetch-Mode') == 'navigate'
            and headers.get('Sec-Fetch-Dest') == 'document')


def network_info(port=8877):
    host = bind_host()
    addresses = [value for value in local_ipv4() if not value.startswith('127.')]
    try:
        advertised = json.loads(NETWORK_CONFIG.read_text(encoding='utf-8-sig')).get('advertise_addresses', [])
        preferred = [value for value in advertised if value in addresses]
        if preferred:
            addresses = preferred
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    if host == '127.0.0.1':
        addresses = []
    elif host != '0.0.0.0':
        addresses = [value for value in addresses if value == host]
    status = {'configured': False, 'message': '需要允许 Windows 防火墙 TCP 8877 的本地子网访问；可运行 setup-lan.ps1。'}
    try:
        saved = json.loads((ROOT / 'data' / 'lan-status.json').read_text(encoding='utf-8-sig'))
        if isinstance(saved, dict):
            status.update(saved)
    except (OSError, ValueError):
        pass
    return {'listen_host': host, 'addresses': addresses,
            'employee_urls': [f'http://{address}:{port}/report' for address in addresses],
            'admin_urls': [f'http://{address}:{port}/admin' for address in addresses],
            'local_employee_url': f'http://127.0.0.1:{port}/report',
            'local_admin_url': f'http://127.0.0.1:{port}/admin', 'firewall': status}


if __name__ == '__main__':
    print(json.dumps(network_info(), ensure_ascii=False))

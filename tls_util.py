"""Self-signed TLS certificate generator for HTTPS mobile access (port 8085)."""
from __future__ import annotations

import datetime
import ipaddress
import socket
import ssl
from pathlib import Path
from typing import List, Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import config as cfg_mod

CERT_DIR = cfg_mod.APP_DIR / "certs"
CERT_FILE = CERT_DIR / "server.crt"
KEY_FILE = CERT_DIR / "server.key"


def get_local_ipv4_addresses() -> List[str]:
    """Collect local IPv4 addresses for SAN entries."""
    ips = {"127.0.0.1"}
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ips.add(sock.getsockname()[0])
        sock.close()
    except Exception:
        pass
    return sorted(ips)


def ensure_tls_cert() -> Tuple[Path, Path]:
    """Generate a self-signed certificate with local IP SANs if not already present."""
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    if CERT_FILE.exists() and KEY_FILE.exists():
        return CERT_FILE, KEY_FILE

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "DualRigRemote"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Amateur Radio Remote"),
        ]
    )

    san_list: List[x509.GeneralName] = [x509.DNSName("localhost")]
    for ip_str in get_local_ipv4_addresses():
        try:
            san_list.append(x509.IPAddress(ipaddress.ip_address(ip_str)))
        except ValueError:
            pass

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1825))
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
        .sign(key, hashes.SHA256())
    )

    KEY_FILE.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return CERT_FILE, KEY_FILE


def create_ssl_context() -> ssl.SSLContext:
    """Return an SSLContext configured for aiohttp HTTPS server."""
    cert_path, key_path = ensure_tls_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx

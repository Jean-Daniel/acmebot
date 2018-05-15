# Verify
import socket
import time
from typing import Optional, Tuple, List

import OpenSSL
from asn1crypto import ocsp as asn1_ocsp

from acmebot.config import CertificateSpec
from acmebot.context import CertificateContext
from acmebot.context import CertificateItem
from acmebot.crypto import Certificate
from . import log, AcmeError, SUPPORTED_KEY_TYPES
from .ocsp import ocsp_response_status


def _send_starttls(ty: str, sock: socket.socket, host_name: str):
    sock.settimeout(30)
    ty = ty.lower()
    if 'smtp' == ty:
        log.debug('SMTP: %s', sock.recv(4096))
        sock.send(b'ehlo acmebot.org\r\n')
        buffer = sock.recv(4096)
        log.debug('SMTP: %s', buffer)
        if b'STARTTLS' not in buffer:
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
            raise AcmeError('STARTTLS not supported on server')
        sock.send(b'starttls\r\n')
        log.debug('SMTP: %s', sock.recv(4096))
    elif 'pop3' == ty:
        log.debug('POP3: %s', sock.recv(4096))
        sock.send(b'STLS\r\n')
        log.debug('POP3: %s', sock.recv(4096))
    elif 'imap' == ty:
        log.debug('IMAP: %s', sock.recv(4096))
        sock.send(b'a001 STARTTLS\r\n')
        log.debug('IMAP: %s', sock.recv(4096))
    elif 'ftp' == ty:
        log.debug('FTP: %s', sock.recv(4096))
        sock.send(b'AUTH TLS\r\n')
        log.debug('FTP: %s', sock.recv(4096))
    elif 'xmpp' == ty:
        sock.send('<stream:stream xmlns:stream="http://etherx.jabber.org/streams" '
                  'xmlns="jabber:client" to="{host}" version="1.0">\n'.format(host=host_name).encode('ascii'))
        log.debug('XMPP: %s', sock.recv(4096), '\n')
        sock.send(b'<starttls xmlns="urn:ietf:params:xml:ns:xmpp-tls"/>')
        log.debug('XMPP: %s', sock.recv(4096), '\n')
    elif 'sieve' == ty:
        buffer = sock.recv(4096)
        log.debug('SIEVE: %s', buffer)
        if b'"STARTTLS"' not in buffer:
            sock.shutdown()
            sock.close()
            raise AcmeError('STARTTLS not supported on server')
        sock.send(b'StartTls\r\n')
        log.debug('SIEVE: %s', sock.recv(4096))
    else:
        sock.shutdown(socket.SHUT_RDWR)
        sock.close()
        raise Exception('Unsuppoprted STARTTLS type: ' + ty)
    sock.settimeout(None)


def fetch_tls_info(addr, ssl_context, host_name: str, starttls: Optional[str]) -> Tuple[List[OpenSSL.crypto.X509], asn1_ocsp.OCSPResponse]:
    sock = socket.socket(addr[0], socket.SOCK_STREAM)
    sock.connect(addr[4])

    if starttls:
        _send_starttls(starttls, sock, host_name)

    def _process_ocsp(conn: OpenSSL.SSL.Connection, ocsp_data, data):
        conn.set_app_data(asn1_ocsp.OCSPResponse.load(ocsp_data) if (b'' != ocsp_data) else None)
        return True

    ssl_context.set_ocsp_client_callback(_process_ocsp)
    ssl_sock = OpenSSL.SSL.Connection(ssl_context, sock)
    ssl_sock.set_connect_state()
    ssl_sock.set_tlsext_host_name(host_name.encode('ascii'))
    ssl_sock.request_ocsp()
    ssl_sock.do_handshake()
    ocsp = ssl_sock.get_app_data()
    log.debug('Connected to %s, protocol %s, cipher %s, OCSP Staple %s', ssl_sock.get_servername(), ssl_sock.get_protocol_version_name(),
              ssl_sock.get_cipher_name(), ocsp_response_status(ocsp).upper() if ocsp else 'missing')
    installed_certificates = ssl_sock.get_peer_cert_chain()  # type: List[OpenSSL.crypto.X509]

    ssl_sock.shutdown()
    ssl_sock.close()
    return installed_certificates, ocsp


def _verify_certificate_installation(item: CertificateItem, host_name: str, port_number: int, starttls: Optional[str], cipher_list,
                                     max_ocsp_verify_attempts: int, ocsp_verify_retry_delay: int):
    ssl_context = OpenSSL.SSL.Context(OpenSSL.SSL.SSLv23_METHOD)
    ssl_context.set_cipher_list(cipher_list)

    try:
        if host_name.startswith('*.'):
            host_name = 'wildcard-test.' + host_name[2:]
        addr_info = socket.getaddrinfo(host_name, port_number, proto=socket.IPPROTO_TCP)
    except Exception as error:
        log.error('[%s:%s] VALIDATION ERROR: Unable to get address for %s: %s',
                  item.name, item.type.upper(), host_name, str(error))
        return

    for addr in addr_info:
        host_desc = host_name + ' at ' + (('[' + addr[4][0] + ']') if (socket.AF_INET6 == addr[0]) else addr[4][0]) + ':' + str(port_number)
        try:
            log.debug('Connecting to %s with %s ciphers', host_desc, item.type.upper())
            installed_certificates, ocsp_staple = fetch_tls_info(addr, ssl_context, host_name, starttls)
            if item.certificate.has_oscp_must_staple:
                attempts = 1
                while (not ocsp_staple) and (attempts < max_ocsp_verify_attempts):
                    time.sleep(ocsp_verify_retry_delay)
                    log.debug('Retry to fetch OCSP staple')
                    installed_certificates, ocsp_staple = fetch_tls_info(addr, ssl_context, host_name, starttls)
                    attempts += 1

            installed_certificate = Certificate(installed_certificates[0].to_cryptography())
            installed_chain = [Certificate(cert.to_cryptography()) for cert in installed_certificates[1:]]
            if item.certificate == installed_certificate:
                log.info('[%s:%s] certificate present on %s', item.name, item.type.upper(), host_desc, extra={'color': 'green'})
            else:
                log.error('[%s:%s] VALIDATION ERROR: certificate %s mismatch on %s', item.name, item.type.upper(), installed_certificate.common_name, host_desc)
            if len(item.chain) != len(installed_chain):
                log.error('[%s:%s] VALIDATION ERROR: certificate chain length mismatch on %s, got %s intermediate(s), expected %s',
                          item.name, item.type.upper(), host_desc, len(installed_chain), len(item.chain))
            else:
                for intermediate, installed_intermediate in zip(item.chain, installed_chain):
                    if intermediate == installed_intermediate:
                        log.info('[%s:%s] Intermediate certificate "%s" present on %s',
                                 item.name, item.type.upper(), intermediate.common_name, host_desc, extra={'color': 'green'})
                    else:
                        log.error('[%s:%s] VALIDATION ERROR: Intermediate certificate "%s" mismatch on %s',
                                  item.name, item.type.upper(), installed_intermediate.common_name, host_desc)
            if ocsp_staple:
                log.debug('Verify OCSP response status')
                ocsp_status = ocsp_response_status(ocsp_staple)
                if 'good' == ocsp_status.lower():
                    log.info('OCSP staple status is GOOD on %s', host_desc, extra={'color': 'green'})
                else:
                    log.error('ERROR: OCSP staple has status: %s on %s', ocsp_status.upper(), host_desc)
            else:
                if item.certificate.has_oscp_must_staple:
                    log.error('[%s:%s] VALIDATION ERROR: Certificate has OCSP Must-Staple but no OSCP staple found on %s',
                              item.name, item.type.upper(), host_desc)
        except Exception as error:
            log.error('[%s:%s] VALIDATION ERROR: Unable to connect to %s: %s', item.name, item.type.upper(), host_desc, str(error))


def verify_certificate_installation(context: CertificateContext, max_ocsp_verify_attempts: int, ocsp_verify_retry_delay: int):
    key_type_ciphers = {}
    ssl_context = OpenSSL.SSL.Context(OpenSSL.SSL.SSLv23_METHOD)
    ssl_sock = OpenSSL.SSL.Connection(ssl_context, socket.socket())
    all_ciphers = ssl_sock.get_cipher_list()
    for key_type in context.spec.key_types:
        key_type_ciphers[key_type] = ':'.join([cipher_name for cipher_name in all_ciphers if key_type.upper() in cipher_name]).encode('ascii')

    verify_list = context.spec.verify
    if not verify_list:
        return

    for item in context:  # type: CertificateItem
        certificate = item.certificate
        if not certificate:
            log.warning('[%s:%s] certificate not found', context.name, item.type.upper())
            continue

        chain = item.chain
        if not chain:
            log.warning('[%s:%s] chain not found', context.name, item.type.upper())
            continue

        for verify in verify_list:
            if verify.key_types and item.type not in verify.key_types:
                continue
            for host_name in verify.hosts or context.spec.alt_names:
                _verify_certificate_installation(item, host_name, verify.port, verify.starttls, key_type_ciphers[item.type],
                                                 max_ocsp_verify_attempts, ocsp_verify_retry_delay)

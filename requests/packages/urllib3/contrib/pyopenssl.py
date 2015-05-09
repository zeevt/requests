'''SSL with SNI_-support for Python 2. Follow these instructions if you would
like to verify SSL certificates in Python 2. Note, the default libraries do
*not* do certificate checking; you need to do additional work to validate
certificates yourself.

This needs the following packages installed:

* pyOpenSSL (tested with 0.13)
* ndg-httpsclient (tested with 0.3.2)
* pyasn1 (tested with 0.1.6)

You can install them with the following command:

    pip install pyopenssl ndg-httpsclient pyasn1

To activate certificate checking, call
:func:`~urllib3.contrib.pyopenssl.inject_into_urllib3` from your Python code
before you begin making HTTP requests. This can be done in a ``sitecustomize``
module, or at any other time before your application begins using ``urllib3``,
like this::

    try:
        import urllib3.contrib.pyopenssl
        urllib3.contrib.pyopenssl.inject_into_urllib3()
    except ImportError:
        pass

Now you can use :mod:`urllib3` as you normally would, and it will support SNI
when the required modules are installed.

Activating this module also has the positive side effect of disabling SSL/TLS
compression in Python 2 (see `CRIME attack`_).

If you want to configure the default list of supported cipher suites, you can
set the ``urllib3.contrib.pyopenssl.DEFAULT_SSL_CIPHER_LIST`` variable.

Module Variables
----------------

:var DEFAULT_SSL_CIPHER_LIST: The list of supported SSL/TLS cipher suites.

.. _sni: https://en.wikipedia.org/wiki/Server_Name_Indication
.. _crime attack: https://en.wikipedia.org/wiki/CRIME_(security_exploit)

'''

try:
    from ndg.httpsclient.ssl_peer_verification import SUBJ_ALT_NAME_SUPPORT
    from ndg.httpsclient.subj_alt_name import SubjectAltName as BaseSubjectAltName
except SyntaxError as e:
    raise ImportError(e)

import OpenSSL.SSL
from pyasn1.codec.der import decoder as der_decoder
from pyasn1.type import univ, constraint
from socket import _fileobject, timeout
import ssl
import select

from .. import connection
from .. import util

__all__ = ['inject_into_urllib3', 'extract_from_urllib3']

# SNI only *really* works if we can read the subjectAltName of certificates.
HAS_SNI = SUBJ_ALT_NAME_SUPPORT

# Map from urllib3 to PyOpenSSL compatible parameter-values.
_openssl_versions = {
    ssl.PROTOCOL_SSLv23: OpenSSL.SSL.SSLv23_METHOD,
    ssl.PROTOCOL_TLSv1: OpenSSL.SSL.TLSv1_METHOD,
}

try:
    _openssl_versions.update({ssl.PROTOCOL_SSLv3: OpenSSL.SSL.SSLv3_METHOD})
except AttributeError:
    pass

_openssl_verify = {
    ssl.CERT_NONE: OpenSSL.SSL.VERIFY_NONE,
    ssl.CERT_OPTIONAL: OpenSSL.SSL.VERIFY_PEER,
    ssl.CERT_REQUIRED: OpenSSL.SSL.VERIFY_PEER
                       + OpenSSL.SSL.VERIFY_FAIL_IF_NO_PEER_CERT,
}

DEFAULT_SSL_CIPHER_LIST = util.ssl_.DEFAULT_CIPHERS


orig_util_HAS_SNI = util.HAS_SNI
orig_connection_ssl_wrap_socket = connection.ssl_wrap_socket
orig_verifiedhttpsconnection_connect = connection.VerifiedHTTPSConnection.connect
cipherlist = (
    # PFS AEAD (only these are secure)
    'EECDH+ECDSA+AESGCM',
    'EECDH+aRSA+AESGCM',
    'ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305',
    'EDH+aRSA+AESGCM',
    # PFS + CBC
    'EECDH+ECDSA+AES',
    'EECDH+aRSA+AES',
    'EDH+aRSA+AES',
    'EDH+aRSA+CAMELLIA',
    'ECDHE-ECDSA-DES-CBC3-SHA:ECDHE-RSA-DES-CBC3-SHA:EDH-RSA-DES-CBC3-SHA',
    # no PFS
    'RSA+AESGCM',
    'RSA+AES',
    'RSA+CAMELLIA',
    'DES-CBC3-SHA',
    # BROKEN RC4
    'ECDHE-ECDSA-RC4-SHA:ECDHE-RSA-RC4-SHA:RC4-SHA:RC4-MD5',
)

def inject_into_urllib3(strong_cipher_suites_first=True):
    'Monkey-patch urllib3 with PyOpenSSL-backed SSL-support.'

    connection.ssl_wrap_socket = ssl_wrap_socket
    util.HAS_SNI = HAS_SNI
    if strong_cipher_suites_first:
        available_ciphers = []
        global cipherlist
        for cipher_suites in cipherlist:
            ctx = OpenSSL.SSL.Context(OpenSSL.SSL.SSLv23_METHOD)
            try:
                ctx.set_cipher_list(cipher_suites)
            except Exception:
                pass
            else:
                available_ciphers.append(cipher_suites)
        if available_ciphers:
            cipherlist = available_ciphers
            connection.VerifiedHTTPSConnection.connect = connect_retry_with_downgrade


def extract_from_urllib3():
    'Undo monkey-patching by :func:`inject_into_urllib3`.'

    connection.ssl_wrap_socket = orig_connection_ssl_wrap_socket
    util.HAS_SNI = orig_util_HAS_SNI
    VerifiedHTTPSConnection.connect = orig_verifiedhttpsconnection_connect


### Note: This is a slightly bug-fixed version of same from ndg-httpsclient.
class SubjectAltName(BaseSubjectAltName):
    '''ASN.1 implementation for subjectAltNames support'''

    # There is no limit to how many SAN certificates a certificate may have,
    #   however this needs to have some limit so we'll set an arbitrarily high
    #   limit.
    sizeSpec = univ.SequenceOf.sizeSpec + \
        constraint.ValueSizeConstraint(1, 1024)


### Note: This is a slightly bug-fixed version of same from ndg-httpsclient.
def get_subj_alt_name(peer_cert):
    # Search through extensions
    dns_name = []
    if not SUBJ_ALT_NAME_SUPPORT:
        return dns_name

    general_names = SubjectAltName()
    for i in range(peer_cert.get_extension_count()):
        ext = peer_cert.get_extension(i)
        ext_name = ext.get_short_name()
        if ext_name != 'subjectAltName':
            continue

        # PyOpenSSL returns extension data in ASN.1 encoded form
        ext_dat = ext.get_data()
        decoded_dat = der_decoder.decode(ext_dat,
                                         asn1Spec=general_names)

        for name in decoded_dat:
            if not isinstance(name, SubjectAltName):
                continue
            for entry in range(len(name)):
                component = name.getComponentByPosition(entry)
                if component.getName() != 'dNSName':
                    continue
                dns_name.append(str(component.getComponent()))

    return dns_name


class WrappedSocket(object):
    '''API-compatibility wrapper for Python OpenSSL's Connection-class.

    Note: _makefile_refs, _drop() and _reuse() are needed for the garbage
    collector of pypy.
    '''

    def __init__(self, connection, socket, suppress_ragged_eofs=True):
        self.connection = connection
        self.socket = socket
        self.suppress_ragged_eofs = suppress_ragged_eofs
        self._makefile_refs = 0

    def fileno(self):
        return self.socket.fileno()

    def makefile(self, mode, bufsize=-1):
        self._makefile_refs += 1
        return _fileobject(self, mode, bufsize, close=True)

    def recv(self, *args, **kwargs):
        try:
            data = self.connection.recv(*args, **kwargs)
        except OpenSSL.SSL.SysCallError as e:
            if self.suppress_ragged_eofs and e.args == (-1, 'Unexpected EOF'):
                return b''
            else:
                raise
        except OpenSSL.SSL.ZeroReturnError as e:
            if self.connection.get_shutdown() == OpenSSL.SSL.RECEIVED_SHUTDOWN:
                return b''
            else:
                raise
        except OpenSSL.SSL.WantReadError:
            rd, wd, ed = select.select(
                [self.socket], [], [], self.socket.gettimeout())
            if not rd:
                raise timeout('The read operation timed out')
            else:
                return self.recv(*args, **kwargs)
        else:
            return data

    def settimeout(self, timeout):
        return self.socket.settimeout(timeout)

    def _send_until_done(self, data):
        while True:
            try:
                return self.connection.send(data)
            except OpenSSL.SSL.WantWriteError:
                _, wlist, _ = select.select([], [self.socket], [],
                                            self.socket.gettimeout())
                if not wlist:
                    raise timeout()
                continue

    def sendall(self, data):
        while len(data):
            sent = self._send_until_done(data)
            data = data[sent:]

    def close(self):
        if self._makefile_refs < 1:
            return self.connection.shutdown()
        else:
            self._makefile_refs -= 1

    def getpeercert(self, binary_form=False):
        x509 = self.connection.get_peer_certificate()

        if not x509:
            return x509

        if binary_form:
            return OpenSSL.crypto.dump_certificate(
                OpenSSL.crypto.FILETYPE_ASN1,
                x509)

        return {
            'subject': (
                (('commonName', x509.get_subject().CN),),
            ),
            'subjectAltName': [
                ('DNS', value)
                for value in get_subj_alt_name(x509)
            ]
        }

    def _reuse(self):
        self._makefile_refs += 1

    def _drop(self):
        if self._makefile_refs < 1:
            self.close()
        else:
            self._makefile_refs -= 1


def _verify_callback(cnx, x509, err_no, err_depth, return_code):
    return err_no == 0


def ssl_wrap_socket(sock, keyfile=None, certfile=None, cert_reqs=None,
                    ca_certs=None, server_hostname=None,
                    ssl_version=None, cipher_suites=None):
    ctx = OpenSSL.SSL.Context(_openssl_versions[ssl_version])
    if certfile:
        keyfile = keyfile or certfile  # Match behaviour of the normal python ssl library
        ctx.use_certificate_file(certfile)
    if keyfile:
        ctx.use_privatekey_file(keyfile)
    if cert_reqs != ssl.CERT_NONE:
        ctx.set_verify(_openssl_verify[cert_reqs], _verify_callback)
    if ca_certs:
        try:
            ctx.load_verify_locations(ca_certs, None)
        except OpenSSL.SSL.Error as e:
            raise ssl.SSLError('bad ca_certs: %r' % ca_certs, e)
    else:
        ctx.set_default_verify_paths()

    # Disable TLS compression to migitate CRIME attack (issue #309)
    OP_NO_COMPRESSION = 0x20000
    ctx.set_options(OP_NO_COMPRESSION)

    # Set list of supported ciphersuites.
    if cipher_suites is not None:
        ctx.set_cipher_list(cipher_suites)
    else:
        ctx.set_cipher_list(DEFAULT_SSL_CIPHER_LIST)

    cnx = OpenSSL.SSL.Connection(ctx, sock)
    if hasattr(OpenSSL.SSL._lib, 'SSL_set_tlsext_host_name'):
        cnx.set_tlsext_host_name(server_hostname)
    cnx.set_connect_state()
    while True:
        try:
            cnx.do_handshake()
        except OpenSSL.SSL.WantReadError:
            rd, _, _ = select.select([sock], [], [], sock.gettimeout())
            if not rd:
                raise timeout('select timed out')
            continue
        except OpenSSL.SSL.Error as e:
            raise ssl.SSLError('bad handshake', e)
        print "negotiated", cnx.get_cipher_name()
        if hasattr(cnx, 'get_pfs_details'):
            print "pfs details:", cnx.get_pfs_details()
        break

    return WrappedSocket(cnx, sock)


import datetime
from ..util.ssl_ import (
    resolve_cert_reqs,
    resolve_ssl_version,
    assert_fingerprint,
)
from ..packages.ssl_match_hostname import match_hostname
def connect_retry_with_downgrade(self):
    is_time_off = datetime.date.today() < connection.RECENT_DATE
    if is_time_off:
        warnings.warn((
            'System time is way off (before {0}). This will probably '
            'lead to SSL verification errors').format(RECENT_DATE),
            SystemTimeWarning
        )

    caught_exception = None
    for cipher in cipherlist:
        # print "trying", cipher
        conn = self._new_conn()

        resolved_cert_reqs = resolve_cert_reqs(self.cert_reqs)
        resolved_ssl_version = resolve_ssl_version(self.ssl_version)

        hostname = self.host
        if getattr(self, '_tunnel_host', None):
            # _tunnel_host was added in Python 2.6.3
            # (See: http://hg.python.org/cpython/rev/0f57b30a152f)

            self.sock = conn
            # Calls self._set_hostport(), so self.host is
            # self._tunnel_host below.
            self._tunnel()
            # Mark this connection as not reusable
            self.auto_open = 0

            # Override the host with the one we're requesting data from.
            hostname = self._tunnel_host

        # Wrap socket using verification with the root certs in
        # trusted_root_certs
        try:
            self.sock = ssl_wrap_socket(conn, self.key_file, self.cert_file,
                                        cert_reqs=resolved_cert_reqs,
                                        ca_certs=self.ca_certs,
                                        server_hostname=hostname,
                                        ssl_version=resolved_ssl_version,
                                        cipher_suites=cipher)
            break
        except ssl.SSLError as e:
            caught_exception = e
    else:
        raise caught_exception

    if self.assert_fingerprint:
        assert_fingerprint(self.sock.getpeercert(binary_form=True),
                           self.assert_fingerprint)
    elif resolved_cert_reqs != ssl.CERT_NONE \
            and self.assert_hostname is not False:
        cert = self.sock.getpeercert()
        if not cert.get('subjectAltName', ()):
            warnings.warn((
                'Certificate has no `subjectAltName`, falling back to check for a `commonName` for now. '
                'This feature is being removed by major browsers and deprecated by RFC 2818. '
                '(See https://github.com/shazow/urllib3/issues/497 for details.)'),
                SecurityWarning
            )
        match_hostname(cert, self.assert_hostname or hostname)

    self.is_verified = (resolved_cert_reqs == ssl.CERT_REQUIRED
                        or self.assert_fingerprint is not None)


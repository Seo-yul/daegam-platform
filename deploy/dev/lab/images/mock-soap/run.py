"""Run spec2openapi's test SOAP server as a standalone process on :18080.

SoapHandler.log_message is silenced upstream, so a subclass restores
request logging (one line per request on stdout) for gate 5 verification.

With MOCK_SOAP_TLS_DIR set (a mounted kubernetes.io/tls Secret: tls.crt,
tls.key), it serves https on :18443 instead. e2e-instance uses that to
call a backend behind a private CA (D88).

/secured-calc is /calc behind a soap:header check (T60): it answers only
when the envelope's Header carries AuthHeader/token equal to
MOCK_SOAP_HEADER_TOKEN, and with a SOAP Fault otherwise.
"""
from __future__ import annotations

import io
import os
import ssl
import sys
from http.server import ThreadingHTTPServer
from xml.etree import ElementTree

from mock_soap_server import CALC_NS, ENV, SoapHandler, _fault

HOST = "0.0.0.0"
PORT = 18080
TLS_PORT = 18443
SECURED_PATH = "/secured-calc"
HEADER_TOKEN = os.environ.get("MOCK_SOAP_HEADER_TOKEN", "e2e-header-token")
NS = {"s": ENV, "c": CALC_NS}
FAULT = _fault("AuthHeader token missing or wrong")


def _header_token(body: bytes) -> str | None:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None
    token = root.find("s:Header/c:AuthHeader/c:token", NS)
    return token.text if token is not None else None


class LoggingSoapHandler(SoapHandler):
    def log_message(self, fmt, *args):
        # BaseHTTPRequestHandler.log_request -> '"POST /calc HTTP/1.1" 200 -'
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def do_POST(self):
        if self.path != SECURED_PATH:
            return super().do_POST()
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if _header_token(body) != HEADER_TOKEN:
            self.send_response(500)
            self.send_header("Content-Type", "text/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(FAULT)))
            self.end_headers()
            self.wfile.write(FAULT)
            return None
        # the upstream handler reads the body itself and routes on the path
        self.rfile = io.BytesIO(body)
        self.path = "/calc"
        return super().do_POST()


def main() -> None:
    tls_dir = os.environ.get("MOCK_SOAP_TLS_DIR")
    port = TLS_PORT if tls_dir else PORT
    server = ThreadingHTTPServer((HOST, port), LoggingSoapHandler)
    if tls_dir:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(f"{tls_dir}/tls.crt", f"{tls_dir}/tls.key")
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    scheme = "https" if tls_dir else "http"
    print(f"mock-soap listening on {scheme}://{HOST}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())

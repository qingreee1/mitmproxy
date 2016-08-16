from __future__ import absolute_import, print_function, division

import socket
import struct

from OpenSSL import SSL

from mitmproxy import exceptions
from mitmproxy import models
from mitmproxy.protocol import base

import netlib.exceptions
from netlib import tcp
from netlib import http
from netlib import websockets


class WebSocketsLayer(base.Layer):
    """
        WebSockets layer to intercept, modify, and forward WebSockets connections

        Only version 13 is supported (as specified in RFC6455)
        Only HTTP/1.1-initiated connections are supported.

        The client starts by sending an Upgrade-request.
        In order to determine the handshake and negotiate the correct protocol
        and extensions, the Upgrade-request is forwarded to the server.
        The response from the server is then parsed and negotiated settings are extracted.
        Finally the handshake is completed by forwarding the server-response to the client.
        After that, only WebSockets frames are exchanged.

        PING/PONG frames pass through and must be answered by the other endpoint.

        CLOSE frames are forwarded before this WebSocketsLayer terminates.

        This layer is transparent to any negotiated extensions.
        This layer is transparent to any negotiated subprotocols.
        Only raw frames are forwarded to the other endpoint.
    """

    def __init__(self, ctx, request):
        super(WebSocketsLayer, self).__init__(ctx)
        self._request = request

        self.client_key = websockets.get_client_key(self._request.headers)
        self.client_protocol = websockets.get_protocol(self._request.headers)
        self.client_extensions = websockets.get_extensions(self._request.headers)

        self.server_accept = None
        self.server_protocol = None
        self.server_extensions = None

    def _initiate_server_conn(self):
        self.establish_server_connection(
            self._request.host,
            self._request.port,
            self._request.scheme,
        )

        self.server_conn.send(netlib.http.http1.assemble_request(self._request))
        response = netlib.http.http1.read_response(self.server_conn.rfile, self._request, body_size_limit=None)

        if not websockets.check_handshake(response.headers):
            raise exceptions.ProtocolException("Establishing WebSockets connection with server failed: {}".format(response.headers))

        self.server_accept = websockets.get_server_accept(response.headers)
        self.server_protocol = websockets.get_protocol(response.headers)
        self.server_extensions = websockets.get_extensions(response.headers)

    def _complete_handshake(self):
        headers = websockets.server_handshake_headers(self.client_key, self.server_protocol, self.server_extensions)
        self.send_response(models.HTTPResponse(
            self._request.http_version,
            101,
            http.status_codes.RESPONSES.get(101),
            headers,
            b"",
        ))

    def _handle_frame(self, frame, source_conn, other_conn, is_server):
        self.log(
            "WebSockets Frame received from {}".format("server" if is_server else "client"),
            "debug",
            [repr(frame)]
        )

        if frame.header.opcode & 0x8 == 0:
            # forward the data frame to the other side
            other_conn.send(bytes(frame))
            self.log("WebSockets frame received by {}: {}".format(is_server, frame), "debug")
        elif frame.header.opcode in (websockets.OPCODE.PING, websockets.OPCODE.PONG):
            # just forward the ping/pong to the other side
            other_conn.send(bytes(frame))
        elif frame.header.opcode == websockets.OPCODE.CLOSE:
            other_conn.send(bytes(frame))

            code = '(status code missing)'
            msg = None
            reason = '(message missing)'
            if len(frame.payload) >= 2:
                code, = struct.unpack('!H', frame.payload[:2])
                msg = websockets.CLOSE_REASON.get_name(code, default='unknown status code')
            if len(frame.payload) > 2:
                reason = frame.payload[2:]
            self.log("WebSockets connection closed: {} {}, {}".format(code, msg, reason), "info")

            # close the connection
            return False
        else:
            # unknown frame - just forward it
            other_conn.send(bytes(frame))

        # continue the connection
        return True

    def __call__(self):
        self._initiate_server_conn()
        self._complete_handshake()

        client = self.client_conn.connection
        server = self.server_conn.connection
        conns = [client, server]

        try:
            while not self.channel.should_exit.is_set():
                r = tcp.ssl_read_select(conns, 1)
                for conn in r:
                    source_conn = self.client_conn if conn == client else self.server_conn
                    other_conn = self.server_conn if conn == client else self.client_conn
                    is_server = (conn == self.server_conn.connection)

                    frame = websockets.Frame.from_file(source_conn.rfile)

                    if not self._handle_frame(frame, source_conn, other_conn, is_server):
                        return
        except (socket.error, netlib.exceptions.TcpException, SSL.Error) as e:
            self.log("WebSockets connection closed unexpectedly by {}: {}".format(
                "server" if is_server else "client", repr(e)), "info")
        except Exception as e:  # pragma: no cover
            raise exceptions.ProtocolException("Error in WebSockets connection: {}".format(repr(e)))

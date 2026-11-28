"""A fake Samsung Tizen TV for the tests: the REST device description (plain http) and
the remote-control WebSocket (TLS with a self-signed certificate, like the real TV), on
127.0.0.1 with free ports. Never the real TV.

`mode` on a connection without a known token: "accept" (a new token in
`ms.channel.connect`), "refuse" (`ms.channel.unauthorized`), "silent" (no answer).
Records every connection (name, token), key and launch."""

import base64
import contextlib
import datetime
import json
import ssl
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.asyncio.server import serve

DEVICE_NAME = "85&quot; Crystal UHD"
FAKE_MAC = "02:00:5e:00:00:01"  # a locally administered address: no real device


def _server_ssl() -> ssl.SSLContext:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SmartViewSDK")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .sign(key, hashes.SHA256()))
    d = Path(tempfile.mkdtemp())
    (d / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(d / "cert.pem", d / "key.pem")
    return ctx


class FakeSamsungTv:
    def __init__(self, *, mode: str = "accept", power: str = "on",
                 power_key_works: bool = True, launch_replies: dict | None = None,
                 samsung: bool = True) -> None:
        self.mode, self.power, self.power_key_works = mode, power, power_key_works
        # appId -> reply code (None: no reply); unknown ids answer 404
        self.launch_replies = ({"org.tizen.browser": 200} if launch_replies is None
                               else launch_replies)
        self.samsung = samsung
        self.tokens: set[str] = set()
        self.issued = 0
        self.connections: list[dict] = []
        self.keys: list[str] = []
        self.launches: list[dict] = []
        self.rest_port = self.ws_port = 0
        self._servers = []

    # -- REST --------------------------------------------------------------------------

    def device_json(self) -> dict:
        if not self.samsung:
            return {"type": "Something else", "name": "router"}
        return {"device": {"PowerState": self.power, "TokenAuthSupport": "true",
                           "modelName": "UE85DU7190UXZT", "model": "24_KSUE_UB_T09",
                           "name": DEVICE_NAME, "resolution": "3840x2160",
                           "type": "Samsung SmartTV", "wifiMac": FAKE_MAC,
                           "ip": "127.0.0.1"},
                "name": DEVICE_NAME, "type": "Samsung SmartTV", "version": "2.0.25"}

    def _rest(self, connection, request):
        if request.path.startswith("/api/v2"):
            return connection.respond(200, json.dumps(self.device_json()))
        return connection.respond(404, "not found")

    async def _rest_handler(self, ws):  # never reached: every request is answered above
        await ws.close()

    # -- WebSocket ---------------------------------------------------------------------

    async def _ws(self, ws):
        query = parse_qs(urlsplit(ws.request.path).query)
        name = base64.b64decode(query.get("name", [""])[0]).decode()
        token = query.get("token", [None])[0]
        self.connections.append({"name": name, "token": token,
                                 "path": urlsplit(ws.request.path).path})
        if token and token in self.tokens:
            await ws.send(json.dumps({"event": "ms.channel.connect",
                                      "data": {"token": token, "id": "x"}}))
        elif self.mode == "accept":
            self.issued += 1
            new = f"fake-tv-token-{self.issued:04d}-abcdef"
            self.tokens.add(new)
            await ws.send(json.dumps({"event": "ms.channel.connect",
                                      "data": {"token": new, "id": "x"}}))
        elif self.mode == "refuse":
            await ws.send(json.dumps({"event": "ms.channel.unauthorized"}))
            await ws.close()
            return
        else:  # silent: the prompt waits on the TV
            with contextlib.suppress(Exception):
                async for _ in ws:
                    pass
            return
        try:
            async for raw in ws:
                msg = json.loads(raw)
                params = msg.get("params") or {}
                if msg.get("method") == "ms.remote.control":
                    key = params.get("DataOfCmd")
                    self.keys.append(key)
                    if key == "KEY_POWER" and self.power_key_works:
                        self.power = "standby" if self.power == "on" else "on"
                elif params.get("event") == "ed.apps.launch":
                    data = params.get("data") or {}
                    self.launches.append({"appId": data.get("appId"),
                                          "metaTag": data.get("metaTag"),
                                          "action_type": data.get("action_type")})
                    code = self.launch_replies.get(data.get("appId"), 404)
                    if code is not None:
                        await ws.send(json.dumps({"event": "ed.apps.launch", "data": code,
                                                  "from": "host"}))
        except Exception:  # noqa: BLE001, S110  (the client closed)
            pass

    async def start(self) -> "FakeSamsungTv":
        rest = await serve(self._rest_handler, "127.0.0.1", 0, process_request=self._rest)
        wss = await serve(self._ws, "127.0.0.1", 0, ssl=_server_ssl())
        self._servers = [rest, wss]
        self.rest_port = rest.sockets[0].getsockname()[1]
        self.ws_port = wss.sockets[0].getsockname()[1]
        return self

    def point(self, monkeypatch) -> None:
        """Point the TV client's ports at this fake."""
        from smtrading.tv import samsung

        monkeypatch.setattr(samsung, "REST_PORT", self.rest_port)
        monkeypatch.setattr(samsung, "WS_PORT", self.ws_port)

    async def stop(self) -> None:
        for s in self._servers:
            s.close()
            await s.wait_closed()

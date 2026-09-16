from .pipe_hub import CfquantPipeHub
from .protocol import loads_message
from .policy import validate


class MarketHub(CfquantPipeHub):
    def _handle_api_request(self, conn, envelope):
        message = loads_message(envelope.get("payload")) or {}
        try:
            validate(message.get("action"), message.get("params"))
        except ValueError as error:
            client_id = envelope.get("client_id") or message.get("client_id")
            self._send_error(self._client_rx_conn(client_id) or conn, message.get("id"), str(error))
            return
        return super(MarketHub, self)._handle_api_request(conn, envelope)

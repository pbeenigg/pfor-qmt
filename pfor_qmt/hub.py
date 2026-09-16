from .pipe_hub import CfquantPipeHub
from .protocol import loads_message
from .policy import validate
import time


class MarketHub(CfquantPipeHub):
    def _handle_api_request(self, conn, envelope):
        message = loads_message(envelope.get("payload")) or {}
        try:
            validate(message.get("action"), message.get("params"))
            timeout = min(600, max(1, float(message.get('timeout') or self.pending_timeout_seconds)))
        except ValueError as error:
            client_id = envelope.get("client_id") or message.get("client_id")
            self._send_error(self._client_rx_conn(client_id) or conn, message.get("id"), str(error))
            return
        result = super(MarketHub, self)._handle_api_request(conn, envelope)
        with self.state_lock:
            pending = self.pending.get(message.get('id'))
            if pending is not None:
                pending['timeout_seconds'] = timeout
        return result

    def _cleanup_expired_pending(self):
        now = time.perf_counter()
        expired = []
        with self.state_lock:
            for request_id, pending in list(self.pending.items()):
                timeout = pending.get('timeout_seconds', self.pending_timeout_seconds)
                if timeout <= 0 or now - pending.get('api_received_at', now) < timeout:
                    continue
                self.pending.pop(request_id, None)
                expired.append((request_id, pending))
        for request_id, pending in expired:
            self._send_error(pending.get('conn'), request_id, 'QMT pipe bridge response timeout for action=%s' % pending.get('action'))
        return len(expired)

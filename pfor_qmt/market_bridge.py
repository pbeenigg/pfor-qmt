"""Market-only QMT dispatcher. This module must remain Python 3.6 compatible."""
import inspect
import queue
import threading
import uuid

from .config import get_config
from .pipe_transport import PipeTxClient
from .policy import ACTIONS, validate
from .protocol import loads_message, pack_response, pack_event
from .qmt_methods import QmtMethods
from .quote import quote_plain, quote_callback_data


class SignatureUnavailable(NotImplementedError):
    pass


class MarketBridge(QmtMethods):
    def __init__(self, context, globals_dict=None, tx=None):
        self.context = context
        self.globals_dict = globals_dict or {}
        cfg = get_config()
        self.tx = tx or PipeTxClient(pipe_name=cfg['pipe_name'], request_channel=cfg['request_channel'],
                                     bridge_id='pfor-market', endpoint_name='PFOR_MARKET', show=False)
        self.subscriptions = {}
        self.lock = threading.RLock()
        self.generation = 0
        self.instance = uuid.uuid4().hex

    def start(self):
        self.tx.start()
        return self

    def _log(self, message):
        print('[pfor-qmt] ' + str(message))

    def _call_variants(self, func, variants):
        # Bind before calling so a TypeError inside QMT is never mistaken for a signature mismatch.
        try:
            signature = inspect.signature(func)
        except (ValueError, TypeError):
            args, kwargs = variants[0]
            return func(*args, **kwargs)
        for args, kwargs in variants:
            try:
                signature.bind(*args, **kwargs)
            except TypeError:
                continue
            return func(*args, **kwargs)
        raise SignatureUnavailable('Unsupported QMT callable signature')

    def _instrument_detail_callable_missing(self, error):
        return isinstance(error, (NotImplementedError, AttributeError))

    def _fallback_instrument_detail(self, params, code):
        raise NotImplementedError('QMT instrument details unavailable')

    def dispatch(self, action, params=None, message=None):
        p = validate(action, params)
        message = message or {}
        if action in ('pfor.ping', 'pfor.status'):
            return {'source': 'qmt', 'mode': 'market-only', 'actions': sorted(ACTIONS),
                    'instance': self.instance, 'generation': self.generation}
        method = action.split('.', 1)[1]
        if method in ('get_market_data_ex', 'get_local_data', 'get_instrument_detail', 'get_stock_list_in_sector'):
            return getattr(self, '_' + method)(p)
        if method == 'get_sector_list':
            return self._get_sector_list()
        if method == 'get_full_tick':
            return self._require_qmt_callable(method)(p.get('code_list', []))
        if method == 'get_trading_dates':
            return self._require_qmt_callable(method)(p.get('stockcode', '000001.SH'), p.get('start_date', ''),
                p.get('end_date', ''), p.get('count', -1), p.get('period', '1d'))
        if method == 'get_divid_factors':
            return self._require_qmt_callable(method)(p['stock_code'])
        if method.startswith('download_history_data'):
            return self._download(method, p, message)
        if method == 'unsubscribe_quote':
            return self._unsubscribe(p['subscribe_id'], message.get('client_id'))
        return self._subscribe(method, p, message)

    def _event(self, message, name, data, subscription_id=None):
        client = message.get('client_id') or message.get('reply_channel')
        if client and name:
            return self.tx.push('event', pack_event(name, data, client, subscription_id), client)

    def _download(self, method, p, message):
        args = (p.get('period', '1d'), p.get('start_time', ''), p.get('end_time', ''))
        if method == 'download_history_data2':
            func = self._get_callable('download_history_data2', 'down_history_data2')
            if func:
                callback = lambda data: self._event(message, p.get('callback_event'), data)
                base = (p['stock_list'],) + args
                variants = [(base + (callback, p['incrementally']), {})] if p.get('incrementally') is not None else []
                variants += [(base + (callback,), {}), (base, {})]
                try:
                    return self._call_variants(func, variants)
                except SignatureUnavailable:
                    pass
            return {code: self._download('download_history_data', dict(p, stock_code=code), message)
                    for code in p['stock_list']}
        func = self._require_qmt_callable('download_history_data', 'down_history_data')
        base = (p['stock_code'],) + args
        variants = [(base + (p['incrementally'],), {})] if p.get('incrementally') is not None else []
        return self._call_variants(func, variants + [(base, {})])

    def _subscribe(self, method, p, message):
        func = self._require_qmt_callable(method)
        seq = uuid.uuid4().hex
        sub = {'client_id': message.get('client_id'), 'native': None}
        pending = []
        def callback(data):
            with self.lock:
                if self.subscriptions.get(seq) is not sub:
                    return
                payload = quote_plain(data) if method == 'subscribe_whole_quote' else quote_callback_data(data)
                if sub['native'] is None:
                    pending.append(payload)
                else:
                    self._event(message, p.get('callback_event'), payload, seq)
        with self.lock:
            self.subscriptions[seq] = sub
        try:
            if method == 'subscribe_whole_quote':
                native = self._call_variants(func, [((p['code_list'],), {'callback': callback}), ((p['code_list'], callback), {})])
            else:
                if p.get('start_time') or p.get('end_time') or p.get('count', 0):
                    self._get_market_data_ex(dict(p, stock_list=[p['stock_code']]))
                native = func(p['stock_code'], p.get('period', '1d'), 'none', 'dict', callback)
            if native is None or isinstance(native, bool) or int(native) <= 0:
                raise RuntimeError('QMT subscription rejected')
            with self.lock:
                sub['native'] = native
                for payload in pending:
                    self._event(message, p.get('callback_event'), payload, seq)
        except Exception:
            self.subscriptions.pop(seq, None)
            raise
        return {'subscribe_id': seq, 'internal_subscribe_id': native, 'callback_event': p.get('callback_event')}

    def _unsubscribe(self, seq, client=None):
        sub = self.subscriptions.get(seq)
        if not sub:
            return True
        if client and sub['client_id'] != client:
            raise ValueError('Subscription belongs to another client')
        result = self._require_qmt_callable('unsubscribe_quote')(sub['native'])
        if result is False or isinstance(result, (int, float)) and result < 0:
            raise RuntimeError('QMT unsubscribe failed')
        self.subscriptions.pop(seq, None)
        return True

    def pump(self, max_count=20):
        generation = getattr(self.tx, 'connection_generation', 0)
        if generation != self.generation:
            if self.generation:
                for seq in list(self.subscriptions):
                    self._unsubscribe(seq)
            self.generation = generation
        for _ in range(max_count):
            try:
                raw = self.tx.Q.get_nowait()
            except queue.Empty:
                return
            msg = loads_message(raw)
            if not msg or msg.get('type') != 'request':
                continue
            try:
                result = self.dispatch(msg.get('action'), msg.get('params'), msg)
                response = pack_response(msg['id'], result=result)
            except Exception as error:
                response = pack_response(msg['id'], ok=False, error=error)
            self.tx.push('response', response, msg.get('client_id') or msg.get('reply_channel'))

    def close(self):
        for seq in list(self.subscriptions):
            try:
                self._unsubscribe(seq)
            except Exception as error:
                self._log('Subscription cleanup failed: ' + str(error))
        self.tx.close()

import hashlib
import hmac
import json
import mimetypes
import queue
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psycopg
import web_dashboard

from .client import CfquantError
from .data import codes, json_default
from .protocol import encode_value


class HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def handler_for(app):
    static = Path(web_dashboard.__file__).parent

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_):
            pass

        def send(self, code, body, content_type='application/json; charset=utf-8', headers=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False, default=json_default, allow_nan=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; frame-ancestors 'none'; base-uri 'none'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def authenticated(self):
            auth = self.headers.get('Authorization', '')
            if auth.startswith('Bearer ') and hmac.compare_digest(auth[7:].encode('utf-8'), app.settings.api_key.encode('utf-8')):
                return True
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
            except Exception:
                return False
            token = cookie.get('pfor_session')
            return bool(token and app.sessions.get(token.value, 0) > time.time())

        def do_GET(self):
            self.handle_request('GET')

        def do_POST(self):
            self.handle_request('POST')

        def handle_request(self, method):
            try:
                self.connection.settimeout(15)
                host = self.headers.get('Host', '')
                expected = {'127.0.0.1:' + str(self.server.server_port), 'localhost:' + str(self.server.server_port)}
                if host not in expected:
                    return self.send(403, {'error': '仅允许本机访问'})
                origin = self.headers.get('Origin')
                if origin and origin not in {'http://' + item for item in expected}:
                    return self.send(403, {'error': '请求来源无效'})
                url = urlsplit(self.path)
                if not url.path.startswith('/api/v1/'):
                    if method != 'GET':
                        return self.send(405, {'error': 'Method not allowed'})
                    target = (static / ('index.html' if url.path == '/' else url.path.lstrip('/'))).resolve()
                    if not target.is_relative_to(static.resolve()) or not target.is_file() or target.suffix not in ('.html','.css','.js','.png','.svg','.ico'):
                        return self.send(404, {'error': '文件不存在'})
                    return self.send(200, target.read_bytes(), mimetypes.guess_type(target)[0] or 'application/octet-stream')
                path = url.path[len('/api/v1'):]
                p = {key: values[-1] for key, values in parse_qs(url.query).items()}
                if method == 'POST':
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        return self.send(415, {'error': '需要 application/json'})
                    size = int(self.headers.get('Content-Length', 0))
                    if not 0 <= size <= 1024 * 1024:
                        return self.send(413, {'error': '请求过大'})
                    p = json.loads(self.rfile.read(size) or b'{}')
                    if not isinstance(p, dict):
                        raise ValueError('请求必须是 JSON 对象')
                if method == 'POST' and path == '/login':
                    now = time.monotonic()
                    attempts = getattr(self.server, 'login_attempts', [])
                    attempts = [stamp for stamp in attempts if now - stamp < 60]
                    self.server.login_attempts = attempts
                    if len(attempts) >= 10:
                        return self.send(429, {'error': '尝试过于频繁，请一分钟后重试'})
                    attempts.append(now)
                    credential = str(p.get('credential',''))
                    if not hmac.compare_digest(credential.encode('utf-8'), app.settings.api_key.encode('utf-8')) and not app.settings.check_password(credential):
                        return self.send(401, {'error': 'API Key 或密码不正确'})
                    token = secrets.token_urlsafe(32)
                    app.sessions = {key: until for key, until in app.sessions.items() if until > time.time()}
                    app.sessions[token] = time.time() + 8 * 3600
                    return self.send(200, {'ok': True}, headers={'Set-Cookie': 'pfor_session=' + token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'})
                if not self.authenticated():
                    return self.send(401, {'error': '请先登录'})
                if method == 'POST' and path == '/logout':
                    cookie = SimpleCookie(self.headers.get('Cookie',''))
                    if cookie.get('pfor_session'):
                        app.sessions.pop(cookie['pfor_session'].value, None)
                    return self.send(200, {'ok': True}, headers={'Set-Cookie': 'pfor_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
                if method == 'GET' and path.startswith('/files/'):
                    parts = path.strip('/').split('/')
                    if len(parts) not in (2,3):
                        raise ValueError('无效文件路径')
                    job = app.store.job(parts[1])
                    if job['kind'] != 'export' or job['state'] not in ('completed','succeeded'):
                        raise ValueError('导出尚未完成')
                    filename = job['result']['metadata' if len(parts) == 3 and parts[2] == 'metadata' else 'file']
                    target = app.settings.runtime / 'exports' / filename
                    if target.parent != app.settings.runtime / 'exports':
                        raise ValueError('无效文件路径')
                    # Stream files so large exports do not duplicate into HTTP memory.
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Length', str(target.stat().st_size))
                    self.send_header('Content-Disposition', 'attachment; filename="' + target.name + '"')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    with target.open('rb') as stream:
                        while chunk := stream.read(1024 * 1024):
                            self.wfile.write(chunk)
                    return
                return self.send(200, app.dispatch(method, path, p))
            except LookupError:
                return self.send(404, {'error': '接口或记录不存在'})
            except (ValueError, KeyError, TypeError) as error:
                return self.send(400, {'error': str(error)[:500]})
            except psycopg.Error:
                return self.send(503, {'error': '数据库操作失败，请检查连接、迁移状态和数据约束'})
            except (CfquantError, ConnectionError, TimeoutError, OSError):
                return self.send(503, {'error': '行情源未连接或操作失败，请检查 QMT 与本地服务'})
            except Exception:
                return self.send(500, {'error': '操作失败，请查看任务状态并检查配置'})
    return Handler


def start_websocket(app, port, http_port):
    from websockets.sync.server import serve
    from websockets.exceptions import ConnectionClosed
    app.ws_port = port

    def handle(socket):
        query = parse_qs(urlsplit(socket.request.path).query)
        ticket = query.get('ticket', [''])[0]
        with app.lock:
            expires = app.tickets.pop(ticket, 0)
        if expires < time.time():
            socket.close(1008, 'Invalid ticket')
            return
        listener = queue.Queue(maxsize=200)
        subscription = None
        watch = []
        last_attempt = 0
        source_identity = None
        watch_generation = 0
        with app.lock:
            app.listeners.add(listener)
        def callback_for(generation):
            def callback(data):
                if generation != watch_generation:
                    return
                try:
                    listener.put_nowait({'event':'quote','data':encode_value(data),'_generation':generation})
                except queue.Full:
                    pass
            return callback

        def release_subscription():
            if subscription is not None:
                try:
                    result = app.source.unsubscribe_quote(subscription)
                    if result is False or isinstance(result, (int, float)) and result < 0:
                        raise RuntimeError('Unsubscribe rejected')
                except Exception as error:
                    raise ValueError('退订失败，原订阅仍保留，请重试') from error
        try:
            while True:
                try:
                    raw = socket.recv(timeout=0.25)
                    command = json.loads(raw)
                    if not isinstance(command, dict) or command.get('action') not in ('watch', 'unwatch'):
                        raise ValueError('无效的订阅请求')
                    if command['action'] == 'watch':
                        if command.get('source','qmt') != 'qmt':
                            raise ValueError('Tushare本期不提供实时订阅')
                        selected = codes(command.get('codes', []))
                        if len(selected) > 100:
                            raise ValueError('网页最多订阅 100 个证券')
                        release_subscription()
                        watch_generation += 1
                        watch, subscription, last_attempt = selected, None, 0
                    else:
                        release_subscription()
                        watch_generation += 1
                        watch, subscription = [], None
                        socket.send(json.dumps({'event':'watch','codes':[]}))
                except TimeoutError:
                    pass
                except (ValueError, KeyError, TypeError) as error:
                    message = str(error) if isinstance(error, ValueError) else '无效的订阅请求'
                    socket.send(json.dumps({'event':'error','message':message,'codes':watch}))
                if watch and subscription is None and time.monotonic() - last_attempt > 5:
                    last_attempt = time.monotonic()
                    try:
                        from .client import get_client
                        status = get_client().request('pfor.ping', timeout=3)
                        source_identity = (status.get('instance'), status.get('generation'))
                        watch_generation += 1
                        subscription = app.source.subscribe_whole_quote(watch, callback_for(watch_generation))
                        socket.send(json.dumps({'event':'source','connected':True}))
                        socket.send(json.dumps({'event':'watch','codes':watch}))
                    except Exception:
                        watch_generation += 1
                        socket.send(json.dumps({'event':'source','connected':False}))
                if subscription and time.monotonic() - last_attempt > 15:
                    last_attempt = time.monotonic()
                    try:
                        from .client import get_client
                        status = get_client().request('pfor.ping', timeout=3)
                        if source_identity != (status.get('instance'), status.get('generation')):
                            raise ConnectionError('QMT bridge reconnected')
                    except Exception:
                        watch_generation += 1
                        try:
                            release_subscription()
                        except Exception:
                            pass
                        subscription = None
                        socket.send(json.dumps({'event':'source','connected':False}))
                for _ in range(30):
                    try:
                        event = listener.get_nowait()
                        generation = event.pop('_generation', None)
                        if event['event'] == 'quote' and generation != watch_generation:
                            continue
                        socket.send(json.dumps(event, default=json_default, ensure_ascii=False))
                    except queue.Empty:
                        break
        except (ConnectionClosed, OSError):
            pass
        finally:
            watch_generation += 1
            with app.lock:
                app.listeners.discard(listener)
            if subscription:
                try:
                    app.source.unsubscribe_quote(subscription)
                except Exception:
                    pass
    server = serve(handle, '127.0.0.1', port, origins=[None, 'http://127.0.0.1:' + str(http_port), 'http://localhost:' + str(http_port)], max_size=65536)
    threading.Thread(target=server.serve_forever, daemon=True, name='pfor-websocket').start()
    return server

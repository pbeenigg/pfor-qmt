import argparse
import json
import os
import threading


def main():
    parser = argparse.ArgumentParser(prog='pfor-qmt')
    parser.add_argument('--config', help='本地 TOML 配置路径，默认 ./config.toml')
    parser.add_argument('--runtime', help='临时覆盖运行目录')
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--host')
    serve.add_argument('--port', type=int)
    serve.add_argument('--ws-port', type=int)
    commands.add_parser('key')
    commands.add_parser('migrate')
    configure = commands.add_parser('configure')
    configure.add_argument('--qmt-root')
    configure.add_argument('--database', action='store_true')
    deploy = commands.add_parser('deploy')
    deploy.add_argument('action', choices=['inspect','prepare','activate'])
    deploy.add_argument('--qmt-root')
    deploy.add_argument('--account', default='')
    args = parser.parse_args()
    from .settings import Settings
    settings = Settings(args.runtime, args.config, getattr(args,'port',None), getattr(args,'ws_port',None), getattr(args,'host',None))
    if args.command == 'key':
        print(settings.api_key)
        return
    if args.command == 'configure':
        if args.qmt_root:
            settings.data['qmt_root'] = args.qmt_root
        if args.database:
            from getpass import getpass
            settings.data['dsn'] = getpass('PostgreSQL DSN (hidden): ')
        settings.save()
        print('本地配置已保存')
        return
    if args.command == 'migrate':
        from .storage import Store
        Store(settings.dsn).migrate()
        print('pfor_qmt schema 已初始化')
        return
    if args.command == 'deploy':
        from . import deploy
        function = getattr(deploy, 'inspect_root' if args.action == 'inspect' else args.action)
        root = args.qmt_root or settings.qmt_root
        result = function(root, args.account) if args.action == 'activate' else function(root, pipe_config=settings.pipe) if args.action == 'prepare' else function(root)
        print(json.dumps(result, ensure_ascii=False))
        return
    from .service import Application
    from .server import HTTPServer, handler_for, start_websocket
    from .hub import MarketHub
    from .instance import ServiceInstance
    instance = ServiceInstance()
    os.environ['PFOR_QMT_RUNTIME_DIR'] = str(settings.runtime)
    from .client import configure as configure_pipe
    configure_pipe(**settings.pipe)
    app = Application(settings)
    host = settings.value('host')
    port, ws_port = settings.value('port'), settings.value('ws_port')
    server = HTTPServer((host, port), handler_for(app))
    websocket = start_websocket(app, ws_port, port, host)
    hub = MarketHub(pipe_name=settings.pipe['pipe_name'], default_request_channel=settings.pipe['request_channel'], show=False)
    hub.pending_timeout_seconds = settings.value('pending_timeout')
    hub.qmt_heartbeat_timeout_seconds = settings.value('heartbeat_timeout')
    hub.maintenance_interval_seconds = settings.value('maintenance_interval')
    threading.Thread(target=hub.start, daemon=True, name='pfor-pipe-hub').start()
    app.worker.start()
    app.tushare_worker.start()
    print('pfor-qmt: http://%s:%s (WebSocket %s)' % (host, port, ws_port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.worker.stop.set()
        app.tushare_worker.stop.set()
        websocket.shutdown()
        server.server_close()
        hub.close()
        instance.close()


if __name__ == '__main__':
    main()

import argparse
import json
import os
import threading


def main():
    parser = argparse.ArgumentParser(prog='pfor-qmt')
    parser.add_argument('--runtime', default='runtime')
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--port', type=int, default=8766)
    serve.add_argument('--ws-port', type=int, default=8767)
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
    settings = Settings(args.runtime)
    if args.command == 'key':
        print(settings.data['api_key'])
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
        root = args.qmt_root or settings.data['qmt_root']
        print(json.dumps(function(root, args.account) if args.action == 'activate' else function(root), ensure_ascii=False))
        return
    from .service import Application
    from .server import HTTPServer, handler_for, start_websocket
    from .hub import MarketHub
    from .instance import ServiceInstance
    instance = ServiceInstance()
    os.environ['PFOR_QMT_RUNTIME_DIR'] = str(settings.runtime)
    app = Application(settings)
    server = HTTPServer(('127.0.0.1', args.port), handler_for(app))
    websocket = start_websocket(app, args.ws_port, args.port)
    hub = MarketHub(show=False)
    threading.Thread(target=hub.start, daemon=True, name='pfor-pipe-hub').start()
    app.worker.start()
    print('pfor-qmt: http://127.0.0.1:%s (WebSocket %s)' % (args.port, args.ws_port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.worker.stop.set()
        websocket.shutdown()
        server.server_close()
        hub.close()
        instance.close()


if __name__ == '__main__':
    main()

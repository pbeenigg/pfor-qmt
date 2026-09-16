# coding: gbk
import datetime as dt
import os
import sys

# The deployer replaces this value with the isolated embedded runtime directory.
PFOR_RUNTIME = ''
if PFOR_RUNTIME:
    sys.path.insert(0, PFOR_RUNTIME)

from pfor_qmt.market_bridge import MarketBridge

_bridge = None
_timer = None
_scheduled = False


def _pump(*args, **kwargs):
    if _bridge:
        _bridge.pump()


def init(ContextInfo):
    global _bridge
    if _bridge is None:
        _bridge = MarketBridge(ContextInfo, globals()).start()
    _bridge.context = ContextInfo
    _schedule(ContextInfo)


def _schedule(ContextInfo):
    global _timer, _scheduled
    if _scheduled:
        return
    try:
        _timer = ContextInfo.schedule_run(_pump, dt.datetime.now() + dt.timedelta(seconds=1),
                                         repeat_times=-1, interval=dt.timedelta(milliseconds=500),
                                         name='pfor_market_pump')
        _scheduled = True
    except Exception as error:
        print('[pfor-qmt] timer unavailable: ' + str(error))


def after_init(ContextInfo):
    init(ContextInfo)


def handlebar(ContextInfo):
    if _bridge:
        _bridge.context = ContextInfo
    _pump()


def stop(ContextInfo):
    global _bridge, _timer, _scheduled
    if _scheduled and hasattr(ContextInfo, 'cancel_schedule_run'):
        ContextInfo.cancel_schedule_run(_timer if _timer is not None else 'pfor_market_pump')
    if _bridge:
        _bridge.close()
    _bridge, _timer, _scheduled = None, None, False

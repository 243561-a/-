#!/usr/bin/env python
# coding: utf-8
"""
Go 采集服务适配器：把 douyinLive 的 WebSocket 消息转成我们的统一事件格式

为什么换这条链路：
  自建栈（Python 直连）在登录态下依然收不到礼物消息 —— 上游把 user_unique_id
  和 first_req_ms 写死成 2024 年的值，服务端不认这个登录态。
  douyinLive v2.3.2 原生支持 Cookie，连接参数每次实时生成，实测能收到
  WebcastGiftMessage。

输出：一行一条 JSON，与 collector.py 完全一致，下游规则引擎无需改动。

用法：
    python go_collector.py <直播间号> [秒数] [--dump]
"""

import json
import os
import sys
import time

import websocket

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

HOST = os.environ.get('ROBOTLIVE_GO_HOST', '127.0.0.1:1088')

# 调试开关放在 gifts.json，和话术表同一个文件，GUI 保存时不会丢
DEBUG_FULL = False


def _load_debug_flag():
    global DEBUG_FULL
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gifts.json'),
                  encoding='utf-8') as f:
            DEBUG_FULL = bool(json.load(f).get('debug_gift_full_log', False))
    except Exception:
        DEBUG_FULL = False


def log(*a):
    """诊断信息一律走 stderr，stdout 只留 NDJSON 给下游"""
    sys.stderr.write(' '.join(str(x) for x in a) + '\n')
    sys.stderr.flush()


def gift_kind(name, gift_obj=None):
    """分三类：粉丝团灯牌 / 盲盒 / 普通。用于日志前缀，方便核对规则命中"""
    n = name or ''
    if any(k in n for k in ('灯牌', '粉丝团', '团牌')):
        return '粉丝团'
    if any(k in n for k in ('盲盒', '宝箱', '盒子', '福袋')):
        return '盲盒'
    return '普通'


def log_gift_full(ev, raw=None):
    """礼物全字段结构化日志（debug_gift_full_log 开启时才打）"""
    if not DEBUG_FULL:
        return
    kind = gift_kind(ev.get('gift'))
    price = int(ev.get('diamond') or 0)
    count = max(int(ev.get('repeat') or 1), int(ev.get('count') or 1))
    total = price * count
    combo = int(ev.get('combo') or 0)
    log(f"[礼物·{kind}] 用户:{ev.get('user_name')}({ev.get('user_id')}) | "
        f"礼物:{ev.get('gift')}(ID:{ev.get('gift_id')}) | "
        f"数量:{count} | 单价值:{price}抖币 | 总价值:{total}抖币 | "
        f"连击:{'是' if (combo or count > 1) else '否'}(连击数{combo or count}) | "
        f"msg:{ev.get('msg_id')} | 到达:{time.strftime('%H:%M:%S')}")


def emit(ev):
    sys.stdout.write(json.dumps(ev, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def _ts():
    return int(time.time() * 1000)


def _get(d, *names, default=''):
    for n in names:
        if isinstance(d, dict) and d.get(n) not in (None, ''):
            return d[n]
    return default


def _user(obj):
    u = obj.get('user') or {}
    # 有些消息把用户放在 displayText.pieces[].userValue.user
    if not u:
        for piece in (_get(obj.get('common', {}), 'displayText', default={}) or {}).get('pieces', []) or []:
            uv = piece.get('userValue') or {}
            if uv.get('user'):
                u = uv['user']
                break
    return u


def _gift_name_from_describe(desc):
    """describe 形如 '太阳☀️:送给主播 1个人气票'，兜底从里面取礼物名"""
    if not desc or '送给主播' not in desc:
        return ''
    tail = desc.split('送给主播', 1)[1].strip()
    # 去掉开头的数字与量词
    i = 0
    while i < len(tail) and (tail[i].isdigit() or tail[i] in '个xX× '):
        i += 1
    return tail[i:].strip()


def normalize(obj):
    """把一条上游消息转成统一事件；不认识的返回 None"""
    method = obj.get('type') or obj.get('method') or ''
    common = obj.get('common') or {}
    msg_id = _get(common, 'msgId', 'msg_id')
    u = _user(obj)
    uid = _get(u, 'id', default='')
    uname = _get(u, 'nickname', 'nickName', 'displayId')

    if method == 'WebcastGiftMessage':
        gift = obj.get('gift') or {}
        name = _get(gift, 'name', 'describe')
        if not name:
            name = _gift_name_from_describe(_get(common, 'describe'))
        diamond = _get(gift, 'diamondCount', 'diamond_count', default=0)
        repeat = obj.get('repeatCount', obj.get('repeat_count', 1))
        combo = obj.get('comboCount', obj.get('combo_count', 0))
        group = obj.get('groupCount', obj.get('group_count', 0))
        return {
            'ts': _ts(), 'type': 'gift',
            'user_id': str(uid), 'user_name': uname,
            'gift': name,
            'gift_id': str(_get(gift, 'id', 'giftId', default='')),
            'diamond': int(diamond or 0),
            'repeat': int(repeat or 1),
            'combo': int(combo or 0),
            'group': int(group or 0),
            'msg_id': str(msg_id or ''),
            'raw_describe': _get(common, 'describe'),
        }

    if method in ('WebcastChatMessage', 'WebcastEmojiChatMessage'):
        text = _get(obj, 'content', 'defaultContent', 'text')
        return {
            'ts': _ts(), 'type': 'chat',
            'user_id': str(uid), 'user_name': uname,
            'text': text, 'msg_id': str(msg_id or ''),
        }

    if method == 'WebcastMemberMessage':
        return {
            'ts': _ts(), 'type': 'enter',
            'user_id': str(uid), 'user_name': uname,
            'msg_id': str(msg_id or ''),
        }

    if method == 'WebcastLikeMessage':
        return {
            'ts': _ts(), 'type': 'like',
            'user_id': str(uid), 'user_name': uname,
            'count': int(_get(obj, 'count', default=1) or 1),
            'total': int(_get(obj, 'total', default=0) or 0),
            'msg_id': str(msg_id or ''),
        }

    if method == 'WebcastSocialMessage':
        return {
            'ts': _ts(), 'type': 'follow',
            'user_id': str(uid), 'user_name': uname,
            'msg_id': str(msg_id or ''),
        }

    if method == 'system':
        return {'ts': _ts(), 'type': 'meta', 'event': obj.get('event'),
                'room': obj.get('room_id'), 'live': obj.get('live')}

    return None


def main():
    pos = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not pos:
        print('用法: python go_collector.py <直播间号> [秒数] [--dump]', file=sys.stderr)
        sys.exit(2)
    room = pos[0]
    seconds = float(pos[1]) if len(pos) > 1 else 0
    dump = '--dump' in sys.argv
    _load_debug_flag()

    url = f'ws://{HOST}/ws/{room}'
    log(f'[自检] 1/3 连接采集服务 {url} ...')
    log(f'[自检] 礼物全字段日志：{"开启" if DEBUG_FULL else "关闭"}')

    st = {'any': False, 'system': None, 'biz': 0, 'gift': 0, 'first_biz': 0}
    t0 = time.time()

    def on_message(ws, message):
        try:
            obj = json.loads(message)
        except Exception:
            return
        st['any'] = True
        if dump:
            log('[go] 原文: ' + message[:300])
        ev = normalize(obj)
        if not ev:
            return
        if ev.get('type') == 'meta':
            st['system'] = ev
        else:
            st['biz'] += 1
            if ev.get('type') == 'gift':
                st['gift'] += 1
                log_gift_full(ev)
        if ev:
            emit(ev)

    def on_error(ws, err):
        log(f'[自检][失败] WebSocket 错误：{err}')
        log('  → 检查 douyinLive 是否在 1088 端口运行；Cookie 是否过期（bin/config.yaml）')

    def on_open(ws):
        log('[自检] 2/3 采集服务已连接，等待房间状态 ...')

    ws = websocket.WebSocketApp(url, on_message=on_message,
                                on_error=on_error, on_open=on_open)
    import threading
    threading.Thread(target=ws.run_forever, kwargs={'ping_interval': 20}, daemon=True).start()

    emit({'ts': _ts(), 'type': 'meta', 'event': 'start', 'room': room})

    # ---- 等待首个消息，判定服务与房间状态（最多 8 秒）
    deadline = time.time() + 8
    while time.time() < deadline and not st['any']:
        time.sleep(0.2)

    if not st['any']:
        log('[自检][失败] 8 秒内没有收到任何消息，无法确认连接')
        log('  → 可能原因：直播间号错误 / 未开播 / 采集服务未启动 / Cookie 失效')
        log('  → 排查：curl --noproxy \'*\' http://127.0.0.1:1088/health')
        sys.exit(3)

    sysdef = st['system'] or {}
    live = sysdef.get('live')
    if sysdef and live is False:
        log(f'[自检][失败] 直播间状态：未开播（{sysdef.get("code") or sysdef.get("event")}）')
        log('  → 请确认已开播；私密开播请确认测试小号在可见名单内')
        sys.exit(4)
    log(f'[自检] 2/3 直播间状态：'
        + ('已开播' if live else '已连接（未收到明确开播标记）')
        + (f" / {sysdef.get('event')}" if sysdef.get('event') else ''))

    # ---- 3 秒内是否有业务消息
    mark = time.time()
    while time.time() - mark < 3 and st['biz'] == 0:
        time.sleep(0.2)
    if st['biz'] == 0:
        log('[自检][告警] 3 秒内没有业务消息（弹幕/礼物/进场）')
        log('  → 冷门直播间属正常；若持续如此，检查 Cookie 是否登录、房间是否静音无人')
    else:
        cost = int((time.time() - t0) * 1000)
        log(f'[自检] 3/3 消息接收：正常（{cost} ms 内已收到业务消息）')

    log('[自检] 全部通过，开始采集' + ('（礼物日志已开启全字段）' if DEBUG_FULL else ''))

    if seconds > 0:
        time.sleep(seconds)
        ws.close()
    else:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            ws.close()


if __name__ == '__main__':
    main()

#!/usr/bin/env python
# coding: utf-8
"""
连接 douyinLive 本地服务的 WebSocket，统计收到哪些消息类型。

用法：
    python ws_collect.py <直播间号> [秒数] [--dump]
"""

import collections
import json
import sys
import time

import websocket

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


def main():
    if len(sys.argv) < 2:
        print('用法: python ws_collect.py <直播间号> [秒数] [--dump]')
        sys.exit(2)
    room = sys.argv[1]
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith('--') else 60
    dump = '--dump' in sys.argv

    url = f'ws://127.0.0.1:1088/ws/{room}'
    print(f'=== 连接 {url} | {seconds:.0f}s ===')

    counts = collections.Counter()
    samples = {}
    gifts = []
    chats = []

    def on_message(ws, message):
        try:
            obj = json.loads(message)
        except Exception:
            return
        method = obj.get('type') or obj.get('method') or obj.get('msg_type') or 'unknown'
        counts[method] += 1
        samples.setdefault(method, obj)
        if 'Gift' in str(method):
            gifts.append(obj)
        if 'Chat' in str(method):
            data = obj.get('data') or obj
            chats.append((data.get('user_name') or data.get('nickname') or '', data.get('content') or data.get('text') or ''))

    def on_error(ws, err):
        print('WS 错误:', err)

    ws = websocket.WebSocketApp(url, on_message=on_message, on_error=on_error)
    import threading
    t = threading.Thread(target=ws.run_forever, kwargs={'ping_interval': 20}, daemon=True)
    t.start()
    time.sleep(seconds)
    ws.close()

    print('\n=== 消息类型统计 ===')
    if not counts:
        print('  （没有任何消息）')
    for k, v in counts.most_common():
        print(f'  {k:32s} {v:5d}')
    print(f'\n礼物消息: {counts.get("WebcastGiftMessage", 0)} 条')
    for g in gifts[:5]:
        print('  ', json.dumps(g, ensure_ascii=False)[:260])
    if chats:
        print('\n弹幕样例:')
        for u, c in chats[:3]:
            print(f'  {u}: {c}')

    if dump:
        print('\n=== 每种类型的第一条原文 ===')
        for k, v in samples.items():
            print(f'\n--- {k} ---')
            print(json.dumps(v, ensure_ascii=False)[:700])


if __name__ == '__main__':
    main()

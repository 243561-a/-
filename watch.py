#!/usr/bin/env python
# coding: utf-8
"""
直播间实时看板：连上房间，把弹幕/礼物/进场/点赞实时打在屏幕上，结束时出汇总。

用途就是"先看见"——确认抓得到、抓得准，再谈机器人怎么反应。

    python watch.py <直播间号>
    python watch.py <直播间号> --seconds 60
    python watch.py <直播间号> --only chat,gift

输出同时落盘到 runs/<房间号>-<时间>.jsonl，事后可以复盘。
Ctrl+C 随时停，汇总照常打印。
"""

import argparse
import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


def collect(room, out_q, stop):
    cmd = [sys.executable, os.path.join(HERE, 'collector.py'), room, '0']
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         cwd=HERE, env=env, text=True, encoding='utf-8',
                         errors='replace', bufsize=1)
    try:
        for line in p.stdout:
            if stop.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                out_q.put(json.loads(line))
            except Exception:
                pass
    finally:
        try:
            p.terminate()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('room')
    ap.add_argument('--seconds', type=float, default=0, help='0 表示一直跑')
    ap.add_argument('--only', default=None, help='只显示这些类型，如 chat,gift')
    ap.add_argument('--quiet-enter', action='store_true', help='不逐条打印进场/点赞')
    args = ap.parse_args()

    only = set(args.only.split(',')) if args.only else None
    out_dir = os.path.join(HERE, 'runs')
    os.makedirs(out_dir, exist_ok=True)
    dump_path = os.path.join(out_dir, f'{args.room}-{time.strftime("%H%M%S")}.jsonl')

    out_q, stop = queue.Queue(), threading.Event()
    threading.Thread(target=collect, args=(args.room, out_q, stop), daemon=True).start()

    counts = collections.Counter()
    chatters = collections.Counter()
    gifts = []
    online = 0
    last_tick = time.time()
    t0 = time.time()

    print(f'=== 正在看 {args.room}（Ctrl+C 结束）===')
    print(f'    原始事件写入 {dump_path}\n')

    f = open(dump_path, 'w', encoding='utf-8')
    try:
        while True:
            if args.seconds and time.time() - t0 >= args.seconds:
                break
            try:
                ev = out_q.get(timeout=0.3)
            except queue.Empty:
                ev = None

            if ev is None:
                continue
            f.write(json.dumps(ev, ensure_ascii=False) + '\n')
            f.flush()

            t = ev.get('type')
            counts[t] += 1
            if only and t not in only and t not in ('stats', 'meta'):
                continue

            ts = time.strftime('%H:%M:%S')
            if t == 'chat':
                chatters[ev.get('user_name', '?')] += 1
                print(f'[{ts}] 弹幕  {ev.get("user_name","?"):<10} {ev.get("text","")}')
            elif t == 'gift':
                gifts.append(ev)
                print(f'[{ts}] 礼物  {ev.get("user_name","?"):<10} 送 {ev.get("gift","?")} '
                      f'x{ev.get("repeat", 1)}（{ev.get("diamond", 0)} 钻）')
            elif t == 'enter' and not args.quiet_enter:
                print(f'[{ts}] 进场  {ev.get("user_name","?")}')
            elif t == 'follow':
                print(f'[{ts}] 关注  {ev.get("user_name","?")}')
            elif t == 'stats':
                online = int(ev.get('online') or ev.get('total') or online)

            if time.time() - last_tick >= 30:
                last_tick = time.time()
                print(f'—— {int(time.time()-t0)}s：弹幕 {counts["chat"]} / 礼物 {counts["gift"]}'
                      f' / 进场 {counts["enter"]} / 点赞 {counts["like"]} / 关注 {counts["follow"]}'
                      + (f' / 在线 {online}' if online else ''))
    except KeyboardInterrupt:
        print('\n（手动结束）')
    finally:
        stop.set()
        f.close()

    dur = time.time() - t0
    print(f'\n=== 汇总 · {dur:.0f} 秒 ===')
    print(f'  弹幕 {counts["chat"]}  礼物 {counts["gift"]}  进场 {counts["enter"]}'
          f'  点赞 {counts["like"]}  关注 {counts["follow"]}')
    if gifts:
        print('  礼物明细：')
        for g in gifts[:12]:
            print(f'    {g.get("user_name")} 送 {g.get("gift")} x{g.get("repeat", 1)}')
    else:
        print('  礼物明细：这一段没有礼物')
    if chatters:
        top = '，'.join(f'{u}×{n}' for u, n in chatters.most_common(5))
        print(f'  发言最多：{top}')
    print(f'  原始事件：{dump_path}')


if __name__ == '__main__':
    main()

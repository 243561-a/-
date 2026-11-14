#!/usr/bin/env python
# coding: utf-8
"""
直播间链路验靶：跑一段固定时间，直接给结论。

用法：
    python verify.py <直播间号> [秒数，默认 120]

它做三件事：
1. 拉起 collector.py 采同一房间的弹幕/礼物/进场/点赞；
2. 实时打印关键事件（礼物高亮，带“接收于第几秒”）；
3. 结束时给一张判决表：弹幕通不通、礼物通不通、是否需要登录 Cookie。

判决逻辑：
- 收到 chat  -> 游客态可见弹幕，弹幕链路 OK
- 收到 gift  -> 游客态可见礼物，**不用 Cookie**，礼物链路 OK
- 有 chat 无 gift 且房间有人说话 -> 疑似游客态不下发礼物，需要 Cookie
- 两者都没有 -> 房间没开播 / 直播间号不对 / 被风控
"""

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.join(HERE, 'collector.py')

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

KEEP = 'chat,gift,enter,like,stats,control'


def main():
    if len(sys.argv) < 2:
        print('用法: python verify.py <直播间号> [秒数]')
        sys.exit(2)
    room = sys.argv[1]
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0

    print(f'=== 目标直播间 {room} | 采集 {seconds:.0f} 秒 | 游客态（不带 Cookie）===')
    print('现在去直播间：发一条弹幕，再送一个 1 钻的小心心\n')

    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    p = subprocess.Popen(
        [sys.executable, COLLECTOR, room, str(seconds), f'--only={KEEP}'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd=HERE, env=env, text=True, encoding='utf-8', errors='replace', bufsize=1,
    )

    t0 = time.time()
    counts = {'chat': 0, 'gift': 0, 'enter': 0, 'like': 0, 'control': 0}
    gifts = []
    chats = []
    online = 0
    first_ts = {}

    for line in p.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get('type')
        el = time.time() - t0
        if t == 'stats':
            online = e.get('online', online)
            continue
        if t not in counts:
            continue
        counts[t] = counts.get(t, 0) + 1
        first_ts.setdefault(t, el)

        if t == 'chat':
            chats.append((el, e.get('user_name', ''), e.get('text', '')))
            print(f'[{el:6.1f}s] 弹幕  {e.get("user_name", "?"):<12} {e.get("text", "")}')
        elif t == 'gift':
            gifts.append((el, e))
            print(f'[{el:6.1f}s] >>> 礼物 {e.get("user_name", "?")} 送 '
                  f'{e.get("gift", "?")} x{e.get("repeat", 1)} '
                  f'(combo {e.get("combo", 1)}, {e.get("diamond", 0)} 钻)')
        elif t == 'control':
            print(f'[{el:6.1f}s] 控制消息 status={e.get("status")}')

    p.wait()
    total = sum(counts.values())

    print('\n' + '=' * 52)
    print('判决表')
    print('=' * 52)
    print(f'  运行时长        {time.time() - t0:.0f} 秒')
    print(f'  在线人数        {online}')
    print(f'  弹幕 chat       {counts["chat"]}')
    print(f'  礼物 gift       {counts["gift"]}')
    print(f'  进场 enter      {counts["enter"]}')
    print(f'  点赞 like       {counts["like"]}')
    print(f'  事件总数        {total}')
    if gifts:
        el, g = gifts[0]
        print(f'  首个礼物        {el:.1f} 秒时到达 -> {g.get("user_name")} / {g.get("gift")}')

    print('\n结论')
    if counts['gift'] > 0:
        print('  ✅ 游客态能收到礼物，采集不需要任何 Cookie。')
        print('     礼物链路已通，延迟量级 '
              f'{first_ts.get("gift", 0):.1f} 秒内（含首次连接开销）。')
    elif counts['chat'] > 0:
        print('  ⚠️ 游客态能看弹幕，但这一段没收到礼物。')
        print('     先确认测试号确实送了礼；若确定送了仍无 gift 事件，')
        print('     说明礼物消息需要登录态 -> 必须提供小号 Cookie。')
    elif total == 0:
        print('  ❌ 一个事件都没收到：房间没开播 / 直播间号不对 / 网络被拦。')
        print('     先在浏览器打开该直播间，确认画面在动、弹幕在滚。')
    else:
        print('  ⚠️ 只有进场/点赞，没有弹幕也没有礼物。')
        print('     要么这一段确实没人说话，要么房间开了“仅粉丝可评论”/评论审核。')
    print('=' * 52)
    sys.exit(0 if total else 1)


if __name__ == '__main__':
    main()

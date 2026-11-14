#!/usr/bin/env python
# coding: utf-8
"""
直播礼物链路总装：抖音直播间 -> 事件流 -> 规则引擎 -> 机器人开口

    collector.py (子进程)  ->  规则引擎  ->  speaker.py (SSH + RPC)

用法：
    python pipeline.py <直播间号>                    # 一直跑
    python pipeline.py <直播间号> --seconds 120       # 跑两分钟
    python pipeline.py <直播间号> --dry-run           # 只打话术，不让机器人说话
    python pipeline.py <直播间号> --replay 假礼物.json # 用假事件验流程，不连抖音

设计要点：
- 采集在读线程，播报在发线程，互不阻塞（机器人一句话说 3 秒也不会卡住收弹幕）
- 连击汇总、幂等去重、冷却全部在规则引擎里，这里只管串起来
- 退出时把没结算的礼物补播，不漏最后一笔
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from rules import GiftRules  # noqa: E402
from speaker import Speaker  # noqa: E402
from bans import BanList  # noqa: E402
from reply_engine import ReplyEngine, ReplyWorker  # noqa: E402
from interactions import InteractionRules  # noqa: E402
from admin import AdminContext, AdminServer, load_or_create_token  # noqa: E402

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


def now_ms():
    return int(time.time() * 1000)


class SpeechWorker(threading.Thread):
    """独立发线程：机器人说话再慢也不影响采集。"""

    def __init__(self, speaker, dry_run=False, on_spoken=None):
        super().__init__(daemon=True)
        self.sp = speaker
        self.dry = dry_run
        self.on_spoken = on_spoken
        self.q = queue.Queue()
        self.deferred = []
        self.sent = 0
        self.failed = 0
        self.deferred_sent = 0
        self.stop_flag = False
        self.busy = False

    def submit(self, task):
        self.q.put(task)

    def run(self):
        while not self.stop_flag:
            task = None
            now = time.time()
            for i, (due, t) in enumerate(self.deferred):
                if due <= now:
                    task = t
                    self.deferred.pop(i)
                    self.deferred_sent += 1
                    break
            if task is None:
                try:
                    task = self.q.get(timeout=0.2)
                except queue.Empty:
                    continue

            self.busy = True
            t0 = time.time()
            try:
                if self.dry:
                    self.sent += 1
                    print(f'  [dry-run] 会说：{task["text"]}')
                    if self.on_spoken:
                        self.on_spoken(task['text'])
                    continue
                r = self.sp.speak(task['text'], priority=task['priority'],
                                  interrupt=task['interrupt'], domain='robot-live-gift',
                                  source=task.get('source') or f"礼物/{task.get('gift','')}")
                cost = int((time.time() - t0) * 1000)

                # 高档礼物正在播报时，低优先级会被明确拒绝（priority_rejected）。
                # 这是无副作用的拒绝，延后重提即可，不会重复播报。
                if not r['accepted'] and r.get('error') == 'priority_rejected' \
                        and task.get('retry', 0) < 3:
                    task['retry'] = task.get('retry', 0) + 1
                    task['interrupt'] = False
                    self.deferred.append((time.time() + 1.8, task))
                    print(f'  [延后重试] 高优先级正在播报，{task["retry"]} 次后重提：{task["text"]}')
                    continue

                self.sent += 1
                flag = 'OK ' if r['accepted'] else '未受理'
                extra = f'  {r.get("error")}' if not r['accepted'] and r.get('error') else ''
                print(f'  [播报{flag}] {cost}ms  p{task["priority"]}'
                      f'{" 打断" if task["interrupt"] else ""}  {task["text"]}{extra}')
                if r['accepted'] and self.on_spoken:
                    self.on_spoken(task['text'])
            except Exception as e:
                self.failed += 1
                print(f'  [播报失败] {task["text"]} -> {e}')
            finally:
                self.busy = False

    def drain(self, timeout=15.0):
        deadline = time.time() + timeout
        while (not self.q.empty() or self.busy or self.deferred) and time.time() < deadline:
            time.sleep(0.1)
        if self.deferred:
            print(f'  [收尾] {len(self.deferred)} 句因高优先级占用未播出，已放弃')
        self.stop_flag = True


def preflight():
    """启动前确认采集服务在线；没起就自己拉起来，不再依赖控制台。"""
    from backend import go_alive, ensure_backend
    if go_alive():
        print('[自检] 采集服务：已连接 127.0.0.1:1088')
        return True
    print('[自检] 采集服务未运行，正在自动拉起…')
    ok, msg = ensure_backend()
    print('[自检] ' + msg)
    if ok:
        return True
    print('  → 手动启动：cd bin && ./douyinLive.exe -config config.yaml -port 1088')
    print('  → 若服务在跑但仍报错，检查 bin/config.yaml 里的 Cookie 是否过期')
    return False


def collect_thread(room, seconds, out_q, stop_event, source='go'):
    script = 'go_collector.py' if source == 'go' else 'collector.py'
    cmd = [sys.executable, os.path.join(HERE, script), room, str(seconds)]
    if source != 'go':
        cmd.append('--only=chat,gift,stats,control')
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         cwd=HERE, env=env, text=True, encoding='utf-8',
                         errors='replace', bufsize=1)
    try:
        for line in p.stdout:
            if stop_event.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                out_q.put(json.loads(line))
            except Exception:
                # 采集器的自检/诊断信息（非 JSON），直接透传到控制台，
                # 这样"连不上/未开播/Cookie失效"能立刻看见
                print(line, flush=True)
                continue
    finally:
        try:
            p.terminate()
        except Exception:
            pass


def replay_thread(path, out_q, stop_event):
    """假事件回放：验证规则引擎和播报，不需要直播间。"""
    with open(path, 'r', encoding='utf-8') as f:
        events = json.load(f)
    t0 = time.time()
    for ev in events:
        if stop_event.is_set():
            return
        delay = float(ev.pop('_delay', 0))
        if delay:
            time.sleep(delay)
        out_q.put(ev)
    print(f'  [replay] {len(events)} 条假事件已投递，用时 {time.time()-t0:.1f}s')
    time.sleep(3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('room', help='直播间号；使用 --replay 时可填任意占位')
    ap.add_argument('--seconds', type=float, default=0)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--replay', default=None)
    ap.add_argument('--gifts', default=None)
    ap.add_argument('--source', choices=['go', 'py'], default='go',
                    help='采集来源：go=douyinLive 服务（能收礼物，需登录态）；py=自建栈')
    ap.add_argument('--admin-port', type=int, default=8770,
                    help='控制台端口，0 表示不开')
    args = ap.parse_args()

    # room 填 auto/last 时，用控制台上次记住的直播间，方便做开机自启
    if args.room in ('auto', 'last'):
        try:
            with open(os.path.join(HERE, 'gui-state.json'), encoding='utf-8') as f:
                args.room = (json.load(f).get('room') or '').strip()
        except Exception:
            args.room = ''
        if not args.room:
            print('[自检][失败] 没记住上次的直播间号，请直接传入房间号')
            sys.exit(2)
        print(f'[自检] 使用上次的直播间：{args.room}')

    rules = GiftRules(args.gifts)
    bans = BanList(os.path.join(HERE, 'bans.json'))
    reply = ReplyEngine(os.path.join(HERE, 'replies.json'))
    inter = InteractionRules(os.path.join(HERE, 'interactions.json'))
    sp = None if args.dry_run else Speaker()
    worker = SpeechWorker(sp, args.dry_run)
    ctx = AdminContext(worker, bans, args.room)
    worker.on_spoken = ctx.note_spoken
    worker.start()

    srv = None
    if args.admin_port:
        token = load_or_create_token(os.path.join(HERE, 'admin-token.txt'))
        # 只绑本机：控制台永远走 127.0.0.1，没有远程访问的需求
        srv = AdminServer(ctx, port=args.admin_port, host='127.0.0.1', token=token)
        srv.start()
        # 等端口真的绑好再报，别在没绑上时也打印"已就绪"
        for _ in range(10):
            if srv.httpd is not None:
                break
            time.sleep(0.1)
        if srv.httpd is not None:
            print(f'=== 管理接口：http://127.0.0.1:{args.admin_port}/ '
                  f'（仅本机，口令见 admin-token.txt）===')
        else:
            print(f'[自检][告警] 管理接口 {args.admin_port} 没起来，'
                  f'控制台的「点名回复」会不可用')

    def submit_task(task):
        """礼物自动话术的统一入口：先过「不回答清单」和「暂停」。"""
        hit = bans.hit(task['text']) or bans.hit(str(task.get('gift') or ''))
        if hit:
            ctx.blocked += 1
            print(f'  [拦截] 命中「{hit}」，不说：{task["text"]}')
            return
        if ctx.paused():
            ctx.blocked += 1
            print(f'  [暂停中] 不说：{task["text"]}')
            return
        worker.submit(task)

    # 弹幕回复：独立线程 + 串行队列，一个人说完再轮到下一个
    reply_worker = ReplyWorker(reply, submit_task)
    reply_worker.start()
    # 让控制台能"点名让机器人回某条弹幕"，复用同一个串行队列
    ctx.reply_engine = reply
    ctx.reply_worker = reply_worker

    out_q, stop_event = queue.Queue(), threading.Event()
    if args.replay:
        t = threading.Thread(target=replay_thread, args=(args.replay, out_q, stop_event), daemon=True)
    else:
        if not preflight():
            sys.exit(2)
        t = threading.Thread(target=collect_thread,
                             args=(args.room, args.seconds or 0, out_q, stop_event, args.source),
                             daemon=True)
    t.start()

    mode = '回放假事件' if args.replay else f'直播间 {args.room}'
    print(f'=== 礼物链路运行中 | {mode} | {"dry-run" if args.dry_run else "真播报"} ===')
    rmark = reply.marker if reply.enabled else '（已关闭）'
    print(f'（收到礼物会等连击停顿后汇总播一次；带 {rmark} 的弹幕会念出来并回答）\n')

    start = time.time()
    counts = {'chat': 0, 'gift': 0, 'enter': 0, 'reply': 0, 'like': 0, 'follow': 0,
              'thanks': 0}
    last_tick = 0

    try:
        while True:
            if args.seconds and not args.replay and time.time() - start > args.seconds:
                break
            if args.replay and not t.is_alive() and out_q.empty():
                break
            # 采集线程意外退出（自检不通过 / 连接断开）时不要空转，直接收尾
            if not t.is_alive() and not stop_event.is_set():
                print('[自检][失败] 采集线程已退出，主流程结束')
                print('  → 常见原因：直播间未开播 / 直播间号错误 / Cookie 失效 / 采集服务断开')
                print('  → 排查顺序：看上方 [自检] 日志 → curl 1088/health → 检查 bin/config.yaml 的 Cookie')
                break
            try:
                ev = out_q.get(timeout=0.2)
            except queue.Empty:
                ev = None

            if ev is not None:
                et = ev.get('type')
                ctx.note_event()
                if et == 'chat':
                    counts['chat'] += 1
                    print(f'[{time.strftime("%H:%M:%S")}] 弹幕 {ev.get("user_name")}'
                          f'：{ev.get("text")}')
                    if bans.hit(ev.get('text') or ''):
                        ctx.blocked += 1   # 命中「不回答清单」，问答环也不理
                    else:
                        # 只有带唤醒标记的弹幕会走问答；这里只入队，不阻塞采集
                        item, why = reply.feed(ev, now_ms())
                        if item:
                            reply_worker.enqueue(item)
                            if why == 'queued':
                                counts['reply'] = counts.get('reply', 0) + 1
                                print(f'  [弹幕回复] 已入队（前面还有 '
                                      f'{reply_worker.q.qsize() - 1} 条）', flush=True)
                            else:
                                print(f'  [弹幕回复] {why}', flush=True)
                        elif why:
                            print(f'  [弹幕回复] 跳过：{why}', flush=True)
                elif et == 'gift':
                    counts['gift'] += 1
                    ctx.gifts += 1
                    print(f'[{time.strftime("%H:%M:%S")}] 收到礼物 {ev.get("user_name")} / '
                          f'{ev.get("gift")} x{ev.get("repeat", 1)}')
                    for task in rules.feed(ev, now_ms()):
                        submit_task(task)
                elif et == 'enter':
                    counts['enter'] += 1
                    print(f'[{time.strftime("%H:%M:%S")}] 进场 {ev.get("user_name")}')
                elif et in ('like', 'follow'):
                    # 每个人只谢一次（点赞会反复推、关注可能重发），
                    # 点赞之间还有最小间隔，防止一批新人涌进来把话占满
                    counts[et] = counts.get(et, 0) + 1
                    task, why = inter.feed(ev, now_ms())
                    label = '点赞' if et == 'like' else '关注'
                    if task:
                        counts['thanks'] = counts.get('thanks', 0) + 1
                        print(f'[{time.strftime("%H:%M:%S")}] {label} '
                              f'{ev.get("user_name")} → {task["text"]}')
                        submit_task(task)
                    elif why == 'gap':
                        print(f'  [{label}] 太密集，这次先跳过 {ev.get("user_name")}')

            if now_ms() - last_tick >= 200:
                last_tick = now_ms()
                rules.maybe_reload()
                bans.maybe_reload()
                reply.maybe_reload()
                inter.maybe_reload()
                for task in rules.tick(now_ms()):
                    submit_task(task)
    except KeyboardInterrupt:
        print('\n收到中断，收尾中…')

    stop_event.set()
    reply_worker.stop()
    for task in rules.flush_all(now_ms()):
        submit_task(task)
    worker.drain()
    if srv:
        srv.stop()
    if sp:
        sp.close()

    print(f'\n=== 统计 ===')
    print(f'  礼物事件 {counts["gift"]} 条 -> 播报 {worker.sent} 次，失败 {worker.failed} 次')
    print(f'  弹幕 {counts["chat"]} 条 -> 回复 {reply_worker.replied} 条，'
          f'丢弃 {reply_worker.dropped} 条（标记/冷却/队列）')
    print(f'  进场 {counts["enter"]} 条')
    print(f'  点赞 {counts["like"]} 条 / 关注 {counts["follow"]} 条 -> 感谢 {counts["thanks"]} 次'
          f'（每人只谢一次、太密集会跳过，所以次数更少是正常的）')
    print(f'  拦截 {ctx.blocked} 次（命中不回答清单或暂停中）')
    print(f'  运行 {time.time()-start:.0f} 秒')


if __name__ == '__main__':
    main()

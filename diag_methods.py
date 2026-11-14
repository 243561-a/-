#!/usr/bin/env python
# coding: utf-8
"""
消息类型普查探针（诊断用，不参与生产）

目的：回答一个必须确定的问题 —— 礼物消息是"服务端没下发"，还是"下发了但被
上游的 `except Exception: pass` 吞掉了"。

上游 _wsOnMessage 的写法：
    }.get(method)(msg.payload)
    except Exception: pass

两处都会静默吞：
  1. method 不在字典里 -> .get() 返回 None -> None(...) 抛 TypeError -> 吞
  2. 解析抛异常 -> 吞
两者表现完全一样：什么都没看到。

本探针完全接管 _wsOnMessage，在分发之前就把每一种 msg.method 计数，
并对已知类型单独 try，把异常原文打出来。这样"没来"和"来了但坏了"能彻底分开。

用法：
    python diag_methods.py <直播间号> [秒数]
"""

import builtins
import gzip
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = None
for _cand in (os.environ.get('ROBOTLIVE_SRC'),
              os.path.join(HERE, 'vendor', 'src'),
              os.path.join(HERE, 'src')):
    if _cand and os.path.isdir(os.path.join(_cand, 'protobuf')):
        SRC = _cand
        break
if SRC is None:
    sys.stderr.write('找不到抖音抓取源码目录（vendor/src 或 src）\n')
    sys.exit(2)
sys.path.insert(0, SRC)
os.chdir(SRC)

# 上游会在解析里 print 大量内容，这里全部静音，只留我们自己的报告
builtins.print = lambda *a, **k: None

from liveMan import DouyinLiveWebFetcher  # noqa: E402
from protobuf.douyin import PushFrame, Response  # noqa: E402
from protobuf.douyin import GiftMessage  # noqa: E402

# 借用 collector 里的 Cookie 注入（覆盖 ttwid 属性），否则探针还是游客态
import collector  # noqa: E402

COOKIE_LOADED = bool(collector._USER_TTWID or collector._USER_EXTRA)


def out(*a):
    sys.stderr.write(' '.join(str(x) for x in a) + '\n')
    sys.stderr.flush()


class DiagFetcher(collector.EventFetcher):
    """统计所有到达的消息类型，并暴露解析异常（继承 collector 的 Cookie 注入）"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.method_count = {}
        self.parse_error = {}
        self.gift_raw = []
        self.frame_count = 0

    def _wsOnMessage(self, ws, message):
        # 完全重写，不复用上游的静默分发
        try:
            package = PushFrame().parse(message)
            response = Response().parse(gzip.decompress(package.payload))
        except Exception as e:
            self.parse_error['__frame__:' + type(e).__name__] = \
                self.parse_error.get('__frame__:' + type(e).__name__, 0) + 1
            return

        self.frame_count += 1

        if response.need_ack:
            ack = PushFrame(log_id=package.log_id,
                            payload_type='ack',
                            payload=response.internal_ext.encode('utf-8')
                            ).SerializeToString()
            try:
                ws.send(ack, 0x2)  # OPCODE_BINARY
            except Exception:
                pass

        for msg in response.messages_list:
            method = msg.method or '(空)'
            self.method_count[method] = self.method_count.get(method, 0) + 1

            # 礼物单独深挖：解析成功就记录，失败就把异常留下
            if method == 'WebcastGiftMessage':
                try:
                    g = GiftMessage().parse(msg.payload)
                    self.gift_raw.append({
                        'user': g.user.nick_name,
                        'uid': str(g.user.id),
                        'gift': g.gift.name,
                        'combo': g.combo_count,
                        'repeat': g.repeat_count,
                        'diamond': g.gift.diamond_count,
                    })
                    out(f"  >>> 礼物！{g.user.nick_name} 送 {g.gift.name} "
                        f"x{g.combo_count} ({g.gift.diamond_count}钻)")
                except Exception as e:
                    key = f'GiftMessage:{type(e).__name__}:{str(e)[:60]}'
                    self.parse_error[key] = self.parse_error.get(key, 0) + 1
            elif method == 'WebcastChatMessage':
                # 实时把弹幕打出来，方便和现场发的对账
                try:
                    from protobuf.douyin import ChatMessage
                    c = ChatMessage().parse(msg.payload)
                    out(f"  [弹幕] {c.user.nick_name}: {c.content}")
                except Exception:
                    pass

    def report(self):
        out('')
        out('=' * 66)
        out(f'收到数据帧 {self.frame_count} 个')
        out('-' * 66)
        out('服务端实际下发的消息类型：')
        if not self.method_count:
            out('  （空）')
        for k, v in sorted(self.method_count.items(), key=lambda x: -x[1]):
            mark = '  <-- 礼物' if 'Gift' in k else ''
            out(f'  {v:>5}  {k}{mark}')
        out('-' * 66)
        if self.parse_error:
            out('解析异常（这些就是被上游静默吞掉的部分）：')
            for k, v in sorted(self.parse_error.items(), key=lambda x: -x[1]):
                out(f'  {v:>5}  {k}')
        else:
            out('解析异常：无（凡是到达的消息都解析成功了）')
        out('-' * 66)
        if self.gift_raw:
            out(f'礼物明细（{len(self.gift_raw)} 条）：')
            for g in self.gift_raw[:15]:
                out(f"  {g['user']} 送 {g['gift']} x{g['combo']} "
                    f"(repeat={g['repeat']}, {g['diamond']}钻)")
        else:
            out('礼物明细：0 条')
        out('=' * 66)


def main():
    if len(sys.argv) < 2:
        out('用法: python diag_methods.py <直播间号> [秒数]')
        sys.exit(2)
    room = sys.argv[1]
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 90

    out(f'[diag] 普查直播间 {room}，时长 {seconds:.0f}s')
    out(f'[diag] 登录态：' + ('已注入 Cookie' if COOKIE_LOADED else '游客态（收不到礼物）'))

    f = DiagFetcher(room)

    # start() 会阻塞在 run_forever()，所以用定时器到点出报告并退出
    import threading

    def _finish():
        f.report()
        os._exit(0)

    threading.Timer(seconds, _finish).start()
    f.start()


if __name__ == '__main__':
    main()

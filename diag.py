#!/usr/bin/env python
# coding: utf-8
"""
诊断：服务端到底下发了哪些消息类型，以及哪些解析失败了。

背景：上游 liveMan.py 的分发是 `except Exception: pass`，
解析失败和"消息没来"在外面看起来一模一样。这个脚本把两者分开。

用法：
    python diag.py <直播间号> [秒数]
"""

import collections
import gzip
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'vendor', 'src')
sys.path.insert(0, SRC)
os.chdir(SRC)

from liveMan import DouyinLiveWebFetcher  # noqa: E402
from protobuf.douyin import PushFrame, Response  # noqa: E402
import websocket  # noqa: E402

COUNT = collections.Counter()
ERRORS = collections.Counter()
ERROR_MSG = {}
RAW_BYTES = collections.Counter()


class DiagFetcher(DouyinLiveWebFetcher):
    def _wsOnMessage(self, ws, message):
        package = PushFrame().parse(message)
        response = Response().parse(gzip.decompress(package.payload))

        if response.need_ack:
            ack = PushFrame(log_id=package.log_id, payload_type='ack',
                            payload=response.internal_ext.encode('utf-8')).SerializeToString()
            ws.send(ack, websocket.ABNF.OPCODE_BINARY)

        table = {
            'WebcastChatMessage': self._parseChatMsg,
            'WebcastGiftMessage': self._parseGiftMsg,
            'WebcastLikeMessage': self._parseLikeMsg,
            'WebcastMemberMessage': self._parseMemberMsg,
            'WebcastSocialMessage': self._parseSocialMsg,
            'WebcastRoomUserSeqMessage': self._parseRoomUserSeqMsg,
            'WebcastFansclubMessage': self._parseFansclubMsg,
            'WebcastControlMessage': self._parseControlMsg,
            'WebcastEmojiChatMessage': self._parseEmojiChatMsg,
            'WebcastRoomStatsMessage': self._parseRoomStatsMsg,
            'WebcastRoomMessage': self._parseRoomMsg,
            'WebcastRoomRankMessage': self._parseRankMsg,
            'WebcastRoomStreamAdaptationMessage': self._parseRoomStreamAdaptationMsg,
        }

        for msg in response.messages_list:
            method = msg.method
            COUNT[method] += 1
            RAW_BYTES[method] += len(msg.payload or b'')
            fn = table.get(method)
            if fn is None:
                continue
            try:
                fn(msg.payload)
            except Exception as e:
                ERRORS[method] += 1
                ERROR_MSG.setdefault(method, repr(e)[:160])

    # 静音噪音，但礼物保持真实解析（解析失败会被计数，礼物到达会打印）
    def _parseChatMsg(self, p): pass
    def _parseLikeMsg(self, p): pass
    def _parseMemberMsg(self, p): pass
    def _parseSocialMsg(self, p): pass
    def _parseFansclubMsg(self, p): pass
    def _parseControlMsg(self, p): pass
    def _parseEmojiChatMsg(self, p): pass
    def _parseRoomStatsMsg(self, p): pass
    def _parseRoomMsg(self, p): pass
    def _parseRankMsg(self, p): pass
    def _parseRoomStreamAdaptationMsg(self, p): pass
    def _parseRoomUserSeqMsg(self, p):
        from protobuf.douyin import RoomUserSeqMessage
        m = RoomUserSeqMessage().parse(p)
        print(f'  [在线人数] {int(m.total or 0)}', file=sys.stderr)


def main():
    if len(sys.argv) < 2:
        print('用法: python diag.py <直播间号> [秒数]')
        sys.exit(2)
    room = sys.argv[1]
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60

    print(f'=== 诊断 {room} | {seconds:.0f}s ===', file=sys.stderr)

    def report():
        print('\n=== 服务端实际下发的消息类型 ===', file=sys.stderr)
        for method, n in COUNT.most_common():
            tag = f'  解析失败 {ERRORS[method]} 次: {ERROR_MSG.get(method, "")}' if ERRORS[method] else ''
            print(f'  {method:46s} {n:5d} 条  {RAW_BYTES[method]/1024:8.1f} KB{tag}', file=sys.stderr)
        if not COUNT:
            print('  （一条消息都没收到）', file=sys.stderr)
        print('\n礼物消息 WebcastGiftMessage: '
              + ('有下发 ' + str(COUNT['WebcastGiftMessage']) + ' 条'
                 if COUNT['WebcastGiftMessage'] else '未下发'), file=sys.stderr)
        sys.stderr.flush()
        os._exit(0)

    import threading
    threading.Timer(seconds, report).start()

    try:
        DiagFetcher(room).start()
    except Exception as e:
        print(f'连接异常: {e!r}', file=sys.stderr)


if __name__ == '__main__':
    main()

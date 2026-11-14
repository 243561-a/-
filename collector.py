#!/usr/bin/env python
# coding: utf-8
"""
抖音直播间事件采集器（v1）

职责单一：连上直播间 -> 把弹幕/礼物/进场/点赞转成统一的 JSON 行，打到 stdout。
下游（规则引擎、TTS）只消费 stdout，不关心抖音协议细节。

用法：
    python collector.py <直播间号> [运行秒数]
    python collector.py 871442823764 60

输出示例：
    {"ts":1763...,"type":"chat","user_id":"123","user_name":"小明","text":"主播多大了"}
    {"ts":1763...,"type":"gift","user_id":"456","user_name":"小红","gift":"玫瑰","count":1,"repeat":1}

设计原则：
- stdout 只出 JSON，人类可读日志一律走 stderr，方便被管线消费
- 抖音协议相关异常不抛出，交给上层看门狗重连
"""

import builtins
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 上游源码目录：电脑上是 vendor/src，装到机器人上是 src/
# 允许用 ROBOTLIVE_SRC 环境变量覆盖，便于在不同部署位置复用同一份代码
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
# 上游用相对路径找 sign.js / a_bogus.js / protobuf，必须切到源码目录
os.chdir(SRC)

_out = builtins.print


def log(*args, **kwargs):
    kwargs.setdefault('file', sys.stderr)
    kwargs.setdefault('flush', True)
    _out(*args, **kwargs)


builtins.print = log


ONLY = None


def emit(event):
    if ONLY and event.get('type') not in ONLY:
        return
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + '\n')
    sys.stdout.flush()


from liveMan import DouyinLiveWebFetcher  # noqa: E402


def _ts():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- Cookie 注入
# 未登录会话拿不到礼物消息（实测：75s/153 帧，服务端连 WebcastGiftMessage
# 这个类型都没下发）。登录态靠一份小号 Cookie 补上。
#
# 上游把 Cookie 写成 f"ttwid={self.ttwid}"，所以只要让 ttwid 属性返回
# "ttwid值; sessionid=...; ..."，拼出来的头就正好是完整 Cookie，不用改上游。
_COOKIE_FILE = os.path.join(HERE, 'cookie.txt')


def _load_cookie():
    """优先环境变量，其次 cookie.txt；返回 (ttwid, 其余字段)"""
    raw = os.environ.get('ROBOTLIVE_COOKIE', '').strip()
    if not raw and os.path.isfile(_COOKIE_FILE):
        with open(_COOKIE_FILE, encoding='utf-8') as f:
            raw = f.read().strip()
    if not raw:
        return None, ''
    raw = raw.replace('Cookie:', '').replace('\n', ' ').strip().strip('"').strip("'")
    parts, ttwid = [], ''
    for seg in raw.split(';'):
        seg = seg.strip()
        if not seg:
            continue
        if seg.lower().startswith('ttwid='):
            ttwid = seg.split('=', 1)[1]
        else:
            parts.append(seg)
    return ttwid, '; '.join(parts)


_USER_TTWID, _USER_EXTRA = _load_cookie()


class EventFetcher(DouyinLiveWebFetcher):
    """只用它连线和解析，事件吐成 JSON。"""

    @property
    def ttwid(self):
        """把登录 Cookie 拼进上游的 f"ttwid={...}" 里，得到完整 Cookie 头。"""
        if _USER_TTWID:
            return _USER_TTWID + ('; ' + _USER_EXTRA if _USER_EXTRA else '')
        base = DouyinLiveWebFetcher.ttwid.fget(self)
        return base + ('; ' + _USER_EXTRA if _USER_EXTRA else '')

    def _common(self, msg):
        c = getattr(msg, 'common', None)
        return {
            'msg_id': str(getattr(c, 'msg_id', '') or ''),
            'create_time': int(getattr(c, 'create_time', 0) or 0),
        }

    def _parseChatMsg(self, payload):
        from protobuf.douyin import ChatMessage
        m = ChatMessage().parse(payload)
        emit({
            'ts': _ts(), 'type': 'chat',
            'user_id': str(m.user.id), 'user_name': m.user.nick_name,
            'text': m.content, **self._common(m),
        })

    def _parseEmojiChatMsg(self, payload):
        from protobuf.douyin import EmojiChatMessage
        m = EmojiChatMessage().parse(payload)
        emit({
            'ts': _ts(), 'type': 'chat',
            'user_id': str(getattr(m.user, 'id', '')), 'user_name': getattr(m.user, 'nick_name', ''),
            'text': f"[表情] {m.default_content or m.emoji_id}", **self._common(m),
        })

    def _parseGiftMsg(self, payload):
        from protobuf.douyin import GiftMessage
        m = GiftMessage().parse(payload)
        emit({
            'ts': _ts(), 'type': 'gift',
            'user_id': str(m.user.id), 'user_name': m.user.nick_name,
            'gift': m.gift.name,
            'gift_id': str(getattr(m.gift, 'id', '') or ''),
            'repeat': int(getattr(m, 'repeat_count', 1) or 1),
            'combo': int(getattr(m, 'combo_count', 1) or 1),
            'diamond': int(getattr(m.gift, 'diamond_count', 0) or 0),
            **self._common(m),
        })

    def _parseMemberMsg(self, payload):
        from protobuf.douyin import MemberMessage
        m = MemberMessage().parse(payload)
        emit({
            'ts': _ts(), 'type': 'enter',
            'user_id': str(m.user.id), 'user_name': m.user.nick_name, **self._common(m),
        })

    def _parseLikeMsg(self, payload):
        from protobuf.douyin import LikeMessage
        m = LikeMessage().parse(payload)
        emit({
            'ts': _ts(), 'type': 'like',
            'user_id': str(getattr(m.user, 'id', '')), 'user_name': getattr(m.user, 'nick_name', ''),
            'count': int(getattr(m, 'count', 0) or 0), **self._common(m),
        })

    def _parseSocialMsg(self, payload):
        from protobuf.douyin import SocialMessage
        m = SocialMessage().parse(payload)
        emit({
            'ts': _ts(), 'type': 'follow',
            'user_id': str(getattr(m.user, 'id', '')), 'user_name': getattr(m.user, 'nick_name', ''),
            **self._common(m),
        })

    def _parseFansclubMsg(self, payload):
        from protobuf.douyin import FansclubMessage
        m = FansclubMessage().parse(payload)
        emit({'ts': _ts(), 'type': 'fansclub', 'text': m.content, **self._common(m)})

    # 其余类型全部静音，避免刷屏
    def _parseRoomUserSeqMsg(self, payload):
        from protobuf.douyin import RoomUserSeqMessage
        m = RoomUserSeqMessage().parse(payload)
        emit({'ts': _ts(), 'type': 'stats', 'online': int(m.total or 0),
              'total_view': int(getattr(m, 'total_pv_for_anchor', 0) or 0)})

    def _parseRoomStatsMsg(self, payload):
        pass

    def _parseRankMsg(self, payload):
        pass

    def _parseRoomMsg(self, payload):
        pass

    def _parseRoomStreamAdaptationMsg(self, payload):
        pass

    def _parseControlMsg(self, payload):
        from protobuf.douyin import ControlMessage
        m = ControlMessage().parse(payload)
        emit({'ts': _ts(), 'type': 'control', 'status': int(getattr(m, 'status', 0) or 0)})


def main():
    global ONLY
    argv = sys.argv[1:]
    for a in argv:
        if a.startswith('--only='):
            ONLY = {x.strip() for x in a.split('=', 1)[1].split(',') if x.strip()}
            ONLY.add('meta')
    pos = [a for a in argv if not a.startswith('--')]
    if not pos:
        log('用法: python collector.py <直播间号> [运行秒数] [--only=gift,chat]')
        sys.exit(2)
    live_id = pos[0]
    seconds = float(pos[1]) if len(pos) > 1 else 0

    log(f'[collector] 目标直播间 {live_id}' + (f' 过滤={sorted(ONLY)}' if ONLY else ''))
    log(f'[collector] 登录态：' + ('已注入 Cookie（可收礼物）' if (_USER_TTWID or _USER_EXTRA)
                                  else '游客态（收不到礼物消息）'))
    emit({'ts': _ts(), 'type': 'meta', 'event': 'start', 'room': live_id})

    if seconds > 0:
        import threading
        threading.Timer(seconds, lambda: (log(f'[collector] 到时退出 {seconds:.0f}s'), os._exit(0))).start()

    while True:
        try:
            EventFetcher(live_id).start()
        except Exception as e:
            log(f'[collector] 连接异常: {e!r}，5 秒后重试')
            emit({'ts': _ts(), 'type': 'meta', 'event': 'reconnect', 'error': str(e)})
            time.sleep(5)


if __name__ == '__main__':
    main()

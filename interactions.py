# coding: utf-8
"""点赞 / 关注的感谢话术。

两条原则，都是被真实直播场景逼出来的：

1. **每个人只谢一次**。抖音的点赞会反复推（一个人连点几十下），关注也可能重发。
   同一个 uid 从第二次起完全静默，否则观众一直点、机器人一直念，话全被占满。

2. **不能刷屏**。忽然涌入一批新人点赞时，感谢之间留最小间隔（默认 2 秒），
   撞上间隔的这条直接丢掉。宁可少谢一个人，也不能把礼物道谢挤到后面去。

配置在 interactions.json，改完不用重启（pipeline 每 200ms 检查 mtime）。

    from interactions import InteractionRules
    inter = InteractionRules()
    task, why = inter.feed(ev)
    if task:
        submit_task(task)
"""

import json
import os
import random
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 昵称清洗：只留中英文数字下划线，去掉表情、空格、标点
_NAME_OK = re.compile(r'[^\u4e00-\u9fffA-Za-z0-9_]')

# 兜底：配置文件丢了也能跑
DEFAULT = {
    "version": 1,
    "max_name_chars": 10,
    "like": {
        "enabled": True,
        "priority": 6,
        "min_gap_ms": 2000,
        "thanks": [
            "谢谢{user}的点赞，这一下点在我心巴上了！",
            "谢谢{user}的赞，我这就精神了，继续给你们整活。",
            "谢谢{user}的点赞，收到收到，比心。",
        ],
    },
    "follow": {
        "enabled": True,
        "priority": 8,
        "thanks": [
            "谢谢{user}的关注，以后就是自己人，常来玩啊。",
            "谢谢{user}的关注，我这就把你记下了，下次开播等你。",
            "谢谢{user}的关注，欢迎欢迎，这下咱们算认识了。",
        ],
    },
    "forbidden_words": ["傻", "蠢", "滚", "死", "妈的", "操"],
}


class InteractionRules:
    def __init__(self, path=None):
        self.path = path or os.path.join(HERE, 'interactions.json')
        self._mtime = 0
        self.cfg = dict(DEFAULT)
        self.like = dict(DEFAULT['like'])
        self.follow = dict(DEFAULT['follow'])
        self.max_name = 10
        self.forbidden = list(DEFAULT['forbidden_words'])
        # 已经谢过的人：like / follow 各自一套
        self._seen = {'like': set(), 'follow': set()}
        # 每个池上次抽中的句子，用来避免连着两次说同一句（和礼物话术同一套选法）
        self._last_pick = {}
        self._last_like_ms = -10 ** 9
        self.load()

    # ---------- 配置 ----------
    def load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError('顶层不是对象')
            like = cfg.get('like') if isinstance(cfg.get('like'), dict) else dict(DEFAULT['like'])
            follow = cfg.get('follow') if isinstance(cfg.get('follow'), dict) else dict(DEFAULT['follow'])
            max_name = int(cfg.get('max_name_chars', 10) or 10)
            forbidden = [w for w in (cfg.get('forbidden_words') or DEFAULT['forbidden_words']) if w]
            mtime = os.path.getmtime(self.path)
        except Exception as e:
            now = time.time()
            if now - getattr(self, '_warn_ts', 0) > 5:
                self._warn_ts = now
                print('  [点赞关注] 配置这次没读成功（%s），继续用上一份'
                      % type(e).__name__, flush=True)
            return False
        self.cfg = cfg
        self.like = like
        self.follow = follow
        self.max_name = max_name
        self.forbidden = forbidden
        self._mtime = mtime
        return True

    def maybe_reload(self):
        try:
            if os.path.getmtime(self.path) != self._mtime:
                return self.load()
        except Exception:
            pass
        return False

    # ---------- 处理事件 ----------
    def feed(self, ev, now_ms=None):
        """返回 (task 或 None, 原因)。

        原因用于打日志：ok / seen / gap / off / forbidden / empty / type
        """
        et = (ev.get('type') or '').strip()
        if et not in ('like', 'follow'):
            return None, 'type'
        cfg = self.like if et == 'like' else self.follow
        if cfg.get('enabled') is False:
            return None, 'off'

        uid = str(ev.get('user_id') or '').strip()
        if not uid:
            uid = 'name:' + str(ev.get('user_name') or '').strip()
        if not uid or uid == 'name:':
            return None, 'type'

        seen = self._seen[et]
        if uid in seen:                 # 不是第一次，静默
            return None, 'seen'

        now = now_ms if now_ms is not None else int(time.time() * 1000)
        if et == 'like':
            gap = int(cfg.get('min_gap_ms', 2000) or 0)
            if now - self._last_like_ms < gap:
                # 这次跳过就记下，避免他反复点时一直撞同一个窗口
                seen.add(uid)
                return None, 'gap'
            self._last_like_ms = now

        seen.add(uid)

        pool = [t for t in (cfg.get('thanks') or []) if isinstance(t, str) and t.strip()]
        if not pool:
            return None, 'empty'
        tpl = self._pick(et, pool)
        name = self.clean_name(ev.get('user_name'))
        text = (tpl.replace('{user}', name)
                   .replace('{count}', str(ev.get('count') or 1))).strip()
        if not text:
            return None, 'empty'
        if any(w in text for w in self.forbidden):
            return None, 'forbidden'
        return {'text': text,
                'priority': int(cfg.get('priority', 7)),
                'interrupt': False}, 'ok'

    # ---------- 小工具 ----------
    def _pick(self, key, pool):
        """从话术池里随机抽一句 —— 和礼物话术完全同一套选法。

        池子里有多句时，本轮会排除「这个池上次抽中的那句」，所以三种说法
        会自然轮着来，不会连着两次说同一句。一次事件只抽一句。
        """
        if not pool:
            return ''
        candidates = pool
        last = self._last_pick.get(key)
        if len(pool) > 1 and last is not None:
            candidates = [p for p in pool if p != last] or pool
        tpl = random.choice(candidates)
        self._last_pick[key] = tpl
        return tpl

    def clean_name(self, raw):
        name = _NAME_OK.sub('', (raw or '').strip()) or '这位朋友'
        name = name[:self.max_name]
        if any(w in name for w in self.forbidden):
            return '这位朋友'
        return name

    def stats(self):
        """给控制台显示：本场已经谢过多少人"""
        return {'like': len(self._seen['like']), 'follow': len(self._seen['follow'])}

    def for_get(self):
        """给控制台读的配置（不含内部状态）"""
        return {'like': self.like, 'follow': self.follow,
                'max_name_chars': self.max_name,
                'counts': self.stats()}


def _save(path, cfg):
    """控制台保存用（原子写，避免 pipeline 读到半截）"""
    from jsonio import write_json
    body = dict(cfg or {})
    body.setdefault('version', 1)
    body.setdefault('max_name_chars', 10)
    body.setdefault('forbidden_words', DEFAULT['forbidden_words'])
    return write_json(path, body)

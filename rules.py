#!/usr/bin/env python
# coding: utf-8
"""
礼物规则引擎：把 gift 事件变成"该说哪句话"。

它解决四个真问题：
1. 连击重复推送 —— 抖音连击时同一条礼物会推很多次，靠 msg_id 去重
2. 连击逐条播报 —— 同一人同一礼物等连击停顿后，汇总总数只播一次
3. 一个人刷屏霸占 —— 同一用户有播报冷却
4. 脏昵称/脏文案 —— 昵称清洗 + 输出侧黑名单兜底

用法：
    r = GiftRules('gifts.json')
    for task in r.feed(event, now_ms) + r.tick(now_ms):
        speaker.speak(task['text'], task['priority'], task['interrupt'])
"""

import json
import os
import random
import re
import time

# 昵称清洗：只保留中英文数字和下划线，去掉表情、空格、标点
_NAME_OK = re.compile(r'[^\u4e00-\u9fffA-Za-z0-9_]')


class GiftRules:
    def __init__(self, path=None):
        self.path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gifts.json')
        self._mtime = 0
        self._pending = {}
        self._seen = {}
        self._last_spoke = {}
        self._last_text = ''
        self._last_pick = {}   # 每个话术池各自记住上次抽中的那句，避免连着重复
        self._spoken = {}      # (用户, 礼物) -> (播报时刻, 当时的数量)，用于识别重复尾包
        # 先给一套默认值：配置文件读不出来时也要能跑，不能一启动就崩
        self.cfg = {}
        self.combo_window = 1800
        self.dup_window = 6000
        self.cooldown = 8000
        self.max_name = 10
        self.dedupe_ttl = 120000
        self.forbidden = []
        self.tiers = []
        self.load()

    # ---------- 配置热加载 ----------
    def load(self):
        """解析成功才落到 self 上。

        pipeline 每 200ms 会调一次，控制台保存时可能正读到半截文件；
        全部先在局部算好，最后一次性赋值，失败就保留上一份能用的配置。
        """
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError('顶层不是对象')
            combo_window = int(cfg.get('combo_window_ms', 1800))
            # 同一次礼物会被服务端推两条（间隔 2~4 秒、msg_id 却不同）。
            # 刚为「这个人 + 这个礼物」播过之后，这段窗口内没涨连击数的同礼物消息按重复处理。
            dup_window = int(cfg.get('same_gift_dedupe_ms', 6000))
            cooldown = int(cfg.get('cooldown_ms_per_user', 8000))
            max_name = int(cfg.get('max_name_chars', 10))
            dedupe_ttl = int(cfg.get('dedupe_ttl_ms', 120000))
            forbidden = [w for w in cfg.get('forbidden_words', []) if w]
            tiers = sorted(cfg.get('tiers', []),
                           key=lambda t: t.get('min_diamond', 0))
            mtime = os.path.getmtime(self.path)
        except Exception as e:
            now = time.time()
            if now - getattr(self, '_warn_ts', 0) > 5:
                self._warn_ts = now
                print('  [礼物配置] 这次没读成功（%s），继续用上一份，不影响播报'
                      % type(e).__name__, flush=True)
            return False
        self.cfg = cfg
        self._mtime = mtime
        self.combo_window = combo_window
        self.dup_window = dup_window
        self.cooldown = cooldown
        self.max_name = max_name
        self.dedupe_ttl = dedupe_ttl
        self.forbidden = forbidden
        self.tiers = tiers
        return True

    def maybe_reload(self):
        try:
            if os.path.getmtime(self.path) != self._mtime:
                return self.load()
        except Exception:
            pass
        return False

    # ---------- 工具 ----------
    def _clean_name(self, raw):
        name = _NAME_OK.sub('', (raw or '').strip()) or '这位朋友'
        name = name[:self.max_name]
        # 昵称本身带敏感词：换掉称呼，但不要丢掉整句感谢
        if any(w in name for w in self.forbidden):
            return '这位朋友'
        return name

    def _tier_for(self, diamond):
        for t in self.tiers:
            if t['min_diamond'] <= diamond <= t['max_diamond']:
                return t
        return self.tiers[0] if self.tiers else {}

    def _override_for(self, gift):
        """指定礼物的专属设置，优先于钻石档位。
        gift_overrides: {"玫瑰": {"templates": [...], "priority": 9, "label": "玫瑰专属"}}
        """
        ov = (self.cfg.get('gift_overrides') or {}).get(gift)
        return ov if isinstance(ov, dict) else None

    def _render(self, tier, user, gift, count, pool_key=''):
        """从话术池里随机抽一句。

        池子里有多句时，本轮会排除「这个池上次抽中的那句」，所以三种说法
        会真正轮着来，不会连着两次说同一句。
        """
        if count > 1 and self.cfg.get('combo_templates'):
            pool = self.cfg['combo_templates']
            pool_key = (pool_key or 'pool') + '|combo'
        else:
            pool = tier.get('templates') or []

        fallback = self.cfg.get('fallback', '谢谢{user}的{gift}！')
        pool = [p for p in pool if (p or '').strip()] or [fallback]

        candidates = pool
        last = self._last_pick.get(pool_key)
        if len(pool) > 1 and last is not None:
            candidates = [p for p in pool if p != last] or pool

        tpl = random.choice(candidates)
        if pool_key:
            self._last_pick[pool_key] = tpl
        return tpl.format(user=user, gift=gift, count=count)

    def _forbidden_hit(self, text):
        return any(w in text for w in self.forbidden)

    # ---------- 主流程 ----------
    def feed(self, event, now_ms):
        """收到一个 gift 事件。返回本次要立刻播报的任务列表（通常是空，等连击结束）。"""
        if event.get('type') != 'gift':
            return []

        msg_id = event.get('msg_id') or ''
        repeat_hint = int(event.get('repeat') or 1)
        if msg_id:
            # 连击时 msg_id 可能不变、只有 repeat_count 在涨；
            # 只有"同 msg_id 且 repeat 没涨"才算重复推送
            prev = self._seen.get(msg_id)
            if prev is not None and prev[1] >= repeat_hint:
                return []
            self._seen[msg_id] = (now_ms, repeat_hint)

        user_id = str(event.get('user_id') or '')
        gift_key = str(event.get('gift_id') or event.get('gift') or '')
        if not user_id or not gift_key:
            return []

        repeat = int(event.get('repeat') or 1)
        combo = int(event.get('combo') or 1)
        count = max(repeat, combo, 1)

        key = (user_id, gift_key)

        # 抖音会把同一次送礼推两条：一条是送礼瞬间，一条是连击结算，
        # 两条 msg_id 不同、数量都是 1，间隔 2~4 秒。结果就是「刷一个玫瑰谢两遍」。
        # 这里判断：刚为这个人的这个礼物播过，且数量没变多 → 是重复尾包，丢掉。
        prev_spoken = self._spoken.get(key)
        if prev_spoken and now_ms - prev_spoken[0] < self.dup_window \
                and count <= prev_spoken[1]:
            return []

        slot = self._pending.get(key)
        if slot:
            slot['count'] = max(slot['count'], count)
            slot['last_ts'] = now_ms
        else:
            self._pending[key] = {
                'count': count,
                'last_ts': now_ms,
                'diamond': int(event.get('diamond') or 0),
                'user': event.get('user_name') or '',
                'gift': event.get('gift') or '礼物',
                'user_id': user_id,
            }
        return []

    def tick(self, now_ms):
        """连击停顿的礼物，在这里结算成播报任务。"""
        ready = []
        for key in list(self._pending.keys()):
            slot = self._pending[key]
            if now_ms - slot['last_ts'] < self.combo_window:
                continue
            del self._pending[key]

            uid = slot['user_id']
            last = self._last_spoke.get(uid, 0)
            if now_ms - last < self.cooldown:
                # 冷却期内仍然要谢，但不打断别人：降到排队优先级，且不带打断
                pass

            tier = self._tier_for(slot['diamond'])
            ov = self._override_for(slot['gift'])
            user = self._clean_name(slot['user'])
            if ov and ov.get('templates'):
                # 指定礼物用专属话术，钻石档位只负责兜底
                text = self._render({'templates': ov['templates']}, user,
                                    slot['gift'], slot['count'],
                                    pool_key='gift:' + slot['gift'])
            else:
                text = self._render(tier, user, slot['gift'], slot['count'],
                                    pool_key='tier:' + str(tier.get('name', '')))

            if self._forbidden_hit(text):
                continue

            in_cooldown = (now_ms - last) < self.cooldown
            prio = int((ov or {}).get('priority') or tier.get('priority', 8))
            label = (ov or {}).get('label') or tier.get('name', '')
            if self.cfg.get('debug_gift_full_log'):
                print(f'  [规则] 命中:{label}{"（专属）" if ov else ""} p{prio} | '
                      f'礼物:{slot["gift"]} x{slot["count"]} | 话术:{text}', flush=True)
            ready.append({
                'text': text,
                'priority': prio,
                'interrupt': bool((ov or {}).get('interrupt',
                                                 tier.get('interrupt', True))) and not in_cooldown,
                'user': user,
                'gift': slot['gift'],
                'count': slot['count'],
                'diamond': slot['diamond'],
                'tier': label,
                'in_cooldown': in_cooldown,
                'source': f'礼物/{slot["gift"]}',
            })
            self._last_spoke[uid] = now_ms
            self._last_text = text
            # 记下"刚为这个人的这个礼物播过"，用于丢掉随后那条重复尾包
            self._spoken[key] = (now_ms, max(int(slot['count']), 1))

        # 清理过期去重表，避免长跑吃内存
        if len(self._seen) > 5000:
            cutoff = now_ms - self.dedupe_ttl
            self._seen = {k: v for k, v in self._seen.items() if v[0] >= cutoff}
        if len(self._spoken) > 2000:
            cutoff = now_ms - max(self.dup_window * 4, 60000)
            self._spoken = {k: v for k, v in self._spoken.items() if v[0] >= cutoff}
        return ready

    def flush_all(self, now_ms):
        """退出前把还没结算的礼物立刻吐出来。"""
        for slot in self._pending.values():
            slot['last_ts'] = 0
        return self.tick(now_ms)

#!/usr/bin/env python
# coding: utf-8
"""
弹幕阅读回复引擎

只有带唤醒标记（默认 #远仔）的弹幕才会被处理：
    观众：主播多大了            ← 没标记，只统计，不回复
    观众：#远仔 你多大了       ← 有标记，念出来并回答

回答来源按顺序：
    1. 预设问答库（关键词命中，零延迟零成本）
    2. 大模型 API（可选，OpenAI 兼容接口；不配 key 就跳过）
    3. 兜底话术（随机一句，保证不冷场）

回复是**串行**的：一个人说完、机器人播完，才轮到下一个人。
队列里等太久的会作废，避免答三分钟前的问题。

用法：
    python reply_engine.py "＃远仔 你多大了"        # 手动试一句
    python reply_engine.py --list                   # 看当前配置摘要
"""

import json
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.request

from experience import ExperienceStore
from knowledge import KnowledgeBase

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, 'replies.json')

# 全角/半角
_MARKER_ALIASES = ('#', '＃')

_weather_mod = None


def _weather():
    """懒加载天气模块；缺失或导入失败就当没有，不影响问答。"""
    global _weather_mod
    if _weather_mod is None:
        try:
            import weather as m
            _weather_mod = m
        except Exception:
            _weather_mod = False
    return _weather_mod or None


def _clean(text):
    return re.sub(r'\s+', ' ', (text or '')).strip()


def llm_key(cfg):
    """Key 来源：环境变量 → 项目内 secrets/llm_key（控制台里填了就写在这里）"""
    llm = (cfg or {}).get('llm') or {}
    key = os.environ.get(llm.get('api_key_env') or 'ROBOTLIVE_LLM_KEY', '').strip()
    if key:
        return key
    p = llm.get('api_key_file') or os.path.join(HERE, 'secrets', 'llm_key')
    try:
        if os.path.isfile(p):
            return open(p, 'r', encoding='utf-8').read().strip()
    except OSError:
        pass
    return ''


def llm_complete(cfg, system, user, timeout=None):
    """一次 OpenAI 兼容的 chat/completions 调用；没配好就返回空串。

    问答和建议生成共用这一份，避免两处各写一套 Key / 代理 / 超时逻辑。
    """
    llm = (cfg or {}).get('llm') or {}
    key = llm_key(cfg)
    base = (llm.get('base_url') or '').rstrip('/')
    if not key or not base:
        return ''
    body = json.dumps({
        'model': llm.get('model') or 'deepseek-chat',
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        'temperature': float(llm.get('temperature', 0.85)),
        'max_tokens': int(llm.get('max_tokens', 120)),
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(
        base + '/chat/completions', data=body,
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + key})
    # 跟随系统代理：国内直连 api.deepseek.com 实测要 12 秒，走代理只要 0.8 秒
    opener = urllib.request.build_opener()
    with opener.open(req, timeout=float(timeout or llm.get('timeout', 12))) as r:
        data = json.loads(r.read().decode('utf-8', 'replace'))
    choices = data.get('choices') or []
    if not choices:
        return ''
    return (choices[0].get('message') or {}).get('content') or ''


class ReplyEngine:
    def __init__(self, path=None, knowledge=None, experience=None):
        self.path = path or CFG_PATH
        self._mtime = 0
        self.cfg = {}
        self._last_user = {}      # uid -> 上次回复时刻
        self._last_answer = {}    # uid -> 上次回答文本（避免同一人连续得到同句）
        # 知识库和经验库默认跟 replies.json 放同一目录：
        # 测试传临时配置时天然隔离，不会污染真实数据。
        base = os.path.dirname(os.path.abspath(self.path)) or HERE
        self.knowledge = knowledge if knowledge is not None else \
            KnowledgeBase(os.path.join(base, 'knowledge.md'))
        self.experience = experience if experience is not None else \
            ExperienceStore(os.path.join(base, 'experience.json'),
                            knowledge=self.knowledge,
                            cfg_getter=lambda: self.cfg)
        self.load()

    # ---------- 配置 ----------
    def load(self):
        """解析成功才动 self。

        pipeline 每 200ms 调一次，控制台保存时文件可能正被写到一半；
        读坏了就保留上一份能用的配置，绝不能让半截 JSON 打断整条管线。
        """
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError('顶层不是对象')
        except Exception as e:
            now = time.time()
            if now - getattr(self, '_warn_ts', 0) > 5:
                self._warn_ts = now
                print('  [问答配置] 这次没读成功（%s），继续用上一份，回复不受影响'
                      % type(e).__name__, flush=True)
            return False
        self.cfg = cfg
        self._mtime = os.path.getmtime(self.path)
        self.enabled = bool(self.cfg.get('enabled', True))
        self.marker = self.cfg.get('marker', '#远仔')
        # 唤醒词：除了带 # 的标记，还认「远仔远仔 问题」「远仔 问题」这种口语叫法。
        # marker 里的词会自动加入唤醒词，所以把标记改成 #远仔 就自动支持「远仔远仔」。
        words = [w.strip() for w in (self.cfg.get('wake_words') or []) if w and w.strip()]
        bare = self.marker.replace('＃', '#').lstrip('#').strip()
        if bare and bare not in words:
            words.append(bare)
        # 太短的词（1 个字）容易误触发，只保留强标记
        self.wake_words = sorted({w for w in words if len(w) >= 2}, key=len, reverse=True)
        self.wake_anywhere = bool(self.cfg.get('wake_anywhere', False))
        # 口语唤醒至少要连喊几遍才认（默认 2：认「远仔远仔」，不认单次「远仔」，
        # 免得观众随口提到名字就被当成提问）
        self.wake_min_repeat = max(1, int(self.cfg.get('wake_min_repeat', 2) or 1))
        self.priority = int(self.cfg.get('priority', 6))
        self.interrupt = bool(self.cfg.get('interrupt', False))
        # 同一个人多久内只回一次（默认 5 分钟）
        self.user_cd = int(self.cfg.get('per_user_cooldown_ms', 300000))
        self.max_q = int(self.cfg.get('max_question_chars', 60))
        self.max_a = int(self.cfg.get('max_reply_chars', 60))
        self.forbidden = [w for w in self.cfg.get('forbidden_in_question', []) if w]
        self.presets = self.cfg.get('presets', []) or []
        # 自我介绍：配几段，手动挑一段说，或弹幕问到时随机挑一段
        self.intros = [x for x in (self.cfg.get('intros') or []) if x]
        self.intro_keys = [k for k in (self.cfg.get('intro_keys') or []) if k]
        self._last_intro = ''
        # 串行队列
        self.max_pending = int(self.cfg.get('max_pending', 6))
        self.stale_ms = int(self.cfg.get('queue_stale_ms', 60000))
        self.est_ms_per_char = int(self.cfg.get('est_ms_per_char', 300))
        self.est_base_ms = int(self.cfg.get('est_base_ms', 900))
        # 念问题时最多念多少字（太长观众听着累，也拖慢下一个人）
        self.speak_q_max = int(self.cfg.get('speak_question_max_chars', 26))
        # 一次播报总字数上限（问题+回答），0 表示不限
        self.speak_total_max = int(self.cfg.get('max_speak_chars', 64) or 0)
        self.speak_trunc = self.cfg.get('speak_truncate_suffix', '……')
        return True

    def maybe_reload(self):
        changed = False
        try:
            if os.path.getmtime(self.path) != self._mtime:
                changed = self.load()
        except Exception:
            pass
        # 知识库/经验库也可能被控制台或手工改过，顺带刷新
        if self.knowledge is not None:
            try:
                self.knowledge.maybe_reload()
            except Exception:
                pass
        if self.experience is not None:
            try:
                self.experience.maybe_reload()
            except Exception:
                pass
        return changed

    # ---------- 标记识别 ----------
    def parse(self, text):
        """返回 (算不算在叫我, 去掉唤醒词后的问题)

        三种叫法都认：
            #远仔 你多大了        ← 带标记，位置不限
            远仔远仔 你多大了      ← 口语重复唤醒（句首）
            远仔 你多大了          ← 口语单次唤醒（句首）
        """
        t = _clean(text)
        if not t:
            return False, ''
        norm = t.replace('＃', '#')

        # 1) 强标记：#远仔 / # 远仔，出现即算，位置不限
        mk = self.marker.replace('＃', '#').strip()
        if mk:
            pat = re.escape(mk)
            if mk.startswith('#'):
                pat = r'#\s*' + re.escape(mk[1:])
            m = re.search(pat, norm, re.I)
            if m:
                return True, self._strip_q(norm[:m.start()] + ' ' + norm[m.end():])

        # 2) 唤醒词在句首，可以连着喊几遍（远仔远仔）
        hit, rest, times = self._peel_wake(norm)
        if hit and times >= self.wake_min_repeat:
            return True, self._strip_q(rest)

        # 3) 可选：唤醒词出现在句子中间（默认关，免得聊到名字就插嘴）
        if self.wake_anywhere:
            for w in self.wake_words:
                m = re.search(re.escape(w), norm, re.I)
                if m:
                    return True, self._strip_q(norm[:m.start()] + ' ' + norm[m.end():])
        return False, ''

    def _peel_wake(self, s):
        """把句首连续的唤醒词一个个剥掉，返回 (剥到过吗, 剩下的部分, 剥了几遍)"""
        low = s
        pos, n = 0, 0
        while True:
            m = re.match(r'[\s，,。.!！?？、:：~～\-]*', low[pos:])
            p = pos + m.end()
            hit = ''
            for w in self.wake_words:
                if low[p:p + len(w)].lower() == w.lower():
                    hit = w
                    break
            if not hit:
                break
            pos = p + len(hit)
            n += 1
        return n > 0, low[pos:], n

    def _strip_q(self, s):
        """去掉唤醒词后清理问题开头：残留的重复唤醒词、@提及、标点"""
        s = re.sub(r'@\S+', ' ', s)          # @某人 整体删掉，别只剩个名字
        q = re.sub(r'^[\s，,。.!！?？、:：~～\-]+', '', s.strip())
        changed = True
        while changed:
            changed = False
            for w in self.wake_words:
                if q[:len(w)].lower() == w.lower():
                    q = q[len(w):].lstrip(' ，,。.!！?？、:：~～-')
                    changed = True
                    break
        return q.strip()

    # ---------- 回答 ----------
    def _intro_answer(self, q):
        """弹幕问「自我介绍」时，随机挑一段（避免连续两次同一条）。"""
        if not self.intros or not self.intro_keys:
            return ''
        if not any(k in q for k in self.intro_keys):
            return ''
        pool = [x for x in self.intros if x != self._last_intro] or self.intros
        pick = random.choice(pool)
        self._last_intro = pick
        return pick

    def _preset_answer(self, q):
        for item in self.presets:
            keys = item.get('keys') or []
            if any(k and k in q for k in keys):
                return item.get('reply') or ''
        return ''

    def _llm_answer(self, q):
        llm = self.cfg.get('llm') or {}
        if not llm.get('enabled'):
            return ''
        system = llm.get('system') or (
            '你是直播机器人「远仔」，人设：戏精损友型，热情嘴碎、爱演会接梗。'
            '观众在直播间用弹幕向你提问，用一到两句话回答，口语化、有梗但不低俗。'
            '绝不复述观众原话里可能有的脏话或敏感内容；碰到政治、色情、暴力、'
            '人身攻击、医疗诊断、投资建议，就绕开并换话题。只输出要说的话。'
        )
        extra = self._context_block(q)
        if extra:
            system = system + '\n\n' + extra
        return llm_complete(self.cfg, system, q[:200])

    def _context_block(self, q):
        """把知识库和相似历史问答拼进提示词；任何异常都不影响正常回答。"""
        parts = []
        if self.knowledge is not None:
            try:
                ctx = self.knowledge.context(q)
                if ctx:
                    parts.append('【可参考的设定与知识】（不要逐字念，融进回答里）\n' + ctx)
            except Exception:
                pass
        if self.experience is not None:
            try:
                shots = self.experience.few_shot(q, limit=2)
                if shots:
                    lines = ['【以前类似问题的回答】']
                    for question, answer in shots:
                        lines.append('- 问：%s → 答：%s' % (question, answer))
                    parts.append('\n'.join(lines))
            except Exception:
                pass
        return '\n\n'.join(parts)

    def _is_weather_question(self, q):
        wcfg = self.cfg.get('weather') or {}
        wmod = _weather() if wcfg.get('enabled', True) else None
        if wmod is None:
            return False
        try:
            return bool(wmod.is_weather_question(q))
        except Exception:
            return False

    def _fallback(self, uid):
        pool = [x for x in (self.cfg.get('fallback') or []) if x] or [
            '这个我一时没答上来，你换个问法试试？']
        last = self._last_answer.get(uid)
        cand = [x for x in pool if x != last] or pool
        return random.choice(cand)

    def answer(self, q, uid=''):
        """返回 (要说的话, 来源)"""
        q = _clean(q)[:self.max_q]
        if not q:
            return self.cfg.get('ask_when_empty', '你想问我啥呀？说清楚点呗。'), 'empty'

        if any(w in q for w in self.forbidden):
            return self.cfg.get('refuse_reply', '这个话题我就不接啦，换个别的问我。'), 'blocked'

        text, source = '', 'fallback'
        try:
            # 自我介绍优先：问「介绍一下你自己」时直接说配置好的那段
            text = self._intro_answer(q)
            if text:
                source = 'intro'
        except Exception:
            pass

        # 知识直答优先于预设：采纳过的高频问题要能命中，否则会被宽匹配的预设抢走。
        # 天气类问题即使命中了旧条目，也要走下面的实时查询，不能拿过期知识糊弄。
        if not text and self.knowledge is not None:
            try:
                hit = self.knowledge.direct(q)
                if hit and not self._is_weather_question(q):
                    body = (hit.get('body') or '').strip()
                    if body:
                        text = body
                        source = 'knowledge'
            except Exception:
                pass

        if not text:
            try:
                text = self._preset_answer(q)
                if text:
                    source = 'preset'
            except Exception:
                pass

        if not text:
            # 问天气的走实时数据（预设答不了这个，模型也不知道今天几度）
            wcfg = self.cfg.get('weather') or {}
            wmod = _weather() if wcfg.get('enabled', True) else None
            if wmod is not None:
                try:
                    if wmod.is_weather_question(q):
                        text, _city = wmod.answer(
                            q, wcfg.get('default_city', ''),
                            timeout=float(wcfg.get('timeout', 6)),
                            cache_seconds=int(wcfg.get('cache_seconds', 300)))
                        if text:
                            source = 'weather'
                except Exception as e:
                    print('  [弹幕回复] 天气查询失败：%s' % type(e).__name__, flush=True)

        if not text:
            try:
                text = self._llm_answer(q)
                if text:
                    source = 'llm'
            except Exception as e:
                print('  [弹幕回复] 模型调用失败：%s' % type(e).__name__, flush=True)

        if not text:
            text = self._fallback(uid)

        text = _clean(text).strip('"“”\'')
        if len(text) > self.max_a:
            cut = max(text.rfind(p, 0, self.max_a) for p in '，。！？,!.?')
            text = (text[:cut] if cut > 8 else text[:self.max_a]).rstrip('，,')
        return text, source

    # ---------- 播报文本拼装 ----------
    _AT_URL = re.compile(r'@\S+|https?://\S+|www\.\S+')
    _EMOJI = re.compile(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u2B00-\u2BFF]')

    def speakable_question(self, q):
        """把弹幕原文变成能念出口的问句：去掉 @、链接、表情、碎符号，太长就截。"""
        q = _clean(q)
        if not q:
            return ''
        q = self._AT_URL.sub(' ', q)
        q = self._EMOJI.sub('', q)
        q = re.sub(r'[【】\[\]（）()<>《》「」『』]', '，', q)
        q = re.sub(r'[~^*_+=|\\/]{1,}', '', q)
        q = _clean(q)
        # 中文之间的空格当逗号断句，念起来才像话
        q = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '，', q)
        q = re.sub(r'[，,、]{2,}', '，', q)
        q = re.sub(r'[，,、]+(?=[。！？!?…])', '', q)
        q = q.strip(' ，,。.、:：;；!！?？"“”\'')
        if not q:
            return ''
        limit = self.speak_q_max
        if limit > 0 and len(q) > limit:
            cut = max(q.rfind(p, 0, limit) for p in '，。！？,!.? ')
            q = (q[:cut] if cut > 6 else q[:limit]).rstrip('，,、 ')
            q += self.speak_trunc
        return q

    def compose(self, item, answer_text=None):
        """返回真正要念的全文。

        前缀模板支持 {user} 和 {question}：
            "{user}问，{question}。"     → 陈家人问，你多大了。
        老配置里没有 {question} 的（比如 "{user}问，"），自动把问题补在后面，
        避免机器人只念一句"某某问"就没了。
        """
        name = (item.get('name') or '朋友')[:10]
        name = self._EMOJI.sub('', name).strip(' 　') or '朋友'
        q = self.speakable_question(item.get('question') or '')
        text = answer_text if answer_text is not None else (item.get('text') or '')
        text = (text or '').strip()

        tmpl = self.cfg.get('speak_prefix') or ''
        if not q:
            # 只喊了名字没问内容：别念"某某问，。"这种半句话
            empty_t = self.cfg.get('speak_prefix_empty', '{user}，')
            prefix = empty_t.replace('{user}', name)
        else:
            t = tmpl or '{user}问，'
            if '{question}' in t:
                prefix = t.replace('{user}', name).replace('{question}', q)
            else:
                prefix = t.replace('{user}', name) + q + '。'

        # 总长控制：答案太长就砍答案，保证问题能念完
        limit = self.speak_total_max
        if limit and len(prefix) + len(text) > limit:
            keep = max(14, limit - len(prefix))
            if len(text) > keep:
                cut = max(text.rfind(p, 0, keep) for p in '，。！？,!.? ')
                text = (text[:cut] if cut > 8 else text[:keep]).rstrip('，,、 ')
                if not text.endswith(('。', '！', '？', '!', '?')):
                    text += '。'
        return self._tidy_speak(prefix + text)

    @staticmethod
    def _tidy_speak(s):
        """收尾：删掉「……。」「。。」这类叠标点，TTS 念起来才不出怪音。"""
        s = s.replace('……。', '……，').replace('…。', '…，')
        s = re.sub(r'([，。！？、])\1+', r'\1', s)
        s = re.sub(r'(…){3,}', '……', s)
        s = re.sub(r'……，{2,}', '……，', s)
        s = re.sub(r'[，,、]{2,}', '，', s)
        return s.strip()

    # ---------- 入口：只判断是否该回，回答与播报交给 ReplyWorker ----------
    def feed(self, event, now_ms):
        """返回 (待回复条目 | None, 说明)"""
        if not self.enabled:
            return None, '关闭'
        if event.get('type') != 'chat':
            return None, ''

        has, q = self.parse(event.get('text') or '')
        if not has:
            return None, ''

        uid = str(event.get('user_id') or '')
        name = (event.get('user_name') or '朋友')[:10]

        blocked = any(w in q for w in self.forbidden)
        # 先记账再判冷却：被冷却挡掉的提问也是真实需求，频次不能漏
        if not blocked and self.experience is not None:
            try:
                self.experience.record(q, uid, name)
            except Exception:
                pass

        wait = self.user_cd - (now_ms - self._last_user.get(uid, 0))
        if wait > 0:
            return None, '同一人冷却中（还剩 %d 秒）' % int(wait / 1000 + 0.5)

        if blocked:
            return {'uid': uid, 'name': name, 'question': q,
                    'text': self.cfg.get('refuse_reply', '这个话题我就不接啦，换个别的问我。'),
                    'source': 'blocked', 'ts': now_ms}, 'ok:blocked'

        # 先占住这个人的冷却，避免他连发几条排进队列
        self._last_user[uid] = now_ms
        return {'uid': uid, 'name': name, 'question': q,
                'text': '', 'source': '', 'ts': now_ms}, 'queued'

    def build_task(self, item):
        """把队列条目变成播报任务（回答在这里生成，不阻塞采集）"""
        text = item.get('text') or ''
        source = item.get('source') or ''
        if not text:
            text, source = self.answer(item['question'], item['uid'])
        if self.experience is not None:
            try:
                self.experience.record_answer(item['question'], text, source)
            except Exception:
                pass
        item = dict(item, text=text)
        full = self.compose(item)
        est = self.est_base_ms + len(full) * self.est_ms_per_char
        return {
            'text': full,
            'priority': self.priority,
            'interrupt': self.interrupt,
            'user': item['name'],
            'gift': '',
            'count': 1,
            'diamond': 0,
            'tier': '弹幕回复',
            'source': '弹幕/%s/%s' % (item['name'], source),
            'question': item['question'],
            'est_ms': est,
        }, source

    def accept_suggestion(self, cid, text=None, keywords=None):
        """采纳一条建议：写进 knowledge.md，并把经验库状态标记为已采纳。"""
        if self.experience is None or self.knowledge is None:
            return False, '知识库/经验库未启用', {}
        entry = self.experience.accept(cid, text, keywords)
        if not entry:
            return False, '这条还没有可采纳的回答', {}
        try:
            self.knowledge.upsert(entry)
        except Exception as e:
            return False, '写知识库失败：%s' % e, {}
        conflict = self._preset_conflict(entry)
        msg = '已采纳，之后同类问题直接回答'
        if conflict:
            msg += '（注意：该问题也会命中预设「%s」，现在知识优先）' % conflict
        return True, msg, {'entry': entry, 'preset_conflict': conflict}

    def _preset_conflict(self, entry):
        """提醒：采纳的知识会先于预设命中，别让两边话术打架。"""
        keys = [k for k in (entry.get('keywords') or []) if k]
        for item in self.presets:
            for k in (item.get('keys') or []):
                if not k:
                    continue
                for mine in keys:
                    if k in mine or mine in k:
                        return k
        return ''


class ReplyWorker(threading.Thread):
    """串行回复：一个人说完，机器播完，才轮到下一个。"""

    def __init__(self, engine, submit, log=print):
        super().__init__(daemon=True)
        self.engine = engine
        self.submit = submit
        self.log = log
        self.q = queue.Queue()
        self.stop_flag = False
        self.busy_until = 0.0   # 当前这条预计播完的时刻
        self.replied = 0
        self.dropped = 0
        self.pending = 0

    def enqueue(self, item):
        if self.q.qsize() >= self.engine.max_pending:
            self.dropped += 1
            self.log('  [弹幕回复] 队列已满（%d 条在等），丢掉：%s'
                     % (self.q.qsize(), item.get('question') or '')[:80])
            return False
        item['enq_at'] = time.time()
        self.q.put(item)
        self.pending = self.q.qsize()
        return True

    def run(self):
        while not self.stop_flag:
            try:
                item = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            self.pending = self.q.qsize()

            # 等上一条真正播完（按字数估算）
            wait = self.busy_until - time.time()
            while wait > 0 and not self.stop_flag:
                time.sleep(min(wait, 0.2))
                wait = self.busy_until - time.time()

            # 等太久的就不答了：三分钟前的问题答了反而尴尬
            age = (time.time() - item.get('enq_at', time.time())) * 1000
            if age > self.engine.stale_ms:
                self.dropped += 1
                self.log('  [弹幕回复] 排队太久（%.0f 秒），跳过：%s'
                         % (age / 1000, item.get('question') or ''))
                continue

            try:
                task, source = self.engine.build_task(item)
            except Exception as e:
                self.log('  [弹幕回复] 生成回答失败：%s: %s' % (type(e).__name__, e))
                continue

            if not task['text']:
                continue
            self.submit(task)
            self.replied += 1
            self.busy_until = time.time() + task.get('est_ms', 2000) / 1000.0
            self.pending = self.q.qsize()
            self.log('  [弹幕回复] ok:%s | 问：%s → 说：%s（排队 %d）'
                     % (source, item.get('question') or '-', task['text'],
                        self.q.qsize()))

    def stop(self):
        self.stop_flag = True


def main():
    eng = ReplyEngine()
    if '--list' in sys.argv:
        print('启用：', eng.enabled)
        print('标记：', eng.marker)
        print('预设条目：', len(eng.presets))
        print('模型：', '已开启' if (eng.cfg.get('llm') or {}).get('enabled') else '未开启（走预设+兜底）')
        print('同一人冷却：%.0f 秒' % (eng.user_cd / 1000))
        print('队列上限：%d 条 / 超过 %.0f 秒作废' % (eng.max_pending, eng.stale_ms / 1000))
        for p in eng.presets:
            print('   -', '/'.join(p.get('keys') or [])[:40], '=>', (p.get('reply') or '')[:40])
        return

    text = ' '.join(a for a in sys.argv[1:] if not a.startswith('--'))
    if not text:
        print('用法: python reply_engine.py "＃远仔 你多大了"')
        return
    has, q = eng.parse(text)
    print('识别标记：', has, '| 问题：', q)
    if not has:
        print('→ 这条不会回复（没有标记）')
        return
    ans, src = eng.answer(q, 'test')
    print('来源：', src)
    print('会说：', eng.compose({'name': '测试用户', 'question': q, 'text': ans}))


if __name__ == '__main__':
    main()

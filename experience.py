#!/usr/bin/env python
# coding: utf-8
"""高频问题经验库：统计同一问题被问了多少次，够次数就让大模型给一版更好的回答。

只做三件事：
    1. 把弹幕问题归一化后聚类计数（纯内存 + 定时原子落盘）
    2. 同一簇达到 min_count 次，后台线程调一次大模型生成「建议回答」
    3. 控制台人工采纳后，由 ReplyEngine 写进 knowledge.md，之后同题直答

数据写在 experience.json，只有管线进程会写它；控制台通过 8771 端口代理读写。
不落盘也不影响直播：最坏情况只是这次的计数丢失。
"""

import copy
import json
import os
import re
import threading
import time

from jsonio import write_json
from knowledge import norm, make_entry

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, 'experience.json')

DEFAULT = {
    'version': 1,
    'enabled': True,
    'min_count': 3,
    'merge_threshold': 0.72,
    'suggest_cooldown_s': 1800,
    'max_clusters': 200,
    'max_samples': 5,
    'max_answers': 5,
    'archive_days': 30,
    'max_reply_chars': 60,
    'clusters': [],
}

# 观众提问常见的称呼/开场，归一化时剥掉，让「你多大了」和「主播多大了」落进同一簇
_PREFIX = ['请问一下', '请问', '想问一下', '想问', '问一下', '我想问',
           '主播', '远仔', '你们', '大家', '老师', '宝宝', '你', '我', '那个', '这个']
# 句尾语气词，剥掉后同一句的不同说法归一
_SUFFIX = '吗呢啊吧呀啦嘛哦喔呗了'
_SUFFIX_RE = re.compile('[%s]+$' % _SUFFIX)
_NEG = '不没别无非'

# 同义说法：只收不会改变语义的（「多少→几」会把「多少钱」改成「几钱」，所以不做）
_SYNONYMS = [('多大', '几岁'), ('年纪', '几岁'), ('年龄', '几岁'), ('几岁', '几岁')]


def _strip_prefix(s):
    """剥掉句首称呼/开场词（「主播」「你」「这个」等）。"""
    changed = True
    while changed:
        changed = False
        for p in _PREFIX:
            if s.startswith(p) and len(s) > len(p):
                s = s[len(p):]
                changed = True
                break
    return s


def qnorm(text):
    """问题归一化：称呼/语气词/同义说法统一，用于聚类。"""
    s = _strip_prefix(norm(text))
    for a, b in _SYNONYMS:
        s = s.replace(a, b)
    s = _SUFFIX_RE.sub('', s)
    return s


def _has_neg(s):
    return any(c in s for c in _NEG)


def _similar(a, b, max_len_gap=2):
    """两个归一化问题的相似度判定，带否定守卫。"""
    from knowledge import bigrams, dice
    if not a or not b:
        return 0.0
    if abs(len(a) - len(b)) > max_len_gap:
        return 0.0
    if _has_neg(a) != _has_neg(b):
        return 0.0      # 「吃了吗」和「没吃吗」不能合成一簇
    return dice(bigrams(a), bigrams(b))


def _keywords_from(canonical, samples):
    """从代表句和变体里抽关键词。

    只取归一化后的核心词会丢掉观众真正的问法，所以三种都留：
    完整问法、去称呼/语气词的核心、去掉同义替换前的短语。
    例：「你多大了」→ 你多大了 / 几岁 / 多大，这样「主播多大了」也能命中。
    """
    out = []

    def add(k):
        if len(k) >= 2 and k not in out:
            out.append(k)

    for s in [canonical] + list(samples or []):
        full = norm(s)
        add(full)
        add(qnorm(s))
        mid = _SUFFIX_RE.sub('', _strip_prefix(full))
        add(mid)
        if len(mid) >= 5:      # 「这个产品多少钱」→ 多少钱
            add(mid[-3:])
        if len(out) >= 6:
            break
    return out[:6]


class ExperienceStore:
    def __init__(self, path=None, knowledge=None, llm_fn=None, cfg_getter=None):
        self.path = path or PATH
        self.knowledge = knowledge
        self._llm_fn = llm_fn
        self._cfg_getter = cfg_getter
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = False
        self._started = False
        self._thread = None
        self._dirty = False
        self._warn_ts = 0
        self._mtime = 0
        self.cfg = dict(DEFAULT)
        self.clusters = []
        self.load()

    # ---------- 加载与落盘 ----------
    def load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError('顶层不是对象')
        except FileNotFoundError:
            self.cfg = dict(DEFAULT)
            self.clusters = []
            self._mtime = 0
            return True
        except Exception as e:
            now = time.time()
            if now - self._warn_ts > 5:
                self._warn_ts = now
                print('  [经验库] 这次没读成功（%s），继续用上一份' % type(e).__name__,
                      flush=True)
            return False
        self.cfg = dict(DEFAULT, **cfg)
        self.clusters = [c for c in (cfg.get('clusters') or []) if isinstance(c, dict)]
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = 0
        return True

    def maybe_reload(self):
        """外部改过经验文件才重载；本地有未落盘的计数时不覆盖。"""
        if self._dirty:
            return False
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return False
        if m == self._mtime:
            return False
        return self.load()

    def _snapshot(self):
        cfg = dict(self.cfg)
        # 深拷贝：落盘在锁外进行，避免 json.dumps 遍历时有别的线程改计数
        cfg['clusters'] = copy.deepcopy(self.clusters)
        return cfg

    def flush(self, force=False):
        with self._lock:
            if not force and not self._dirty:
                return True
            cfg = self._snapshot()
            self._dirty = False
        try:
            write_json(self.path, cfg)
            try:
                self._mtime = os.path.getmtime(self.path)
            except OSError:
                pass
            return True
        except Exception as e:
            with self._lock:
                self._dirty = True
            now = time.time()
            if now - self._warn_ts > 5:
                self._warn_ts = now
                print('  [经验库] 落盘失败：%s' % type(e).__name__, flush=True)
            return False

    # ---------- 统计 ----------
    def _match(self, n):
        """先精确匹配归一化串，再找相似度最高且过阈值的一簇。"""
        best, best_s = None, 0.0
        thr = float(self.cfg.get('merge_threshold', 0.72) or 0.72)
        for c in self.clusters:
            cn = c.get('norm') or ''
            if cn == n:
                return c
            s = _similar(n, cn)
            if s >= thr and s > best_s:
                best, best_s = c, s
        return best

    def _new_cluster(self, q, n):
        return {
            'id': 'c_%x' % int(time.time() * 1000000),
            'canonical': (q or '').strip()[:40],
            'norm': n,
            'count': 0,
            'samples': [],
            'answers': [],
            'status': 'open',
            'suggestion': None,
            'last_suggest_ts': 0,
            'accepted_entry_id': '',
            'last_ts': 0,
        }

    def record(self, q, uid='', name=''):
        """记一次提问。不写盘，由后台线程每 5 秒批量落一次。"""
        if not self.cfg.get('enabled', True):
            return None
        raw = (q or '').strip()
        n = qnorm(raw)
        if not n:            # 纯标点/表情；单字问题照常计数，只是不会触发建议
            return None
        now = int(time.time() * 1000)
        max_s = int(self.cfg.get('max_samples', 5) or 5)
        with self._lock:
            c = self._match(n)
            if c is None:
                c = self._new_cluster(raw, n)
                self.clusters.append(c)
            c['count'] = int(c.get('count') or 0) + 1
            c['last_ts'] = now
            if raw and raw not in (c.get('samples') or []):
                samples = list(c.get('samples') or [])
                samples.append(raw)
                c['samples'] = samples[-max_s:]
            self._dirty = True
            return dict(c)

    def record_answer(self, q, text, source=''):
        """把实际说出的回答挂到簇上，生成建议时参考（不重复记同一句）。"""
        text = (text or '').strip()
        if not text or source in ('fallback', 'blocked', 'empty'):
            return
        n = qnorm(q)
        if not n:
            return
        max_a = int(self.cfg.get('max_answers', 5) or 5)
        with self._lock:
            c = self._match(n)
            if c is None:
                return
            answers = list(c.get('answers') or [])
            if text not in answers:
                answers.append(text)
                c['answers'] = answers[-max_a:]
                self._dirty = True

    def few_shot(self, q, limit=2):
        """从已采纳的问题里找最像的几条，给大模型当风格参考。"""
        n = qnorm(q)
        if not n:
            return []
        out = []
        with self._lock:
            scored = []
            for c in self.clusters:
                if c.get('status') != 'accepted':
                    continue
                answer = (c.get('suggestion') or {}).get('text') or ''
                if not answer:
                    answers = c.get('answers') or []
                    answer = answers[-1] if answers else ''
                if not answer:
                    continue
                s = _similar(n, c.get('norm') or '')
                if s >= 0.5:
                    scored.append((s, c.get('canonical') or '', answer))
            scored.sort(key=lambda x: x[0], reverse=True)
            for _s, question, answer in scored[:limit]:
                out.append((question, answer))
        return out

    def all_clusters(self):
        """按次数倒序返回簇副本，供控制台展示。"""
        with self._lock:
            out = []
            for c in sorted(self.clusters, key=lambda x: (x.get('count') or 0,
                                                          x.get('last_ts') or 0),
                            reverse=True):
                d = dict(c)
                d['samples'] = list(c.get('samples') or [])
                d['answers'] = list(c.get('answers') or [])
                out.append(d)
            return out

    def stats(self):
        with self._lock:
            pending = sum(1 for c in self.clusters if c.get('status') == 'suggested')
            total = sum(int(c.get('count') or 0) for c in self.clusters)
            return {'clusters': len(self.clusters), 'pending': pending,
                    'asks': total, 'enabled': bool(self.cfg.get('enabled', True))}

    def settings(self):
        with self._lock:
            return {k: self.cfg.get(k) for k in
                    ('enabled', 'min_count', 'merge_threshold',
                     'suggest_cooldown_s', 'max_reply_chars')}

    def update_settings(self, body):
        """只接受白名单字段，别让一次误提交把整份配置冲掉。"""
        with self._lock:
            for k, lo, hi in (('enabled', None, None), ('min_count', 2, 50),
                              ('merge_threshold', 0.4, 0.95),
                              ('suggest_cooldown_s', 60, 86400),
                              ('max_reply_chars', 20, 120)):
                if k not in body:
                    continue
                v = body[k]
                if k == 'enabled':
                    self.cfg[k] = bool(v)
                elif k == 'merge_threshold':
                    self.cfg[k] = min(hi, max(lo, float(v)))
                else:
                    self.cfg[k] = min(hi, max(lo, int(v)))
            self._dirty = True
        self.flush(force=True)
        return self.settings()

    # ---------- 采纳 / 忽略 ----------
    def get(self, cid):
        with self._lock:
            for c in self.clusters:
                if c.get('id') == cid:
                    return dict(c)
            return None

    def accept(self, cid, text=None, keywords=None):
        """返回要写进知识库的条目；状态同步为 accepted。"""
        with self._lock:
            c = None
            for x in self.clusters:
                if x.get('id') == cid:
                    c = x
                    break
            if c is None:
                return None
            body = (text or ((c.get('suggestion') or {}).get('text')) or '').strip()
            if not body:
                return None
            body = body[:int(self.cfg.get('max_reply_chars', 60) or 60)]
            kws = [k for k in (keywords or []) if str(k).strip()] or \
                _keywords_from(c.get('canonical') or '', c.get('samples') or [])
            entry = make_entry(c.get('canonical') or '新问答', kws, body,
                               etype='faq', source='adopted', from_cluster=cid)
            old_id = c.get('accepted_entry_id') or ''
            if old_id:
                entry['id'] = old_id          # 已采纳过就更新原条目，不重复新增
            c['status'] = 'accepted'
            c['accepted_entry_id'] = entry['id']
            c['suggestion'] = {'text': body, 'model': (c.get('suggestion') or {}).get('model', ''),
                               'ts': int(time.time() * 1000)}
            self._dirty = True
        self.flush(force=True)
        return entry

    def dismiss(self, cid):
        with self._lock:
            for c in self.clusters:
                if c.get('id') == cid:
                    c['status'] = 'dismissed'
                    self._dirty = True
                    break
            else:
                return False
        self.flush(force=True)
        return True

    def regenerate(self, cid):
        with self._lock:
            for c in self.clusters:
                if c.get('id') == cid:
                    c['status'] = 'open'
                    c['last_suggest_ts'] = 0
                    self._dirty = True
                    break
            else:
                return False
        self.flush(force=True)
        self._wake.set()
        return True

    # ---------- 建议生成 ----------
    def _pick_candidate(self, now_ms):
        with self._lock:
            if not self.cfg.get('enabled', True):
                return None
            min_count = int(self.cfg.get('min_count', 3) or 3)
            cd = int(self.cfg.get('suggest_cooldown_s', 1800) or 1800) * 1000
            cands = [c for c in self.clusters
                     if c.get('status') == 'open'
                     and int(c.get('count') or 0) >= min_count
                     and len(c.get('canonical') or '') >= 3
                     and now_ms - int(c.get('last_suggest_ts') or 0) >= cd]
            if not cands:
                return None
            cands.sort(key=lambda x: int(x.get('count') or 0), reverse=True)
            return dict(cands[0])

    def _llm(self, system, user):
        if self._llm_fn is not None:
            return self._llm_fn(system, user)
        from reply_engine import llm_complete
        cfg = self._cfg_getter() if self._cfg_getter else {}
        return llm_complete(cfg or {}, system, user)

    def _prompt(self, c):
        samples = list(c.get('samples') or [])[:5]
        answers = list(c.get('answers') or [])[:3]
        ctx = ''
        if self.knowledge is not None:
            try:
                ctx = self.knowledge.context(c.get('canonical') or '', limit=400)
            except Exception:
                ctx = ''
        system = ('你是直播话术编辑。根据知识库和观众问法，为直播机器人「远仔」写一句回答。'
                  '要求：口语化、有梗但不低俗，不超过 %d 字，只输出回答本身，不加引号、'
                  '不要解释。避开政治、色情、暴力、医疗诊断和投资建议。'
                  % int(self.cfg.get('max_reply_chars', 60) or 60))
        lines = ['观众问法：']
        lines += ['%d. %s' % (i + 1, s) for i, s in enumerate(samples)]
        if ctx:
            lines += ['', '知识库参考：', ctx]
        if answers:
            lines += ['', '之前说过的：'] + ['- ' + a for a in answers]
        lines += ['', '请写一版更自然、更有记忆点的回答。']
        return system, '\n'.join(lines)

    def _generate_one(self):
        c = self._pick_candidate(int(time.time() * 1000))
        if not c:
            return False
        try:
            system, user = self._prompt(c)
            text = (self._llm(system, user) or '').strip().strip('"“”\'')
            text = re.sub(r'\s+', ' ', text)[:int(self.cfg.get('max_reply_chars', 60) or 60)]
        except Exception as e:
            text = ''
            print('  [经验库] 生成建议失败：%s' % type(e).__name__, flush=True)
        with self._lock:
            if text and len(text) >= 4:
                for x in self.clusters:
                    if x.get('id') == c['id']:
                        x['status'] = 'suggested'
                        x['suggestion'] = {'text': text, 'model': 'llm',
                                           'ts': int(time.time() * 1000)}
                        x['last_suggest_ts'] = int(time.time() * 1000)
                        break
                self._dirty = True
            else:
                # 失败也记退避时间，避免每 25 秒反复打模型
                for x in self.clusters:
                    if x.get('id') == c['id']:
                        x['last_suggest_ts'] = int(time.time() * 1000)
                        break
                self._dirty = True
        self.flush()
        return bool(text)

    # ---------- 后台线程 ----------
    def start(self):
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='experience')
        self._thread.start()

    def _loop(self):
        tick = 0
        while not self._stop:
            self._wake.wait(timeout=5.0)
            self._wake.clear()
            if self._stop:
                break
            try:
                self.flush()
                tick += 1
                if tick % 5 == 0:
                    self._generate_one()
                    self._prune()
            except Exception as e:
                print('  [经验库] 后台任务异常：%s' % type(e).__name__, flush=True)

    def _prune(self):
        """控制簇数量：超上限丢「没被采纳、次数最少、最久没出现」的。"""
        cap = int(self.cfg.get('max_clusters', 200) or 200)
        days = int(self.cfg.get('archive_days', 30) or 30)
        cutoff = int((time.time() - days * 86400) * 1000)
        with self._lock:
            keep = [c for c in self.clusters
                    if c.get('status') == 'accepted' or int(c.get('last_ts') or 0) >= cutoff]
            if len(keep) > cap:
                accepted = [c for c in keep if c.get('status') == 'accepted']
                rest = [c for c in keep if c.get('status') != 'accepted']
                rest.sort(key=lambda x: (int(x.get('count') or 0),
                                         int(x.get('last_ts') or 0)), reverse=True)
                keep = accepted + rest[:max(0, cap - len(accepted))]
            if len(keep) != len(self.clusters):
                self.clusters = keep
                self._dirty = True

    def stop(self):
        self._stop = True
        self._wake.set()
        self.flush(force=True)

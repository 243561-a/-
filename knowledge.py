#!/usr/bin/env python
# coding: utf-8
"""机器人知识库：一份 Markdown 就是全部知识。

为什么用 md：
    产品资料、人设细节、常见问答都由人维护，写在一份可读文档里最省事：
    直接改 knowledge.md、保存即生效，能进 git、能备份，不需要数据库或后台表单。

约定（见 knowledge.md 顶部说明）：
    ## 标题        一段知识
    关键词：a, b   可选，至少 2 个字才参与匹配
    类型：faq      faq=直接念 / fact=只给模型参考 / persona=语气人设，默认 fact
    其余内容     正文

检索仍然是「关键词命中 + 中文 bigram 相似度」，纯标准库、毫秒级。
高频道问题被采纳后，经验库会调 upsert() 把新段落追加到 md 末尾（带 id 注释）。
"""

import os
import re
import time
import unicodedata
import zlib

from jsonio import write_text

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, 'knowledge.md')

VALID_TYPES = ('fact', 'faq', 'persona')
HEADER = '# 远仔知识库\n'
MAX_CONTEXT = 600

# 和 reply_engine.speakable_question 用的是同一套清洗口径
_AT_URL = re.compile(r'@\S+|https?://\S+|www\.\S+')
_EMOJI = re.compile(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u2B00-\u2BFF]')
_PUNCT = re.compile(r'[\s\W_]+', re.UNICODE)

_SECTION = re.compile(r'^##\s+(.+?)\s*$')
_KV = re.compile(r'^(关键词|类型)\s*[：:]\s*(.*)$')
_MARK = re.compile(r'<!--\s*id\s*:\s*([^\s>]+)\s*-->', re.I)

# 直答阈值：关键词命中，或与条目标题/关键词的字面相似度足够高
DIRECT_SIM = 0.78


def norm(text):
    """归一化：全角转半角、小写、去链接/表情/标点空白。"""
    s = unicodedata.normalize('NFKC', text or '').lower()
    s = _AT_URL.sub(' ', s)
    s = _EMOJI.sub('', s)
    s = _PUNCT.sub('', s)
    return s


def bigrams(text):
    """中文按相邻两字切；单字问题退化成单字集合。"""
    s = text or ''
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def dice(a, b):
    """bigram Dice 系数，0~1。两个都空算 0。"""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return 2.0 * inter / (len(a) + len(b))


def _entry_id(title, body=''):
    """没有显式 id 时，用标题算一个稳定 id（手工改正文不会变）。"""
    return 'md_%08x' % (zlib.crc32((title or '').encode('utf-8')) & 0xffffffff)


def make_entry(title, keywords, body, etype='faq', source='manual', from_cluster=''):
    now = int(time.time() * 1000)
    return {
        'id': 'k_%x' % now,
        'title': (title or '').strip()[:30],
        'keywords': [str(k).strip() for k in (keywords or []) if str(k).strip()][:8],
        'body': (body or '').strip(),
        'type': etype if etype in VALID_TYPES else 'fact',
        'source': source,
        'from_cluster': from_cluster or '',
        'created_at': now,
        'updated_at': now,
    }


def parse(text):
    """把 Markdown 切成条目。标题层级只认 `##`，正文里的 `###` 原样保留。"""
    entries, title, buf = [], None, []

    def flush():
        nonlocal title, buf
        if title is None:
            return
        entry = make_entry(title, [], '', etype='fact')
        body_lines, has_id = [], False
        for line in buf:
            m = _MARK.search(line)
            if m:
                entry['id'] = m.group(1)
                has_id = True
                line = _MARK.sub('', line)
            kv = _KV.match(line.strip())
            if kv:
                key, val = kv.group(1), kv.group(2).strip()
                if key == '关键词':
                    entry['keywords'] = [x.strip() for x in
                                         re.split(r'[,，、]', val) if x.strip()][:8]
                else:
                    t = val.lower()
                    entry['type'] = t if t in VALID_TYPES else 'fact'
                continue
            body_lines.append(line)
        entry['body'] = '\n'.join(body_lines).strip()
        if not has_id:
            entry['id'] = _entry_id(title, entry['body'])
        if entry['body'] or entry['keywords']:
            entries.append(entry)
        title, buf = None, []

    for line in (text or '').splitlines():
        m = _SECTION.match(line)
        if m:
            flush()
            title = m.group(1).strip()
        elif line.startswith('# '):
            flush()
            title = None
        elif title is not None:
            buf.append(line)
    flush()
    return entries


def _render(entry):
    lines = ['## ' + (entry.get('title') or '未命名'),
             '<!-- id: %s -->' % (entry.get('id') or _entry_id(entry.get('title')))]
    kws = [str(k).strip() for k in (entry.get('keywords') or []) if str(k).strip()]
    if kws:
        lines.append('关键词：' + '、'.join(kws))
    lines.append('类型：' + (entry.get('type') if entry.get('type') in VALID_TYPES
                            else 'fact'))
    lines.append('')
    lines.append((entry.get('body') or '').strip())
    return '\n'.join(lines).rstrip() + '\n'


class KnowledgeBase:
    def __init__(self, path=None):
        self.path = path or PATH
        self._mtime = 0
        self._warn_ts = 0
        self.entries = []
        self.enabled = True
        self.max_context_chars = MAX_CONTEXT
        self._cache = []          # [(entry, 归一化标题+关键词, bigram, [归一化关键词])]
        self.load()

    # ---------- 加载 ----------
    def load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                text = f.read()
        except FileNotFoundError:
            self.entries = []
            self._cache = []
            self._mtime = 0
            return True
        except Exception as e:
            now = time.time()
            if now - self._warn_ts > 5:
                self._warn_ts = now
                print('  [知识库] 这次没读成功（%s），继续用上一份' % type(e).__name__,
                      flush=True)
            return False
        self.entries = parse(text)
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = 0
        self._rebuild()
        return True

    def maybe_reload(self):
        try:
            if os.path.getmtime(self.path) != self._mtime:
                return self.load()
        except Exception:
            pass
        return False

    def _rebuild(self):
        cache = []
        for e in self.entries:
            if e.get('enabled', True) is False:
                continue
            title = norm(e.get('title') or '')
            keys = [norm(k) for k in (e.get('keywords') or []) if norm(k)]
            text = title + ''.join(keys)
            cache.append((e, text, bigrams(text), keys))
        self._cache = cache

    # ---------- 读写 ----------
    def all(self):
        return list(self.entries)

    def get(self, kid):
        for e in self.entries:
            if e.get('id') == kid:
                return e
        return None

    def upsert(self, entry):
        """按 id 替换对应段落，没有就追加到文件末尾，然后重新加载。"""
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                text = f.read()
        except FileNotFoundError:
            text = HEADER
        eid = entry.get('id') or _entry_id(entry.get('title'), entry.get('body'))
        entry = dict(entry, id=eid)
        rendered = _render(entry)

        lines = text.splitlines(keepends=True)
        starts = [i for i, ln in enumerate(lines) if ln.startswith('## ')]
        span = None
        for i, s in enumerate(starts):
            e = starts[i + 1] if i + 1 < len(starts) else len(lines)
            if _MARK.search(''.join(lines[s:e])):
                if _MARK.search(''.join(lines[s:e])).group(1) == eid:
                    span = (s, e)
                    break
        if span:
            head = ''.join(lines[:span[0]]).rstrip('\n')
            tail = ''.join(lines[span[1]:]).lstrip('\n')
            parts = [p for p in (head, rendered.rstrip('\n'), tail.rstrip('\n')) if p]
            text = '\n\n'.join(parts) + '\n'
        else:
            text = text.rstrip('\n') + '\n\n' + rendered
        write_text(self.path, text)
        self.load()
        return self.get(eid) or entry

    # ---------- 检索 ----------
    def _score(self, qn, qb, tb, keys):
        hit = 0.0
        for k in keys:
            if len(k) >= 2 and k in qn:      # 单字关键词不参与，避免「我」「你」误触发
                hit = max(hit, 1.0 + min(len(k), 6) * 0.1)
        sim = dice(qb, tb)
        return hit + 0.8 * sim, hit, sim

    def search(self, q, top_k=3, min_score=0.5):
        if not self.enabled:
            return []
        qn = norm(q)
        if not qn:
            return []
        qb = bigrams(qn)
        scored = []
        for e, _text, tb, keys in self._cache:
            s, _hit, _sim = self._score(qn, qb, tb, keys)
            if s >= min_score:
                scored.append((s, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:max(0, top_k)]

    def direct(self, q):
        """能直接念出来的 faq 条目（关键词命中或高度相似），没有就 None。"""
        if not self.enabled:
            return None
        qn = norm(q)
        if not qn:
            return None
        qb = bigrams(qn)
        best, best_score = None, 0.0
        for e, _text, tb, keys in self._cache:
            if e.get('type') != 'faq':
                continue
            s, hit, sim = self._score(qn, qb, tb, keys)
            if not (hit > 0 or sim >= DIRECT_SIM):
                continue
            if s > best_score:
                best, best_score = e, s
        return best

    def context(self, q, limit=None, exclude_ids=()):
        """拼成给大模型看的「参考资料」段落，超长截断。"""
        limit = self.max_context_chars if limit is None else limit
        if not limit:
            return ''
        parts, used = [], 0
        for _s, e in self.search(q, top_k=3):
            if e.get('id') in exclude_ids:
                continue
            title = (e.get('title') or '').strip()
            body = (e.get('body') or '').strip()
            line = ('【%s】%s' % (title, body)) if title else body
            if not line:
                continue
            if used + len(line) > limit:
                line = line[:max(0, limit - used)]
            if not line:
                break
            parts.append(line)
            used += len(line)
            if used >= limit:
                break
        return '\n'.join(parts)

#!/usr/bin/env python
# coding: utf-8
"""
「不回答」清单：运营在直播中随时增删，改完立即生效。

落地成 bans.json，pipeline 每 200ms 检查 mtime，不用重启进程。
现在作用于：礼物话术（命中就不谢）+ 弹幕统计（标记为不该答，等问答环接上直接复用）。

    {"words": ["前任", "多少钱", "住址"]}
"""

import json
import os
import time

DEFAULT = {
    "words": [],
    "note": "命中这些词的弹幕不回答、礼物不道谢；运营可在控制台随时增删",
}


class BanList:
    def __init__(self, path=None):
        self.path = path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'bans.json')
        self._mtime = 0
        self.words = []
        self.hits = 0
        self.load()

    # ---------- 读写 ----------
    def load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError('顶层不是对象')
            words = [w.strip() for w in cfg.get('words', []) if w and w.strip()]
        except (OSError, ValueError):
            # 读失败（可能正被写、或文件坏了）就保留现有词表，
            # 绝不能把黑名单清空——否则不该答的话会全放出去
            return False
        self.words = words
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = 0
        return True

    def save(self):
        data = dict(DEFAULT, words=self.words)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._mtime = os.path.getmtime(self.path)

    def maybe_reload(self):
        try:
            if os.path.getmtime(self.path) != self._mtime:
                return self.load()
        except Exception:
            pass
        return False

    # ---------- 维护 ----------
    def add(self, word):
        word = (word or '').strip()
        if not word or word in self.words:
            return False
        self.words.append(word)
        self.save()
        return True

    def remove(self, word):
        word = (word or '').strip()
        if word not in self.words:
            return False
        self.words.remove(word)
        self.save()
        return True

    # ---------- 判定 ----------
    def hit(self, text):
        """返回命中的词，没命中返回 None。"""
        if not text:
            return None
        low = text.lower()
        for w in self.words:
            if w.lower() in low:
                self.hits += 1
                return w
        return None

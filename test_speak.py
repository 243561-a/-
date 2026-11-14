#!/usr/bin/env python
# coding: utf-8
"""回归测试：弹幕回复的播报文本必须把「谁问的」和「问了什么」都念出来。"""

import sys

from reply_engine import ReplyEngine

eng = ReplyEngine()
MK = eng.marker                                  # 例如 #远仔
BARE = MK.replace('＃', '#').lstrip('#').strip()  # 例如 远仔

CASES = [
    # (弹幕原文, 名字, 期望包含的片段列表)
    (MK + ' 你多大了', '陈家人', ['陈家人问', '你多大了', '一岁']),
    (MK + ' 你会跳舞吗', '小李', ['小李问', '你会跳舞吗', '跳舞']),
    (BARE + BARE + ' 你好呀', '阿强', ['阿强问', '你好呀']),   # 口语重复唤醒
    (MK + ' 你会跳舞吗', '小张', ['小张问', '你会跳舞吗']),
    (MK, '大熊', ['大熊', '没说要问啥']),
    (MK + ' @某人 快看 https://live.douyin.com/123 你会跳舞吗', '小张',
     ['小张问', '你会跳舞吗']),
]

fail = 0
for raw, name, must in CASES:
    has, q = eng.parse(raw)
    assert has, '标记没识别: %r' % raw
    ans, src = eng.answer(q, 'u-' + name)
    speak = eng.compose({'name': name, 'question': q, 'text': ans})
    miss = [m for m in must if m not in speak]
    status = 'OK  ' if not miss else 'FAIL'
    if miss:
        fail += 1
    print('%s %-40r → %s' % (status, raw[:36], speak))
    if miss:
        print('     缺：%s' % miss)

# 超长弹幕：问题要截断，且有句结尾
long_raw = '＃' + BARE + ' 我今天刚刚下班回家路上看到一只特别特别可爱的橘猫它一直跟着我走了好远好远你说它是不是想跟我回家啊'
has, q = eng.parse(long_raw)
ans, src = eng.answer(q, 'u-long')
speak = eng.compose({'name': '话痨', 'question': q, 'text': ans})
ok = len(speak) <= int(eng.cfg.get('max_speak_chars', 64)) + 4
print('%s 长弹幕 → %s（%d 字）' % ('OK  ' if ok else 'FAIL', speak, len(speak)))
fail += 0 if ok else 1

print('\n%s' % ('全部通过' if not fail else '%d 项失败' % fail))
sys.exit(1 if fail else 0)

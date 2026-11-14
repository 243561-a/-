# coding: utf-8
"""弹幕回复的串行队列与冷却测试（不需要直播、不需要机器人）"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reply_engine import ReplyEngine, ReplyWorker  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, 'replies.json')


def chat(uid, name, text, ms):
    return {'ts': ms, 'type': 'chat', 'user_id': uid, 'user_name': name, 'text': text}


def test_serial():
    eng = ReplyEngine(CFG)
    eng.est_base_ms = 200
    eng.est_ms_per_char = 80
    eng.max_pending = 6
    sent = []
    w = ReplyWorker(eng, lambda t: sent.append((time.time(), t['user'], t['text'])))
    w.start()

    t0 = int(time.time() * 1000)
    for i, name in enumerate(['甲', '乙', '丙']):
        item, why = eng.feed(chat('u' + str(i), name, eng.marker + ' 你多大了', t0 + i * 10), t0 + i * 10)
        assert item, '应该入队'
        w.enqueue(item)
    print('  入队 3 条，等待串行播报…')
    time.sleep(9)
    w.stop()

    print('  实际发送顺序：')
    base = sent[0][0] if sent else 0
    for ts, user, text in sent:
        print('    +%.1fs  %s | %s' % (ts - base, user, text[:28]))
    assert len(sent) == 3, '应该发 3 条，实际 %d' % len(sent)
    assert [x[1] for x in sent] == ['甲', '乙', '丙'], '顺序必须是先进先出'
    gaps = [sent[i + 1][0] - sent[i][0] for i in range(len(sent) - 1)]
    assert all(g > 1.0 for g in gaps), '两条之间必须等前一条播完，间隔 %s' % gaps
    print('  ✓ 三条按序、串行、互不重叠（间隔 %.1f / %.1f 秒）' % (gaps[0], gaps[1]))


def test_cooldown():
    eng = ReplyEngine(CFG)
    t0 = int(time.time() * 1000)
    first, why1 = eng.feed(chat('u9', '老王', eng.marker + ' 你多大了', t0), t0)
    assert first is not None, '第一条应入队'
    print('  第一次：', why1)

    for gap_s, expect in [(10, False), (299, False), (301, True)]:
        eng2 = ReplyEngine(CFG)
        eng2.feed(chat('u9', '老王', eng.marker + ' 你多大了', t0), t0)
        item, why = eng2.feed(chat('u9', '老王', eng.marker + ' 会跳舞吗', t0 + gap_s * 1000),
                              t0 + gap_s * 1000)
        ok = item is not None
        print('  隔 %3d 秒再问：%s（%s）' % (gap_s, '入队' if ok else '被拦', why))
        assert ok == expect, '冷却判定不符：%d 秒 -> %s' % (gap_s, why)
    print('  ✓ 同一人 5 分钟内只回一次')


def test_marker():
    eng = ReplyEngine(CFG)
    mk = eng.marker                    # 例如 #元仔
    bare = mk.replace('＃', '#').lstrip('#').strip()   # 例如 元仔
    cases = [
        ('主播多大了', False),
        (mk + ' 你多大了', True),
        (mk.replace('#', '＃') + ' 你多大了', True),
        ('# ' + bare + ' 你多大了', True),
        (bare + bare + ' 你多大了', True),      # 元仔元仔 你多大了
        (bare + bare + '你多大了', True),       # 元仔元仔你多大了
        (bare + ' 你多大了', False),           # 单次只喊一声不算（wake_min_repeat=2）
        (bare + '好可爱', False),              # 观众随口提到名字，不插嘴
        ('我今天看到' + bare + '了', False),   # 句中提到名字不插嘴
    ]
    for text, expect in cases:
        has, q = eng.parse(text)
        print('  %-18s -> %s  %s' % (text, '有标记' if has else '无标记', q))
        assert has == expect, '标记识别不符：%s' % text
    print('  ✓ 标记/唤醒词识别正常（标记 %s，唤醒词 %s）' % (mk, bare))


if __name__ == '__main__':
    print('== 标记识别 ==')
    test_marker()
    print('== 冷却 ==')
    test_cooldown()
    print('== 串行队列 ==')
    test_serial()
    print('\n全部通过')

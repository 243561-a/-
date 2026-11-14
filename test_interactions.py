# coding: utf-8
"""点赞 / 关注感谢的回归测试。

要守住的四件事：
1. 同一个人点一百下赞，只谢一次
2. 不同的人各谢各的
3. 一批人同时点赞时，间隔内的被跳过（不刷屏）
4. 配置读坏了不影响已加载的话术

    python test_interactions.py
"""

import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from interactions import InteractionRules  # noqa: E402


def mk(tmpdir, **over):
    cfg = {
        'version': 1, 'max_name_chars': 10,
        'like': {'enabled': True, 'priority': 6, 'min_gap_ms': 2000,
                 'thanks': ['A谢谢{user}', 'B谢谢{user}', 'C谢谢{user}']},
        'follow': {'enabled': True, 'priority': 8,
                   'thanks': ['关注1 {user}', '关注2 {user}']},
        'forbidden_words': ['傻'],
    }
    cfg.update(over)
    p = os.path.join(tmpdir, 'interactions.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False)
    return InteractionRules(p), p


def like(uid, name, t):
    return {'type': 'like', 'user_id': uid, 'user_name': name, 'count': 1, 'ts': t}


def follow(uid, name, t):
    return {'type': 'follow', 'user_id': uid, 'user_name': name, 'ts': t}


def test_once_per_user():
    d = tempfile.mkdtemp()
    r, _ = mk(d)
    got = 0
    for i in range(100):                 # 同一个人连点 100 下
        task, why = r.feed(like('u1', '小明', 10000 + i * 100), 10000 + i * 100)
        if task:
            got += 1
        elif i > 0:
            assert why == 'seen', '第 %d 次应是 seen，实际 %s' % (i + 1, why)
    assert got == 1, '同一个人被谢了 %d 次' % got
    print('  ✓ 同一个人点 100 下赞，只谢 1 次')
    shutil.rmtree(d, ignore_errors=True)


def test_different_users():
    d = tempfile.mkdtemp()
    r, _ = mk(d)
    out = []
    t = 10000
    for i, name in enumerate(['甲', '乙', '丙']):
        t += 5000                        # 间隔够，都会被谢
        task, why = r.feed(like('u%d' % i, name, t), t)
        assert task, '第 %d 个人没被谢（%s）' % (i + 1, why)
        out.append(task['text'])
    for i in range(1, len(out)):
        assert out[i] != out[i - 1], '第 %d、%d 句连着重复：%r' % (i, i + 1, out)
    print('  ✓ 不同的人各谢各的，相邻两次不会说同一句')
    shutil.rmtree(d, ignore_errors=True)


def test_no_adjacent_repeat():
    """随机抽句，但绝不允许连着两次抽到同一句（和礼物话术同一套规则）"""
    d = tempfile.mkdtemp()
    r, _ = mk(d)
    out, t = [], 10000
    for i in range(30):
        t += 5000
        task, why = r.feed(like('n%d' % i, '观众%d' % i, t), t)
        assert task, '第 %d 个人没被谢（%s）' % (i + 1, why)
        out.append(task['text'])
    for i in range(1, len(out)):
        assert out[i] != out[i - 1], '第 %d 句和上一句重复了：%s' % (i + 1, out[i])
    used = set(x[0] for x in out)        # 池子是 A/B/C 开头
    assert len(used) == 3, '30 个人里只出现了 %d 种说法，随机没铺开：%r' % (len(used), used)
    print('  ✓ 30 次随机不连重，三种说法都用上了')
    shutil.rmtree(d, ignore_errors=True)


def test_like_follow_independent():
    """点赞和关注各有一个「上次抽中的句子」，互不干扰"""
    d = tempfile.mkdtemp()
    r, _ = mk(d)
    likes, follows, t = [], [], 10000
    for i in range(6):
        t += 5000
        a, _ = r.feed(like('l%d' % i, 'L%d' % i, t), t)
        t += 5000
        b, _ = r.feed(follow('f%d' % i, 'F%d' % i, t), t)
        assert a and b
        likes.append(a['text'])
        follows.append(b['text'])
    for i in range(1, 6):
        assert likes[i] != likes[i - 1], '点赞池连重：%r' % likes
        assert follows[i] != follows[i - 1], '关注池连重：%r' % follows
    assert all(x.startswith('关注') for x in follows), '关注串到点赞池了：%r' % follows
    print('  ✓ 点赞与关注各自随机不连重，互不串池')


def test_gap():
    d = tempfile.mkdtemp()
    r, _ = mk(d)
    t1 = 10000
    a, _ = r.feed(like('u1', '甲', t1), t1)
    assert a, '第一个人应该被谢'
    b, why = r.feed(like('u2', '乙', t1 + 300), t1 + 300)   # 300ms 后
    assert b is None and why == 'gap', '间隔内应跳过，实际 %s' % why
    c, why2 = r.feed(like('u2', '乙', t1 + 3000), t1 + 3000)  # 又被跳过就不补
    assert c is None and why2 == 'seen', '被跳过的人再点不应补谢，实际 %s' % why2
    print('  ✓ 太密集的点赞被跳过，且不会事后补念')
    shutil.rmtree(d, ignore_errors=True)


def test_follow():
    d = tempfile.mkdtemp()
    r, _ = mk(d)
    a, why = r.feed(follow('f1', '小红', 10000), 10000)
    assert a, '第一次关注应该被谢（%s）' % why
    b, why = r.feed(follow('f1', '小红', 10500), 10500)
    assert b is None and why == 'seen', '重复关注应静默，实际 %s' % why
    c, why = r.feed(follow('f2', '小刚', 10600), 10600)   # 关注不受点赞间隔限制
    assert c, '另一个人的关注应该被谢（%s）' % why
    print('  ✓ 关注每人只谢一次，且不受点赞间隔影响')
    shutil.rmtree(d, ignore_errors=True)


def test_dirty_name():
    d = tempfile.mkdtemp()
    r, _ = mk(d)
    task, _ = r.feed(like('x1', '傻瓜😡', 10000), 10000)
    assert task, '脏昵称也要能谢'
    assert '傻' not in task['text'], '禁用词漏出来了：%s' % task['text']
    assert '这位朋友' in task['text'], '脏昵称应换成「这位朋友」：%s' % task['text']
    print('  ✓ 昵称带禁用词会换成「这位朋友」，整句不丢')
    shutil.rmtree(d, ignore_errors=True)


def test_switch_off():
    d = tempfile.mkdtemp()
    r, p = mk(d)
    with open(p, encoding='utf-8') as f:
        cfg = json.load(f)
    cfg['like']['enabled'] = False
    time.sleep(0.02)     # Windows 文件时间戳精度有限，隔开一点才认得出改动
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False)
    r.maybe_reload()
    task, why = r.feed(like('u1', '甲', 20000), 20000)
    assert task is None and why == 'off', '关掉之后不该说话，实际 %s' % why
    print('  ✓ 关掉开关后完全静默')
    shutil.rmtree(d, ignore_errors=True)


def test_broken_config():
    d = tempfile.mkdtemp()
    r, p = mk(d)
    before = list(r.like['thanks'])
    time.sleep(0.02)
    with open(p, 'w', encoding='utf-8') as f:
        f.write('{"like": {"enabled": true, "thanks": ["半')
    r.maybe_reload()
    assert r.like['thanks'] == before, '配置读坏后话术被改了'
    task, why = r.feed(like('u9', '甲', 30000), 30000)
    assert task, '配置读坏后仍应能用旧话术（%s）' % why
    print('  ✓ 配置文件写坏时保留旧话术，照常工作')
    shutil.rmtree(d, ignore_errors=True)


def main():
    print('点赞/关注 回归测试')
    test_once_per_user()
    test_different_users()
    test_no_adjacent_repeat()
    test_like_follow_independent()
    test_gap()
    test_follow()
    test_dirty_name()
    test_switch_off()
    test_broken_config()
    print('全部通过')


if __name__ == '__main__':
    main()

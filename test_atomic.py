# coding: utf-8
"""回归测试：配置文件被写坏的瞬间，热加载不能崩、也不能把好配置冲掉。

背景：控制台保存时直接覆写 JSON，pipeline 每 200ms 热加载一次。
两者撞上时曾抛 JSONDecodeError，直接打断主循环、整条直播链路停摆。
现在要求：读坏 = 当作没发生，继续用上一份。

    python test_atomic.py
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bans import BanList          # noqa: E402
from jsonio import write_json     # noqa: E402
from reply_engine import ReplyEngine  # noqa: E402
from rules import GiftRules       # noqa: E402

CASES = ('replies.json', 'gifts.json', 'bans.json')


def tmp_copy():
    d = tempfile.mkdtemp(prefix='rl_atomic_')
    for n in CASES:
        src = os.path.join(HERE, n)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(d, n))
    return d


def tear_file(path):
    """模拟"正在被写"：只写进半截 JSON"""
    with open(path, 'w', encoding='utf-8') as f:
        f.write('{"enabled": true, "marker": "#元')


def test_reply():
    d = tmp_copy()
    p = os.path.join(d, 'replies.json')
    e = ReplyEngine(p)
    marker, presets, intros = e.marker, len(e.presets), len(e.intros)

    tear_file(p)
    raised = ''
    try:
        ret = e.maybe_reload()
    except Exception as ex:      # 修复前这里会抛 JSONDecodeError
        raised = type(ex).__name__
        ret = None
    assert not raised, '热加载不该抛异常，实际抛了 %s' % raised
    assert ret is False, '读坏了应返回 False'
    assert e.marker == marker, '唤醒词被改掉了：%s -> %s' % (marker, e.marker)
    assert len(e.presets) == presets, '预设条数变了'
    assert len(e.intros) == intros, '自我介绍段数变了'
    print('  ✓ 问答配置：半截 JSON 不再抛异常，旧配置完好')
    shutil.rmtree(d, ignore_errors=True)


def test_rules():
    d = tmp_copy()
    p = os.path.join(d, 'gifts.json')
    r = GiftRules(p)
    tiers, dup = len(r.tiers), r.dup_window

    tear_file(p)
    raised = ''
    try:
        ret = r.maybe_reload()
    except Exception as ex:
        raised = type(ex).__name__
        ret = None
    assert not raised, '礼物规则热加载抛了 %s' % raised
    assert ret is False
    assert len(r.tiers) == tiers, '档位丢了'
    assert r.dup_window == dup
    print('  ✓ 礼物规则：档位与去重窗口都还在')
    shutil.rmtree(d, ignore_errors=True)


def test_bans():
    d = tmp_copy()
    p = os.path.join(d, 'bans.json')
    b = BanList(p)
    b.words = ['前任', '住址']          # 模拟已加载的黑名单
    b._mtime = os.path.getmtime(p) if os.path.exists(p) else 0

    tear_file(p)
    ret = b.maybe_reload()
    assert ret is False
    assert b.words == ['前任', '住址'], '黑名单被清空了：%r' % (b.words,)
    print('  ✓ 不回答清单：读坏了不会把黑名单清空')
    shutil.rmtree(d, ignore_errors=True)


def test_atomic_write():
    d = tmp_copy()
    p = os.path.join(d, 'out.json')
    write_json(p, {'a': 1, '中文': '值'})
    with open(p, encoding='utf-8') as f:
        got = json.load(f)
    assert got == {'a': 1, '中文': '值'}, got
    assert not os.path.exists(p + '.tmp'), '临时文件没清掉'
    # 覆盖已有文件
    write_json(p, {'b': 2})
    with open(p, encoding='utf-8') as f:
        assert json.load(f) == {'b': 2}
    print('  ✓ 原子写：落盘正确、无 .tmp 残留、可覆盖')
    shutil.rmtree(d, ignore_errors=True)


def main():
    print('配置文件容错回归测试')
    test_reply()
    test_rules()
    test_bans()
    test_atomic_write()
    print('全部通过')


if __name__ == '__main__':
    main()

# coding: utf-8
"""高频问题经验库测试（不需要直播、不需要真调模型）

    python test_experience.py
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from experience import ExperienceStore, qnorm  # noqa: E402
from knowledge import KnowledgeBase, make_entry, norm  # noqa: E402


def tmp_path():
    d = tempfile.mkdtemp(prefix='rl_exp_')
    return d, os.path.join(d, 'experience.json')


def test_qnorm():
    cases = [
        ('＃元仔 你多大了？', '几岁'),
        ('主播多大了', '几岁'),
        ('你几岁呀', '几岁'),
        ('你吃了吗', '吃'),
        ('你吃饭了吗', '吃饭'),
    ]
    for raw, expect in cases:
        got = qnorm(raw)
        assert got == expect, '归一化不符：%r -> %r（期望 %r）' % (raw, got, expect)
    print('  ✓ 问题归一化：称呼/语气词/同义说法都归并了')


def test_cluster():
    d, p = tmp_path()
    s = ExperienceStore(p)
    for q, uid in [('你多大了', 'a'), ('主播多大了', 'b'), ('你几岁', 'c')]:
        s.record(q, uid, '观众' + uid)
    cl = s.all_clusters()
    assert len(cl) == 1, '三种问法应合成一簇，实际 %d 簇' % len(cl)
    assert cl[0]['count'] == 3, '计数不对：%s' % cl[0]['count']
    assert qnorm(cl[0]['canonical']) in ('几岁',), cl[0]['canonical']
    print('  ✓ 聚类：3 种问法 → 1 簇，计数 3')

    # 语义不同 / 否定句不能合并
    s.record('你吃了吗', 'd')
    s.record('你没吃饭吗', 'e')
    assert len(s.all_clusters()) == 3, '「吃了吗」和「没吃吗」不能合并'
    print('  ✓ 否定守卫：语义相反的问题各自成簇')

    # 落盘后重新加载，计数还在
    s.flush(force=True)
    s2 = ExperienceStore(p)
    assert len(s2.all_clusters()) == 3
    assert sum(c['count'] for c in s2.all_clusters()) == 5
    print('  ✓ 持久化：重新加载后计数完好')
    shutil.rmtree(d, ignore_errors=True)


def test_suggestion_and_accept():
    d, p = tmp_path()
    calls = []

    def stub(system, user):
        calls.append(user)
        return '我今年刚满一岁，正是最能折腾的年纪，你猜我明天会不会长大？'

    kd = tempfile.mkdtemp(prefix='rl_kb_')
    kb = KnowledgeBase(os.path.join(kd, 'knowledge.md'))
    kb.upsert(make_entry('年龄', ['多大', '几岁'], '我今年刚满一岁。', etype='fact'))

    s = ExperienceStore(p, knowledge=kb, llm_fn=stub)
    s.cfg['min_count'] = 3
    for _ in range(3):
        s.record('你多大了', 'u1', '甲')
    assert s._generate_one() is True, '达到阈值应生成建议'
    c = s.all_clusters()[0]
    assert c['status'] == 'suggested', c['status']
    assert c['suggestion']['text'].startswith('我今年刚满一岁'), c['suggestion']
    assert '知识库参考' in calls[0] and '年龄' in calls[0], '建议 prompt 应带知识库'
    assert len(c['suggestion']['text']) <= 60

    # 冷却期内不重复生成
    assert s._generate_one() is False, '冷却期内不该重复生成'
    print('  ✓ 建议生成：达阈值触发、带知识上下文、冷却期内不重复')

    entry = s.accept(c['id'])
    assert entry and entry['type'] == 'faq' and entry['source'] == 'adopted'
    assert '几岁' in entry['keywords'], entry['keywords']
    assert s.all_clusters()[0]['status'] == 'accepted'
    print('  ✓ 采纳：生成 faq 条目，关键词含归一化核心说法')

    # 再次采纳应复用同一条目 id，不重复新增
    entry2 = s.accept(c['id'], text='换个说法的一句回答')
    assert entry2['id'] == entry['id'], '重复采纳应更新原条目'
    print('  ✓ 重复采纳：更新原条目而不是新增')
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(kd, ignore_errors=True)


def test_llm_failure_backoff():
    d, p = tmp_path()

    def boom(system, user):
        raise RuntimeError('no network')

    s = ExperienceStore(p, llm_fn=boom)
    s.cfg['min_count'] = 2
    s.record('你叫什么名字')
    s.record('你叫什么名字')
    assert s._generate_one() is False, '模型失败应返回 False'
    c = s.all_clusters()[0]
    assert c['status'] == 'open', '失败不能把状态改成 suggested'
    assert c['last_suggest_ts'] > 0, '失败也要记退避时间，避免死循环调用'
    print('  ✓ 模型失败：保持 open + 退避，不影响统计')

    assert s.regenerate(c['id']) is True
    assert s.dismiss(c['id']) is True
    assert s.all_clusters()[0]['status'] == 'dismissed'
    print('  ✓ 忽略/重新生成：状态可切换')
    shutil.rmtree(d, ignore_errors=True)


def test_settings():
    d, p = tmp_path()
    s = ExperienceStore(p)
    got = s.update_settings({'min_count': 99, 'merge_threshold': 5,
                             'suggest_cooldown_s': 1, 'enabled': False})
    assert got['min_count'] == 50, got
    assert got['merge_threshold'] == 0.95, got
    assert got['suggest_cooldown_s'] == 60, got
    assert got['enabled'] is False
    assert s.record('你多大了') is None, '关闭学习后不再记录'
    print('  ✓ 设置：越界值被夹紧，关闭后不再记录')
    shutil.rmtree(d, ignore_errors=True)


def main():
    print('高频问题经验库测试')
    test_qnorm()
    test_cluster()
    test_suggestion_and_accept()
    test_llm_failure_backoff()
    test_settings()
    print('全部通过')


if __name__ == '__main__':
    main()

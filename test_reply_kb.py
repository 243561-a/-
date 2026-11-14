# coding: utf-8
"""知识库接入问答链路的测试（不需要直播、不需要机器人、不真调模型）

    python test_reply_kb.py
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import reply_engine                                  # noqa: E402
from experience import ExperienceStore               # noqa: E402
from knowledge import KnowledgeBase, make_entry      # noqa: E402
from reply_engine import ReplyEngine                 # noqa: E402


def tmp_env():
    d = tempfile.mkdtemp(prefix='rl_reply_')
    shutil.copy2(os.path.join(HERE, 'replies.json'), os.path.join(d, 'replies.json'))
    return d


def build(d, entries, llm_fn=None, answers=None):
    kb = KnowledgeBase(os.path.join(d, 'knowledge.md'))
    for e in (entries or []):
        kb.upsert(e)
    exp = ExperienceStore(os.path.join(d, 'experience.json'), knowledge=kb,
                          llm_fn=llm_fn or (lambda s, u: ''))
    eng = ReplyEngine(os.path.join(d, 'replies.json'), knowledge=kb, experience=exp)
    return eng, kb, exp


def test_direct_skips_llm():
    d = tmp_env()

    def boom(system, user):
        raise AssertionError('命中知识时不该再调模型')

    eng, _kb, _exp = build(d, [make_entry('年龄', ['多大'], '我今年一岁半，正是折腾的年纪。')],
                           llm_fn=boom)
    text, source = eng.answer('你多大了', 'u1')
    assert source == 'knowledge', '应走知识直答，实际 %s' % source
    assert '一岁半' in text, text
    print('  ✓ 知识直答：命中后不调模型（把模型桩设成抛异常也照样回答）')
    shutil.rmtree(d, ignore_errors=True)


def test_context_injected():
    d = tmp_env()
    seen = {}

    def fake_llm(cfg, system, user):
        seen['system'] = system
        seen['user'] = user
        return '我能拍照还能讲故事。'

    eng, _kb, _exp = build(
        d, [make_entry('产品功能', ['产品', '功能'], '能拍照、讲故事、陪孩子聊天。',
                       etype='fact')])
    old = reply_engine.llm_complete
    reply_engine.llm_complete = fake_llm
    try:
        text, source = eng.answer('你们产品能干嘛', 'u2')
    finally:
        reply_engine.llm_complete = old
    assert source == 'llm', '没命中知识直答时应走模型，实际 %s' % source
    assert '产品功能' in seen['system'], '提示词里应带知识标题'
    assert '能拍照' in seen['system'], '提示词里应带知识正文'
    assert '你们产品能干嘛' in seen['user'], '用户问题要原样传给模型'
    print('  ✓ 上下文注入：知识标题和正文都进了 system prompt')
    shutil.rmtree(d, ignore_errors=True)


def test_weather_not_hijacked():
    d = tmp_env()

    class FakeWeather:
        @staticmethod
        def is_weather_question(q):
            return '天气' in q

        @staticmethod
        def answer(q, city, timeout=6, cache_seconds=300):
            return '今天晴，25 度。', city

    old = reply_engine._weather_mod
    reply_engine._weather_mod = FakeWeather
    try:
        eng, _kb, _exp = build(
            d, [make_entry('天气', ['天气'], '今天下雨，记得带伞。')])
        text, source = eng.answer('今天天气怎么样', 'u3')
        assert source == 'weather', '天气问题必须走实时查询，实际 %s' % source
        assert '25 度' in text, text
        print('  ✓ 天气优先：旧知识条目不劫持实时天气')
    finally:
        reply_engine._weather_mod = old
        shutil.rmtree(d, ignore_errors=True)


def test_feed_counts_before_cooldown():
    d = tmp_env()
    eng, _kb, exp = build(d, [])
    t0 = 1794560000000
    mk = eng.marker
    item, why = eng.feed({'type': 'chat', 'user_id': 'u9', 'user_name': '老王',
                          'text': mk + ' 你多大了'}, t0)
    assert item, '第一次应入队'
    _item, why2 = eng.feed({'type': 'chat', 'user_id': 'u9', 'user_name': '老王',
                            'text': mk + ' 你多大了'}, t0 + 1000)
    assert _item is None, '冷却期内不应再入队'
    cl = exp.all_clusters()
    assert len(cl) == 1 and cl[0]['count'] == 2, \
        '被冷却挡掉的提问也要计数：%r（%s）' % (cl, why2)
    print('  ✓ 频次统计：冷却挡掉的那次也计数（%s）' % why2)
    shutil.rmtree(d, ignore_errors=True)


def test_build_task_records_answer():
    d = tmp_env()
    eng, _kb, exp = build(d, [make_entry('年龄', ['多大'], '我今年一岁半。')])
    item, _why = eng.feed({'type': 'chat', 'user_id': 'u1', 'user_name': '甲',
                           'text': eng.marker + ' 你多大了'}, 1794560000000)
    assert item, '应先入队'
    task, source = eng.build_task(item)
    assert source == 'knowledge'
    assert '一岁半' in task['text'], task['text']
    cl = exp.all_clusters()
    assert cl and cl[0]['count'] == 1, '生成回答时应记一次提问'
    assert cl[0]['answers'] and '一岁半' in cl[0]['answers'][0], '应记下实际回答'
    print('  ✓ 回答回流：build_task 把问题与实际回答都记进经验库')
    shutil.rmtree(d, ignore_errors=True)


def test_accept_writes_knowledge():
    d = tmp_env()
    eng, kb, exp = build(d, [])
    for _ in range(3):
        exp.record('你多大了', 'u1', '甲')
    exp._generate_one()          # 桩模型返回空 → 不会成功，这里手动造一条建议
    cid = exp.all_clusters()[0]['id']
    ok, msg, extra = eng.accept_suggestion(cid, text='我今年一岁半，正是最能折腾的年纪。')
    assert ok, msg
    assert extra['entry']['type'] == 'faq'
    assert kb.direct('你多大了'), '采纳后应立即能直答'
    assert exp.all_clusters()[0]['status'] == 'accepted'
    print('  ✓ 采纳链路：写入知识库 + 经验状态同步，立即可直答（%s）' % msg)
    shutil.rmtree(d, ignore_errors=True)


def main():
    print('知识库接入问答链路测试')
    test_direct_skips_llm()
    test_context_injected()
    test_weather_not_hijacked()
    test_feed_counts_before_cooldown()
    test_build_task_records_answer()
    test_accept_writes_knowledge()
    print('全部通过')


if __name__ == '__main__':
    main()

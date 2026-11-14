# coding: utf-8
"""知识库（knowledge.md）回归测试（不需要直播、不需要模型）

    python test_knowledge.py
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from knowledge import KnowledgeBase, make_entry, norm, parse  # noqa: E402

SAMPLE = """# 远仔知识库

> 说明文字不该变成条目。

## 我是谁
关键词：你是谁, 叫什么
类型：faq

我叫远仔，启元 T1 机器人。

## 产品功能
关键词：产品, 功能
类型：fact

能拍照、讲故事、陪孩子聊天。

### 补充说明
这段属于产品功能，不能单独成条。

## 说话风格
类型：persona

戏精损友型，热情嘴碎。
"""


def tmp_md(text=SAMPLE):
    d = tempfile.mkdtemp(prefix='rl_kb_')
    p = os.path.join(d, 'knowledge.md')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(text)
    return d, p


def test_norm():
    cases = [
        ('你 多大了？', '你多大了'),
        ('＃远仔　你几岁😀', '远仔你几岁'),
        ('@张三 你好呀', '你好呀'),
    ]
    for raw, expect in cases:
        got = norm(raw)
        assert got == expect, '归一化不符：%r -> %r（期望 %r）' % (raw, got, expect)
    print('  ✓ 归一化：全角/空白/表情/@ 都处理了')


def test_parse():
    entries = parse(SAMPLE)
    assert len(entries) == 3, '应解析出 3 段，实际 %d' % len(entries)
    titles = [e['title'] for e in entries]
    assert titles == ['我是谁', '产品功能', '说话风格'], titles
    assert entries[0]['type'] == 'faq' and entries[0]['keywords'] == ['你是谁', '叫什么']
    assert entries[1]['type'] == 'fact', entries[1]['type']
    assert '### 补充说明' in entries[1]['body'], '正文里的 ### 不能被切成新条目'
    assert entries[2]['type'] == 'persona'
    assert entries[2]['keywords'] == []
    assert all(e['id'] for e in entries), '每段都应有稳定 id'
    print('  ✓ 解析：标题/关键词/类型/正文都对，### 不会被误切')


def test_direct_and_search():
    d, p = tmp_md()
    kb = KnowledgeBase(p)

    hit = kb.direct('你是谁啊')
    assert hit and hit['title'] == '我是谁', 'faq 应能直答'
    assert kb.direct('你们产品有什么功能') is None, 'fact 不能直接念'
    assert kb.search('你们产品有什么功能'), 'fact 应出现在检索结果里'
    print('  ✓ 直答与检索：faq 直答，fact 只进上下文')

    ctx = kb.context('你们产品有什么功能')
    assert '产品功能' in ctx and '拍照' in ctx, ctx
    print('  ✓ 上下文：带标题和正文，长度 %d' % len(ctx))
    shutil.rmtree(d, ignore_errors=True)


def test_upsert():
    d, p = tmp_md()
    kb = KnowledgeBase(p)
    n = len(kb.all())

    e = make_entry('发货时间', ['发货', '快递'], '发货问主播，我不敢乱说。')
    kb.upsert(e)
    assert len(kb.all()) == n + 1, '新条目应追加'
    with open(p, encoding='utf-8') as f:
        text = f.read()
    assert '<!-- id: %s -->' % e['id'] in text, '应带 id 注释，方便以后更新'
    assert '## 我是谁' in text and '## 说话风格' in text, '原有段落不能被冲掉'
    assert kb.direct('什么时候发货')['id'] == e['id'], '追加后应立即能直答'
    print('  ✓ 追加：写回 md 且保留原有段落')

    # 同一 id 再采纳 = 更新原文，不新增段落
    e2 = dict(e, body='发货时间以主播说的为准。')
    kb.upsert(e2)
    assert len(kb.all()) == n + 1, '重复采纳应更新原段落'
    assert kb.get(e['id'])['body'] == '发货时间以主播说的为准。'
    print('  ✓ 更新：按 id 替换原段落，不重复追加')
    shutil.rmtree(d, ignore_errors=True)


def test_reload_and_missing():
    d, p = tmp_md()
    kb = KnowledgeBase(p)
    n = len(kb.all())

    with open(p, 'a', encoding='utf-8') as f:
        f.write('\n## 新加的\n类型：faq\n\n这是后来加的。\n')
    assert kb.maybe_reload() is True, 'mtime 变了应重载'
    assert len(kb.all()) == n + 1
    assert kb.direct('新加的')['body'] == '这是后来加的。'
    print('  ✓ 热加载：外部改 md 后立即可见')

    missing = os.path.join(d, 'none.md')
    kb2 = KnowledgeBase(missing)
    assert kb2.all() == [], '文件不存在应视为空库'
    assert kb2.direct('随便问') is None
    print('  ✓ 缺文件：按空库处理，不报错')
    shutil.rmtree(d, ignore_errors=True)


def main():
    print('知识库（Markdown）测试')
    test_norm()
    test_parse()
    test_direct_and_search()
    test_upsert()
    test_reload_and_missing()
    print('全部通过')


if __name__ == '__main__':
    main()

import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rules

def run(label, events, end_ms):
    r = rules.GiftRules(os.path.join(HERE, 'gifts.json'))
    feed_at = {e[0]: e[1:] for e in events}
    out = []
    for t in range(0, end_ms + 1, 100):
        if t in feed_at:
            rep, mid = feed_at[t]
            r.feed({'ts': t, 'type': 'gift', 'user_id': 'u1', 'user_name': '你别管',
                    'gift': '玫瑰', 'gift_id': '2001', 'diamond': 1,
                    'repeat': rep, 'combo': 0, 'msg_id': mid}, t)
        for task in r.tick(t):
            out.append((t, task['count'], task['text']))
    print('--- %s ---' % label)
    for ms, c, txt in out:
        print('  t=%6dms  x%d  %s' % (ms, c, txt))
    print('  => 播报 %d 次' % len(out))
    return len(out)

n1 = run('刷一个玫瑰(服务端推两条 0/4000)，再刷一个(10000/12000)',
         [(0, 1, 'a'), (4000, 1, 'b'), (10000, 1, 'c'), (12000, 1, 'd')], 20000)
n2 = run('真连刷5个(repeat 1->5)',
         [(0, 1, 'a'), (300, 2, 'b'), (600, 3, 'c'), (900, 4, 'd'), (1200, 5, 'e')], 6000)
n3 = run('隔12秒再送一个',
         [(0, 1, 'a'), (4000, 1, 'b'), (16000, 1, 'c'), (20000, 1, 'd')], 28000)
print('期望 2 / 1 / 2，实际 %d / %d / %d' % (n1, n2, n3))

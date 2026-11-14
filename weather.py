#!/usr/bin/env python
# coding: utf-8
"""天气问答：观众问「郑州什么天气」，直接查实时数据再播报。

链路（都走国内源，无需 key）：
    城市名 → toy1.weather.com.cn/search 换城市编码 → t.weather.itboy.net 拿实时+预报

查不到、超时、接口挂了都返回空字符串，由上层走兜底话术，绝不卡住队列。
同一城市 5 分钟内复用结果，避免一堆人问同一座城市时反复打接口。
"""

import json
import re
import threading
import time
import urllib.parse
import urllib.request

_SEARCH = 'http://toy1.weather.com.cn/search?cityname='
_DETAIL = 'http://t.weather.itboy.net/api/weather/city/'
_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')
_REFERER = 'http://www.weather.com.cn/'

# 问天气的信号词
_WORDS = ('天气', '气温', '温度', '多少度', '几度', '下雨', '下雪', '冷不冷',
          '热不热', '穿什么', '带伞')

# 提城市时要先删掉的疑问词和语气词
_NOISE = ('怎么样', '如何', '什么', '咋样', '咋', '呢', '啊', '呀', '吗', '嘛',
          '请问', '帮我', '查一下', '查查', '看看', '告诉我', '一下')

# 出现在城市位置但不属于城市的词
_STOP = ('今天', '明天', '后天', '现在', '目前', '这边', '那边', '这里', '那里',
         '我们', '你们', '他们', '当地', '外面', '户外', '这个', '那个', '的')

_cache = {}
_lock = threading.Lock()


def is_weather_question(q):
    return any(w in (q or '') for w in _WORDS)


def extract_city(q, default=''):
    """从问句里抠城市名：「郑州什么天气」→ 郑州；没提到就用默认城市。"""
    t = (q or '').strip()
    for w in _NOISE:
        t = t.replace(w, '')
    # 城市名 + 可选时间词 + 天气类关键词
    m = re.search(r'([\u4e00-\u9fa5]{2,8}?)(?:今天|明天|后天|现在)?(?:的)?'
                  r'(?:天气|气温|温度|多少度|几度|下雨|下雪|冷不冷|热不热|穿什么|带伞)', t)
    city = (m.group(1) if m else '').strip()
    # 掐掉前缀里的时间词/语气词（明天郑州 → 郑州）
    changed = True
    while changed and city:
        changed = False
        for s in sorted(_STOP, key=len, reverse=True):
            if city.startswith(s) and len(city) > len(s):
                city = city[len(s):]
                changed = True
                break
    if city in _STOP or len(city) < 2:
        city = ''
    return city or (default or '')


def _get(url, timeout, referer=None):
    headers = {'User-Agent': _UA}
    if referer:
        headers['Referer'] = referer
    req = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


def _city_code(city, timeout):
    raw = _get(_SEARCH + urllib.parse.quote(city), timeout, _REFERER).strip()
    # 返回形如 ([{"ref":"101180101~henan~郑州~..."},...])
    raw = raw.strip('();')
    try:
        items = json.loads(raw)
    except ValueError:
        return ''
    if not items:
        return ''
    ref = (items[0].get('ref') or '').split('~')
    code = ref[0] if ref else ''
    # 只认城市级编码（9 位），景区的 11 位编码不要
    if len(code) == 9 and code.isdigit():
        return code
    for it in items:
        c = (it.get('ref') or '').split('~')[0]
        if len(c) == 9 and c.isdigit():
            return c
    return ''


def _num(s):
    m = re.search(r'-?\d+', s or '')
    return m.group(0) if m else ''


def fetch(city, timeout=6, cache_seconds=300):
    """返回 {city,temp,type,low,high,notice,tomorrow_type,tomorrow_low,tomorrow_high} 或 None"""
    if not city:
        return None
    key = city.strip()
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < cache_seconds:
            return hit[1]

    try:
        code = _city_code(key, timeout)
        if not code:
            return None
        data = json.loads(_get(_DETAIL + code, timeout))
        d = data.get('data') or {}
        fc = (d.get('forecast') or [])
        if not fc:
            return None
        today = fc[0]
        out = {
            'city': ((data.get('cityInfo') or {}).get('city') or key).replace('市', ''),
            'temp': _num(d.get('wendu')),
            'type': (today.get('type') or '').strip(),
            'low': _num(today.get('low')),
            'high': _num(today.get('high')),
            'notice': (today.get('notice') or '').strip(),
            'tomorrow_type': (fc[1].get('type') or '').strip() if len(fc) > 1 else '',
            'tomorrow_low': _num(fc[1].get('low')) if len(fc) > 1 else '',
            'tomorrow_high': _num(fc[1].get('high')) if len(fc) > 1 else '',
            'tomorrow_notice': (fc[1].get('notice') or '').strip() if len(fc) > 1 else '',
        }
        with _lock:
            _cache[key] = (now, out)
        return out
    except Exception:
        return None


def answer(q, default_city='', timeout=6, cache_seconds=300):
    """把天气数据说成一句人话；查不到返回空串。"""
    city = extract_city(q, default_city)
    w = fetch(city, timeout, cache_seconds)
    if not w:
        return '', city
    tmrw = '明天' in (q or '')
    if tmrw and w['tomorrow_type']:
        head = '%s明天%s，%s到%s度' % (w['city'], w['tomorrow_type'],
                                     w['tomorrow_low'], w['tomorrow_high'])
        tail = w['tomorrow_notice'] or ''
    else:
        head = '%s今天%s，%s到%s度' % (w['city'], w['type'], w['low'], w['high'])
        if w['temp']:
            head += '，现在%s度' % w['temp']
        tail = w['notice'] or ''
    if tail and len(tail) > 16:
        cut = max(tail.find(p) for p in '，。；')
        tail = tail[:cut] if cut > 6 else tail[:16]
    text = head + ('，%s。' % tail if tail else '。')
    return text, w['city']


if __name__ == '__main__':
    import sys
    q = ' '.join(sys.argv[1:]) or '郑州什么天气'
    print('问：', q)
    print('城市：', extract_city(q, '郑州'))
    print('答：', answer(q, '郑州')[0] or '（查不到）')

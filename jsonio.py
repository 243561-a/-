# coding: utf-8
"""配置文件的安全写入。

为什么需要：控制台保存配置时，pipeline 每 200ms 会检查一次文件并热加载。
如果直接用 open(path,'w') 覆写，中间存在"文件已被截断、内容还没写完"的瞬间，
对方读到的就是半截 JSON。所以统一走"写临时文件 → 原子替换"。

Windows 上 os.replace 可能被占用（对方正好打开着读），重试几次；
实在不行退回普通覆写，保证数据不丢（此时才可能出现半秒的不一致）。
"""

import json
import os
import time


def write_json(path, obj, indent=2, retries=5):
    """原子写 JSON。返回 True 表示成功。"""
    text = json.dumps(obj, ensure_ascii=False, indent=indent)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    for i in range(retries):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            # 对方正在读（Windows 独占），等一会儿再试
            time.sleep(0.04 * (i + 1))
        except OSError:
            break
    # 兜底：直接覆写，至少别把用户的修改丢掉
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return True


def read_json(path):
    """读 JSON，失败抛异常（调用方决定怎么兜）"""
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

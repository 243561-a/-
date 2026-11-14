#!/usr/bin/env python
# coding: utf-8
"""
采集服务（douyinLive.exe）的公共管理

控制台和 pipeline 都要用：没起就拉起来，避免「点了开始 / 跑了脚本却没反应」。
"""

import os
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GO_BIN = os.path.join(HERE, 'bin', 'douyinLive.exe')
GO_CFG = os.path.join(HERE, 'bin', 'config.yaml')
GO_HEALTH = 'http://127.0.0.1:1088/health'


def go_alive():
    import urllib.request
    try:
        with urllib.request.urlopen(GO_HEALTH, timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_backend(quiet=True):
    """采集服务没起就拉起来。返回 (ok, msg)"""
    if go_alive():
        return True, '采集服务已在运行'
    if not os.path.isfile(GO_BIN):
        return False, '找不到采集服务：%s' % GO_BIN
    if not os.path.isfile(GO_CFG):
        return False, '找不到配置文件：%s（Cookie 配在这里）' % GO_CFG
    try:
        subprocess.Popen([GO_BIN, '-config', 'config.yaml', '-port', '1088',
                          '-log-level', 'info'],
                         cwd=os.path.dirname(GO_BIN),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception as e:
        return False, '采集服务启动失败：%s' % e
    for _ in range(16):
        time.sleep(0.5)
        if go_alive():
            return True, '采集服务已启动'
    return False, '采集服务启动后没响应（1088 端口未就绪）'

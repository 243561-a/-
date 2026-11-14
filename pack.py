#!/usr/bin/env python
# coding: utf-8
"""把项目打包成"拷到另一台电脑即可用"的压缩包。

只复制白名单里的文件，避免把 .venv / node_modules / vendor / 日志 / 浏览器配置
这些又大又不可移植的东西带进去。

用法：双击 打包.bat，或 python pack.py
产出：打包输出/RobotLive-YYYYmmdd-HHmm.zip
"""

import os
import shutil
import subprocess
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, '打包输出')
STAGE = os.path.join(OUT, 'RobotLive')

# 根目录下的必需文件
FILES = [
    # 启动入口
    '启动控制台.bat', '启动直播.bat', '安装依赖.bat',
    # 维护工具（新电脑上也能重新打包 / 改完代码再出一版）
    '打包.bat', 'pack.py',
    # 说明与依赖
    'requirements.txt', 'robot.conf', 'robot.conf.example',
    '迁移与注意事项.md', 'README.md', 'setup.py',
    # 主程序
    'gui.py', 'pipeline.py', 'admin.py', 'backend.py', 'rules.py',
    'reply_engine.py', 'speaker.py', 'weather.py', 'bans.py', 'go_collector.py',
    'jsonio.py', 'interactions.py', 'knowledge.py', 'experience.py',
    # 运行配置
    'replies.json', 'gifts.json', 'bans.json', 'interactions.json',
    'knowledge.md', 'gui-state.json', 'admin-token.txt',
    # 备用与自测
    'cookie.txt', 'replay-sample.json', 'replay-interactions.json', 'test-reply.json',
    'test_dedup.py', 'test_reply_queue.py', 'test_speak.py', 'test_atomic.py',
    'test_interactions.py', 'test_knowledge.py', 'test_experience.py',
    'test_reply_kb.py',
    # 一键获取 Cookie / 识别直播间号（需要 node，可选功能）
    'login-and-grab.mjs', 'detect-room.mjs', 'package.json', 'package-lock.json',
]

# 子目录：目录名 -> 要带的文件
DIRS = {
    'bin': ['douyinLive.exe', 'config.yaml', 'LICENSE', 'README.md'],
    'secrets': ['qiyuan_t1', 'llm_key'],
}

# 整棵目录树都带走：一键获取 Cookie 依赖 playwright-core，
# 不带的话新电脑上那个按钮就用不了（只能手动粘贴）
TREES = ['node_modules']


def _unlock(stage):
    """上次如果在打包目录里跑过安装自检，私钥会被 icacls 改成只读，覆盖会失败。
    这里先把整个暂存目录的权限恢复成继承，再复制。"""
    if os.name != 'nt' or not os.path.isdir(stage):
        return
    try:
        subprocess.run(['icacls', stage, '/reset', '/t', '/q'],
                       capture_output=True, timeout=60)
    except Exception:
        pass


def main():
    # 不用整目录删除（会被安全策略拦），直接覆盖复制；
    # 想要全新目录的话，先自己删掉 打包输出\RobotLive
    os.makedirs(STAGE, exist_ok=True)
    _unlock(STAGE)

    missing, copied = [], 0
    for name in FILES:
        src = os.path.join(HERE, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(STAGE, name))
            copied += 1
        else:
            missing.append(name)

    for d, names in DIRS.items():
        os.makedirs(os.path.join(STAGE, d), exist_ok=True)
        for name in names:
            src = os.path.join(HERE, d, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(STAGE, d, name))
                copied += 1
            else:
                missing.append('%s/%s' % (d, name))

    for d in TREES:
        src = os.path.join(HERE, d)
        if not os.path.isdir(src):
            missing.append(d + '/')
            continue
        for root, _subdirs, files in os.walk(src):
            rel = os.path.relpath(root, HERE)
            os.makedirs(os.path.join(STAGE, rel), exist_ok=True)
            for fn in files:
                shutil.copy2(os.path.join(root, fn), os.path.join(STAGE, rel, fn))
                copied += 1

    stamp = time.strftime('%Y%m%d-%H%M')
    zip_path = os.path.join(OUT, 'RobotLive-%s.zip' % stamp)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(STAGE):
            for fn in files:
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, OUT)   # 压缩包内保留 RobotLive/ 顶层目录
                z.write(full, arc)

    size_mb = os.path.getsize(zip_path) / 1024.0 / 1024.0
    raw_mb = sum(os.path.getsize(os.path.join(r, f))
                 for r, _d, fs in os.walk(STAGE) for f in fs) / 1024.0 / 1024.0

    print('打包完成')
    print('  文件 %d 个，原始 %.1f MB，压缩后 %.1f MB' % (copied, raw_mb, size_mb))
    print('  压缩包：%s' % zip_path)
    if missing:
        print('\n以下文件不存在，已跳过：')
        for m in missing:
            print('  -', m)
    print('\n拷到新电脑后：解压 → 双击「安装依赖.bat」→ 改 robot.conf 的 host → 双击「启动控制台.bat」')
    print('注意：包里含 SSH 私钥、API Key、抖音 Cookie，别外传。')


if __name__ == '__main__':
    main()

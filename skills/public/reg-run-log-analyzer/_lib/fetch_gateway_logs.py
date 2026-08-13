#!/usr/bin/env python3
"""通过 docker.sock 直连 Docker API 拉取 gateway 容器日志，按 thread_id 过滤。

沙箱镜像无 docker CLI 时使用（python3 + unix socket 标准库即可，零依赖）。
依赖：/var/run/docker.sock 已挂载进沙箱（config.yaml sandbox.mounts）。

用法：
  python3 fetch_gateway_logs.py WN_PH-Platform-262327
  python3 fetch_gateway_logs.py WN_PH-Platform-262327 --since-hours 72
  python3 fetch_gateway_logs.py WN_PH-Platform-262327 --tail 2000
  python3 fetch_gateway_logs.py Run-created        # 特殊：列出最近创建的 run
"""
import argparse
import socket
import struct
import sys
import time

SOCK = '/var/run/docker.sock'
CONTAINER = 'deer-flow-gateway'


def http_get(path: str) -> tuple[int, bytes]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(SOCK)
    except PermissionError:
        print(
            'ERROR: 无权限访问 /var/run/docker.sock（容器用户不在 docker 组）\n'
            '修复：宿主执行 chmod 666 /var/run/docker.sock，并参考 SKILL.md 的持久化方法',
            file=sys.stderr,
        )
        sys.exit(2)
    except FileNotFoundError:
        print(
            'ERROR: /var/run/docker.sock 不存在，请确认 config.yaml sandbox.mounts 已挂载 docker.sock 且 gateway 已重启',
            file=sys.stderr,
        )
        sys.exit(2)
    req = f'GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'
    s.sendall(req.encode())
    data = b''
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b'\r\n\r\n')
    try:
        status = int(head.split(b' ', 2)[1])
    except (IndexError, ValueError):
        return 0, data
    return status, body


def parse_logs(body: bytes) -> str:
    """Docker logs API 返回 8 字节帧头(1流类型+3保留+4长度) + 数据块。"""
    out = bytearray()
    i = 0
    while i + 8 <= len(body):
        size = struct.unpack('>I', body[i + 4:i + 8])[0]
        if size == 0 or i + 8 + size > len(body):
            break
        out += body[i + 8:i + 8 + size]
        i += 8 + size
    if not out:
        out = bytearray(body)  # 兼容非帧格式（如 404 错误体）
    return bytes(out).decode('utf-8', 'replace')


def main() -> None:
    ap = argparse.ArgumentParser(description='拉取 gateway 容器日志并过滤 thread_id')
    ap.add_argument('thread_id', help='如 WN_PH-Platform-262327；传 Run-created 列出最近 run')
    ap.add_argument('--tail', type=int, default=500, help='拉取尾部行数，默认 500')
    ap.add_argument('--since-hours', type=float, default=24, help='时间窗口（小时），默认 24')
    args = ap.parse_args()

    since = int(time.time() - args.since_hours * 3600)
    path = (
        f'/containers/{CONTAINER}/logs?stdout=1&stderr=1&timestamps=1'
        f'&since={since}&tail={args.tail}'
    )
    status, body = http_get(path)
    if status != 200:
        print(
            f'ERROR: Docker API 返回 {status}: {body[:300].decode("utf-8", "replace")}',
            file=sys.stderr,
        )
        sys.exit(1)

    text = parse_logs(body)
    if args.thread_id == 'Run-created':
        for line in text.splitlines():
            if 'Run created' in line:
                print(line)
        return
    for line in text.splitlines():
        if args.thread_id in line:
            print(line)


if __name__ == '__main__':
    main()

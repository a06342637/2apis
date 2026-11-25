"""root systemd 更新助手，只在本地 Unix socket 接受固定动作。"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

from src.instance_lock import InstanceLock
from src.updater.deployment import HostConfig
from src.updater.service import UpdateService


class UnixHTTPServer(ThreadingMixIn, HTTPServer):
    # Windows 可以导入测试/查看帮助；main 在创建服务器前拒绝非 POSIX 主机。
    address_family = getattr(socket, "AF_UNIX", None)
    daemon_threads = True

    def server_bind(self):
        self.socket.bind(self.server_address)
        self.server_name = "localhost"
        self.server_port = 0


def handler_for(service: UpdateService, secret: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            # 不记录请求体、secret 或带认证的客户端地址。
            return

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/action" or not 0 < length <= 32768:
                    raise ValueError("无效的助手请求")
                request = json.loads(self.rfile.read(length))
                supplied = request.get("secret")
                if not isinstance(supplied, str) or not hmac.compare_digest(supplied, secret):
                    raise ValueError("更新助手认证失败")
                result = service.dispatch(request.get("action"), request.get("payload", {}))
                body = {"ok": True, "result": result}
            except ValueError as error:
                body = {"ok": False, "error": str(error)}
            except (KeyError, TypeError):
                body = {"ok": False, "error": "无效的助手请求结构"}
            except Exception:
                body = {"ok": False, "error": "主机助手处理失败，请检查 GitHub 访问与部署状态"}
            encoded = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.name != "posix" or os.geteuid() != 0:
        raise SystemExit("主机更新助手需要 Linux root systemd 服务")
    if args.config.stat().st_uid != 0 or args.config.stat().st_mode & 0o022:
        raise SystemExit("助手配置必须由 root 持有且不可由组/其他用户写入")
    config = HostConfig.load(args.config)
    secret = config.secret_file.read_text().strip()
    if len(secret) < 32:
        raise SystemExit("助手认证 secret 至少 32 个字符")
    # InstanceLock.__enter__ 取锁；hold_instance_lock 已取锁，不能再进入其 context。
    with InstanceLock(config.state_dir):
        service = UpdateService(config)
        service.recover()
        config.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        config.socket_path.unlink(missing_ok=True)
        with UnixHTTPServer(str(config.socket_path), handler_for(service, secret)) as server:
            # Unix socket 的 connect 需要写权限；仅专用应用组可连接，父目录 0750。
            os.chmod(config.socket_path, 0o660)  # nosec B103 - IPC 专用组，其他用户无权限
            server.serve_forever()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""生成 Web Push 需要的 VAPID 密钥对。

为什么需要这个脚本：
    「推送订阅」在页面上能点、订阅也能存进数据库，但一条都发不出去——因为
    vapid_private_key_configured() 一直是 False：既没有 data/vapid_private.pem，
    环境变量 WORKBENCH_VAPID_PRIVATE_KEY / _PUBLIC_KEY 也从来没配过，而项目里
    也没有任何生成密钥的工具。于是这个功能从上线起就是"存了不发"的状态。

    这个脚本补上缺失的第一步。

用法：
    python3 scripts/generate-vapid-keys.py                # 写入 data/vapid_private.pem 并打印公钥
    python3 scripts/generate-vapid-keys.py --print-only   # 只打印，不写文件
    python3 scripts/generate-vapid-keys.py --force        # 覆盖已存在的私钥（会让现有订阅全部失效）

生成之后：
    1. 把脚本打印的两行加进服务器的 .env
    2. systemctl restart workbench
    3. 在工作台点「推送订阅」重新订阅一次（换了密钥，旧订阅必须重新授权）
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError:  # pragma: no cover - 依赖缺失时给出可执行的提示
    print("缺少 cryptography：请先 pip install -r requirements-optional.txt", file=sys.stderr)
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KEY_FILE = ROOT / "data" / "vapid_private.pem"


def urlsafe_b64(raw: bytes) -> str:
    """VAPID 用的是不带 padding 的 URL-safe base64。"""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_keypair() -> tuple[bytes, str]:
    # Web Push 规范（RFC 8292）要求 P-256 上的 ECDSA。
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_numbers = private_key.public_key().public_numbers()
    # 公钥是未压缩点格式：0x04 || X(32) || Y(32)
    raw_public = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
    return pem, urlsafe_b64(raw_public)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Workbench Web Push 的 VAPID 密钥对")
    parser.add_argument("--key-file", default=str(DEFAULT_KEY_FILE), help=f"私钥写入路径，默认 {DEFAULT_KEY_FILE}")
    parser.add_argument("--print-only", action="store_true", help="只打印，不写任何文件")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的私钥文件")
    parser.add_argument("--subject", default="mailto:workbench@localhost", help="VAPID subject，通常是 mailto: 邮箱")
    args = parser.parse_args()

    key_file = Path(args.key_file).expanduser()
    if not args.print_only and key_file.exists() and not args.force:
        print(f"私钥已存在：{key_file}", file=sys.stderr)
        print("换密钥会让所有已有订阅立即失效，需要每台设备重新授权。", file=sys.stderr)
        print("确认要重新生成请加 --force。", file=sys.stderr)
        return 1

    pem, public_key = build_keypair()

    if args.print_only:
        print(pem.decode("ascii"))
    else:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        # 私钥必须只有服务账号自己能读。
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
        os.chmod(key_file, 0o600)
        print(f"私钥已写入 {key_file}（权限 600）")

    print()
    print("把下面两行加进服务器的 .env：")
    print()
    print(f"WORKBENCH_VAPID_PUBLIC_KEY={public_key}")
    print(f"WORKBENCH_VAPID_SUBJECT={args.subject}")
    if not args.print_only:
        print(f"# 私钥默认从 {key_file} 读取；如需改路径用 WORKBENCH_VAPID_PRIVATE_KEY_FILE")
    print()
    print("然后 systemctl restart workbench，再到工作台点「推送订阅」重新订阅一次。")
    print("注意：公钥是浏览器订阅时用的，换密钥后旧订阅会全部失效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

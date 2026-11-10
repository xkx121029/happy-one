"""命令行入口。

常用：
    python -m happyone.cli migrate
    python -m happyone.cli create-user --username admin --admin
    python -m happyone.cli create-client --client-id app_market \
        --name "开心软件市场" --redirect-uri http://127.0.0.1:53682/callback \
        --scope "openid profile email offline_access"
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from typing import Sequence

from happyone.audit.service import AuditService
from happyone.authz.service import OAuthService
from happyone.config import Settings, set_global_settings
from happyone.core.errors import AppError
from happyone.core.security import Ed25519Signer
from happyone.db import Database, bind_database
from happyone.identity.service import IdentityService

_log = logging.getLogger("happyone.cli")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="happyone", description="happy-one 运维命令")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="执行数据库迁移")
    sub.add_parser("list-clients", help="列出已登记的 OIDC 客户端")

    user = sub.add_parser("create-user", help="创建用户")
    user.add_argument("--username", required=True)
    user.add_argument("--password", help="不传则交互输入（推荐）")
    user.add_argument("--email")
    user.add_argument("--display-name")
    user.add_argument("--admin", action="store_true", help="创建为管理员")

    client = sub.add_parser("create-client", help="登记 OIDC 客户端")
    client.add_argument("--client-id", required=True)
    client.add_argument("--name")
    client.add_argument("--type", choices=["public", "confidential"], default="public")
    client.add_argument(
        "--redirect-uri",
        action="append",
        required=True,
        help="可重复传入，需完整匹配",
    )
    client.add_argument(
        "--scope",
        default="openid profile email offline_access",
        help="空格分隔，必须是客户端允许的 scope",
    )
    client.add_argument("--secret", help="机密客户端密钥；不传则自动生成并打印一次")
    return parser


def _prepare(settings: Settings) -> tuple[Database, IdentityService]:
    set_global_settings(settings)
    settings.ensure_dirs()
    database = Database(settings.database_path)
    bind_database(database)
    database.migrate(settings.migrations_path)
    return database, IdentityService(database, settings)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(levelname)s %(message)s",
    )

    try:
        database, identity = _prepare(settings)
        audit = AuditService(database)

        if args.command == "migrate":
            status = database.migration_status()
            print(f"迁移完成，共 {len(status)} 个版本：")
            for item in status:
                print(f"  {item.version}  {item.applied_at}  {item.checksum[:12]}")
            return 0

        if args.command == "create-user":
            password = args.password
            if not password:
                password = getpass.getpass("密码：")
                confirm = getpass.getpass("确认密码：")
                if password != confirm:
                    print("两次输入的密码不一致", file=sys.stderr)
                    return 2
            user = identity.create_user(
                username=args.username,
                password=password,
                email=args.email,
                display_name=args.display_name,
                is_admin=args.admin,
            )
            audit.record(
                action="cli.user.create",
                target_type="user",
                target_id=user.id,
                detail={"username": user.username, "is_admin": user.is_admin},
            )
            print(f"已创建用户：{user.username}（{user.id}）管理员={user.is_admin}")
            return 0

        if args.command == "create-client":
            signer = Ed25519Signer.load_or_create(settings.oidc_signing_key_path)
            oauth = OAuthService(database, settings, signer, users=identity)
            client, secret = oauth.create_client(
                client_id=args.client_id,
                name=args.name or args.client_id,
                client_type=args.type,
                redirect_uris=args.redirect_uri,
                scopes=args.scope.split(),
                client_secret=args.secret,
            )
            audit.record(
                action="cli.client.create",
                target_type="client",
                target_id=client.client_id,
                detail={"client_type": client.client_type, "scopes": client.scopes},
            )
            print(f"已登记客户端：{client.client_id}（{client.client_type}）")
            print(f"  名称：{client.name}")
            print(f"  回调：{', '.join(client.redirect_uris)}")
            print(f"  scope：{' '.join(client.scopes)}")
            if secret:
                print(f"  客户端密钥（仅显示一次，请立即保存）：{secret}")
            return 0

        if args.command == "list-clients":
            signer = Ed25519Signer.load_or_create(settings.oidc_signing_key_path)
            oauth = OAuthService(database, settings, signer, users=identity)
            clients = oauth.list_clients()
            if not clients:
                print("暂无客户端")
                return 0
            for client in clients:
                print(
                    f"{client.client_id}\t{client.client_type}\t"
                    f"{client.status}\t{' '.join(client.scopes)}"
                )
            return 0

    except AppError as exc:
        print(f"[{exc.code}] {exc.message}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 需要给出可读错误
        _log.error("命令执行失败", exc_info=True)
        print(f"命令执行失败：{exc}", file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

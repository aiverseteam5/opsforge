"""`opsforge` command-line entry point (scaffolding + admin tasks).

  opsforge skill new <slug> [--dir skills]   scaffold a new skill pack
  opsforge skill install <dir>               validate + install one skill
  opsforge skills sync                        install all built-in skill packs
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def _set_win_loop() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def _create_token(name: str, role: str = "admin") -> str:
    """Mint an org-scoped API token bound to a user with the given role.

    The role gates Phase-2 approvals (admin/operator may approve actions).
    """
    from sqlalchemy import text

    from .config import get_settings
    from .db import session_factory
    from .security import generate_token

    raw, token_hash = generate_token()
    org_id = get_settings().org_id
    async with session_factory().begin() as s:
        user_id = (
            await s.execute(
                text(
                    "INSERT INTO users (org_id, email, name, role) "
                    "VALUES (:org, :email, :name, :role) RETURNING id"
                ),
                {
                    "org": org_id,
                    "email": f"{name}@cli.local",
                    "name": name,
                    "role": role,
                },
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO api_tokens (org_id, user_id, token_hash, name) "
                "VALUES (:org, :uid, :h, :n)"
            ),
            {"org": org_id, "uid": user_id, "h": token_hash, "n": name},
        )
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opsforge")
    sub = parser.add_subparsers(dest="group", required=True)

    skill = sub.add_parser("skill", help="single-skill commands")
    skill_sub = skill.add_subparsers(dest="action", required=True)

    p_new = skill_sub.add_parser("new", help="scaffold a new skill pack")
    p_new.add_argument("slug")
    p_new.add_argument("--dir", default="skills", help="destination root")

    p_install = skill_sub.add_parser("install", help="validate + install one skill dir")
    p_install.add_argument("directory")

    skills = sub.add_parser("skills", help="bulk-skill commands")
    skills_sub = skills.add_subparsers(dest="action", required=True)
    skills_sub.add_parser("sync", help="install all built-in skill packs")

    token = sub.add_parser("token", help="API token commands")
    token_sub = token.add_subparsers(dest="action", required=True)
    p_tok = token_sub.add_parser("create", help="mint an API token (printed once)")
    p_tok.add_argument("--name", default="cli")
    p_tok.add_argument(
        "--role", default="admin", choices=["admin", "operator", "viewer"]
    )

    args = parser.parse_args(argv)

    if args.group == "token" and args.action == "create":
        _set_win_loop()
        raw = asyncio.run(_create_token(args.name, args.role))
        print(raw)
        return 0

    # Import lazily so `skill new` (no DB) works without a database configured.
    from .skills import install_builtin_skills, install_skill_dir, scaffold_skill

    if args.group == "skill" and args.action == "new":
        path = scaffold_skill(args.slug, args.dir)
        print(f"created {path}")
        return 0

    if args.group == "skill" and args.action == "install":
        _set_win_loop()
        skill_id = asyncio.run(install_skill_dir(args.directory, source="org"))
        print(f"installed {args.directory} as skill {skill_id}")
        return 0

    if args.group == "skills" and args.action == "sync":
        _set_win_loop()
        ids = asyncio.run(install_builtin_skills())
        print(f"installed {len(ids)} built-in skill(s)")
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

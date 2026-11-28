"""scraper0 command line: serve | migrate | tick | export-recipes | import-recipes | create-admin | create-tenant | create-api-key | gen-secret-key."""
from __future__ import annotations

import argparse
import getpass
import os
import sys


def main(argv: list[str] | None = None) -> None:
    from scraper0.config import get_settings
    from scraper0.logging import configure_logging
    st = get_settings()
    configure_logging(st.log_format, st.log_level)

    ap = argparse.ArgumentParser(prog="scraper0")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the API server")
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("tick", help="run due scheduled sources once")
    a = sub.add_parser("create-admin", help="create a platform admin user")
    a.add_argument("email")
    t = sub.add_parser("create-tenant")
    t.add_argument("slug")
    t.add_argument("name")
    t.add_argument("--budget", type=float, default=20.0)
    k = sub.add_parser("create-api-key")
    k.add_argument("tenant_slug")
    k.add_argument("name")
    e = sub.add_parser("export-recipes", help="write every locked action version as a JSON file under DIR")
    e.add_argument("dir")
    e.add_argument("--prune", action="store_true", help="delete recipe files of actions that are no longer locked")
    i = sub.add_parser("import-recipes", help="load recipe files (a directory or a bundle file) as new versions")
    i.add_argument("path")
    i.add_argument("--lock", action="store_true", help="lock the imported versions (default: add them as drafts)")
    sub.add_parser("gen-secret-key", help="print a new Fernet key for SCRAPER0_SECRET_KEY")
    args = ap.parse_args(argv)

    if args.cmd == "gen-secret-key":
        from cryptography.fernet import Fernet
        print(Fernet.generate_key().decode())
        return
    if args.cmd == "serve":
        import uvicorn

        from scraper0.api.app import create_app
        port = int(os.getenv("PORT") or st.port)
        host = "0.0.0.0" if os.getenv("PORT") else st.host
        uvicorn.run(create_app(), host=host, port=port, log_config=None, proxy_headers=True,
                    forwarded_allow_ips="*", timeout_keep_alive=75)
        return

    from scraper0.bootstrap import migrate
    migrate()
    from sqlalchemy import select

    from scraper0.db.models import Tenant
    from scraper0.db.session import session_scope
    if args.cmd == "migrate":
        print("migrated")
    elif args.cmd == "tick":
        from scraper0.jobs.scheduler import tick
        print(tick())
    elif args.cmd == "export-recipes":
        from scraper0.services import recipe_export
        r = recipe_export.export_dir(args.dir, prune=args.prune)
        print(f"{r['written']} written, {r['unchanged']} unchanged, {r['removed']} removed → {args.dir}")
    elif args.cmd == "import-recipes":
        from scraper0.services import recipe_export
        try:
            r = recipe_export.import_path(args.path, lock=args.lock)
        except recipe_export.RecipeError as err:
            sys.exit(str(err))
        print(f"{r['created']} created, {r['updated']} updated, {r['unchanged']} unchanged, {len(r['errors'])} failed")
        for bad in r["errors"]:
            print(f"  {bad['file']}: {bad['error']}", file=sys.stderr)
        if r["errors"]:
            sys.exit(1)
    elif args.cmd == "create-admin":
        from scraper0.services.auth import create_user
        pw = os.getenv("SCRAPER0_NEW_ADMIN_PASSWORD") or getpass.getpass("password (min 10 chars): ")
        with session_scope() as s:
            create_user(s, args.email, pw, "Admin", platform_admin=True)
        print(f"platform admin {args.email} created")
    elif args.cmd == "create-tenant":
        with session_scope() as s:
            s.add(Tenant(slug=args.slug, name=args.name, monthly_ai_budget_usd=args.budget))
        print(f"tenant {args.slug} created")
    elif args.cmd == "create-api-key":
        from scraper0.services.auth import create_api_key
        with session_scope() as s:
            t = s.scalar(select(Tenant).where(Tenant.slug == args.tenant_slug))
            if t is None:
                sys.exit(f"no tenant {args.tenant_slug}")
            _, raw = create_api_key(s, t.id, args.name)
        print(raw)


if __name__ == "__main__":
    main()

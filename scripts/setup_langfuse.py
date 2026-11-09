"""Generate private local Langfuse bootstrap settings without printing credentials."""
from __future__ import annotations

import argparse
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".env.langfuse"))
    parser.add_argument("--email", default="admin@localhost.local")
    args = parser.parse_args()
    values = {
        "IC_LANGFUSE_ENABLED": "true",
        "IC_LANGFUSE_CAPTURE_CONTENT": "false",
        "IC_LANGFUSE_PUBLIC_URL": "http://localhost:3000",
        "LANGFUSE_BASE_URL": "http://localhost:3000",
        "LANGFUSE_PROJECT_ID": "incident-copilot",
        "LANGFUSE_PUBLIC_KEY": "pk-lf-" + secrets.token_hex(16),
        "LANGFUSE_SECRET_KEY": "sk-lf-" + secrets.token_hex(32),
        "LANGFUSE_INIT_USER_EMAIL": args.email,
        "LANGFUSE_INIT_USER_PASSWORD": secrets.token_hex(24),
    }
    for name in ("POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "REDIS_PASSWORD", "MINIO_PASSWORD",
                 "SALT", "ENCRYPTION_KEY", "NEXTAUTH_SECRET"):
        values["LANGFUSE_" + name] = secrets.token_hex(32)
    # Exclusive creation prevents silently changing keys for an initialized database.
    with args.output.open("x", encoding="utf-8") as output:
        output.write("# Private local Langfuse credentials. Do not commit or share.\n")
        output.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    print(f"Created {args.output}. Initial account credentials are stored in that private file.")


if __name__ == "__main__":
    main()

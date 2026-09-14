from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path

from loadtests import ACCOUNT_POOL_SIZE


@dataclass(frozen=True, slots=True)
class LoadTestAccount:
    index: int
    email: str
    password: str
    display_name: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=True, sort_keys=True)

    @classmethod
    def from_json(cls, value: str) -> "LoadTestAccount":
        payload = json.loads(value)
        return cls(
            index=int(payload["index"]),
            email=str(payload["email"]).strip().lower(),
            password=str(payload["password"]),
            display_name=str(payload["display_name"]),
        )


def generate_accounts(
    *,
    count: int = ACCOUNT_POOL_SIZE,
    prefix: str = "loadtest",
    domain: str = "loadtest.invalid",
    password: str,
) -> list[LoadTestAccount]:
    if count < 1:
        raise ValueError("account count must be positive")
    if count < 400:
        raise ValueError("at least 400 accounts are required")
    if "@" in prefix or not prefix.strip():
        raise ValueError("account prefix must be a non-empty email local-part prefix")
    normalized_domain = domain.strip().lower()
    if not normalized_domain or "@" in normalized_domain:
        raise ValueError("account domain is invalid")
    if len(password) < 12:
        raise ValueError("load-test password must contain at least 12 characters")
    width = max(4, len(str(count)))
    return [
        LoadTestAccount(
            index=index,
            email=f"{prefix.strip().lower()}{index:0{width}d}@{normalized_domain}",
            password=password,
            display_name=f"Load Test User {index:0{width}d}",
        )
        for index in range(1, count + 1)
    ]


def write_accounts(path: Path, accounts: list[LoadTestAccount]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for account in accounts:
                handle.write(account.to_json())
                handle.write("\n")
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    path.chmod(0o600)


def load_accounts(path: Path) -> list[LoadTestAccount]:
    accounts = [
        LoadTestAccount.from_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not accounts:
        raise ValueError(f"account file is empty: {path}")
    emails = [account.email for account in accounts]
    if len(set(emails)) != len(emails):
        raise ValueError(f"account file contains duplicate email addresses: {path}")
    return accounts


def accounts_from_environment() -> list[LoadTestAccount]:
    password = os.getenv("LOADTEST_ACCOUNT_PASSWORD", "")
    if not password:
        raise ValueError("LOADTEST_ACCOUNT_PASSWORD is required")
    return generate_accounts(
        count=int(os.getenv("LOADTEST_ACCOUNT_POOL_SIZE", str(ACCOUNT_POOL_SIZE))),
        prefix=os.getenv("LOADTEST_ACCOUNT_PREFIX", "loadtest"),
        domain=os.getenv("LOADTEST_ACCOUNT_DOMAIN", "loadtest.invalid"),
        password=password,
    )


def configured_accounts(path: Path) -> list[LoadTestAccount]:
    return load_accounts(path) if path.is_file() else accounts_from_environment()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate isolated load-test accounts.")
    parser.add_argument("--output", type=Path, default=Path("loadtests/data/accounts.jsonl"))
    parser.add_argument("--count", type=int, default=ACCOUNT_POOL_SIZE)
    parser.add_argument("--prefix", default="loadtest")
    parser.add_argument("--domain", default="loadtest.invalid")
    parser.add_argument("--password", default=os.getenv("LOADTEST_ACCOUNT_PASSWORD", ""))
    args = parser.parse_args()
    if not args.password:
        parser.error("--password or LOADTEST_ACCOUNT_PASSWORD is required")
    accounts = generate_accounts(
        count=args.count,
        prefix=args.prefix,
        domain=args.domain,
        password=args.password,
    )
    write_accounts(args.output, accounts)
    print(json.dumps({"path": str(args.output), "account_count": len(accounts)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

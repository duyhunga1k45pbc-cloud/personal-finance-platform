from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request


def fetch_json(url: str, timeout: float) -> tuple[int, dict]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return int(response.status), json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"body": body[:500]}
        return int(exc.code), payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a deployed V1 API.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    checks = []
    ok = True
    for path, expected_status, expected_body_status in (
        ("/health/live", 200, "ok"),
        ("/health/ready", 200, "ready"),
    ):
        status, body = fetch_json(base + path, args.timeout)
        passed = status == expected_status and body.get("status") == expected_body_status
        ok = ok and passed
        checks.append(
            {
                "path": path,
                "status_code": status,
                "passed": passed,
                "body": body,
            }
        )

    print(json.dumps({"ok": ok, "checks": checks}, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

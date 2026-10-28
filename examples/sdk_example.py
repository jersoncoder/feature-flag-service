"""Evaluate flags in-process with the SDK, against a running server.

Run the curl walkthrough first (it creates the ``new-checkout`` flag), then:

    FLAGS_SDK_KEY=sdk.env_... python examples/sdk_example.py [base-url]
"""

from __future__ import annotations

import os
import sys

from flagservice.sdk import FlagClient


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    with FlagClient(base_url, os.environ["FLAGS_SDK_KEY"], poll_interval=10) as flags:
        if not flags.wait_until_ready(timeout=5):
            sys.exit("could not load the flag config")
        contexts: list[dict[str, object]] = [
            {"key": "user-17", "email": "ana@shop.example"},
            {"key": "user-42", "email": "bo@mail.example"},
            {},
        ]
        for context in contexts:
            result = flags.evaluate("new-checkout", context)
            print(f"{context!s:50} {result.variant!s:4} {result.reason} rule={result.rule_index}")

        # Typed getters return the default when a flag is missing or has another type.
        print("bool_value(new-checkout):", flags.bool_value("new-checkout", contexts[0], False))
        print("bool_value(no-such-flag):", flags.bool_value("no-such-flag", contexts[0], False))
        print("string_value(new-checkout):", flags.string_value("new-checkout", {}, "n/a"))

        users = 10_000
        enabled = sum(
            flags.bool_value("new-checkout", {"key": f"user-{i}"}, False) for i in range(users)
        )
        print(f"{enabled} of {users} generated users are in the 10% rollout")


if __name__ == "__main__":
    main()

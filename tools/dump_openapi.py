"""Write the API's OpenAPI document where the desktop's contract test reads it.

The desktop speaks to the API over JSON, and until now nothing checked that
the two agreed. Two defects shipped that way and were found by a person, not
by the suite: `GET /me` answers `{user, organization}` and the client decoded
a flat profile, so every sign-in ended in "the service answered something
unexpected"; and the client asked for `/meetings/{id}/detail`, a path the API
has never served.

Both are the same mistake - a contract written from memory - and both are
visible in this document. So it is committed, regenerated here, and checked in
CI: a route renamed or a field moved changes this file, and a changed file that
nobody regenerated fails the build before it reaches an installer.

A third way to disagree has nothing to do with the code: the **deployed**
server can be older than both. That is how a 44-second recording sat in
UPLOADING answering "the NovaBrief service answered 404" with the desktop, the
schema and the tests all correct - the VPS had not been redeployed since the
resumable-upload endpoint was merged. `--deployed` is the thirty-second check
that says so, and belongs after every deployment.

    python tools/dump_openapi.py            # rewrite the snapshot
    python tools/dump_openapi.py --check    # fail if it is out of date
    python tools/dump_openapi.py --deployed https://api.novabrief.cloud
                                            # does the running server match?

The first two need the API importable, which means `apps/api` and `packages`
on the path; `--deployed` needs neither, only the network:

    PYTHONPATH="apps/api:packages" python tools/dump_openapi.py
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

# The repository root, from tools/.
ROOT = Path(__file__).resolve().parent.parent

# Inside the crate that does the talking, so `include_str!` reaches it and so
# the snapshot moves with the code that depends on it.
SNAPSHOT = (
    ROOT / "apps" / "desktop" / "src-tauri" / "crates" / "api-client" / "contract" / "openapi.json"
)


def document() -> dict[str, object]:
    """The OpenAPI document, built from the routes as they stand."""
    # Imported here rather than at module scope: the script must be able to
    # print its own --help without a configured API.
    sys.path[:0] = [str(ROOT / "apps" / "api"), str(ROOT / "packages")]
    from app.main import create_app

    return create_app().openapi()


def rendered() -> str:
    """Stable text: sorted keys and a trailing newline, so a diff means something."""
    return json.dumps(document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def compare_deployed(base_url: str) -> int:
    """Say whether the running server serves what this repository describes.

    Paths only, and deliberately so. A field added to a response is something
    the desktop ignores; a path that is not there is a call that answers 404
    and an upload that never completes.
    """
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    url = base_url.rstrip("/") + "/api/v1/openapi.json"

    with urllib.request.urlopen(url, timeout=30) as answer:  # noqa: S310
        live = json.loads(answer.read().decode("utf-8"))

    here = set(snapshot["paths"])
    there = set(live.get("paths", {}))

    if missing := sorted(here - there):
        print(
            f"{base_url} is behind this repository. It does not serve:\n"
            + "".join(f"    {path}\n" for path in missing)
            + "Every desktop calling one of those gets a 404. Redeploy: see "
            "infra/deploy/README.md.",
            file=sys.stderr,
        )
        return 1

    if extra := sorted(there - here):
        # Not a failure: a server ahead of the checkout is what a deployment
        # looks like from a branch that has not pulled yet.
        print(f"{base_url} serves {len(extra)} path(s) this checkout does not: {extra}")

    print(f"{base_url} serves every path this repository describes")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if the snapshot is out of date",
    )
    parser.add_argument(
        "--deployed",
        metavar="URL",
        help="do not write; compare the snapshot against a running server",
    )
    arguments = parser.parse_args()

    if arguments.deployed:
        return compare_deployed(arguments.deployed)

    fresh = rendered()
    current = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.exists() else None

    if arguments.check:
        if current == fresh:
            print(f"{SNAPSHOT.relative_to(ROOT)} is up to date")
            return 0
        print(
            f"{SNAPSHOT.relative_to(ROOT)} is out of date.\n"
            "The API contract changed and the desktop's copy of it did not. Run:\n"
            '    PYTHONPATH="apps/api:packages" python tools/dump_openapi.py\n'
            "then look at what moved before committing: the desktop may need to "
            "change with it.",
            file=sys.stderr,
        )
        return 1

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    # The line ending is stated rather than left to the platform: on Windows
    # the default rewrites every one of them, and the file lands in Git as
    # CRLF against a repository that holds LF. Reading is unaffected - Python
    # translates on the way in - so --check would have gone on saying "up to
    # date" while every line of the diff was noise.
    with SNAPSHOT.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(fresh)
    print(f"wrote {SNAPSHOT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

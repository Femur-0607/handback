"""Run handback's app compatibility checks against freshly installed CLIs (CI only).

The dashboard runs the same checks (compat.run_checks) only after an update lands
on a user's machine. CI installs the newest published CLIs first, so a breaking
release surfaces as a GitHub issue before users see it. Nothing is recorded in
app-compat.json; every run checks whatever is installed.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from handback import compat  # noqa: E402

DASHBOARD_FOOTER = "_handback 현황판이"


def check(agents, now=None):
    """One compat-shaped result per agent; an unreadable version is itself a failure."""
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    versions = compat.current_versions({agent: compat.VERSION_READERS[agent] for agent in agents})
    results = []
    for agent in agents:
        version = versions.get(agent)
        rows = (compat.run_checks(agent) if version else
                [compat._row("installed version", False, "--version printed no version")])
        results.append({"agent": agent, "version": version or "unknown", "previous": None,
                        "ok": all(row["ok"] for row in rows), "checks": rows, "utc": stamp})
    return results


def issue_body(result, source):
    """compat.summary without its dashboard footer, plus where this run came from."""
    lines = compat.summary(result).splitlines()
    if lines and lines[-1].startswith(DASHBOARD_FOOTER):
        lines = lines[:-1]
    return "\n".join(lines + [f"_GitHub Actions 정기 점검이 연 이슈입니다 ({source})._"])


def job_summary(results, channel):
    lines = [f"### 앱 호환성 점검 — npm `{channel}`", "", "| 앱 | 버전 | 결과 |", "|---|---|---|"]
    lines += [f"| {compat.NAMES[r['agent']]} | {r['version']} | {'통과' if r['ok'] else '실패'} |"
              for r in results]
    for result in results:
        lines += ["", f"#### {compat.NAMES[result['agent']]}"]
        lines += [f"- {'OK  ' if row['ok'] else 'FAIL'} {row['check']}: {row['detail']}"
                  for row in result["checks"]]
    return "\n".join(lines) + "\n"


def _gh(*args, stdin=None):
    done = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=True)
    return done.stdout


def open_issue(title, body):
    """Create the issue unless an open one already has this exact title."""
    listed = json.loads(_gh("issue", "list", "--state", "open", "--search", f'"{title}" in:title',
                            "--json", "title,url") or "[]")
    existing = next((issue["url"] for issue in listed if issue["title"] == title), None)
    if existing:
        print(f"issue already open: {existing}")
        return
    print(_gh("issue", "create", "--title", title, "--body-file", "-", stdin=body).strip())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agents", nargs="+", default=["codex", "claude"], choices=compat.AGENTS)
    parser.add_argument("--channel", default="latest", help="npm dist-tag the CLIs came from")
    parser.add_argument("--open-issues", action="store_true", help="open a GitHub issue per failure")
    args = parser.parse_args(argv)

    results = check(args.agents)
    report = job_summary(results, args.channel)
    print(report)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as handle:
            handle.write(report)

    run = "/".join(os.environ.get(key, "") for key in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY"))
    source = f"npm {args.channel}" + (f", {run}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                                      if os.environ.get("GITHUB_RUN_ID") else "")
    failed = [result for result in results if not result["ok"]]
    for result in failed:
        title, body = compat.issue_title(result), issue_body(result, source)
        if args.open_issues:
            open_issue(title, body)
        else:
            print(f"--- would open: {title}\n{body}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

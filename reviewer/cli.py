"""Command line interface: reviewer analyze | ingest | serve | status."""

import argparse
import json
import sys
import webbrowser
from collections import Counter
from pathlib import Path

from reviewer import github, repo
from reviewer.analysis import analyze_revisions, ranking_input, report_data
from reviewer.ranking import validate
from reviewer.server import ReportServer
from reviewer.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reviewer", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    analyze = commands.add_parser("analyze", help="analyze a PR and write what the model should rank")
    analyze.add_argument("pr", nargs="?", help="number, [host/]owner/repo#n or URL; default: the current branch's PR")
    analyze.add_argument("--repo", help="[host/]owner/name, when pr is a number")
    analyze.add_argument("--out", type=Path, help="write the JSON here instead of stdout")

    ingest = commands.add_parser("ingest", help="store the model's ranking of an analysis")
    ingest.add_argument("pr")
    ingest.add_argument("ranking", type=Path)
    ingest.add_argument("--repo")
    ingest.add_argument("--model", help="the model that wrote the ranking")

    serve = commands.add_parser("serve", help="open the report of an analyzed PR")
    serve.add_argument("pr", nargs="?")
    serve.add_argument("--repo")
    serve.add_argument("--port", type=int, default=0, help="default: a free port")
    serve.add_argument("--no-open", action="store_true", help="don't open the browser")

    status = commands.add_parser("status", help="list analyzed PRs")
    status.add_argument("pr", nargs="?")
    status.add_argument("--repo")

    args = parser.parse_args(argv)
    commands = {"analyze": cmd_analyze, "ingest": cmd_ingest, "serve": cmd_serve, "status": cmd_status}
    try:
        return commands[args.command](args)
    except (github.GitHubError, repo.GitError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def resolve(spec: str | None, repo_option: str | None) -> github.PrRef:
    return github.parse_pr(spec, repo_option) if spec else github.current_pr()


def repo_key(pr: github.PrRef) -> str:
    return f"{pr.host}/{pr.owner}/{pr.repo}"


def cmd_analyze(args) -> int:
    pr = resolve(args.pr, args.repo)
    meta = github.pr_metadata(pr)
    git_dir = repo.ensure_clone(pr)
    repo.fetch_pr(git_dir, pr.number, meta["baseRefName"])
    base = repo.merge_base(git_dir, meta["baseRefOid"], meta["headRefOid"])
    analysis = analyze_revisions(git_dir, base, meta["headRefOid"])

    info = {
        "repo": repo_key(pr),
        "number": pr.number,
        "title": meta["title"],
        "url": meta["url"],
        "author": (meta.get("author") or {}).get("login"),
        "body": meta.get("body") or "",
        "base_ref": meta["baseRefName"],
        "base_sha": meta["baseRefOid"],
        "head_sha": meta["headRefOid"],
        "merge_base": base,
    }
    store = Store()
    store.save_analysis(repo_key(pr), pr.number, info, analysis.units, report_data(analysis))
    ranked = set(store.rankings(u.fingerprint for u in analysis.units))
    data = ranking_input(info, analysis, ranked)

    text = json.dumps(data, indent=2)
    if args.out:
        args.out.write_text(text)
    else:
        print(text)
    counts = data["counts"]
    hidden = sum(counts["hidden"].values())
    print(
        f"{pr}: {counts['units']} changed units, {hidden} hidden as noise, "
        f"{counts['ranked']} already ranked, {counts['to_rank']} to rank",
        file=sys.stderr,
    )
    return 0


def cmd_ingest(args) -> int:
    pr = resolve(args.pr, args.repo)
    store = Store()
    row = store.pr(repo_key(pr), pr.number)
    if row is None:
        print(f"error: {pr} has not been analyzed; run reviewer analyze first", file=sys.stderr)
        return 1
    units = [u for u in store.units(row["id"]) if u["noise"] is None]
    known = {u["fingerprint"] for u in units}
    expected = known - set(store.rankings(known))

    try:
        data = json.loads(args.ranking.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read {args.ranking}: {e}", file=sys.stderr)
        return 1
    if errors := validate(data, expected, known):
        print("error: invalid ranking:", *(f"  - {e}" for e in errors), sep="\n", file=sys.stderr)
        return 1

    store.save_rankings(data["units"], args.model or data.get("model"))
    store.save_summary(row["id"], row["head_sha"], data["overview"], data.get("architecture_summary"), data["components"])
    rankings = store.rankings(known)
    tiers = Counter(rankings[u["fingerprint"]]["tier"] for u in units if u["fingerprint"] in rankings)
    print(f"{pr}: stored {len(data['units'])} rankings; " + ", ".join(f"{n} {t}" for t, n in sorted(tiers.items())))
    return 0


def cmd_serve(args) -> int:
    pr = resolve(args.pr, args.repo)
    store = Store()
    row = store.pr(repo_key(pr), pr.number)
    if row is None:
        print(f"error: {pr} has not been analyzed; run reviewer analyze first", file=sys.stderr)
        return 1
    try:
        server = ReportServer(store, row["id"], args.port)
    except OSError as e:
        print(f"error: cannot listen on port {args.port}: {e.strerror}", file=sys.stderr)
        return 1
    print(f"Report for {pr}: {server.url}  (Ctrl-C to stop)", flush=True)
    if not args.no_open:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_status(args) -> int:
    store = Store()
    if args.pr:
        pr = resolve(args.pr, args.repo)
        rows = [r for r in [store.pr(repo_key(pr), pr.number)] if r]
    else:
        rows = store.prs()
    if not rows:
        print("no analyzed pull requests")
    for row in rows:
        units = store.units(row["id"])
        visible = [u for u in units if u["noise"] is None]
        ranked = store.rankings(u["fingerprint"] for u in visible)
        owner_repo = row["repo"].split("/", 1)[1]
        print(
            f"{owner_repo}#{row['number']}  {row['title']}\n"
            f"  head {row['head_sha'][:7]}, analyzed {row['analyzed_at']}: "
            f"{len(units)} units, {len(units) - len(visible)} hidden, "
            f"{sum(u['fingerprint'] in ranked for u in visible)}/{len(visible)} ranked"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

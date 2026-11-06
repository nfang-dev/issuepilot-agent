"""Command line entry point."""
import argparse
import json
import os
from pathlib import Path

from issuepilot.agent import investigate, propose_patch
from issuepilot.github_api import GitHubAPI, parse_issue_url


def main():
    parser = argparse.ArgumentParser(description="Investigate a public GitHub issue with a read-only LLM agent")
    parser.add_argument("issue_url", help="Example: https://github.com/psf/requests/issues/1234")
    parser.add_argument("--model", default="gpt-5-mini", help="OpenAI model supporting Responses tool calling")
    parser.add_argument("--output", default="investigation.md", help="Output markdown report path")
    parser.add_argument("--trace", default="tool-trace.json", help="Tool-call names/arguments, without source contents")
    parser.add_argument("--propose-patch", action="store_true", help="Generate a separate unverified patch suggestion (no writes to GitHub)")
    parser.add_argument("--patch-output", default="patch-proposal.md", help="Path for the unverified patch proposal")
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is required for an actual LLM agent run")
    from openai import OpenAI
    api = GitHubAPI(parse_issue_url(args.issue_url))
    client = OpenAI()
    result = investigate(api, client, model=args.model)
    Path(args.output).write_text(result.report + "\n", encoding="utf-8")
    Path(args.trace).write_text(json.dumps({"model": result.model, "tool_calls": result.tool_calls,
                                             "usage": result.usage, "status": result.status}, indent=2), encoding="utf-8")
    print(result.report)
    if result.status == "budget_limited":
        print("\nNote: Investigation stopped at the exploration budget; conclusions are provisional.")
    print(f"\nReport: {args.output} | Trace: {args.trace} | tool calls: {len(result.tool_calls)}")
    if args.propose_patch:
        proposal = propose_patch(api, client, result, model=args.model)
        Path(args.patch_output).write_text(proposal, encoding="utf-8")
        print(f"Draft (not applied or tested): {args.patch_output}")


if __name__ == "__main__":
    main()

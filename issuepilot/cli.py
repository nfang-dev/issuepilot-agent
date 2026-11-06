"""Command line entry point."""
import argparse
import json
import os
from pathlib import Path

from issuepilot.agent import investigate
from issuepilot.github_api import GitHubAPI, parse_issue_url


def main():
    parser = argparse.ArgumentParser(description="Investigate a public GitHub issue with a read-only LLM agent")
    parser.add_argument("issue_url", help="Example: https://github.com/psf/requests/issues/1234")
    parser.add_argument("--model", default="gpt-5-mini", help="OpenAI model supporting Responses tool calling")
    parser.add_argument("--output", default="investigation.md", help="Output markdown report path")
    parser.add_argument("--trace", default="tool-trace.json", help="Tool-call names/arguments, without source contents")
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is required for an actual LLM agent run")
    from openai import OpenAI
    api = GitHubAPI(parse_issue_url(args.issue_url))
    result = investigate(api, OpenAI(), model=args.model)
    Path(args.output).write_text(result.report + "\n", encoding="utf-8")
    Path(args.trace).write_text(json.dumps({"model": result.model, "tool_calls": result.tool_calls,
                                             "usage": result.usage}, indent=2), encoding="utf-8")
    print(result.report)
    print(f"\nReport: {args.output} | Trace: {args.trace} | tool calls: {len(result.tool_calls)}")


if __name__ == "__main__":
    main()

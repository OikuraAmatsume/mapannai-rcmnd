#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Agent Harness runner for the mapannai-rcmnd repository.

Each agent runs in a separate `codex exec --ephemeral` process so the context
boundary is explicit and enforced by file artifacts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4


HARNESS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = HARNESS_ROOT.parent
RUNS_ROOT = HARNESS_ROOT / "runs"
TEMPLATES_ROOT = HARNESS_ROOT / "templates"
SCHEMAS_ROOT = HARNESS_ROOT / "schemas"
CONFIG_PATH = HARNESS_ROOT / "config.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config() -> Dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def render_bullets(lines: List[str]) -> str:
    if not lines:
        return "- 无"
    return "\n".join(f"- {line}" for line in lines)


def read_template(name: str) -> str:
    return (TEMPLATES_ROOT / name).read_text(encoding="utf-8")


def ensure_runs_root() -> None:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)


def run_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"


def rel(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT))


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_command(
    command: List[str],
    cwd: Path = PROJECT_ROOT,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    if not allow_failure and completed.returncode != 0:
        raise RuntimeError(
            f"命令执行失败: {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return completed


def git_status_lines() -> List[str]:
    result = run_command(["git", "status", "--porcelain"])
    return [line for line in result.stdout.splitlines() if line.strip()]


def ensure_clean_worktree(config: Dict[str, Any]) -> None:
    if not config["git"].get("require_clean_worktree", True):
        return

    dirty = git_status_lines()
    if dirty:
        raise RuntimeError(
            "执行 Harness 前工作树必须是干净的。请先提交、暂存或清理这些改动：\n"
            + "\n".join(dirty)
        )


def git_commit_all(message: str) -> Optional[Dict[str, str]]:
    if not git_status_lines():
        return None

    run_command(["git", "add", "-A"])
    run_command(["git", "commit", "-m", message])
    sha = run_command(["git", "rev-parse", "HEAD"]).stdout.strip()
    return {"sha": sha, "message": message, "created_at": utc_now()}


def current_head_sha() -> str:
    return run_command(["git", "rev-parse", "HEAD"]).stdout.strip()


def changed_files_for_range(base_ref: str, head_ref: str) -> List[str]:
    result = run_command(["git", "diff", "--name-only", f"{base_ref}..{head_ref}"])
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def added_lines_for_range_file(base_ref: str, head_ref: str, file_path: str) -> str:
    result = run_command(
        ["git", "diff", "--unified=0", f"{base_ref}..{head_ref}", "--", file_path]
    )
    added = []
    for line in result.stdout.splitlines():
        if line.startswith("+++") or not line.startswith("+"):
            continue
        added.append(line[1:])
    return "\n".join(added)


def static_guard_findings(
    config: Dict[str, Any],
    base_ref: Optional[str],
    head_ref: Optional[str],
) -> List[Dict[str, str]]:
    if not base_ref or not head_ref:
        return []

    patterns = config["guardrails"].get("disallowed_database_patterns", [])
    ignored_suffixes = {".md", ".txt", ".rst"}
    findings: List[Dict[str, str]] = []
    for file_path in changed_files_for_range(base_ref, head_ref):
        if file_path.startswith("agent_harness/"):
            continue
        if Path(file_path).suffix.lower() in ignored_suffixes:
            continue
        added_lines = added_lines_for_range_file(base_ref, head_ref, file_path).lower()
        if not added_lines:
            continue
        for pattern in patterns:
            if pattern.lower() in added_lines:
                findings.append(
                    {
                        "severity": "critical",
                        "title": "触发数据库禁用规则",
                        "details": f"新增代码命中了禁止模式 `{pattern}`，需要回到 S3-only 设计。",
                        "file": file_path,
                    }
                )
    return findings


def create_state(run_identifier: str, intent: str) -> Dict[str, Any]:
    return {
        "run_id": run_identifier,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "status": "preparing",
        "raw_intent": intent,
        "user_answers": [],
        "artifacts": {},
        "commits": [],
        "review_rounds": [],
    }


def state_path(run_identifier: str) -> Path:
    return RUNS_ROOT / run_identifier / "state.json"


def load_state(run_identifier: str) -> Dict[str, Any]:
    path = state_path(run_identifier)
    if not path.exists():
        raise FileNotFoundError(f"找不到 run: {run_identifier}")
    return read_json(path)


def save_state(state: Dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    write_json(state_path(state["run_id"]), state)


def run_dir(run_identifier: str) -> Path:
    return RUNS_ROOT / run_identifier


def render_user_answers(answers: List[Dict[str, str]]) -> str:
    if not answers:
        return "- 暂无补充"
    chunks = []
    for index, item in enumerate(answers, start=1):
        chunks.append(f"{index}. [{item['answered_at']}] {item['text']}")
    return "\n".join(chunks)


def codex_exec(
    *,
    model: str,
    sandbox: str,
    schema_path: Optional[Path],
    prompt: str,
    output_path: Path,
    stdout_log: Path,
    stderr_log: Path,
) -> None:
    command = [
        "codex",
        "exec",
        "--ephemeral",
        "--color",
        "never",
        "--sandbox",
        sandbox,
        "-C",
        str(PROJECT_ROOT),
        "-m",
        model,
        "-o",
        str(output_path),
    ]
    if schema_path:
        command.extend(["--output-schema", str(schema_path)])
    command.append(prompt)

    completed = run_command(command, allow_failure=True)
    write_text(stdout_log, completed.stdout)
    write_text(stderr_log, completed.stderr)
    if completed.returncode != 0:
        raise RuntimeError(
            "codex exec 执行失败，请查看日志：\n"
            f"- stdout: {rel(stdout_log)}\n"
            f"- stderr: {rel(stderr_log)}"
        )
    if not output_path.exists():
        raise RuntimeError(f"Codex 未生成输出文件: {rel(output_path)}")


def run_agent1(state: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    current_run_dir = run_dir(state["run_id"])
    request_path = current_run_dir / "01_agent1_request.md"
    output_path = current_run_dir / "02_agent1_output.json"
    prompt_path = current_run_dir / "03_agent2_prompt.md"
    stdout_log = current_run_dir / "agent1.stdout.log"
    stderr_log = current_run_dir / "agent1.stderr.log"

    prompt = read_template("agent1_prompt.md").format(
        project_root=str(PROJECT_ROOT),
        guardrails=render_bullets(config["guardrails"]["hard_rules"]),
        design_context=render_bullets(config["guardrails"]["design_context"]),
        raw_intent=state["raw_intent"],
        user_answers=render_user_answers(state["user_answers"]),
    )
    write_text(request_path, prompt)

    codex_exec(
        model=config["codex"]["agent1_model"],
        sandbox="read-only",
        schema_path=SCHEMAS_ROOT / "agent1_output.schema.json",
        prompt=prompt,
        output_path=output_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )

    payload = read_json(output_path)
    write_text(prompt_path, payload["final_prompt_markdown"].rstrip() + "\n")

    state["artifacts"]["agent1_request"] = rel(request_path)
    state["artifacts"]["agent1_output"] = rel(output_path)
    state["artifacts"]["agent2_prompt"] = rel(prompt_path)
    state["agent1_output"] = payload
    state["status"] = (
        "awaiting_user_response"
        if payload["requires_user_confirmation"] or payload["better_solution_found"]
        else "ready_for_execution"
    )
    save_state(state)
    return payload


def build_agent2_request(
    config: Dict[str, Any],
    final_prompt: str,
) -> str:
    return read_template("agent2_prompt.md").format(
        project_root=str(PROJECT_ROOT),
        guardrails=render_bullets(config["guardrails"]["hard_rules"]),
        final_prompt=final_prompt.strip(),
    )


def run_agent2(
    *,
    state: Dict[str, Any],
    config: Dict[str, Any],
    prompt_text: str,
    prompt_file_name: str,
    output_file_name: str,
    commit_message: str,
) -> Optional[Dict[str, str]]:
    current_run_dir = run_dir(state["run_id"])
    request_path = current_run_dir / prompt_file_name
    output_path = current_run_dir / output_file_name
    stdout_log = current_run_dir / f"{output_file_name}.stdout.log"
    stderr_log = current_run_dir / f"{output_file_name}.stderr.log"

    request = build_agent2_request(config, prompt_text)
    write_text(request_path, request)

    codex_exec(
        model=config["codex"]["agent2_model"],
        sandbox="workspace-write",
        schema_path=None,
        prompt=request,
        output_path=output_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )

    commit = git_commit_all(commit_message)
    if commit:
        state["commits"].append(commit)

    state["artifacts"][prompt_file_name] = rel(request_path)
    state["artifacts"][output_file_name] = rel(output_path)
    save_state(state)
    return commit


def build_agent3_request(
    config: Dict[str, Any],
    baseline_commit: str,
    implementation_commit: str,
    final_prompt: str,
) -> str:
    return read_template("agent3_prompt.md").format(
        baseline_commit=baseline_commit,
        implementation_commit=implementation_commit,
        project_root=str(PROJECT_ROOT),
        guardrails=render_bullets(config["guardrails"]["hard_rules"]),
        design_context=render_bullets(config["guardrails"]["design_context"]),
        final_prompt=final_prompt.strip(),
    )


def merge_static_findings(
    review: Dict[str, Any],
    static_findings: List[Dict[str, str]],
) -> Dict[str, Any]:
    if not static_findings:
        return review

    merged = dict(review)
    merged["findings"] = list(review.get("findings", [])) + static_findings
    merged["passed"] = False
    checks = dict(review.get("constraint_checks", {}))
    checks["no_database_design_passed"] = False
    notes = list(checks.get("notes", []))
    notes.append("静态规则扫描命中数据库禁用模式，已自动判定为不通过。")
    checks["notes"] = notes
    merged["constraint_checks"] = checks
    if not merged.get("rework_prompt_markdown"):
        merged["rework_prompt_markdown"] = (
            "请移除本次改动中引入的数据库相关实现，保持无数据库设计，"
            "并把新增持久化或中间状态改为继续使用当前项目指定的 S3。"
        )
    return merged


def run_agent3(
    *,
    state: Dict[str, Any],
    config: Dict[str, Any],
    baseline_commit: str,
    final_prompt: str,
    implementation_commit: Optional[Dict[str, str]],
    round_index: int,
) -> Dict[str, Any]:
    current_run_dir = run_dir(state["run_id"])
    request_path = current_run_dir / f"30_agent3_request_round_{round_index}.md"
    output_path = current_run_dir / f"31_agent3_output_round_{round_index}.json"
    stdout_log = current_run_dir / f"31_agent3_output_round_{round_index}.stdout.log"
    stderr_log = current_run_dir / f"31_agent3_output_round_{round_index}.stderr.log"

    commit_sha = implementation_commit["sha"] if implementation_commit else current_head_sha()
    request = build_agent3_request(config, baseline_commit, commit_sha, final_prompt)
    write_text(request_path, request)

    codex_exec(
        model=config["codex"]["agent3_model"],
        sandbox="read-only",
        schema_path=SCHEMAS_ROOT / "agent3_output.schema.json",
        prompt=request,
        output_path=output_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )

    review = read_json(output_path)
    review = merge_static_findings(
        review,
        static_guard_findings(
            config,
            baseline_commit,
            commit_sha if implementation_commit else None,
        ),
    )
    write_json(output_path, review)

    round_record = {
        "round_index": round_index,
        "baseline_commit": baseline_commit,
        "implementation_commit": implementation_commit,
        "review_output": rel(output_path),
        "passed": review["passed"],
    }
    state["review_rounds"].append(round_record)
    state["artifacts"][f"agent3_request_round_{round_index}"] = rel(request_path)
    state["artifacts"][f"agent3_output_round_{round_index}"] = rel(output_path)
    save_state(state)
    return review


def prepare_command(args: argparse.Namespace) -> int:
    ensure_runs_root()
    config = load_config()
    if not args.intent.strip():
        raise RuntimeError("`--intent` 不能为空。")
    identifier = run_id()
    state = create_state(identifier, args.intent.strip())
    intent_path = run_dir(identifier) / "00_user_intent.md"
    write_text(intent_path, state["raw_intent"].rstrip() + "\n")
    state["artifacts"]["user_intent"] = rel(intent_path)
    save_state(state)

    payload = run_agent1(state, config)
    print(f"run_id: {identifier}")
    print(f"status: {state['status']}")
    print(f"artifacts: {rel(run_dir(identifier))}")
    if payload["better_solution_question"]:
        print("question:")
        print(payload["better_solution_question"])
    print("prompt:")
    print(payload["final_prompt_markdown"])
    if state["status"] == "awaiting_user_response":
        print(f"next: python3 {rel(HARNESS_ROOT / 'runner.py')} answer --run-id {identifier} --text \"你的补充说明\"")
    else:
        print(f"next: python3 {rel(HARNESS_ROOT / 'runner.py')} execute --run-id {identifier}")
    return 0


def answer_command(args: argparse.Namespace) -> int:
    config = load_config()
    if not args.text.strip():
        raise RuntimeError("`--text` 不能为空。")
    state = load_state(args.run_id)
    state["user_answers"].append({"answered_at": utc_now(), "text": args.text.strip()})
    save_state(state)

    payload = run_agent1(state, config)
    print(f"run_id: {state['run_id']}")
    print(f"status: {state['status']}")
    print(f"artifacts: {rel(run_dir(state['run_id']))}")
    if payload["better_solution_question"]:
        print("question:")
        print(payload["better_solution_question"])
    print("prompt:")
    print(payload["final_prompt_markdown"])
    if state["status"] == "awaiting_user_response":
        print(f"next: python3 {rel(HARNESS_ROOT / 'runner.py')} answer --run-id {state['run_id']} --text \"你的补充说明\"")
    else:
        print(f"next: python3 {rel(HARNESS_ROOT / 'runner.py')} execute --run-id {state['run_id']}")
    return 0


def execute_command(args: argparse.Namespace) -> int:
    config = load_config()
    state = load_state(args.run_id)
    agent1_output = state.get("agent1_output")
    if not agent1_output:
        raise RuntimeError("当前 run 还没有 Agent1 输出，无法执行。")

    if state["status"] == "awaiting_user_response" and not args.force:
        raise RuntimeError("Agent1 仍在等待用户确认。若确认继续，请使用 --force。")

    ensure_clean_worktree(config)
    state["status"] = "running"
    baseline_commit = state.get("baseline_commit") or current_head_sha()
    state["baseline_commit"] = baseline_commit
    save_state(state)

    final_prompt = agent1_output["final_prompt_markdown"]
    commit_prefix = config["git"].get("commit_prefix", "harness")

    implementation_commit = run_agent2(
        state=state,
        config=config,
        prompt_text=final_prompt,
        prompt_file_name="20_agent2_request_initial.md",
        output_file_name="21_agent2_output_initial.md",
        commit_message=f"{commit_prefix}({state['run_id']}): agent2 implementation",
    )

    review = run_agent3(
        state=state,
        config=config,
        baseline_commit=baseline_commit,
        final_prompt=final_prompt,
        implementation_commit=implementation_commit,
        round_index=0,
    )

    max_rework_rounds = int(config["execution"].get("max_rework_rounds", 1))
    round_index = 0
    while not review["passed"] and round_index < max_rework_rounds:
        round_index += 1
        rework_prompt = review["rework_prompt_markdown"].strip()
        implementation_commit = run_agent2(
            state=state,
            config=config,
            prompt_text=rework_prompt,
            prompt_file_name=f"40_agent2_rework_request_round_{round_index}.md",
            output_file_name=f"41_agent2_rework_output_round_{round_index}.md",
            commit_message=f"{commit_prefix}({state['run_id']}): agent3 rework round {round_index}",
        )
        review = run_agent3(
            state=state,
            config=config,
            baseline_commit=baseline_commit,
            final_prompt=final_prompt,
            implementation_commit=implementation_commit,
            round_index=round_index,
        )

    state["status"] = "passed" if review["passed"] else "failed"
    save_state(state)

    print(f"run_id: {state['run_id']}")
    print(f"status: {state['status']}")
    print("summary:")
    print(review["summary"])
    if review["findings"]:
        print("findings:")
        for finding in review["findings"]:
            location = f" [{finding['file']}]" if finding.get("file") else ""
            print(f"- {finding['severity']}: {finding['title']}{location}")
            print(f"  {finding['details']}")
    if state["commits"]:
        print("commits:")
        for commit in state["commits"]:
            print(f"- {commit['sha']} {commit['message']}")
    print(f"artifacts: {rel(run_dir(state['run_id']))}")
    return 0


def status_command(args: argparse.Namespace) -> int:
    state = load_state(args.run_id)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the project Agent Harness.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="运行 Agent1，生成最终执行 prompt。")
    prepare.add_argument("--intent", required=True, help="用户的原始改动意图。")
    prepare.set_defaults(func=prepare_command)

    answer = subparsers.add_parser("answer", help="补充回答 Agent1 的问题并重新生成 prompt。")
    answer.add_argument("--run-id", required=True, help="prepare 阶段生成的 run_id。")
    answer.add_argument("--text", required=True, help="你对 Agent1 问题的补充说明。")
    answer.set_defaults(func=answer_command)

    execute = subparsers.add_parser("execute", help="运行 Agent2 和 Agent3。")
    execute.add_argument("--run-id", required=True, help="prepare 阶段生成的 run_id。")
    execute.add_argument("--force", action="store_true", help="忽略 Agent1 的确认阻塞直接执行。")
    execute.set_defaults(func=execute_command)

    status = subparsers.add_parser("status", help="查看某个 run 的状态。")
    status.add_argument("--run-id", required=True, help="prepare 阶段生成的 run_id。")
    status.set_defaults(func=status_command)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[agent_harness] {exc}", file=sys.stderr)
        raise SystemExit(1)

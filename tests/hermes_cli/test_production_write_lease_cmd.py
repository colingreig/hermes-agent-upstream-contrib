from __future__ import annotations

import argparse
import json


def _parser():
    from hermes_cli.production_write_lease_cmd import register_parser
    parser = argparse.ArgumentParser()
    register_parser(parser.add_subparsers(dest="command"))
    return parser


def test_cli_registers_all_operator_actions():
    parser = _parser()
    commands = {
        "acquire": ["--resources", "fleet-config", "cron-jobs", "skills-policy", "--actor", "fleet-config-installer", "--session-id", "s", "--workspace", "w", "--repo", "hermes-agent", "--commit-sha", "a" * 40, "--reason", "test"],
        "heartbeat": ["--lease-id", "l", "--actor", "a", "--session-id", "s", "--fencing-token", "1"],
        "release": ["--lease-id", "l", "--actor", "a", "--session-id", "s", "--fencing-token", "1"],
        "status": [],
        "recover": ["--lease-id", "l", "--actor", "a", "--session-id", "s", "--fencing-token", "1", "--recovered-by", "operator", "--reason", "test", "--evidence-json", "{}"],
    }
    for action, arguments in commands.items():
        assert parser.parse_args(["production-write-lease", action, *arguments]).production_write_lease_action == action


def test_cli_returns_json_error_for_short_commit(capsys):
    parser = _parser()
    args = parser.parse_args([
        "production-write-lease", "acquire", "--actor", "fleet-config-installer",
        "--resources", "fleet-config", "cron-jobs", "skills-policy", "--session-id", "x",
        "--workspace", "w", "--repo", "hermes-agent", "--commit-sha", "short", "--reason", "test",
    ])
    assert args.func(args) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False

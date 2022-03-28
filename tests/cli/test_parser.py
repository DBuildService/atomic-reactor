"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import re

import pytest

from atomic_reactor import constants
from atomic_reactor.cli import parser, task

BUILD_DIR = "/build"
CONTEXT_DIR = "/context"
NAMESPACE = "test-namespace"
REQUIRED_COMMON_ARGS = ["--build-dir", BUILD_DIR, "--context-dir", CONTEXT_DIR,
                        "--namespace", NAMESPACE]
REQUIRED_PLATFORM_FOR_BINARY_BUILD = ["--platform", "x86_64"]

SOURCE_URI = "git://example.org/namespace/repo"

EXPECTED_ARGS = {
    "quiet": False,
    "verbose": False,
    "build_dir": BUILD_DIR,
    "context_dir": CONTEXT_DIR,
    "config_file": constants.REACTOR_CONFIG_FULL_PATH,
    "namespace": NAMESPACE,
    "user_params": None,
    "user_params_file": None,
}
EXPECTED_ARGS_BINARY_CONTAINER_BUILD = {
    **EXPECTED_ARGS,
    "platform": "x86_64",
}


def test_parse_args_version(capsys):
    with pytest.raises(SystemExit):
        parser.parse_args(["--version"])

    stdout = capsys.readouterr().out
    assert re.match(r"^\d+\.\d+\.(dev)?\d+$", stdout.strip())


@pytest.mark.parametrize(
    "cli_args, expect_parsed_args",
    [
        # required args only
        (
            ["task", *REQUIRED_COMMON_ARGS, "source-container-build"],
            {**EXPECTED_ARGS, "func": task.source_container_build},
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "source-container-exit"],
            {**EXPECTED_ARGS, "func": task.source_container_exit},
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "clone"],
            {**EXPECTED_ARGS, "func": task.clone},
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "binary-container-prebuild"],
            {**EXPECTED_ARGS, "func": task.binary_container_prebuild},
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "binary-container-build",
             *REQUIRED_PLATFORM_FOR_BINARY_BUILD],
            {**EXPECTED_ARGS_BINARY_CONTAINER_BUILD, "func": task.binary_container_build},
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "binary-container-postbuild"],
            {**EXPECTED_ARGS, "func": task.binary_container_postbuild},
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "binary-container-exit"],
            {**EXPECTED_ARGS, "func": task.binary_container_exit},
        ),
        # all common task args
        (
            ["task", *REQUIRED_COMMON_ARGS, "--config-file=config.yaml", "source-container-build"],
            {**EXPECTED_ARGS, "config_file": "config.yaml", "func": task.source_container_build},
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "--config-file=config.yaml", "clone"],
            {**EXPECTED_ARGS, "config_file": "config.yaml", "func": task.clone},
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "--config-file=config.yaml",
             "binary-container-prebuild"],
            {**EXPECTED_ARGS, "config_file": "config.yaml", "func": task.binary_container_prebuild},
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "--config-file=config.yaml",
             "binary-container-build", *REQUIRED_PLATFORM_FOR_BINARY_BUILD],
            {**EXPECTED_ARGS_BINARY_CONTAINER_BUILD, "config_file": "config.yaml",
             "func": task.binary_container_build},
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "--config-file=config.yaml",
             "binary-container-postbuild"],
            {**EXPECTED_ARGS, "config_file": "config.yaml",
             "func": task.binary_container_postbuild},
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "--config-file=config.yaml", "binary-container-exit"],
            {**EXPECTED_ARGS, "config_file": "config.yaml", "func": task.binary_container_exit},
        ),
    ],
)
def test_parse_args_valid(cli_args, expect_parsed_args):
    assert parser.parse_args(cli_args) == expect_parsed_args


@pytest.mark.parametrize(
    "cli_args, expect_error",
    [
        # missing subcommand
        ([], "the following arguments are required: subcommand"),
        # missing task
        (["task", *REQUIRED_COMMON_ARGS], "the following arguments are required: task"),
        # --verbose vs. --quiet
        (["--verbose", "--quiet"], "-q/--quiet: not allowed with argument -v/--verbose"),
        # --user-params vs. --user-params-file
        (
            ["task", *REQUIRED_COMMON_ARGS, "--user-params={}", "--user-params-file=up.json"],
            "--user-params-file: not allowed with argument --user-params",
        ),
        # args in the wrong place
        (
            ["task", *REQUIRED_COMMON_ARGS, "--verbose", "source-container-build"],
            "unrecognized arguments: --verbose",
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "--verbose", "source-container-exit"],
            "unrecognized arguments: --verbose",
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "--verbose", "clone"],
            "unrecognized arguments: --verbose",
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "--verbose", "binary-container-prebuild"],
            "unrecognized arguments: --verbose",
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "--verbose", "binary-container-postbuild"],
            "unrecognized arguments: --verbose",
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "--verbose", "binary-container-exit"],
            "unrecognized arguments: --verbose",
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "source-container-build", "--user-params={}"],
            "unrecognized arguments: --user-params",
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "source-container-exit", "--user-params={}"],
            "unrecognized arguments: --user-params",
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "clone", "--user-params={}"],
            "unrecognized arguments: --user-params",
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "binary-container-prebuild", "--user-params={}"],
            "unrecognized arguments: --user-params",
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "binary-container-postbuild", "--user-params={}"],
            "unrecognized arguments: --user-params",
        ),
        (
            ["task", *REQUIRED_COMMON_ARGS, "binary-container-exit", "--user-params={}"],
            "unrecognized arguments: --user-params",
        ),
        # missing common arguments
        (
            ["task", "source-container-build"],
            "the following arguments are required: --build-dir, --context-dir",
        ),
        (
            ["task", "source-container-exit"],
            "the following arguments are required: --build-dir, --context-dir",
        ),
        (
            ["task", "clone"],
            "the following arguments are required: --build-dir, --context-dir",
        ),
        (
            ["task", "binary-container-prebuild"],
            "the following arguments are required: --build-dir, --context-dir",
        ),
        (
            ["task", "binary-container-postbuild"],
            "the following arguments are required: --build-dir, --context-dir",
        ),
        (
            ["task", "binary-container-exit"],
            "the following arguments are required: --build-dir, --context-dir",
        ),
        # missing --platform for binary-container-build
        (
            ["task", *REQUIRED_COMMON_ARGS, "binary-container-build"],
            "the following arguments are required: --platform",
        )
    ],
)
def test_parse_args_invalid(cli_args, expect_error, capsys):
    with pytest.raises(SystemExit):
        parser.parse_args(cli_args)

    stderr = capsys.readouterr().err
    assert expect_error in stderr

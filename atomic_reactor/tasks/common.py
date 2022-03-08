"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import abc
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict, Any, ClassVar, Generic, TypeVar

from atomic_reactor import config
from atomic_reactor import dirs
from atomic_reactor import inner
from atomic_reactor import source
from atomic_reactor import util


@dataclass(frozen=True)
class TaskParams:
    """Task parameters (coming from CLI arguments)."""

    user_params_schema: ClassVar[str] = "schemas/user_params.json"

    build_dir: str
    context_dir: str
    config_file: str
    user_params: Dict[str, Any]

    # Note: do not give any attributes in this class default values, that would make dataclass
    #   inheritance difficult. If they should have defaults, define them in the CLI parser.

    @property
    def source(self) -> source.Source:
        """Source for the input files the task will operate on (e.g. a git repo)."""
        if "git_uri" not in self.user_params:
            raise ValueError(
                f"{self.__class__.__name__} instance has no source (no git_uri in user params)"
            )

        return source.GitSource(
            provider="git",
            uri=self.user_params["git_uri"],
            provider_params={
                "git_commit": self.user_params.get("git_ref"),
                "git_commit_depth": self.user_params.get("git_commit_depth"),
                "git_branch": self.user_params.get("git_branch"),
            },
            workdir=self.build_dir,
        )

    @classmethod
    def from_cli_args(cls, args: dict):
        """Create a TaskParams instance from CLI arguments."""
        args = cls._drop_known_unset_args(args)
        params_str = args.pop("user_params", None)
        params_file = args.pop("user_params_file", None)

        if params_str:
            user_params = util.read_yaml(params_str, cls.user_params_schema)
        elif params_file:
            user_params = util.read_yaml_from_file_path(params_file, cls.user_params_schema)
        else:
            raise ValueError("Did not receive user params. User params are currently required.")

        return cls(**args, user_params=user_params)

    @classmethod
    def _drop_known_unset_args(cls, args: dict) -> dict:
        # When an argument is not set on the CLI, argparse stores it as None. Drop those arguments
        #   to avoid accidentally setting required attributes to None, make sure we instead get
        #   a TypeError from __init__().
        # Drop only arguments defined on this class (or a parent class), if an unknown argument
        #   is received, we want a TypeError regardless of the value.
        # The CLI should be responsible for not letting any of this happen, but let's double-check.
        known_args = {f.name for f in fields(cls)}
        return {k: v for k, v in args.items() if v is not None or k not in known_args}


ParamsT = TypeVar("ParamsT", bound=TaskParams)


class Task(abc.ABC, Generic[ParamsT]):
    """Task; the main execution unit in atomic-reactor."""

    def __init__(self, params: ParamsT):
        """Initialize a Task."""
        self._params = params

    @abc.abstractmethod
    def execute(self):
        """Execute this task."""

    def get_build_dir(self) -> dirs.RootBuildDir:
        return dirs.RootBuildDir(Path(self._params.build_dir))

    def get_context_dir(self) -> dirs.ContextDir:
        return dirs.ContextDir(Path(self._params.context_dir))

    def load_workflow_data(self) -> inner.ImageBuildWorkflowData:
        context_dir = self.get_context_dir()
        return inner.ImageBuildWorkflowData.load_from_dir(context_dir)

    def load_config(self) -> config.Configuration:
        return config.Configuration(self._params.config_file)

"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import subprocess

import pytest

from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_pyrpkg_fetch_artefacts import DistgitFetchArtefactsPlugin
from osbs.utils import ImageName
from tests.stubs import StubSource
from flexmock import flexmock
from tests.constants import INPUT_IMAGE


class Y(object):
    dockerfile_path = None
    path = None


class X(object):
    image_id = INPUT_IMAGE
    source = Y()
    base_image = ImageName.parse('asd')


@pytest.fixture
def workflow(workflow):
    workflow.build_dir.init_build_dirs(["x86_64", "ppc64le"], workflow.source)
    return workflow


def test_distgit_fetch_artefacts_plugin(workflow):  # noqa
    sources_cmd = 'fedpkg sources'
    workflow.conf.conf['sources_command'] = sources_cmd

    # The for_all_platforms_copy works inside this directory.
    working_build_dir = workflow.build_dir.path / workflow.build_dir.platforms[0]
    expected_sources_outdir = working_build_dir / 'outdir'
    # subprocess.check_call should be called with these parameters.
    expected_check_call_cmd = sources_cmd.split()
    expected_check_call_cmd.append('--outdir')
    expected_check_call_cmd.append(str(expected_sources_outdir))

    expected_sources_files = (
        ("logo.png", b"image"),
        ("app.tar.gz", b"tar"),
    )

    def _mock_check_call(cmd, cwd=None):
        assert cmd == expected_check_call_cmd
        assert cwd == working_build_dir
        # These sources files must be downloaded
        for filename, data in expected_sources_files:
            expected_sources_outdir.joinpath(filename).write_bytes(data)

    (flexmock(subprocess)
     .should_receive('check_call')
     .replace_with(_mock_check_call)
     .once())

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': DistgitFetchArtefactsPlugin.key,
        }]
    )
    runner.run()

    def _assert(build_dir: BuildDir):
        for filename, data in expected_sources_files:
            sources_file = build_dir.path.joinpath(filename)
            assert sources_file.exists()
            assert sources_file.read_bytes() == data

    workflow.build_dir.for_each_platform(_assert)

    assert not expected_sources_outdir.exists()


def test_distgit_fetch_artefacts_failure(workflow, source_dir):  # noqa
    expected_command = 'fedpkg sources'
    workflow.conf.conf['sources_command'] = expected_command

    working_build_dir = workflow.build_dir.path / workflow.build_dir.platforms[0]
    expected_sources_outdir = working_build_dir / 'outdir'

    # subprocess.check_call must be called with these parameters.
    expected_sources_cmd = expected_command.split()
    expected_sources_cmd.append('--outdir')
    expected_sources_cmd.append(str(expected_sources_outdir))

    def _mock_check_call(cmd, cwd=None):
        assert cmd == expected_sources_cmd
        assert cwd == working_build_dir
        # Then, it is time make check_call fail
        raise IOError("critical error")

    (flexmock(subprocess)
     .should_receive("check_call")
     .replace_with(_mock_check_call)
     .once())

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': DistgitFetchArtefactsPlugin.key,
        }]
    )
    with pytest.raises(PluginFailedException):
        runner.run()


def test_distgit_fetch_artefacts_skip(tmpdir, workflow, caplog):  # noqa
    workflow.source = StubSource()
    workflow.source.path = str(tmpdir)

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': DistgitFetchArtefactsPlugin.key,
        }]
    )
    runner.run()

    log_msg = 'no sources command configuration, skipping plugin'
    assert log_msg in caplog.text

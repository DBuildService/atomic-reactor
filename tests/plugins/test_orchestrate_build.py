"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from pathlib import Path

from atomic_reactor.plugin import BuildCanceledException, PluginFailedException
from atomic_reactor.plugin import BuildStepPluginsRunner
from atomic_reactor.plugins.build_orchestrate_build import OrchestrateBuildPlugin
from atomic_reactor.util import df_parser
import atomic_reactor.util
from atomic_reactor.constants import (PLUGIN_ADD_FILESYSTEM_KEY,
                                      PLUGIN_CHECK_AND_SET_PLATFORMS_KEY, DOCKERFILE_FILENAME)
from flexmock import flexmock
from multiprocessing.pool import AsyncResult
from osbs.api import OSBS
from osbs.exceptions import OsbsException
from tests.constants import SOURCE
from tests.util import add_koji_map_in_workflow
from textwrap import dedent
from copy import deepcopy

import json
import os
import sys
import pytest
import time
import platform

pytest.skip("OSBS2 TBD", allow_module_level=True)


MANIFEST_LIST = {
    'manifests': [
        {'platform': {'architecture': 'amd64'}, 'digest': 'sha256:123456'},
        {'platform': {'architecture': 'ppc64le'}, 'digest': 'sha256:123456'},
    ]
}


DEFAULT_CLUSTERS = {
    'x86_64': [
        {
            'name': 'worker_x86_64',
            'max_concurrent_builds': 3
        }
    ],
    'ppc64le': [
        {
            'name': 'worker_ppc64le',
            'max_concurrent_builds': 3
        }
    ]
}


class MockSource(object):

    def __init__(self, source_dir: Path):
        self.dockerfile_path = str(source_dir / DOCKERFILE_FILENAME)
        self.path = str(source_dir)
        self.config = flexmock(image_build_method=None)

    def get_build_file_path(self):
        return self.dockerfile_path, self.path


class fake_imagestream_tag(object):
    def __init__(self, json_cont):
        self.json_cont = json_cont

    def json(self):
        return self.json_cont


class fake_manifest_list(object):
    def __init__(self, json_cont):
        self.content = json_cont

    def json(self):
        return self.content


pytestmark = pytest.mark.usefixtures('user_params')


def mock_workflow(workflow, source_dir: Path, platforms=None):
    source = MockSource(source_dir)
    setattr(workflow, 'source', source)

    with open(source.dockerfile_path, 'w') as f:
        f.write(dedent("""\
            FROM fedora:25
            LABEL com.redhat.component=python \
                  version=2.7 \
                  release=10
            """))
    df = df_parser(source.dockerfile_path)
    flexmock(workflow, df_path=df.dockerfile_path)

    platforms = ['x86_64', 'ppc64le'] if platforms is None else platforms
    workflow.data.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = set(platforms)

    build = {
        "spec": {
            "strategy": {
                "customStrategy": {
                    "from": {"name": "registry/some_image@sha256:123456",
                             "kind": "DockerImage"}}}},
        "metadata": {
            "annotations": {
                "from": json.dumps({"name": "registry/some_image:latest",
                                    "kind": "DockerImage"})}}}
    flexmock(os, environ={'BUILD': json.dumps(build)})


def mock_reactor_config(workflow, source_dir: Path, clusters=None, empty=False, add_config=None):
    if not clusters and not empty:
        clusters = deepcopy(DEFAULT_CLUSTERS)

    koji_map = {
        'hub_url': '/',
        'root_url': '',
        'auth': {}
    }

    conf_json = {
        'version': 1,
        'clusters': clusters,
        'koji': koji_map,
        'source_registry': {'url': 'source_registry'},
        'openshift': {'url': 'openshift_url'},
    }
    if add_config:
        conf_json.update(add_config)

    workflow.conf.conf = conf_json

    with open(source_dir / 'osbs.conf', 'w') as f:
        for plat_clusters in clusters.values():
            for cluster in plat_clusters:
                f.write(dedent("""\
                    [{name}]
                    openshift_url = https://{name}.com/
                    namespace = {name}_namespace
                    """.format(name=cluster['name'])))
    return conf_json


def mock_manifest_list():
    (flexmock(atomic_reactor.util)
     .should_receive('get_manifest_list')
     .and_return(fake_manifest_list(MANIFEST_LIST)))


def mock_orchestrator_platfrom(plat='x86_64'):
    (flexmock(platform)
     .should_receive('processor')
     .and_return(plat))


def mock_osbs(current_builds=2, worker_builds=1, logs_return_bytes=False, worker_expect=None):
    (flexmock(OSBS)
        .should_receive('list_builds')
        .and_return(list(range(current_builds))))

    koji_upload_dirs = set()

    def mock_create_worker_build(**kwargs):
        # koji_upload_dir parameter must be identical for all workers
        koji_upload_dirs.add(kwargs.get('koji_upload_dir'))
        assert len(koji_upload_dirs) == 1

        if worker_expect:
            testkwargs = deepcopy(kwargs)
            testkwargs.pop('koji_upload_dir')

            # ignore openshift configuration, which is dependent on reading ocp configs from rcm
            testkwargs['reactor_config_override'].pop('openshift')
            worker_expect['reactor_config_override'].pop('openshift')
            assert testkwargs == worker_expect

        return make_build_response('worker-build-{}'.format(kwargs['platform']),
                                   'Running')
    (flexmock(OSBS)
        .should_receive('create_worker_build')
        .replace_with(mock_create_worker_build))

    if logs_return_bytes:
        log_format_string = b'line \xe2\x80\x98 - %d'
    else:
        log_format_string = 'line \u2018 - %d'

    (flexmock(OSBS)
        .should_receive('get_build_logs')
        .and_yield(log_format_string % line for line in range(10)))

    def mock_wait_for_build_to_finish(build_name):
        return make_build_response(build_name, 'Complete')
    (flexmock(OSBS)
        .should_receive('wait_for_build_to_finish')
        .replace_with(mock_wait_for_build_to_finish))


def make_build_response(name, status, annotations=None, labels=None):
    build_response = {
        'metadata': {
            'name': name,
            'annotations': annotations or {},
            'labels': labels or {},
        },
        'status': {
            'phase': status
        }
    }

    return build_response


def make_worker_build_kwargs(**overrides):
    kwargs = {
        'git_uri': SOURCE['uri'],
        'git_ref': 'master',
        'git_branch': 'master',
        'user': 'bacon',
    }
    kwargs.update(overrides)
    return kwargs


def teardown_function(function):
    sys.modules.pop('build_orchestrate_build', None)


@pytest.mark.parametrize('config_kwargs', [
    None,
    {},
    {'build_image': 'osbs-buildroot:latest'},
    {'build_image': 'osbs-buildroot:latest', 'sources_command': 'fedpkg source'},
    {'build_image': 'osbs-buildroot:latest',
     'equal_labels': 'label1:label2,label3:label4'},
])
@pytest.mark.parametrize('worker_build_image', [
    'fedora:latest',
    None
])
@pytest.mark.parametrize('logs_return_bytes', [
    True,
    False
])
def test_orchestrate_build(workflow, source_dir, caplog,
                           config_kwargs, worker_build_image, logs_return_bytes):
    mock_workflow(workflow, source_dir, platforms=['x86_64'])
    mock_osbs(logs_return_bytes=logs_return_bytes)
    plugin_args = {
        'platforms': ['x86_64'],
        'build_kwargs': make_worker_build_kwargs(),
    }
    if worker_build_image:
        plugin_args['worker_build_image'] = worker_build_image
    if config_kwargs is not None:
        plugin_args['config_kwargs'] = config_kwargs

    expected_kwargs = {
        'conf_section': str('worker_x86_64'),
        'conf_file': str(source_dir) + '/osbs.conf',
        'sources_command': None,
        'koji_hub': '/',
        'koji_root': ''
    }
    if config_kwargs:
        expected_kwargs['sources_command'] = config_kwargs.get('sources_command')
        if 'equal_labels' in config_kwargs:
            expected_kwargs['equal_labels'] = config_kwargs.get('equal_labels')

    clusters = deepcopy(DEFAULT_CLUSTERS)

    reactor_dict = {'version': 1}
    if config_kwargs and 'sources_command' in config_kwargs:
        reactor_dict['sources_command'] = 'fedpkg source'

    expected_kwargs['source_registry_uri'] = None
    reactor_dict['odcs'] = {'api_url': 'odcs_url'}
    expected_kwargs['odcs_insecure'] = False
    expected_kwargs['odcs_url'] = reactor_dict['odcs']['api_url']
    reactor_dict['prefer_schema1_digest'] = False
    expected_kwargs['prefer_schema1_digest'] = reactor_dict['prefer_schema1_digest']
    reactor_dict['smtp'] = {
        'from_address': 'from',
        'host': 'smtp host'}
    expected_kwargs['smtp_host'] = reactor_dict['smtp']['host']
    expected_kwargs['smtp_from'] = reactor_dict['smtp']['from_address']
    expected_kwargs['smtp_email_domain'] = None
    expected_kwargs['smtp_additional_addresses'] = ""
    expected_kwargs['smtp_error_addresses'] = ""
    expected_kwargs['smtp_to_submitter'] = False
    expected_kwargs['smtp_to_pkgowner'] = False
    reactor_dict['artifacts_allowed_domains'] = ('domain1', 'domain2')
    expected_kwargs['artifacts_allowed_domains'] =\
        ','.join(reactor_dict['artifacts_allowed_domains'])
    reactor_dict['yum_proxy'] = 'yum proxy'
    expected_kwargs['yum_proxy'] = reactor_dict['yum_proxy']
    reactor_dict['content_versions'] = ['v2']
    expected_kwargs['registry_api_versions'] = 'v2'

    if config_kwargs and 'equal_labels' in config_kwargs:
        expected_kwargs['equal_labels'] = config_kwargs['equal_labels']

        label_groups = [x.strip() for x in config_kwargs['equal_labels'].split(',')]

        equal_labels = []
        for label_group in label_groups:
            equal_labels.append([label.strip() for label in label_group.split(':')])

        reactor_dict['image_equal_labels'] = equal_labels

    reactor_dict['clusters'] = clusters
    reactor_dict['platform_descriptors'] = [{'platform': 'x86_64',
                                             'architecture': 'amd64'}]
    reactor_dict['source_registry'] = {'url': 'source_registry'}

    workflow.conf.conf = reactor_dict

    add_koji_map_in_workflow(workflow, hub_url='/', root_url='')

    with open(source_dir / 'osbs.conf', 'w') as f:
        for plat_clusters in clusters.values():
            for cluster in plat_clusters:
                f.write(dedent("""\
                    [{name}]
                    openshift_url = https://{name}.com/
                    namespace = {name}_namespace
                    """.format(name=cluster['name'])))

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args
        }]
    )

    # Update with config_kwargs last to ensure that, when set
    # always has precedence over worker_build_image param.
    if config_kwargs is not None:
        expected_kwargs.update(config_kwargs)
    expected_kwargs['build_from'] = 'image:registry/some_image@sha256:123456'

    # (flexmock(Configuration).should_call('__init__').with_args(**expected_kwargs).once())

    build_result = runner.run()
    assert not build_result.is_failed()

    assert (build_result.annotations == {
        'worker-builds': {
            'x86_64': {
                'build': {
                    'build-name': 'worker-build-x86_64',
                    'cluster-url': None,
                    'namespace': 'default'
                },
                'digests': [],
                'plugins-metadata': {}
            }
        }
    })

    for record in caplog.records:
        if not record.name.startswith("atomic_reactor"):
            continue

        assert hasattr(record, 'arch')
        if record.funcName == 'watch_logs':
            assert record.arch == 'x86_64'
        else:
            assert record.arch == '-'


@pytest.mark.parametrize('metadata_fragment', [
    True,
    False
])
def test_orchestrate_build_annotations_and_labels(workflow, source_dir, metadata_fragment):
    mock_workflow(workflow, source_dir)
    mock_osbs()
    mock_manifest_list()

    md = {
        'metadata_fragment': 'configmap/spam-md',
        'metadata_fragment_key': 'metadata.json'
    }

    def mock_wait_for_build_to_finish(build_name):
        annotations = {
            'digests': json.dumps([
                {
                    'digest': 'sha256:{}-digest'.format(build_name),
                    'tag': '{}-latest'.format(build_name),
                    'registry': '{}-registry'.format(build_name),
                    'repository': '{}-repository'.format(build_name),
                },
            ]),
        }
        if metadata_fragment:
            annotations.update(md)
        return make_build_response(build_name, 'Complete', annotations)

    (flexmock(OSBS)
        .should_receive('wait_for_build_to_finish')
        .replace_with(mock_wait_for_build_to_finish))

    mock_reactor_config(workflow, source_dir)
    workflow.conf.conf['platform_descriptors'] = [{'platform': 'x86_64', 'architecture': 'amd64'}]

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64', 'ppc64le'],
                'build_kwargs': make_worker_build_kwargs(),
                'max_cluster_fails': 2,
                'unreachable_cluster_retry_delay': .1,
            }
        }]
    )
    build_result = runner.run()
    assert not build_result.is_failed()

    expected = {
        'worker-builds': {
            'x86_64': {
                'build': {
                    'build-name': 'worker-build-x86_64',
                    'cluster-url': None,
                    'namespace': 'default'
                },
                'digests': [
                    {
                        'digest': 'sha256:worker-build-x86_64-digest',
                        'tag': 'worker-build-x86_64-latest',
                        'registry': 'worker-build-x86_64-registry',
                        'repository': 'worker-build-x86_64-repository',
                    },
                ],
                'plugins-metadata': {}
            },
            'ppc64le': {
                'build': {
                    'build-name': 'worker-build-ppc64le',
                    'cluster-url': None,
                    'namespace': 'default'
                },
                'digests': [
                    {
                        'digest': 'sha256:worker-build-ppc64le-digest',
                        'tag': 'worker-build-ppc64le-latest',
                        'registry': 'worker-build-ppc64le-registry',
                        'repository': 'worker-build-ppc64le-repository',
                    },
                ],
                'plugins-metadata': {}
            },
        },
    }
    if metadata_fragment:
        expected['worker-builds']['x86_64'].update(md)
        expected['worker-builds']['ppc64le'].update(md)

    assert (build_result.annotations == expected)


def test_orchestrate_choose_cluster_retry(workflow, source_dir):

    mock_osbs()
    mock_manifest_list()

    (flexmock(OSBS).should_receive('list_builds')
        .and_raise(OsbsException)
        .and_raise(OsbsException)
        .and_return([1, 2, 3]))

    mock_workflow(workflow, source_dir)

    mock_reactor_config(workflow, source_dir, {
        'x86_64': [
            {'name': cluster[0], 'max_concurrent_builds': cluster[1]}
            for cluster in [('chosen_x86_64', 5), ('spam', 4)]
        ],
        'ppc64le': [
            {'name': cluster[0], 'max_concurrent_builds': cluster[1]}
            for cluster in [('chosen_ppc64le', 5), ('ham', 5)]
        ]
    })
    workflow.conf.conf['platform_descriptors'] = [{'platform': 'x86_64', 'architecture': 'amd64'}]

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64', 'ppc64le'],
                'build_kwargs': make_worker_build_kwargs(),
                'find_cluster_retry_delay': .1,
                'max_cluster_fails': 2,
            }
        }]
    )

    runner.run()


def test_orchestrate_choose_cluster_retry_timeout(workflow, source_dir):

    mock_manifest_list()
    (flexmock(OSBS).should_receive('list_builds')
        .and_raise(OsbsException)
        .and_raise(OsbsException)
        .and_raise(OsbsException))

    mock_workflow(workflow, source_dir)

    mock_reactor_config(workflow, source_dir, {
        'x86_64': [
            {'name': cluster[0], 'max_concurrent_builds': cluster[1]}
            for cluster in [('chosen_x86_64', 5), ('spam', 4)]
        ],
        'ppc64le': [
            {'name': cluster[0], 'max_concurrent_builds': cluster[1]}
            for cluster in [('chosen_ppc64le', 5), ('ham', 5)]
        ]
    })
    workflow.conf.conf['platform_descriptors'] = [{'platform': 'x86_64', 'architecture': 'amd64'}]

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64', 'ppc64le'],
                'build_kwargs': make_worker_build_kwargs(),
                'find_cluster_retry_delay': .1,
                'max_cluster_fails': 2,
            }
        }]
    )

    build_result = runner.run()
    assert build_result.is_failed()
    fail_reason = json.loads(build_result.fail_reason)['ppc64le']['general']
    assert 'Could not find appropriate cluster for worker build.' in fail_reason


def test_orchestrate_build_cancelation(workflow, source_dir):
    mock_workflow(workflow, source_dir, platforms=['x86_64'])
    mock_osbs()
    mock_manifest_list()
    mock_reactor_config(workflow, source_dir)
    workflow.conf.conf['platform_descriptors'] = [{'platform': 'x86_64', 'architecture': 'amd64'}]

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64'],
                'build_kwargs': make_worker_build_kwargs(),
            }
        }]
    )

    def mock_wait_for_build_to_finish(build_name):
        return make_build_response(build_name, 'Running')
    (flexmock(OSBS)
        .should_receive('wait_for_build_to_finish')
        .replace_with(mock_wait_for_build_to_finish))

    flexmock(OSBS).should_receive('cancel_build').once()

    (flexmock(AsyncResult).should_receive('ready')
        .and_return(False)  # normal execution
        .and_return(False)  # after cancel_build
        .and_return(True))  # finally succeed

    class RaiseOnce(object):
        """
        Only raise an exception the first time this mocked wait() method
        is called.
        """

        def __init__(self):
            self.exception_raised = False

        def get(self, timeout=None):
            time.sleep(0.1)
            if not self.exception_raised:
                self.exception_raised = True
                raise BuildCanceledException()

    raise_once = RaiseOnce()
    (flexmock(AsyncResult).should_receive('get')
        .replace_with(raise_once.get))

    with pytest.raises(PluginFailedException) as exc:
        runner.run()
    assert 'BuildCanceledException' in str(exc.value)


@pytest.mark.parametrize(('clusters_x86_64'), (
    ([('chosen_x86_64', 5), ('spam', 4)]),
    ([('chosen_x86_64', 5000), ('spam', 4)]),
    ([('spam', 4), ('chosen_x86_64', 5)]),
    ([('chosen_x86_64', 5), ('spam', 4), ('bacon', 4)]),
    ([('chosen_x86_64', 5), ('spam', 5)]),
    ([('chosen_x86_64', 1), ('spam', 1)]),
    ([('chosen_x86_64', 2), ('spam', 2)]),
))
@pytest.mark.parametrize(('clusters_ppc64le'), (
    ([('chosen_ppc64le', 7), ('eggs', 6)]),
))
def test_orchestrate_build_choose_clusters(workflow, source_dir, clusters_x86_64, clusters_ppc64le):
    mock_workflow(workflow, source_dir)
    mock_osbs()  # Current builds is a constant 2
    mock_manifest_list()

    mock_reactor_config(workflow, source_dir, {
        'x86_64': [
            {'name': cluster[0], 'max_concurrent_builds': cluster[1]}
            for cluster in clusters_x86_64
        ],
        'ppc64le': [
            {'name': cluster[0], 'max_concurrent_builds': cluster[1]}
            for cluster in clusters_ppc64le
        ]
    })
    workflow.conf.conf['platform_descriptors'] = [{'platform': 'x86_64', 'architecture': 'amd64'}]

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64', 'ppc64le'],
                'build_kwargs': make_worker_build_kwargs(),
            }
        }]
    )

    build_result = runner.run()
    assert not build_result.is_failed()

    annotations = build_result.annotations
    assert set(annotations['worker-builds'].keys()) == {'x86_64', 'ppc64le'}


# This test tests code paths that can no longer be hit in actual operation since
# we exclude platforms with no clusters in check_and_set_platforms.
def test_orchestrate_build_unknown_platform(workflow, source_dir):  # noqa
    mock_workflow(workflow, source_dir, platforms=['x86_64', 'spam'])
    mock_osbs()
    mock_manifest_list()
    mock_reactor_config(workflow, source_dir)
    workflow.conf.conf['platform_descriptors'] = [{'platform': 'x86_64', 'architecture': 'amd64'}]

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                # Explicitly leaving off 'eggs' platform to
                # ensure no errors occur when unknow platform
                # is provided in exclude-platform file.
                'platforms': ['x86_64', 'spam'],
                'build_kwargs': make_worker_build_kwargs(),
            }
        }]
    )

    with pytest.raises(PluginFailedException) as exc:
        runner.run()

    assert "No clusters found for platform spam!" in str(exc.value)


def test_orchestrate_build_failed_create(workflow, source_dir):
    mock_workflow(workflow, source_dir)
    mock_osbs()
    mock_manifest_list()

    def mock_create_worker_build(**kwargs):
        if kwargs['platform'] == 'ppc64le':
            raise OsbsException('it happens')
        return make_build_response('worker-build-1', 'Running')
    (flexmock(OSBS)
     .should_receive('create_worker_build')
     .replace_with(mock_create_worker_build))

    annotation_keys = {'x86_64'}

    mock_reactor_config(workflow, source_dir)
    workflow.conf.conf['platform_descriptors'] = [{'platform': 'x86_64', 'architecture': 'amd64'}]

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64', 'ppc64le'],
                'build_kwargs': make_worker_build_kwargs(),
                'find_cluster_retry_delay': .1,
                'failure_retry_delay': .1,
                'goarch': {'x86_64': 'amd64'},
            }
        }]
    )

    build_result = runner.run()
    assert build_result.is_failed()

    annotations = build_result.annotations
    assert set(annotations['worker-builds'].keys()) == annotation_keys
    fail_reason = json.loads(build_result.fail_reason)['ppc64le']['general']
    assert "Could not find appropriate cluster for worker build." in fail_reason


@pytest.mark.parametrize('pod_available,pod_failure_reason,expected,cancel_fails', [
    # get_pod_for_build() returns error
    (False,
     None,
     KeyError,
     False),

    # get_failure_reason() not available in PodResponse
    (True,
     AttributeError("'module' object has no attribute 'get_failure_reason'"),
     KeyError,
     False),

    # get_failure_reason() result used
    (True,
     {
         'reason': 'reason message',
         'exitCode': 23,
         'containerID': 'abc123',
     },
     {
         'reason': 'reason message',
         'exitCode': 23,
         'containerID': 'abc123',
     },
     False),

    # cancel_build() fails (and failure is ignored)
    (True,
     {
         'reason': 'reason message',
         'exitCode': 23,
         'containerID': 'abc123',
     },
     {
         'reason': 'reason message',
         'exitCode': 23,
         'containerID': 'abc123',
     },
     True)
])
def test_orchestrate_build_failed_waiting(workflow,
                                          source_dir,
                                          pod_available,
                                          pod_failure_reason,
                                          cancel_fails,
                                          expected):
    mock_workflow(workflow, source_dir)
    mock_osbs()

    class MockPodResponse(object):
        def __init__(self, pod_failure_reason):
            self.pod_failure_reason = pod_failure_reason

        def get_failure_reason(self):
            if isinstance(self.pod_failure_reason, Exception):
                raise self.pod_failure_reason

            return self.pod_failure_reason

    def mock_wait_for_build_to_finish(build_name):
        if build_name == 'worker-build-ppc64le':
            raise OsbsException('it happens')
        return make_build_response(build_name, 'Failed')
    (flexmock(OSBS)
     .should_receive('wait_for_build_to_finish')
     .replace_with(mock_wait_for_build_to_finish))
    mock_manifest_list()

    cancel_build_expectation = flexmock(OSBS).should_receive('cancel_build')
    if cancel_fails:
        cancel_build_expectation.and_raise(OsbsException)

    cancel_build_expectation.once()

    expectation = flexmock(OSBS).should_receive('get_pod_for_build')
    if pod_available:
        expectation.and_return(MockPodResponse(pod_failure_reason))
    else:
        expectation.and_raise(OsbsException())

    mock_reactor_config(workflow, source_dir)
    workflow.conf.conf['platform_descriptors'] = [{'platform': 'x86_64', 'architecture': 'amd64'}]

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64', 'ppc64le'],
                'build_kwargs': make_worker_build_kwargs(),
            }
        }]
    )

    build_result = runner.run()
    assert build_result.is_failed()

    annotations = build_result.annotations
    assert set(annotations['worker-builds'].keys()) == {'x86_64', 'ppc64le'}
    fail_reason = json.loads(build_result.fail_reason)['ppc64le']

    if expected is KeyError:
        assert 'pod' not in fail_reason
    else:
        assert fail_reason['pod'] == expected


@pytest.mark.parametrize(('task_id', 'error'), [
    ('1234567', None),
    ('bacon', 'ValueError'),
    (None, 'TypeError'),
])
def test_orchestrate_build_get_fs_task_id(workflow, source_dir, task_id, error):
    mock_workflow(workflow, source_dir, platforms=['x86_64'])
    mock_osbs()

    mock_reactor_config(workflow, source_dir)

    workflow.data.prebuild_results[PLUGIN_ADD_FILESYSTEM_KEY] = {
        'filesystem-koji-task-id': task_id,
    }
    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64'],
                'build_kwargs': make_worker_build_kwargs(),
            }
        }]
    )

    if error is not None:
        with pytest.raises(PluginFailedException) as exc:
            runner.run()
        workflow.data.build_result.is_failed()
        assert error in str(exc.value)

    else:
        build_result = runner.run()
        assert not build_result.is_failed()


@pytest.mark.parametrize('fail_at', ('all', 'first'))
def test_orchestrate_build_failed_to_list_builds(workflow, source_dir, fail_at):
    mock_workflow(workflow, source_dir, platforms=['x86_64'])
    mock_osbs()  # Current builds is a constant 2

    mock_reactor_config(workflow, source_dir, {
        'x86_64': [
            {'name': 'spam', 'max_concurrent_builds': 5},
            {'name': 'eggs', 'max_concurrent_builds': 5}
        ],
    })

    flexmock_chain = flexmock(OSBS).should_receive('list_builds').and_raise(OsbsException("foo"))

    if fail_at == 'all':
        flexmock_chain.and_raise(OsbsException("foo"))

    if fail_at == 'first':
        flexmock_chain.and_return(['a', 'b'])

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': {
                'platforms': ['x86_64'],
                'build_kwargs': make_worker_build_kwargs(),
                'find_cluster_retry_delay': .1,
                'max_cluster_fails': 2
            }
        }]
    )
    if fail_at == 'first':
        build_result = runner.run()
        assert not build_result.is_failed()
    else:
        build_result = runner.run()
        assert build_result.is_failed()
        if fail_at == 'all':
            assert 'Could not find appropriate cluster for worker build.' \
                in build_result.fail_reason


def test_orchestrate_build_worker_build_kwargs(workflow, source_dir, caplog):
    mock_workflow(workflow, source_dir, platforms=['x86_64'])
    expected_kwargs = {
        'git_uri': SOURCE['uri'],
        'git_ref': 'master',
        'git_branch': 'master',
        'user': 'bacon',
        'platform': 'x86_64',
        'release': '10',
        'parent_images_digests': {},
        'operator_manifests_extract_platform': 'x86_64',
    }

    reactor_config_override = mock_reactor_config(workflow, source_dir)
    reactor_config_override['openshift'] = {
        'auth': {'enable': None},
        'insecure': False,
        'url': 'https://worker_x86_64.com/'
    }
    expected_kwargs['reactor_config_override'] = reactor_config_override
    mock_osbs(worker_expect=expected_kwargs)

    plugin_args = {
        'platforms': ['x86_64'],
        'build_kwargs': make_worker_build_kwargs(),
        'worker_build_image': 'fedora:latest',
    }

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args,
        }]
    )
    build_result = runner.run()
    assert not build_result.is_failed()


@pytest.mark.parametrize('overrides', [
    {None: '4242'},
    {'x86_64': '4242'},
    {'x86_64': '4242', None: '1111'},
])
def test_orchestrate_override_build_kwarg(workflow, source_dir, overrides):
    mock_workflow(workflow, source_dir, platforms=['x86_64'])
    expected_kwargs = {
        'git_uri': SOURCE['uri'],
        'git_ref': 'master',
        'git_branch': 'master',
        'user': 'bacon',
        'platform': 'x86_64',
        'release': '4242',
        'parent_images_digests': {},
        'operator_manifests_extract_platform': 'x86_64',
    }
    reactor_config_override = mock_reactor_config(workflow, source_dir)
    reactor_config_override['openshift'] = {
        'auth': {'enable': None},
        'insecure': False,
        'url': 'https://worker_x86_64.com/'
    }
    expected_kwargs['reactor_config_override'] = reactor_config_override
    mock_osbs(worker_expect=expected_kwargs)

    plugin_args = {
        'platforms': ['x86_64'],
        'build_kwargs': make_worker_build_kwargs(),
        'worker_build_image': 'fedora:latest',
    }

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args,
        }]
    )

    build_result = runner.run()
    assert not build_result.is_failed()


@pytest.mark.parametrize('content_versions', [
    ['v1', 'v2'],
    ['v1'],
    ['v2'],
])
def test_orchestrate_override_content_versions(workflow, source_dir, caplog, content_versions):
    mock_workflow(workflow, source_dir, platforms=['x86_64'])
    expected_kwargs = {
        'git_uri': SOURCE['uri'],
        'git_ref': 'master',
        'git_branch': 'master',
        'user': 'bacon',
        'platform': 'x86_64',
        'release': '10',
        'parent_images_digests': {},
        'operator_manifests_extract_platform': 'x86_64',
    }
    add_config = {
        'platform_descriptors': [{
            'platform': 'x86_64',
            'architecture': 'amd64',
        }],
        'content_versions': content_versions
    }

    reactor_config_override = mock_reactor_config(
        workflow, source_dir, workflow, add_config=add_config
    )
    reactor_config_override['openshift'] = {
        'auth': {'enable': None},
        'insecure': False,
        'url': 'https://worker_x86_64.com/'
    }

    will_fail = False
    if 'v2' not in content_versions:
        will_fail = True
    else:
        reactor_config_override['content_versions'] = ['v2']

    expected_kwargs['reactor_config_override'] = reactor_config_override
    mock_osbs(worker_expect=expected_kwargs)

    plugin_args = {
        'platforms': ['x86_64'],
        'build_kwargs': make_worker_build_kwargs(),
        'worker_build_image': 'fedora:latest',
    }

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args,
        }]
    )

    build_result = runner.run()
    if will_fail:
        assert build_result.is_failed()
        assert 'failed to create worker build' in caplog.text
        assert 'content_versions is empty' in caplog.text
    else:
        assert not build_result.is_failed()


@pytest.mark.parametrize(('build', 'exc_str', 'ml', 'ml_cont'), [
    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name_wrong": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}}},
     "Build object is malformed, failed to fetch buildroot image",
     None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind_wrong": "DockerImage"}}}}},
     "Build object is malformed, failed to fetch buildroot image",
     None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "wrong_kind"}}}}},
     "Build kind isn't 'DockerImage' but",
     None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "metadata": {"annotations": {}}},
     "Build wasn't created from BuildConfig and neither has 'from'" +
     " annotation, which is needed for specified arch",
     None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "metadata": {
            "annotations": {
                "from": json.dumps({"kind": "wrong"})}}},
     "Build annotation has unknown 'kind'",
     None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "metadata": {
            "annotations": {
                "from": json.dumps({"kind": "DockerImage",
                                    "name": "registry/image@sha256:123456"})}}},
     "Buildroot image isn't manifest list, which is needed for specified arch",
     False, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
      "metadata": {
        "annotations": {
            "from": json.dumps({"kind": "ImageStreamTag", "name": "ims"})}}},
     "ImageStreamTag not found",
     None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
      "metadata": {
        "annotations": {
            "from": json.dumps({"kind": "ImageStreamTag", "name": "ims"})}}},
     "ImageStreamTag is malformed",
     None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
      "metadata": {
        "annotations": {
            "from": json.dumps({"kind": "ImageStreamTag", "name": "ims"})}}},
     "Image in imageStreamTag 'ims' is missing Labels",
     None, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "metadata": {
            "annotations": {
                "from": json.dumps({"kind": "DockerImage",
                                    "name": "registry/image:tag"})}}},
     "Buildroot image isn't manifest list, which is needed for specified arch",
     False, None),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "metadata": {
            "annotations": {
                "from": json.dumps({"kind": "DockerImage",
                                    "name": "registry/image:tag"})}}},
     "Platform for orchestrator 'x86_64' isn't in manifest list",
     True, {"manifests": [{"platform": {"architecture": "ppc64le"}, "digest": "some_image"}]}),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot@sha256:1949494494",
                         "kind": "DockerImage"}}}},
        "metadata": {
            "annotations": {
                "from": json.dumps({"kind": "DockerImage",
                                    "name": "registry/image:tag"})}}},
     "Orchestrator is using image digest 'osbs-buildroot@sha256:1949494494' " +
     "which isn't in manifest list",
     True, {"manifests": [{"platform": {"architecture": "amd64"}, "digest": "some_image"}]}),

    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "registry/image@osbs-buildroot:latest",
                         "kind": "DockerImage"}}}},
        "metadata": {
            "annotations": {
                "from": json.dumps({"kind": "DockerImage",
                                    "name": "registry/image:tag"})}}},
     "build_image for platform 'ppc64le' not available",
     True, {"manifests": [{"platform": {"architecture": "amd64"},
                           "digest": "osbs-buildroot:latest"}]}),
])
def test_set_build_image_raises(workflow, source_dir, build, exc_str, ml, ml_cont):
    build = json.dumps(build)
    mock_workflow(workflow, source_dir)

    orchestrator_default_platform = 'x86_64'
    (flexmock(platform)
     .should_receive('processor')
     .and_return(orchestrator_default_platform))

    flexmock(os, environ={'BUILD': build})
    mock_osbs()
    mock_reactor_config(workflow, source_dir)

    if ml is False:
        (flexmock(atomic_reactor.util)
         .should_receive('get_manifest_list')
         .and_return(None))
    if ml is True:
        (flexmock(atomic_reactor.util)
         .should_receive('get_manifest_list')
         .and_return(fake_manifest_list(ml_cont)))

    plugin_args = {
        'platforms': ['x86_64', 'ppc64le'],
        'build_kwargs': make_worker_build_kwargs(),
        'worker_build_image': 'osbs-buildroot:latest',
    }
    workflow.conf.conf['platform_descriptors'] = [{'platform': 'x86_64', 'architecture': 'amd64'}]

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args,
        }]
    )

    with pytest.raises(PluginFailedException) as ex:
        runner.run()
    assert "raised an exception: RuntimeError" in str(ex.value)
    assert exc_str in str(ex.value)


@pytest.mark.parametrize(('build', 'ml', 'ml_cont', 'platforms'), [
    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "osbs-buildroot:latest",
                         "kind": "DockerImage"}}}}},
     None, None, ['x86_64']),


    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "registry/osbs-buildroot@sha256:12345",
                         "kind": "DockerImage"}}}},
      "metadata": {
          "annotations": {
              "from": json.dumps({"kind": "ImageStreamTag",
                                  "name": "image_stream_tag"})}}},
     True,
     {"manifests": [{"platform": {"architecture": "ppc64le"},
                     "digest": "sha256:987654321"},
                    {"platform": {"architecture": "amd64"},
                     "digest": "sha256:12345"}]},
     ['ppc64le', 'x86_64']),


    ({"spec": {
        "strategy": {
            "customStrategy": {
                "from": {"name": "registry/osbs-buildroot@sha256:12345",
                         "kind": "DockerImage"}}}},
      "metadata": {
          "annotations": {
              "from": json.dumps({"kind": "ImageStreamTag",
                                  "name": "image_stream_tag"})}}},
     True,
     {"manifests": [{"platform": {"architecture": "ppc64le"},
                     "digest": "sha256:987654321"},
                    {"platform": {"architecture": "amd64"},
                     "digest": "sha256:12345"}]},
     ['ppc64le', 'x86_64']),
])
def test_set_build_image_works(workflow, source_dir, build, ml, ml_cont, platforms):
    build = json.dumps(build)
    mock_workflow(workflow, source_dir, platforms=platforms)

    orchestrator_default_platform = 'x86_64'
    (flexmock(platform)
     .should_receive('processor')
     .and_return(orchestrator_default_platform))

    flexmock(os, environ={'BUILD': build})
    mock_osbs()
    mock_reactor_config(workflow, source_dir)
    if ml is True:
        (flexmock(atomic_reactor.util)
         .should_receive('get_manifest_list')
         .and_return(fake_manifest_list(ml_cont)))

    plugin_args = {
        'platforms': platforms,
        'build_kwargs': make_worker_build_kwargs(),
        'worker_build_image': 'osbs-buildroot:latest',
    }

    workflow.conf.conf['platform_descriptors'] = [{'platform': 'x86_64', 'architecture': 'amd64'}]

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args,
        }]
    )

    runner.run()


@pytest.mark.parametrize(('platforms', 'override'), [
    (['ppc64le', 'x86_64'], ['ppc64le']),
    (['ppc64le'], ['ppc64le']),
])
def test_set_build_image_with_override(workflow, source_dir, platforms, override):
    mock_workflow(workflow, source_dir, platforms=platforms)

    default_build_image = 'registry/osbs-buildroot@sha256:12345'
    build = json.dumps({"spec": {
      "strategy": {
            "customStrategy": {
                "from": {"name": default_build_image, "kind": "DockerImage"}}}},
      "status": {
          "config": {"kind": "BuildConfig", "name": "build config"}}})
    flexmock(os, environ={'BUILD': build})

    mock_osbs()
    mock_manifest_list()
    mock_orchestrator_platfrom()

    reactor_config = {
        'version': 1,
        'openshift': {'url': 'openshift_url'},
        'clusters': deepcopy(DEFAULT_CLUSTERS),
        'platform_descriptors': [{'platform': 'x86_64', 'architecture': 'amd64'}],
        'build_image_override': {plat: 'registry/osbs-buildroot-{}:latest'.format(plat)
                                 for plat in override},
        'source_registry': {'url': 'source_registry'}
    }

    workflow.conf.conf = reactor_config

    add_koji_map_in_workflow(workflow, hub_url='/', root_url='')

    plugin_args = {
        'platforms': platforms,
        'build_kwargs': make_worker_build_kwargs(),
    }

    runner = BuildStepPluginsRunner(
        workflow,
        [{'name': OrchestrateBuildPlugin.key, 'args': plugin_args}]
    )

    runner.run()


def test_no_platforms(workflow, source_dir):
    mock_workflow(workflow, source_dir, platforms=[])
    mock_osbs()
    mock_reactor_config(workflow, source_dir)

    (flexmock(OrchestrateBuildPlugin)
     .should_receive('set_build_image')
     .never())

    plugin_args = {
        'platforms': [],
        'build_kwargs': make_worker_build_kwargs(),
        'worker_build_image': 'osbs-buildroot:latest',
    }

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args,
        }]
    )
    with pytest.raises(PluginFailedException) as exc:
        runner.run()
    assert 'No enabled platform to build on' in str(exc.value)


def test_parent_images_digests(workflow, source_dir, caplog):
    """Test if manifest digests and media types of parent images are propagated
    correctly to OSBS client"""
    media_type = 'application/vnd.docker.distribution.manifest.list.v2+json'
    PARENT_IMAGES_DIGESTS = {
        'registry.fedoraproject.org/fedora:latest': {
            media_type: 'sha256:123456789abcdef',
        }
    }

    mock_workflow(workflow, source_dir, platforms=['x86_64'])
    workflow.data.parent_images_digests.update(PARENT_IMAGES_DIGESTS)
    expected_kwargs = {
        'git_uri': SOURCE['uri'],
        'git_ref': 'master',
        'git_branch': 'master',
        'user': 'bacon',
        'platform': 'x86_64',
        'release': '10',
        'parent_images_digests': PARENT_IMAGES_DIGESTS,
        'operator_manifests_extract_platform': 'x86_64',
    }

    reactor_config_override = mock_reactor_config(workflow, source_dir)
    reactor_config_override['openshift'] = {
        'auth': {'enable': None},
        'insecure': False,
        'url': 'https://worker_x86_64.com/'
    }
    expected_kwargs['reactor_config_override'] = reactor_config_override
    mock_osbs(worker_expect=expected_kwargs)

    plugin_args = {
        'platforms': ['x86_64'],
        'build_kwargs': make_worker_build_kwargs(),
        'worker_build_image': 'fedora:latest',
    }

    runner = BuildStepPluginsRunner(
        workflow,
        [{
            'name': OrchestrateBuildPlugin.key,
            'args': plugin_args,
        }]
    )

    build_result = runner.run()
    assert not build_result.is_failed()


@pytest.mark.parametrize(
    "user_params_for_config, expect_build_from",
    [
        ({"buildroot_is_imagestream": True, "build_image": "ignored"}, None),
        (
            {"build_image": "registry.io/osbs/buildroot:latest"},
            "image:registry.io/osbs/buildroot:latest",
        ),
    ],
)
def test_args_from_user_params(user_params_for_config, expect_build_from, workflow, source_dir):
    mock_workflow(workflow, source_dir)
    mock_reactor_config(workflow, source_dir)

    operator_mods_url = "http://example.org/modifications.json"

    workflow.user_params.update(user_params_for_config)
    workflow.user_params.update({
        "koji_target": "some-target",
        "operator_csv_modifications_url": operator_mods_url,
    })

    runner = BuildStepPluginsRunner(workflow, [])
    plugin = runner.create_instance_from_plugin(OrchestrateBuildPlugin, {})

    if expect_build_from:
        assert plugin.config_kwargs["build_from"] == expect_build_from
    else:
        assert "build_from" not in plugin.config_kwargs

    assert plugin.build_kwargs["target"] == "some-target"
    assert plugin.build_kwargs["operator_csv_modifications_url"] == operator_mods_url

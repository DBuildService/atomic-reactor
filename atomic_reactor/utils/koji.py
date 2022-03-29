"""
Copyright (c) 2016-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import fnmatch
import json
import logging
import os
import tempfile
from typing import Optional, List, Any, Dict, Tuple, Union

import time
import platform
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugins.post_fetch_docker_archive import FetchDockerArchivePlugin

import koji
import koji_cli.lib

from atomic_reactor import __version__ as atomic_reactor_version
from atomic_reactor.constants import (DEFAULT_DOWNLOAD_BLOCK_SIZE,
                                      IMAGE_TYPE_DOCKER_ARCHIVE,
                                      PROG,
                                      KOJI_BTYPE_OPERATOR_MANIFESTS,
                                      OPERATOR_MANIFESTS_ARCHIVE,
                                      PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY,
                                      PLUGIN_RESOLVE_REMOTE_SOURCE,
                                      PLUGIN_MAVEN_URL_SOURCES_METADATA_KEY,
                                      KOJI_MAX_RETRIES,
                                      KOJI_RETRY_INTERVAL, KOJI_OFFLINE_RETRY_INTERVAL,
                                      PLUGIN_FETCH_MAVEN_KEY)
from atomic_reactor.types import RpmComponent
from atomic_reactor.util import (Output, get_image_upload_filename,
                                 get_checksums, get_manifest_media_type,
                                 create_tar_gz_archive, get_config_from_registry,
                                 get_manifest_digests)
from atomic_reactor.plugins.post_rpmqa import PostBuildRPMqaPlugin
from osbs.utils import ImageName

logger = logging.getLogger(__name__)


class NvrRequest(object):

    def __init__(self, nvr, archives=None):
        self.nvr = nvr
        self.archives = archives or []

        for archive in self.archives:
            archive['matched'] = False

    def match(self, build_archive):
        if not self.archives:
            return True

        for archive in self.archives:
            req_filename = archive.get('filename')
            req_group_id = archive.get('group_id')

            if req_filename and not fnmatch.filter([build_archive['filename']],
                                                   req_filename):
                continue

            if req_group_id and req_group_id != build_archive['group_id']:
                continue

            archive['matched'] = True
            return True

        return False

    def match_all(self, build_archives):
        return [archive for archive in build_archives if self.match(archive)]

    def unmatched(self):
        return [archive for archive in self.archives if not archive['matched']]


class KojiUploadLogger(object):
    def __init__(self, logger, notable_percent=10):
        self.logger = logger
        self.notable_percent = notable_percent
        self.last_percent_done = 0

    def callback(self, offset, totalsize, size, t1, t2):  # pylint: disable=W0613
        if offset == 0:
            self.logger.debug("upload size: %.1fMiB", totalsize / 1024 / 1024)

        if not totalsize or not t1:
            return

        percent_done = 100 * offset // totalsize
        if (percent_done >= 99 or
                percent_done - self.last_percent_done >= self.notable_percent):
            self.last_percent_done = percent_done
            self.logger.debug("upload: %d%% done (%.1f MiB/sec)",
                              percent_done, size / t1 / 1024 / 1024)


def koji_login(session,
               proxyuser=None,
               ssl_certs_dir=None,
               krb_principal=None,
               krb_keytab=None):
    """
    Choose the correct login method based on the available credentials,
    and call that method on the provided session object.

    :param session: koji.ClientSession instance
    :param proxyuser: str, proxy user
    :param ssl_certs_dir: str, path to "cert" (required), and "serverca" (optional)
    :param krb_principal: str, name of Kerberos principal
    :param krb_keytab: str, Kerberos keytab
    :return: None
    """

    kwargs = {}
    if proxyuser:
        kwargs['proxyuser'] = proxyuser

    if ssl_certs_dir:
        # Use certificates
        logger.info("Using SSL certificates for Koji authentication")
        kwargs['cert'] = os.path.join(ssl_certs_dir, 'cert')

        # serverca is not required in newer versions of koji, but if set
        # koji will always ensure file exists
        # NOTE: older versions of koji may require this to be set, in
        # that case, make sure serverca is passed in
        serverca_path = os.path.join(ssl_certs_dir, 'serverca')
        if os.path.exists(serverca_path):
            kwargs['serverca'] = serverca_path

        # Older versions of koji actually require this parameter, even though
        # it's completely ignored.
        kwargs['ca'] = None

        result = session.ssl_login(**kwargs)
    else:
        # Use Kerberos
        logger.info("Using Kerberos for Koji authentication")
        if krb_principal and krb_keytab:
            kwargs['principal'] = krb_principal
            kwargs['keytab'] = krb_keytab

        result = session.krb_login(**kwargs)

    if not result:
        raise RuntimeError('Unable to perform Koji authentication')

    return result


def create_koji_session(hub_url, auth_info=None, use_fast_upload=True):
    """
    Creates and returns a Koji session. If auth_info
    is provided, the session will be authenticated.

    :param hub_url: str, Koji hub URL
    :param auth_info: dict, authentication parameters used for koji_login
    :param use_fast_upload: bool, flag to use or not Koji's fast upload API.
    :return: koji.ClientSession instance
    """
    session = koji.ClientSession(hub_url,
                                 opts={'krb_rdns': False,
                                       'use_fast_upload': use_fast_upload,
                                       'anon_retry': True,
                                       'max_retries': KOJI_MAX_RETRIES,
                                       'retry_interval': KOJI_RETRY_INTERVAL,
                                       'offline_retry': True,
                                       'offline_retry_interval': KOJI_OFFLINE_RETRY_INTERVAL})

    if auth_info is not None:
        koji_login(session, **auth_info)

    return session


class TaskWatcher(object):
    def __init__(self, session, task_id, poll_interval=5):
        self.session = session
        self.task_id = task_id
        self.poll_interval = poll_interval
        self.state = 'CANCELED'

    def wait(self):
        logger.debug("waiting for koji task %r to finish", self.task_id)
        while not self.session.taskFinished(self.task_id):
            time.sleep(self.poll_interval)

        logger.debug("koji task is finished, getting info")
        task_info = self.session.getTaskInfo(self.task_id, request=True)
        self.state = koji.TASK_STATES[task_info['state']]
        return self.state

    def failed(self):
        return self.state in ['CANCELED', 'FAILED']


def stream_task_output(session, task_id, file_name,
                       blocksize=DEFAULT_DOWNLOAD_BLOCK_SIZE):
    """
    Generator to download file from task without loading the whole
    file into memory.
    """
    logger.debug('Streaming %s from task %s', file_name, task_id)
    offset = 0
    contents = '[PLACEHOLDER]'
    while contents:
        contents = session.downloadTaskOutput(task_id, file_name, offset,
                                              blocksize)
        offset += len(contents)
        if contents:
            yield contents

    logger.debug('Finished streaming %s from task %s', file_name, task_id)


def tag_koji_build(session, build_id, target, poll_interval=5):
    logger.debug('Finding destination tag for target %s', target)
    target_info = session.getBuildTarget(target)
    dest_tag = target_info['dest_tag_name']
    logger.info('Tagging build with %s', dest_tag)
    task_id = session.tagBuild(dest_tag, build_id)

    task = TaskWatcher(session, task_id, poll_interval=poll_interval)
    task.wait()
    if task.failed():
        raise RuntimeError('Task %s failed to tag koji build' % task_id)

    return dest_tag


def get_koji_task_owner(session, task_id, default=None):
    default = {} if default is None else default
    if task_id:
        try:
            koji_task_info = session.getTaskInfo(task_id)
            koji_task_owner = session.getUser(koji_task_info['owner'])
        except Exception:
            logger.exception('Unable to get Koji task owner')
            koji_task_owner = default
    else:
        koji_task_owner = default
    return koji_task_owner


def get_koji_module_build(session, module_spec):
    """
    Get build information from Koji for a module. The module specification must
    include at least name, stream and version. For legacy support, you can omit
    context if there is only one build of the specified NAME:STREAM:VERSION.

    :param session: koji.ClientSession, Session for talking to Koji
    :param module_spec: ModuleSpec, specification of the module version
    :return: tuple, a dictionary of information about the build, and
        a list of RPMs in the module build
    """

    if module_spec.context is not None:
        # The easy case - we can build the koji "name-version-release" out of the
        # module spec.
        koji_nvr = "{}-{}-{}.{}".format(module_spec.name,
                                        module_spec.stream.replace("-", "_"),
                                        module_spec.version,
                                        module_spec.context)
        logger.info("Looking up module build %s in Koji", koji_nvr)
        build = session.getBuild(koji_nvr)
    else:
        # Without the context, we have to retrieve all builds for the module, and
        # find the one we want. This is pretty inefficient, but won't be needed
        # long-term.
        logger.info("Listing all builds for %s in Koji", module_spec.name)
        package_id = session.getPackageID(module_spec.name)
        builds = session.listBuilds(packageID=package_id, type='module',
                                    state=koji.BUILD_STATES['COMPLETE'])
        build = None

        for b in builds:
            if '.' in b['release']:
                version, _ = b['release'].split('.', 1)
                if version == module_spec.version:
                    if build is not None:
                        raise RuntimeError("Multiple builds found for {}"
                                           .format(module_spec.to_str()))
                    else:
                        build = b

    if build is None:
        raise RuntimeError("No build found for {}".format(module_spec.to_str()))

    archives = session.listArchives(buildID=build['build_id'])
    # The RPM list for the 'modulemd.txt' archive has all the RPMs, recent
    # versions of MBS also write upload 'modulemd.<arch>.txt' archives with
    # architecture subsets.
    archives = [a for a in archives if a['filename'] == 'modulemd.txt']
    assert len(archives) == 1

    rpm_list = session.listRPMs(imageID=archives[0]['id'])

    return build, rpm_list


def get_buildroot(arch: Optional[str] = None) -> Dict[str, Any]:
    """
    Build the buildroot entry of the metadata.
    :return: dict, partial metadata
    """
    # OSBS2 TBD
    # docker_info = tasker.get_info()
    podman_info = None  # podman info
    host_arch = arch or platform.processor()

    buildroot = {
        'id': 1,
        'host': {
            # OSBS2 TBD
            # 'os': docker_info['OperatingSystem'],
            'os': podman_info,
            'arch': host_arch,
        },
        'content_generator': {
            'name': PROG,
            'version': atomic_reactor_version,
        },
        'container': {
            'type': 'none',
            'arch': host_arch,
        },
        'components': [],
        'tools': [],
    }

    return buildroot


def get_image_output(image_type, image_id, arch, pullspec):
    """
    Create the output for the image

    This is the Koji Content Generator metadata, along with the
    'docker save' output to upload.

    :return: tuple, (metadata dict, Output instance)

    """
    # OSBS2 TBD exported_image_sequence will not work for multiple platform
    image_name = get_image_upload_filename(image_type, image_id, arch)

    readme_content = ('Archive is just a placeholder for the koji archive, if you need the '
                      f'content you can use pullspec of the built image: {pullspec}')
    archive_path = create_tar_gz_archive(file_name='README', file_content=readme_content)
    logger.info('Archive for metadata created: %s', archive_path)

    metadata = get_output_metadata(archive_path, image_name)
    output = Output(filename=archive_path, metadata=metadata)

    return metadata, output


def get_source_tarballs_output(workflow) -> List[Output]:
    plugin_results = workflow.data.prebuild_results.get(PLUGIN_RESOLVE_REMOTE_SOURCE) or {}
    output_tarball_files = []
    for remote_source in plugin_results:
        remote_source_tarball = remote_source['remote_source_tarball']
        metadata = get_output_metadata(remote_source_tarball['path'],
                                       remote_source_tarball['filename'])
        output = Output(filename=remote_source_tarball['path'], metadata=metadata)
        output_tarball_files.append(output)
    return output_tarball_files


def get_remote_sources_json_output(workflow) -> List[Output]:
    plugin_results = workflow.data.prebuild_results.get(PLUGIN_RESOLVE_REMOTE_SOURCE) or {}
    output_json_files = []
    tmpdir = tempfile.mkdtemp()
    for remote_source in plugin_results:
        remote_source_json = remote_source['remote_source_json']
        remote_source_json_filename = remote_source_json['filename']
        file_path = os.path.join(tmpdir, remote_source_json_filename)
        with open(file_path, 'w') as f:
            json.dump(remote_source_json['json'], f, indent=4, sort_keys=True)
        metadata = get_output_metadata(file_path, remote_source_json_filename)
        output = Output(filename=file_path, metadata=metadata)
        output_json_files.append(output)
    return output_json_files


def get_maven_metadata(workflow_data) -> Tuple[List[Output], List[RpmComponent]]:
    """
    Get maven metadata generated by post_maven_url_sources_metadata
    and pre_fetch_maven_artifacts
    :param workflow_data: ImageBuildWorkflowData instance
    :return: list, list; remote source file outputs, kojifile components
    :rtype: list[Output], list[dict] # list of output to add to koji build
                                        and list of kojifile components
    """
    maven_url_sources_metadata_results = workflow_data.postbuild_results.get(
        PLUGIN_MAVEN_URL_SOURCES_METADATA_KEY) or {}
    fetch_maven_results = workflow_data.prebuild_results.get(
        PLUGIN_FETCH_MAVEN_KEY) or {}
    outputs = []
    components = fetch_maven_results.get('components', [])

    for remote_source_file in maven_url_sources_metadata_results.get('remote_source_files', []):
        outputs.append(Output(filename=remote_source_file['file'],
                              metadata=remote_source_file['metadata']))
    return outputs, components


def get_image_components(workflow, platform: str) -> List[Dict[str, Union[str, int]]]:
    """
    Re-package the output of the rpmqa plugin into the format required
    for the metadata.
    """
    components = workflow.data.postbuild_results.get(PostBuildRPMqaPlugin.key)
    if components is None:
        logger.error("%s plugin did not run!", PostBuildRPMqaPlugin.key)
        return []
    return components.get(platform, [])


def add_custom_type(output: Output, custom_type: str, content: Optional[Dict[str, Any]] = None):
    output.metadata.update({
        'type': custom_type,
        'extra': {
            'typeinfo': {
                custom_type: content or {}
            },
        },
    })


def get_output(workflow: DockerBuildWorkflow,
               buildroot_id: str,
               pullspec: ImageName,
               platform: str,
               source_build: bool = False,
               logs: Optional[List[Output]] = None):
    """
    Build the 'output' section of the metadata.
    :param buildroot_id: str, buildroot_id
    :param pullspec: ImageName
    :param platform: str, output platform
    :param source_build: bool, is source_build ?
    :param logs: list, of Output logs
    :return: tuple, list of Output instances, and extra Output file
    """
    def add_buildroot_id(output: Output) -> Output:
        output.metadata.update({'buildroot_id': buildroot_id})
        return output

    def add_log_type(output: Output, arch: str) -> Output:
        output.metadata.update({'type': 'log', 'arch': arch})
        return output

    extra_output_file = None
    output_files: List[Output] = []

    arch = os.uname()[4]

    if source_build:
        manifest = workflow.data.koji_source_manifest
        image_id = manifest['config']['digest']
        # we are using digest from manifest, because we can't get diff_ids
        # unless we pull image, which would fail due because there are so many layers
        layer_sizes = [{'digest': layer['digest'], 'size': layer['size']}
                       for layer in manifest['layers']]
        platform = arch

    else:
        output_files = [add_log_type(add_buildroot_id(metadata), arch)
                        for metadata in logs or []]

        # Parent of squashed built image is base image
        # OSBS2 TBD
        image_id = workflow.data.image_id
        parent_id = None
        if not workflow.data.dockerfile_images.base_from_scratch:
            # OSBS2 TBD: inspect the correct architecture
            parent_id = workflow.imageutil.base_image_inspect()['Id']

        layer_sizes = workflow.data.layer_sizes

    digests = get_manifest_digests(pullspec, workflow.conf.registry['uri'],
                                   workflow.conf.registry['insecure'],
                                   workflow.conf.registry.get('secret', None))

    if digests.v2:
        config_manifest_digest = digests.v2
        config_manifest_type = 'v2'
    else:
        config_manifest_digest = digests.oci
        config_manifest_type = 'oci'

    config = get_config_from_registry(pullspec, workflow.conf.registry['uri'],
                                      config_manifest_digest, workflow.conf.registry['insecure'],
                                      workflow.conf.registry.get('secret', None),
                                      config_manifest_type)

    # We don't need container_config section
    if config and 'container_config' in config:
        del config['container_config']

    digest_pullspec = f"{pullspec.to_str(tag=False)}@{select_digest(digests)}"
    repositories = [pullspec.to_str(), digest_pullspec]

    typed_digests = {
        get_manifest_media_type(version): digest
        for version, digest in digests.items()
        if version != "v1"
    }

    if source_build:
        image_type = IMAGE_TYPE_DOCKER_ARCHIVE
    else:
        image_metadatas = workflow.data.postbuild_results[FetchDockerArchivePlugin.key]
        image_type = image_metadatas[platform]["type"]

    tags = set(image.tag for image in workflow.data.tag_conf.images)
    metadata, output = get_image_output(image_type, image_id, platform, pullspec)

    metadata.update({
        'arch': arch,
        'type': 'docker-image',
        'components': [],
        'extra': {
            'image': {
                'arch': arch,
            },
            'docker': {
                'id': image_id,
                'repositories': repositories,
                'layer_sizes': layer_sizes,
                'tags': list(tags),
                'config': config,
                'digests': typed_digests,
            },
        },
    })

    if not config:
        del metadata['extra']['docker']['config']

    if not source_build:
        metadata['components'] = get_image_components(workflow, platform)

        if not workflow.data.dockerfile_images.base_from_scratch:
            metadata['extra']['docker']['parent_id'] = parent_id

    # Add the 'docker save' image to the output
    image = add_buildroot_id(output)

    # when doing regular build, worker already uploads image,
    # so orchestrator needs only metadata,
    # but source contaiener build didn't upload that image yet,
    # so we want metadata, and the image to upload
    if source_build:
        output_files.append(metadata)
        extra_output_file = output
    else:
        output_files.append(image)

    if not source_build:
        # add operator manifests to output
        operator_manifests_path = (workflow.data.postbuild_results
                                   .get(PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY))
        if operator_manifests_path:
            manifests_metadata = get_output_metadata(operator_manifests_path,
                                                     OPERATOR_MANIFESTS_ARCHIVE)
            operator_manifests_output = Output(filename=operator_manifests_path,
                                               metadata=manifests_metadata)
            add_custom_type(operator_manifests_output, KOJI_BTYPE_OPERATOR_MANIFESTS)

            operator_manifests = add_buildroot_id(operator_manifests_output)
            output_files.append(operator_manifests)

    return output_files, extra_output_file


def generate_koji_upload_dir():
    """
    Create a path name for uploading files to

    :return: str, path name expected to be unique
    """
    return koji_cli.lib.unique_path('koji-upload')


def get_output_metadata(path, filename):
    """
    Describe a file by its metadata.

    :return: dict
    """
    checksums = get_checksums(path, ['md5'])
    metadata = {'filename': filename,
                'filesize': os.path.getsize(path),
                'checksum': checksums['md5sum'],
                'checksum_type': 'md5'}

    return metadata


def select_digest(digests):
    digest = digests.default

    return digest

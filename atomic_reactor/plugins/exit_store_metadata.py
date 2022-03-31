"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import json
import os

from osbs.exceptions import OsbsResponseException

from atomic_reactor.plugins.pre_fetch_sources import PLUGIN_FETCH_SOURCES_KEY
from atomic_reactor.constants import PLUGIN_VERIFY_MEDIA_KEY, SCRATCH_FROM
from atomic_reactor.config import get_openshift_session
from atomic_reactor.plugin import ExitPlugin
from atomic_reactor.util import get_manifest_digests


class StoreMetadataPlugin(ExitPlugin):
    key = "store_metadata"
    is_allowed_to_fail = False

    def __init__(self, workflow):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(StoreMetadataPlugin, self).__init__(workflow)
        self.source_build = PLUGIN_FETCH_SOURCES_KEY in self.workflow.data.prebuild_results

    def get_result(self, result):
        if isinstance(result, Exception):
            result = ''

        return result

    def get_pre_result(self, key):
        return self.get_result(self.workflow.data.prebuild_results.get(key, ''))

    def get_post_result(self, key):
        return self.get_result(self.workflow.data.postbuild_results.get(key, ''))

    def get_exit_result(self, key):
        return self.get_result(self.workflow.data.exit_results.get(key, ''))

    def get_digests(self):
        """
        Returns a map of repositories to digests
        """

        digests = {}  # repository -> digest
        registry = self.workflow.conf.registry

        for image in self.workflow.data.tag_conf.images:
            image_digests = get_manifest_digests(image, registry['uri'], registry['insecure'],
                                                 registry.get('secret', None))
            if image_digests:
                digests[image.to_str()] = image_digests

        return digests

    def get_pullspecs(self, digests):
        # v2 registry digests
        pullspecs = []

        for image in self.workflow.data.tag_conf.images:
            image_str = image.to_str()
            if image_str in digests:
                digest = digests[image_str]
                for digest_version in digest.content_type:
                    if digest_version not in digest:
                        continue
                    pullspecs.append({
                        "registry": image.registry,
                        "repository": image.to_str(registry=False, tag=False),
                        "tag": image.tag,
                        "digest": digest[digest_version],
                        "version": digest_version
                    })

        return pullspecs

    def get_plugin_metadata(self):
        wf_data = self.workflow.data
        return {
            "errors": wf_data.plugins_errors,
            "timestamps": wf_data.plugins_timestamps,
            "durations": wf_data.plugins_durations,
        }

    def get_filesystem_metadata(self):
        data = {}
        try:
            data = self.workflow.fs_watcher.get_usage_data()
            self.log.debug("filesystem metadata: %s", data)
        except Exception:
            self.log.exception("Error getting filesystem stats")

        return data

    def _update_labels(self, labels, updates):
        if updates:
            updates = {key: str(value) for key, value in updates.items()}
            labels.update(updates)

    def make_labels(self):
        labels = {}
        self._update_labels(labels, self.workflow.data.labels)

        if 'sources_for_koji_build_id' in labels:
            labels['sources_for_koji_build_id'] = str(labels['sources_for_koji_build_id'])

        return labels

    def set_koji_task_annotations_whitelist(self, annotations):
        """Whitelist annotations to be included in koji task output

        Allow annotations whose names are listed in task_annotations_whitelist
        koji's configuration to be included in the build_annotations.json file,
        which will be attached in the koji task output.
        """
        koji_config = self.workflow.conf.koji
        whitelist = koji_config.get('task_annotations_whitelist')
        if whitelist:
            annotations['koji_task_annotations_whitelist'] = json.dumps(whitelist)

    def _update_annotations(self, annotations, updates):
        if updates:
            updates = {key: json.dumps(value) for key, value in updates.items()}
            annotations.update(updates)

    def apply_plugin_annotations(self, annotations):
        self._update_annotations(annotations, self.workflow.data.annotations)

    def run(self):
        try:
            pipeline_run_name = self.workflow.user_params['pipeline_run_name']
        except KeyError:
            self.log.error("No pipeline_run_name found")
            raise
        self.log.info("pipelineRun name = %s", pipeline_run_name)
        osbs = get_openshift_session(self.workflow.conf,
                                     self.workflow.namespace)

        wf_data = self.workflow.data

        if not self.source_build:
            try:
                commit_id = self.workflow.source.commit_id
            except AttributeError:
                commit_id = ""

            base_image = wf_data.dockerfile_images.original_base_image
            if base_image is not None and not wf_data.dockerfile_images.base_from_scratch:
                base_image_name = base_image
                try:
                    # OSBS2 TBD: we probably don't need this and many other annotations anymore
                    base_image_id = self.workflow.imageutil.base_image_inspect().get('Id', "")
                except KeyError:
                    base_image_id = ""
            else:
                base_image_name = ""
                base_image_id = ""

            parent_images_strings = self.workflow.parent_images_to_str()
            if wf_data.dockerfile_images.base_from_scratch:
                parent_images_strings[SCRATCH_FROM] = SCRATCH_FROM

            try:
                with open(self.workflow.df_path) as f:
                    dockerfile_contents = f.read()
            except AttributeError:
                dockerfile_contents = ""

        annotations = {
            'digests': json.dumps(self.get_pullspecs(self.get_digests())),
            'plugins-metadata': json.dumps(self.get_plugin_metadata()),
            'filesystem': json.dumps(self.get_filesystem_metadata()),
        }

        if self.source_build:
            annotations['image-id'] = ''
            if wf_data.koji_source_manifest:
                annotations['image-id'] = wf_data.koji_source_manifest['config']['digest']
        else:
            annotations['dockerfile'] = dockerfile_contents
            annotations['commit_id'] = commit_id
            annotations['base-image-id'] = base_image_id
            annotations['base-image-name'] = base_image_name
            # OSBS2 TBD
            annotations['image-id'] = wf_data.image_id or ''
            annotations['parent_images'] = json.dumps(parent_images_strings)

        media_types = []

        media_results = wf_data.postbuild_results.get(PLUGIN_VERIFY_MEDIA_KEY)
        if isinstance(media_results, Exception):
            media_results = None

        if media_results:
            media_types += media_results

        if media_types:
            annotations['media-types'] = json.dumps(sorted(list(set(media_types))))

        tar_path = tar_size = tar_md5sum = tar_sha256sum = None
        if len(wf_data.exported_image_sequence) > 0:
            # OSBS2 TBD exported_image_sequence will not work for multiple platform
            info = wf_data.exported_image_sequence[-1]
            tar_path = info.get("path")
            tar_size = info.get("size")
            tar_md5sum = info.get("md5sum")
            tar_sha256sum = info.get("sha256sum")
        # looks like that openshift can't handle value being None (null in json)
        if tar_size is not None and tar_md5sum is not None and tar_sha256sum is not None and \
                tar_path is not None:
            annotations["tar_metadata"] = json.dumps({
                "size": tar_size,
                "md5sum": tar_md5sum,
                "sha256sum": tar_sha256sum,
                "filename": os.path.basename(tar_path),
            })

        self.apply_plugin_annotations(annotations)
        self.set_koji_task_annotations_whitelist(annotations)

        try:
            osbs.update_annotations_on_build(pipeline_run_name, annotations)
        except OsbsResponseException:
            self.log.debug("annotations: %r", annotations)
            raise

        labels = self.make_labels()
        if labels:
            try:
                osbs.update_labels_on_build(pipeline_run_name, labels)
            except OsbsResponseException:
                self.log.debug("labels: %r", labels)
                raise

        return {"annotations": annotations, "labels": labels}

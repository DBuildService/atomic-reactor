"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, unicode_literals, absolute_import

import os
import subprocess
import tempfile

from atomic_reactor.build import BuildResult
from atomic_reactor.constants import (PLUGIN_SOURCE_CONTAINER_KEY, EXPORTED_SQUASHED_IMAGE_NAME,
                                      IMAGE_TYPE_DOCKER_ARCHIVE, PLUGIN_FETCH_SOURCES_KEY)
from atomic_reactor.plugin import BuildStepPlugin
from atomic_reactor.util import get_exported_image_metadata


class SourceContainerPlugin(BuildStepPlugin):
    """
    Build source container image using
    https://github.com/containers/BuildSourceImage
    """

    key = PLUGIN_SOURCE_CONTAINER_KEY

    def export_image(self, image_output_dir):
        output_path = os.path.join(tempfile.mkdtemp(), EXPORTED_SQUASHED_IMAGE_NAME)

        cmd = ['skopeo', 'copy']
        source_img = 'oci:{}'.format(image_output_dir)
        dest_img = 'docker-archive:{}'.format(output_path)
        cmd += [source_img, dest_img]

        self.log.info("Calling: %s", ' '.join(cmd))
        try:
            subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            self.log.error("failed to save docker-archive :\n%s", e.output)
            raise

        img_metadata = get_exported_image_metadata(output_path, IMAGE_TYPE_DOCKER_ARCHIVE)
        self.workflow.exported_image_sequence.append(img_metadata)

    def run(self):
        """Build image inside current environment.

        Returns:
            BuildResult
        """
        fetch_sources_result = self.workflow.prebuild_results.get(PLUGIN_FETCH_SOURCES_KEY, {})
        source_data_dir = fetch_sources_result.get('image_sources_dir')
        remote_source_data_dir = fetch_sources_result.get('remote_sources_dir')

        source_exists = source_data_dir and os.path.isdir(source_data_dir)
        remote_source_exists = remote_source_data_dir and os.path.isdir(remote_source_data_dir)

        if not source_exists and not remote_source_exists:
            err_msg = "No SRPMs directory '{}' available".format(source_data_dir)
            err_msg += "\nNo Remote source directory '{}' available".format(remote_source_data_dir)
            self.log.error(err_msg)
            return BuildResult(logs=err_msg, fail_reason=err_msg)

        if source_exists and not os.listdir(source_data_dir):
            self.log.warning("SRPMs directory '%s' is empty", source_data_dir)
        if remote_source_exists and not os.listdir(remote_source_data_dir):
            self.log.warning("Remote source directory '%s' is empty", remote_source_data_dir)

        image_output_dir = tempfile.mkdtemp()
        cmd = ['bsi', '-d']
        drivers = []

        if source_exists:
            drivers.append('sourcedriver_rpm_dir')
            cmd.append('-s')
            cmd.append('{}'.format(source_data_dir))

        if remote_source_exists:
            drivers.append('sourcedriver_extra_src_dir')
            cmd.append('-e')
            cmd.append('{}'.format(remote_source_data_dir))

        driver_str = ','.join(drivers)
        cmd.insert(2, driver_str)
        cmd.append('-o')
        cmd.append('{}'.format(image_output_dir))

        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            self.log.error("BSI failed with output:\n%s", e.output)
            return BuildResult(logs=e.output, fail_reason='BSI utility failed build source image')

        self.log.debug("Build log:\n%s\n", output)

        self.export_image(image_output_dir)

        return BuildResult(
            logs=output,
            oci_image_path=image_output_dir,
            skip_layer_squash=True
        )

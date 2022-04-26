"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from atomic_reactor.constants import IMAGE_TYPE_DOCKER_ARCHIVE
from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import Plugin
from atomic_reactor.util import get_exported_image_metadata


class FetchDockerArchivePlugin(Plugin):
    key = 'fetch_docker_archive'
    is_allowed_to_fail = False

    def __init__(self, workflow):
        """
        :param workflow: DockerBuildWorkflow instance
        """
        super(FetchDockerArchivePlugin, self).__init__(workflow)

    def download_image(self, build_dir: BuildDir):
        image = self.workflow.data.tag_conf.get_unique_images_with_platform(build_dir.platform)[0]
        image_path = str(build_dir.exported_squashed_image)
        image_type = IMAGE_TYPE_DOCKER_ARCHIVE

        self.log.info('fetching image %s', image)
        self.workflow.imageutil.download_image_archive_tarball(image, image_path)

        metadata = get_exported_image_metadata(image_path, image_type)

        self.log.info('image for platform:%s available at %s', build_dir.platform, image_path)

        return metadata

    def run(self):
        return self.workflow.build_dir.for_each_platform(self.download_image)

"""
Copyright (c) 2017-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Updates the Dockerfile created by pre_flatpak_create_dockerfile with
information from a module compose created by pre_resolve_composes.
"""
import functools
import os

from atomic_reactor.dirs import BuildDir
from flatpak_module_tools.flatpak_builder import FlatpakSourceInfo, FlatpakBuilder, ModuleInfo

import gi
try:
    gi.require_version('Modulemd', '2.0')
except ValueError as e:
    # Normalize to ImportError to simplify handling
    raise ImportError(str(e)) from e
from gi.repository import Modulemd

from osbs.repo_utils import ModuleSpec

from atomic_reactor.constants import PLUGIN_RESOLVE_COMPOSES_KEY
from atomic_reactor.config import get_koji_session
from atomic_reactor.utils.koji import get_koji_module_build
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.plugins.pre_flatpak_create_dockerfile import (FLATPAK_INCLUDEPKGS_FILENAME,
                                                                  FLATPAK_CLEANUPSCRIPT_FILENAME)
from atomic_reactor.util import is_flatpak_build, map_to_user_params


# ODCS API constant
SOURCE_TYPE_MODULE = 2

WORKSPACE_SOURCE_KEY = 'source_info'
WORKSPACE_COMPOSE_KEY = 'compose_info'


class ComposeInfo(object):
    def __init__(self, source_spec, main_module, modules):
        self.source_spec = source_spec
        self.main_module = main_module
        self.modules = modules

    def koji_metadata(self):
        sorted_modules = [self.modules[k] for k in sorted(self.modules.keys())]

        # We exclude the 'platform' pseudo-module here since we don't enable
        # it for package installation - it doesn't influence the image contents
        return {
            'source_modules': [self.source_spec],
            'modules': ['-'.join((m.name, m.stream, m.version)) for
                        m in sorted_modules if m.name != 'platform'],
        }


class FlatpakUpdateDockerfilePlugin(PreBuildPlugin):
    key = "flatpak_update_dockerfile"
    is_allowed_to_fail = False

    args_from_user_params = map_to_user_params("compose_ids")

    def __init__(self, workflow):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super(FlatpakUpdateDockerfilePlugin, self).__init__(workflow)

    def _resolve_modules(self, modules):
        koji_session = get_koji_session(self.workflow.conf)

        resolved_modules = {}
        for module_spec in modules:
            build, rpm_list = get_koji_module_build(koji_session, module_spec)

            # The returned RPM list contains source RPMs and RPMs for all
            # architectures.
            rpms = ['{name}-{epochnum}:{version}-{release}.{arch}.rpm'
                    .format(epochnum=rpm['epoch'] or 0, **rpm)
                    for rpm in rpm_list]

            # strict=False - don't break if new fields are added
            mmd = Modulemd.ModuleStream.read_string(
                build['extra']['typeinfo']['module']['modulemd_str'], strict=False)
            # Make sure we have a version 2 modulemd file
            mmd = mmd.upgrade(Modulemd.ModuleStreamVersionEnum.TWO)

            resolved_modules[module_spec.name] = ModuleInfo(module_spec.name,
                                                            module_spec.stream,
                                                            module_spec.version,
                                                            mmd, rpms)
        return resolved_modules

    def _build_compose_info(self, modules):
        source_spec = get_flatpak_source_spec(self.workflow)
        assert source_spec is not None  # flatpak_create_dockerfile must be run first
        main_module = ModuleSpec.from_str(source_spec)

        resolved_modules = self._resolve_modules(modules)

        main_module_info = resolved_modules[main_module.name]
        assert main_module_info.stream == main_module.stream
        if main_module.version is not None:
            assert main_module_info.version == main_module.version

        return ComposeInfo(source_spec=source_spec,
                           main_module=main_module_info,
                           modules=resolved_modules)

    def _load_compose_info(self):
        source_spec = get_flatpak_source_spec(self.workflow)
        assert source_spec is not None  # flatpak_create_dockerfile must be run first
        main_module = ModuleSpec.from_str(source_spec)

        resolve_comp_result = self.workflow.data.prebuild_results.get(PLUGIN_RESOLVE_COMPOSES_KEY)
        composes = resolve_comp_result['composes']

        for compose_info in composes:
            if compose_info['source_type'] != SOURCE_TYPE_MODULE:
                continue

            modules = [ModuleSpec.from_str(s) for s in compose_info['source'].split()]
            for module in modules:
                if module.name == main_module.name and module.stream == main_module.stream:
                    set_flatpak_compose_info(self.workflow, self._build_compose_info(modules))
                    return

        self.log.debug('Compose info: %s', composes)
        raise RuntimeError("Can't find main module %s in compose result" % source_spec)

    def _load_source(self):
        flatpak_yaml = self.workflow.source.config.flatpak

        compose_info = get_flatpak_compose_info(self.workflow)

        module_spec = ModuleSpec.from_str(compose_info.source_spec)

        source_info = FlatpakSourceInfo(flatpak_yaml,
                                        compose_info.modules,
                                        compose_info.main_module,
                                        module_spec.profile)
        set_flatpak_source_info(self.workflow, source_info)

    def update_dockerfile(self, builder, compose_info, build_dir: BuildDir):
        # Update the dockerfile

        # We need to enable all the modules other than the platform pseudo-module
        enable_modules_str = ' '.join(builder.get_enable_modules())

        install_packages_str = ' '.join(builder.get_install_packages())

        replacements = {
            '@ENABLE_MODULES@': enable_modules_str,
            '@INSTALL_PACKAGES@': install_packages_str,
            '@RELEASE@': compose_info.main_module.version,
        }

        dockerfile = build_dir.dockerfile
        content = dockerfile.content

        # Perform the substitutions; simple approach - should be efficient enough
        for old, new in replacements.items():
            content = content.replace(old, new)

        dockerfile.content = content

    def create_includepkgs_file_and_cleanupscript(self, builder, build_dir: BuildDir):
        # Create a file describing which packages from the base yum repositories are included
        includepkgs = builder.get_includepkgs()
        includepkgs_path = build_dir.path / FLATPAK_INCLUDEPKGS_FILENAME
        with open(includepkgs_path, 'w') as f:
            f.write('includepkgs = ' + ','.join(includepkgs) + '\n')

        # Create the cleanup script
        cleanupscript = build_dir.path / FLATPAK_CLEANUPSCRIPT_FILENAME
        with open(cleanupscript, 'w') as f:
            f.write(builder.get_cleanup_script())
        os.chmod(cleanupscript, 0o0500)
        return [includepkgs_path, cleanupscript]

    def run(self):
        """
        run the plugin
        """
        if not is_flatpak_build(self.workflow):
            self.log.info('not flatpak build, skipping plugin')
            return

        self._load_compose_info()
        compose_info = get_flatpak_compose_info(self.workflow)

        self._load_source()
        source = get_flatpak_source_info(self.workflow)

        builder = FlatpakBuilder(source, None, None)

        builder.precheck()

        flatpak_update = functools.partial(self.update_dockerfile, builder, compose_info)
        self.workflow.build_dir.for_each_platform(flatpak_update)

        create_files = functools.partial(self.create_includepkgs_file_and_cleanupscript,
                                         builder)
        self.workflow.build_dir.for_all_platforms_copy(create_files)

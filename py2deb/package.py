# py2deb: Python to Debian package converter.
#
# Authors:
#  - Arjan Verwer
#  - Peter Odding <peter.odding@paylogic.com>
# Last Change: February 24, 2018
# URL: https://py2deb.readthedocs.io

"""
The :mod:`py2deb.package` module contains the low level conversion logic.

This module defines the :class:`PackageToConvert` class which implements the
low level logic of converting a single Python package to a Debian package. The
separation between the :class:`.PackageConverter` and :class:`PackageToConvert`
classes is somewhat crude (because neither class can work without the other)
but the idea is to separate the high level conversion logic from the low level
conversion logic.
"""

# Standard library modules.
import glob
import logging
import os
import re
import time

# External dependencies.
from cached_property import cached_property
from deb_pkg_tools.control import merge_control_fields, unparse_control_fields
from deb_pkg_tools.package import build_package
from executor import execute
from humanfriendly import concatenate, pluralize
from pkg_resources import Requirement
from pkginfo import UnpackedSDist
from six.moves import configparser

# Modules included in our package.
from py2deb.utils import (embed_install_prefix, normalize_package_version, package_names_match,
                          python_version, TemporaryDirectory)

# Initialize a logger.
logger = logging.getLogger(__name__)

# The following installation prefixes are known to contain a `bin' directory
# that's available on the default executable search path (the environment
# variable $PATH).
KNOWN_INSTALL_PREFIXES = ('/usr', '/usr/local')


class PackageToConvert(object):

    """
    Abstraction for Python packages to be converted to Debian packages.

    Contains a :class:`pip_accel.req.Requirement` object, has a back
    reference to the :class:`.PackageConverter` and provides all of the
    Debian package metadata implied by the Python package metadata.
    """

    def __init__(self, converter, requirement):
        """
        Initialize a package to convert.

        :param converter: The :class:`.PackageConverter` that holds the user
                          options and knows how to transform package names.
        :param requirement: A :class:`pip_accel.req.Requirement` object
                            (created by :func:`~py2deb.converter.PackageConverter.get_source_distributions()`).
        """
        self.converter = converter
        self.requirement = requirement

    def __str__(self):
        """The name, version and extras of the package encoded in a human readable string."""
        version = [self.python_version]
        extras = self.requirement.pip_requirement.extras
        if extras:
            version.append("extras: %s" % concatenate(sorted(extras)))
        return "%s (%s)" % (self.python_name, ', '.join(version))

    @property
    def python_name(self):
        """The name of the Python package (a string)."""
        return self.requirement.name

    @cached_property
    def debian_name(self):
        """The name of the converted Debian package (a string)."""
        return self.converter.transform_name(self.python_name, *self.requirement.pip_requirement.extras)

    @property
    def python_version(self):
        """The version of the Python package (a string)."""
        return self.requirement.version

    @cached_property
    def vcs_revision(self):
        """
        The VCS revision of the Python package.

        This works by parsing the ``.hg_archival.txt`` file generated by the
        ``hg archive`` command so for now this only supports Python source
        distributions exported from Mercurial repositories.
        """
        filename = os.path.join(self.requirement.source_directory, '.hg_archival.txt')
        if os.path.isfile(filename):
            with open(filename) as handle:
                for line in handle:
                    name, _, value = line.partition(':')
                    if name.strip() == 'node':
                        return value.strip()

    @cached_property
    def debian_version(self):
        """
        The version of the Debian package (a string).

        Reformats :attr:`python_version` using
        :func:`.normalize_package_version()`.
        """
        return normalize_package_version(self.python_version)

    @cached_property
    def debian_maintainer(self):
        """
        Get the package maintainer's name and e-mail address.

        The name and e-mail address are combined into a single string that can
        be embedded in a Debian package.
        """
        maintainer = self.metadata.maintainer
        maintainer_email = self.metadata.maintainer_email
        if not maintainer:
            maintainer = self.metadata.author
            maintainer_email = self.metadata.author_email
        if maintainer and maintainer_email:
            return '%s <%s>' % (maintainer, maintainer_email.strip('<>'))
        else:
            return maintainer or 'Unknown'

    @cached_property
    def debian_description(self):
        """
        Get a minimal description for the converted Debian package.

        Includes the name of the Python package and the date at which the
        package was converted.
        """
        text = ["Python package", self.python_name, "converted by py2deb on"]
        # The %e directive (not documented in the Python standard library but
        # definitely available on Linux which is the only platform that py2deb
        # targets, for obvious reasons :-) includes a leading space for single
        # digit day-of-month numbers. I don't like that, fixed width fields are
        # an artefact of 30 years ago and have no place in my software
        # (generally speaking :-). This explains the split/compact duo.
        text.extend(time.strftime('%B %e, %Y at %H:%M').split())
        return ' '.join(text)

    @cached_property
    def metadata(self):
        """
        Get the Python package metadata.

        The metadata is loaded from the ``PKG-INFO`` file generated by ``pip``
        when it unpacked the source distribution archive. Results in a
        pkginfo.UnpackedSDist_ object.

        .. _pkginfo.UnpackedSDist: http://pythonhosted.org/pkginfo/distributions.html
        """
        return UnpackedSDist(self.find_egg_info_file())

    @cached_property
    def namespace_packages(self):
        """
        Get the Python `namespace packages`_ defined by the Python package.

        This property returns the same value that was originally passed to the
        ``namespace_packages`` keyword argument of ``setuptools.setup()``
        (albeit in a very indirect way, but nonetheless the same value :-).

        :returns: A list of dotted names (strings).

        .. _namespace packages: https://pythonhosted.org/setuptools/setuptools.html#namespace-packages
        """
        dotted_names = []
        namespace_packages_file = self.find_egg_info_file('namespace_packages.txt')
        if namespace_packages_file:
            with open(namespace_packages_file) as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        dotted_names.append(line)
        return dotted_names

    @cached_property
    def namespaces(self):
        """
        Get the Python `namespace packages`_ defined by the Python package.

        :returns: A list of unique tuples of strings. The tuples are sorted by
                  increasing length (the number of strings in each tuple) so
                  that e.g. ``zope`` is guaranteed to sort before
                  ``zope.app``.

        This property processes the result of :attr:`namespace_packages`
        into a more easily usable format. Here's an example of the difference
        between :attr:`namespace_packages` and :attr:`namespaces`:

        >>> from py2deb.converter import PackageConverter
        >>> converter = PackageConverter()
        >>> package = next(converter.get_source_distributions(['zope.app.cache']))
        >>> package.namespace_packages
        ['zope', 'zope.app']
        >>> package.namespaces
        [('zope',), ('zope', 'app')]

        The value of this property is used by
        :func:`~py2deb.hooks.initialize_namespaces()` and
        :func:`~py2deb.hooks.cleanup_namespaces()` during installation and
        removal of the generated package.
        """
        namespaces = set()
        for namespace_package in self.namespace_packages:
            dotted_name = []
            for component in namespace_package.split('.'):
                dotted_name.append(component)
                namespaces.add(tuple(dotted_name))
        return sorted(namespaces, key=lambda n: len(n))

    @cached_property
    def has_custom_install_prefix(self):
        """
        Check whether package is being installed under custom installation prefix.

        :returns: ``True`` if the package is being installed under a custom
                  installation prefix, ``False`` otherwise.

        A custom installation prefix is an installation prefix whose ``bin``
        directory is (likely) not available on the default executable search
        path (the environment variable ``$PATH``)
        """
        return self.converter.install_prefix not in KNOWN_INSTALL_PREFIXES

    @cached_property
    def python_requirements(self):
        """
        Find the installation requirements of the Python package.

        :returns: A list of :class:`pkg_resources.Requirement` objects.

        This property used to be implemented by manually parsing the
        ``requires.txt`` file generated by pip when it unpacks a source
        distribution archive.

        While this implementation was eventually enhanced to supported named
        extras, it never supported environment markers.

        Since then this property has been reimplemented to use
        ``pkg_resources.Distribution.requires()`` so that
        environment markers are supported.

        If the new implementation fails the property falls back to the old
        implementation (as a precautionary measure to avoid unexpected side
        effects of the new implementation).
        """
        requirements = []
        try:
            dist = self.requirement.pip_requirement.get_dist()
            extras = self.requirement.pip_requirement.extras
            requirements.extend(dist.requires(extras))
        except Exception:
            logger.warning("Failed to determine installation requirements of %s "
                           "using pkg-resources, falling back to old implementation.",
                           self, exc_info=True)
            filename = self.find_egg_info_file('requires.txt')
            if filename:
                selected_extras = set(extra.lower() for extra in self.requirement.pip_requirement.extras)
                current_extra = None
                with open(filename) as handle:
                    for line in handle:
                        line = line.strip()
                        if line.startswith('['):
                            current_extra = line.strip('[]').lower()
                        elif line and (current_extra is None or current_extra in selected_extras):
                            requirements.append(Requirement.parse(line))
        logger.debug("Python requirements of %s: %r", self, requirements)
        return requirements

    @cached_property
    def debian_dependencies(self):
        """
        Find Debian dependencies of Python package.

        Converts `Python version specifiers`_ to `Debian package
        relationships`_.

        :returns: A list with Debian package relationships (strings) in the
                  format of the ``Depends:`` line of a Debian package
                  ``control`` file. Based on :data:`python_requirements`.

        .. _Python version specifiers: http://www.python.org/dev/peps/pep-0440/#version-specifiers
        .. _Debian package relationships: https://www.debian.org/doc/debian-policy/ch-relationships.html
        """
        dependencies = set()
        for requirement in self.python_requirements:
            debian_package_name = self.converter.transform_name(requirement.project_name, *requirement.extras)
            if requirement.specs:
                for constraint, version in requirement.specs:
                    version = self.converter.transform_version(self, requirement.project_name, version)
                    if version == 'dev':
                        # Requirements like 'pytz > dev' (celery==3.1.16) don't
                        # seem to really mean anything to pip (based on my
                        # reading of the 1.4.x source code) but Debian will
                        # definitely complain because version strings should
                        # start with a digit. In this case we'll just fall
                        # back to a dependency without a version specification
                        # so we don't drop the dependency.
                        dependencies.add(debian_package_name)
                    elif constraint == '==':
                        dependencies.add('%s (= %s)' % (debian_package_name, version))
                    elif constraint == '!=':
                        values = (debian_package_name, version, debian_package_name, version)
                        dependencies.add('%s (<< %s) | %s (>> %s)' % values)
                    elif constraint == '<':
                        dependencies.add('%s (<< %s)' % (debian_package_name, version))
                    elif constraint == '>':
                        dependencies.add('%s (>> %s)' % (debian_package_name, version))
                    elif constraint in ('<=', '>='):
                        dependencies.add('%s (%s %s)' % (debian_package_name, constraint, version))
                    else:
                        msg = "Conversion specifier not supported! (%r used by Python package %s)"
                        raise Exception(msg % (constraint, self.python_name))
            else:
                dependencies.add(debian_package_name)
        dependencies = sorted(dependencies)
        logger.debug("Debian dependencies of %s: %r", self, dependencies)
        return dependencies

    @cached_property
    def existing_archive(self):
        """
        Find ``*.deb`` archive for current package name and version.

        :returns: The pathname of the found archive (a string) or ``None`` if
                  no existing archive is found.
        """
        return (self.converter.repository.get_package(self.debian_name, self.debian_version, 'all') or
                self.converter.repository.get_package(self.debian_name, self.debian_version,
                                                      self.converter.debian_architecture))

    def convert(self):
        """
        Convert current package from Python package to Debian package.

        :returns: The pathname of the generated ``*.deb`` archive.
        """
        with TemporaryDirectory(prefix='py2deb-build-') as build_directory:

            # Prepare the absolute pathname of the Python interpreter on the
            # target system. This pathname will be embedded in the first line
            # of executable scripts (including the post-installation and
            # pre-removal scripts).
            python_executable = '/usr/bin/%s' % python_version()

            # Unpack the binary distribution archive provided by pip-accel inside our build directory.
            build_install_prefix = os.path.join(build_directory, self.converter.install_prefix.lstrip('/'))
            self.converter.pip_accel.bdists.install_binary_dist(
                members=self.transform_binary_dist(),
                prefix=build_install_prefix,
                python=python_executable,
                virtualenv_compatible=False,
            )

            # Determine the directory (at build time) where the *.py files for
            # Python modules are located (the site-packages equivalent).
            if self.has_custom_install_prefix:
                build_modules_directory = os.path.join(build_install_prefix, 'lib')
            else:
                dist_packages_directories = glob.glob(os.path.join(build_install_prefix, 'lib/python*/dist-packages'))
                if len(dist_packages_directories) != 1:
                    msg = "Expected to find a single 'dist-packages' directory inside converted package!"
                    raise Exception(msg)
                build_modules_directory = dist_packages_directories[0]

            # Determine the directory (at installation time) where the *.py
            # files for Python modules are located.
            install_modules_directory = os.path.join('/', os.path.relpath(build_modules_directory, build_directory))

            # Execute a user defined command inside the directory where the Python modules are installed.
            command = self.converter.scripts.get(self.python_name.lower())
            if command:
                execute(command, directory=build_modules_directory, logger=logger)

            # Determine the package's dependencies, starting with the currently
            # running version of Python and the Python requirements converted
            # to Debian packages.
            dependencies = [python_version()] + self.debian_dependencies

            # Check if the converted package contains any compiled *.so files.
            shared_object_files = self.find_shared_object_files(build_directory)
            if shared_object_files:
                # Determine system dependencies by analyzing the linkage of the
                # *.so file(s) found in the converted package.
                dependencies += self.find_system_dependencies(shared_object_files)

            # Make up some control file fields ... :-)
            architecture = self.determine_package_architecture(shared_object_files)
            control_fields = unparse_control_fields(dict(package=self.debian_name,
                                                         version=self.debian_version,
                                                         maintainer=self.debian_maintainer,
                                                         description=self.debian_description,
                                                         architecture=architecture,
                                                         depends=dependencies,
                                                         priority='optional',
                                                         section='python'))

            # Automatically add the Mercurial global revision id when available.
            if self.vcs_revision:
                control_fields['Vcs-Hg'] = self.vcs_revision

            # Apply user defined control field overrides from `stdeb.cfg'.
            control_fields = self.load_control_field_overrides(control_fields)

            # Create the DEBIAN directory.
            debian_directory = os.path.join(build_directory, 'DEBIAN')
            os.mkdir(debian_directory)

            # Generate the DEBIAN/control file.
            control_file = os.path.join(debian_directory, 'control')
            logger.debug("Saving control file fields to %s: %s", control_file, control_fields)
            with open(control_file, 'wb') as handle:
                control_fields.dump(handle)

            # Lintian is a useful tool to find mistakes in Debian binary
            # packages however Lintian checks from the perspective of a package
            # included in the official Debian repositories. Because py2deb
            # doesn't and probably never will generate such packages some
            # messages emitted by Lintian are useless (they merely point out
            # how the internals of py2deb work). Because of this we silence
            # `known to be irrelevant' messages from Lintian using overrides.
            overrides_directory = os.path.join(build_directory, 'usr', 'share',
                                               'lintian', 'overrides')
            overrides_file = os.path.join(overrides_directory, self.debian_name)
            os.makedirs(overrides_directory)
            with open(overrides_file, 'w') as handle:
                for tag in ['debian-changelog-file-missing',
                            'embedded-javascript-library',
                            'extra-license-file',
                            'unknown-control-interpreter',
                            'vcs-field-uses-unknown-uri-format']:
                    handle.write('%s: %s\n' % (self.debian_name, tag))

            # Find the alternatives relevant to the package we're building.
            alternatives = set((link, path) for link, path in self.converter.alternatives
                               if os.path.isfile(os.path.join(build_directory, path.lstrip('/'))))

            # Generate post-installation and pre-removal maintainer scripts.
            self.generate_maintainer_script(filename=os.path.join(debian_directory, 'postinst'),
                                            python_executable=python_executable,
                                            function='post_installation_hook',
                                            package_name=self.debian_name,
                                            alternatives=alternatives,
                                            modules_directory=install_modules_directory,
                                            namespaces=self.namespaces)
            self.generate_maintainer_script(filename=os.path.join(debian_directory, 'prerm'),
                                            python_executable=python_executable,
                                            function='pre_removal_hook',
                                            package_name=self.debian_name,
                                            alternatives=alternatives,
                                            modules_directory=install_modules_directory,
                                            namespaces=self.namespaces)

            # Enable a user defined Python callback to manipulate the resulting
            # binary package before it's turned into a *.deb archive (e.g.
            # manipulate the contents or change the package metadata).
            if self.converter.python_callback:
                logger.debug("Invoking user defined Python callback ..")
                self.converter.python_callback(self.converter, self, build_directory)
                logger.debug("User defined Python callback finished!")

            return build_package(directory=build_directory,
                                 check_package=self.converter.lintian_enabled,
                                 copy_files=False)

    def transform_binary_dist(self):
        """
        Build Python package and transform directory layout.

        Builds the Python package (using :mod:`pip_accel`) and changes the
        names of the files included in the package to match the layout
        corresponding to the given conversion options.

        :returns: An iterable of tuples with two values each:

                  1. A :class:`tarfile.TarInfo` object;
                  2. A file-like object.
        """
        for member, handle in self.converter.pip_accel.bdists.get_binary_dist(self.requirement):
            if self.has_custom_install_prefix:
                # Strip the complete /usr/lib/pythonX.Y/site-packages/ prefix
                # so we can replace it with the custom installation prefix
                # (at this point /usr/ has been stripped by get_binary_dist()).
                member.name = re.sub(r'lib/python\d+(\.\d+)*/(dist|site)-packages/', 'lib/', member.name)
                # Rewrite executable Python scripts so they know about the
                # custom installation prefix.
                if member.name.startswith('bin/'):
                    handle = embed_install_prefix(handle, os.path.join(self.converter.install_prefix, 'lib'))
            else:
                # Rewrite /site-packages/ to /dist-packages/. For details see
                # https://wiki.debian.org/Python#Deviations_from_upstream.
                member.name = member.name.replace('/site-packages/', '/dist-packages/')
            yield member, handle

    def find_shared_object_files(self, directory):
        """
        Search directory tree of converted package for shared object files.

        Runs ``strip --strip-unneeded`` on all ``*.so`` files found.

        :param directory: The directory to search (a string).
        :returns: A list with pathnames of ``*.so`` files.
        """
        shared_object_files = []
        for root, dirs, files in os.walk(directory):
            for filename in files:
                if filename.endswith('.so'):
                    pathname = os.path.join(root, filename)
                    shared_object_files.append(pathname)
                    execute('strip', '--strip-unneeded', pathname, logger=logger)
        if shared_object_files:
            logger.debug("Found one or more shared object files: %s", shared_object_files)
        return shared_object_files

    def find_system_dependencies(self, shared_object_files):
        """
        (Ab)use dpkg-shlibdeps_ to find dependencies on system libraries.

        :param shared_object_files: The pathnames of the ``*.so`` file(s) contained
                                    in the package (a list of strings).
        :returns: A list of strings in the format of the entries on the
                  ``Depends:`` line of a binary package control file.

        .. _dpkg-shlibdeps: https://www.debian.org/doc/debian-policy/ch-sharedlibs.html#s-dpkg-shlibdeps
        """
        logger.debug("Abusing `dpkg-shlibdeps' to find dependencies on shared libraries ..")
        # Create a fake source package, because `dpkg-shlibdeps' expects this...
        with TemporaryDirectory(prefix='py2deb-dpkg-shlibdeps-') as fake_source_directory:
            # Create the debian/ directory expected in the source package directory.
            os.mkdir(os.path.join(fake_source_directory, 'debian'))
            # Create an empty debian/control file because `dpkg-shlibdeps' requires
            # this (even though it is apparently fine for the file to be empty ;-).
            open(os.path.join(fake_source_directory, 'debian', 'control'), 'w').close()
            # Run `dpkg-shlibdeps' inside the fake source package directory, but
            # let it analyze the *.so files from the actual build directory.
            command = ['dpkg-shlibdeps', '-O', '--warnings=0'] + shared_object_files
            output = execute(*command, directory=fake_source_directory, capture=True, logger=logger)
            expected_prefix = 'shlibs:Depends='
            if not output.startswith(expected_prefix):
                msg = ("The output of dpkg-shlibdeps doesn't match the"
                       " expected format! (expected prefix: %r, output: %r)")
                logger.warning(msg, expected_prefix, output)
                return []
            output = output[len(expected_prefix):]
            dependencies = sorted(dependency.strip() for dependency in output.split(','))
            logger.debug("Dependencies reported by dpkg-shlibdeps: %s", dependencies)
            return dependencies

    def determine_package_architecture(self, has_shared_object_files):
        """
        Determine binary architecture that Debian package should be tagged with.

        If a package contains ``*.so`` files we're dealing with a compiled
        Python module. To determine the applicable architecture, we take the
        Debian architecture reported by
        :attr:`~py2deb.converter.PackageConverter.debian_architecture`.

        :param has_shared_objects: ``True`` if the package contains ``*.so``
                                   files, ``False`` otherwise.
        :returns: The architecture string, 'all' or one of the values of
                  :attr:`~py2deb.converter.PackageConverter.debian_architecture`.
        """
        logger.debug("Checking package architecture ..")
        if has_shared_object_files:
            logger.debug("Package contains shared object files, tagging with %s architecture.",
                         self.converter.debian_architecture)
            return self.converter.debian_architecture
        else:
            logger.debug("Package doesn't contain shared object files, dealing with a portable package.")
            return 'all'

    def load_control_field_overrides(self, control_fields):
        """
        Apply user defined control field overrides.

        Looks for an ``stdeb.cfg`` file inside the Python package's source
        distribution and if found it merges the overrides into the control
        fields that will be embedded in the generated Debian binary package.

        This method first applies any overrides defined in the ``DEFAULT``
        section and then it applies any overrides defined in the section whose
        normalized name (see :func:`~py2deb.utils.package_names_match()`)
        matches that of the Python package.

        :param control_fields: The control field defaults constructed by py2deb
                               (a :class:`debian.deb822.Deb822` object).
        :returns: The merged defaults and overrides (a
                  :class:`debian.deb822.Deb822` object).
        """
        py2deb_cfg = os.path.join(self.requirement.source_directory, 'stdeb.cfg')
        if not os.path.isfile(py2deb_cfg):
            logger.debug("Control field overrides file not found (%s).", py2deb_cfg)
        else:
            logger.debug("Loading control field overrides from %s ..", py2deb_cfg)
            parser = configparser.RawConfigParser()
            parser.read(py2deb_cfg)
            # Prepare to load the overrides from the DEFAULT section and
            # the section whose name matches that of the Python package.
            # DEFAULT is processed first on purpose.
            section_names = ['DEFAULT']
            # Match the normalized package name instead of the raw package
            # name because `python setup.py egg_info' normalizes
            # underscores in package names to dashes which can bite
            # unsuspecting users. For what it's worth, PEP-8 discourages
            # underscores in package names but doesn't forbid them:
            # https://www.python.org/dev/peps/pep-0008/#package-and-module-names
            section_names.extend(section_name for section_name in parser.sections()
                                 if package_names_match(section_name, self.python_name))
            for section_name in section_names:
                if parser.has_section(section_name):
                    overrides = dict(parser.items(section_name))
                    logger.debug("Found %i control file field override(s) in section %s of %s: %r",
                                 len(overrides), section_name, py2deb_cfg, overrides)
                    control_fields = merge_control_fields(control_fields, overrides)
        return control_fields

    def generate_maintainer_script(self, filename, python_executable, function, **arguments):
        """
        Generate a post-installation or pre-removal maintainer script.

        :param filename: The pathname of the maintainer script (a string).
        :param python_executable: The absolute pathname of the Python
                                  interpreter on the target system (a string).
        :param function: The name of the function in the :mod:`py2deb.hooks`
                         module to be called when the maintainer script is run
                         (a string).
        :param arguments: Any keyword arguments to the function in the
                          :mod:`py2deb.hooks` are serialized and embedded
                          inside the generated maintainer script.
        """
        # Read the py2deb/hooks.py script.
        py2deb_directory = os.path.dirname(os.path.abspath(__file__))
        hooks_script = os.path.join(py2deb_directory, 'hooks.py')
        with open(hooks_script) as handle:
            contents = handle.read()
        blocks = contents.split('\n\n')
        # Generate the shebang / hashbang line.
        blocks.insert(0, '#!%s' % python_executable)
        # Generate the call to the top level function.
        encoded_arguments = ', '.join('%s=%r' % (k, v) for k, v in arguments.items())
        blocks.append('%s(%s)' % (function, encoded_arguments))
        # Write the maintainer script.
        with open(filename, 'w') as handle:
            handle.write('\n\n'.join(blocks))
            handle.write('\n')
        # Make sure the maintainer script is executable.
        os.chmod(filename, 0o755)

    def find_egg_info_file(self, pattern=''):
        """
        Find pip metadata files in unpacked source distributions.

        When pip unpacks a source distribution archive it creates a directory
        ``pip-egg-info`` which contains the package metadata in a declarative
        and easy to parse format. This method finds such metadata files.

        :param pattern: The :mod:`glob` pattern to search for (a string).
        :returns: A list of matched filenames (strings).
        """
        full_pattern = os.path.join(self.requirement.source_directory, 'pip-egg-info', '*.egg-info', pattern)
        logger.debug("Looking for %r file(s) using pattern %r ..", pattern, full_pattern)
        matches = glob.glob(full_pattern)
        if len(matches) > 1:
            msg = "Source distribution directory of %s (%s) contains multiple *.egg-info directories: %s"
            raise Exception(msg % (self.requirement.project_name, self.requirement.version, concatenate(matches)))
        elif matches:
            logger.debug("Matched %s: %s.", pluralize(len(matches), "file", "files"), concatenate(matches))
            return matches[0]
        else:
            logger.debug("No matching %r files found.", pattern)

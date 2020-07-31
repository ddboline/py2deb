"""
Downgrade to pip < 20.2 when running on PyPy.

Unfortunately pip 20.2 (the most recent release at the time of writing) breaks
compatibility with PyPy, thereby causing Travis CI builds of py2deb to fail as
well. For details please refer to https://github.com/pypa/pip/issues/8653.
"""

import pip
import platform
import subprocess

from distutils.version import LooseVersion

if platform.python_implementation() == 'PyPy':
    installed_release = LooseVersion(pip.__version__)
    known_bad_release = LooseVersion('20.2')
    if installed_release >= known_bad_release:
        # Given that pip is broken, we can't use it to downgrade itself!
        # Fortunately setuptools provides easy_install which works fine.
        subprocess.check_call(['easy_install', 'pip < 20.2'])
# -*- coding: utf-8 -*-
#
# This file is part of PyExifTool.
#
# PyExifTool <http://github.com/sylikc/pyexiftool>
#
# Copyright 2019-2023 Kevin M (sylikc)
# Copyright 2012-2014 Sven Marnach
#
# Community contributors are listed in the CHANGELOG.md for the PRs
#
# PyExifTool is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the licence, or
# (at your option) any later version, or the BSD licence.
#
# PyExifTool is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See COPYING.GPL or COPYING.BSD for more details.

"""

This submodule defines constants which are used by other modules in the package

"""

import os
import sys


##################################
############# HELPERS ############
##################################

# instead of comparing everywhere sys.platform, do it all here in the constants (less typo chances)
# True if Windows
PLATFORM_WINDOWS: bool = (sys.platform == 'win32')
"""sys.platform check, set to True if Windows"""

# Prior to Python 3.3, the value for any Linux version is always linux2; after, it is linux.
# https://stackoverflow.com/a/13874620/15384838
PLATFORM_LINUX: bool = (sys.platform == 'linux' or sys.platform == 'linux2')
"""sys.platform check, set to True if Linux"""



##################################
####### PLATFORM DEFAULTS ########
##################################


# specify the extension so exiftool doesn't default to running "exiftool.py" on windows (which could happen)
DEFAULT_EXECUTABLE: str = "exiftool.exe" if PLATFORM_WINDOWS else "exiftool"
"""The name of the default executable to run.

``exiftool.exe`` (Windows) or ``exiftool`` (Linux/Mac/non-Windows platforms)

By default, the executable is searched for on one of the paths listed in the
``PATH`` environment variable.  If it's not on the ``PATH``, a full path should be specified in the
``executable`` argument of the ExifTool constructor (:py:meth:`exiftool.ExifTool.__init__`).
"""

"""
# flipped the if/else so that the sphinx documentation shows "exiftool" rather than "exiftool.exe"
if not PLATFORM_WINDOWS:  # pytest-cov:windows: no cover
	DEFAULT_EXECUTABLE = "exiftool"
else:
	DEFAULT_EXECUTABLE = "exiftool.exe"
"""


##################################
####### STARTUP CONSTANTS ########
##################################

# for Windows STARTUPINFO
SW_FORCEMINIMIZE: int = 11
"""Windows ShowWindow constant from win32con

Indicates the launched process window should start minimized
"""

# for Linux preexec_fn
PR_SET_PDEATHSIG: int = 1
"""Extracted from linux/prctl.h

Allows a kill signal to be sent to child processes when the parent unexpectedly dies
"""



##################################
######## GLOBAL DEFAULTS #########
##################################

DEFAULT_BLOCK_SIZE: int = 4096
"""The default block size when reading from exiftool.  The standard value
should be fine, though other values might give better performance in
some cases."""

EXIFTOOL_MINIMUM_VERSION: str = "12.15"
"""this is the minimum *exiftool* version required for current version of PyExifTool

* 8.40 / 8.60 (production): implemented the -stay_open flag
* 12.10 / 12.15 (production): implemented exit status on -echo4
"""


DEFAULT_SERVER_HOST: str = "127.0.0.1"
"""Default host for :py:class:`exiftool.server.ExifToolServer` to listen on"""

DEFAULT_SERVER_PORT: int = 0
"""Default port (0 = random available port) for :py:class:`exiftool.server.ExifToolServer`"""

DEFAULT_SERVER_TIMEOUT: float = 10.0
"""Default timeout in seconds for server startup and client requests"""

DEFAULT_SERVER_IDLE_TIMEOUT: float = 60.0
"""Default idle timeout in seconds before server auto-shuts down"""

DEFAULT_SERVER_PORT_FILE: str = "pyexiftool-server.json"
"""Default basename for the server port discovery file"""

DEFAULT_SERVER_PORT_FILE_DIR: str = os.path.join(
	os.path.expanduser("~"),
	".cache", "pyexiftool",
)
"""Default directory for server port discovery and lock files.

Uses ``~/.cache/pyexiftool`` (``$HOME/.cache/pyexiftool``) so that
all processes for the same user share the same port file regardless
of :py:func:`tempfile.gettempdir()` differences.

On Windows this produces ``C:\\Users\\<user>\\.cache\\pyexiftool``
which is functional but unconventional — users may override via the
*port_file* parameter or the ``PYEXIFTOOL_PORT_FILE`` env var.
"""

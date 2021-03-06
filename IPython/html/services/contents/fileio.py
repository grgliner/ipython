"""
Utilities for file-based Contents/Checkpoints managers.
"""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.

import base64
from contextlib import contextmanager
import errno
import io
import os
import shutil
import tempfile

from tornado.web import HTTPError

from IPython.html.utils import (
    to_api_path,
    to_os_path,
)
from IPython import nbformat
from IPython.utils.py3compat import str_to_unicode


def _copy_metadata(src, dst):
    """Copy the set of metadata we want for atomic_writing.
    
    Permission bits and flags. We'd like to copy file ownership as well, but we
    can't do that.
    """
    shutil.copymode(src, dst)
    st = os.stat(src)
    if hasattr(os, 'chflags') and hasattr(st, 'st_flags'):
        os.chflags(dst, st.st_flags)

@contextmanager
def atomic_writing(path, text=True, encoding='utf-8', **kwargs):
    """Context manager to write to a file only if the entire write is successful.
    
    This works by creating a temporary file in the same directory, and renaming
    it over the old file if the context is exited without an error. If other
    file names are hard linked to the target file, this relationship will not be
    preserved.
    
    On Windows, there is a small chink in the atomicity: the target file is
    deleted before renaming the temporary file over it. This appears to be
    unavoidable.
    
    Parameters
    ----------
    path : str
      The target file to write to.
     
    text : bool, optional
      Whether to open the file in text mode (i.e. to write unicode). Default is
      True.
    
    encoding : str, optional
      The encoding to use for files opened in text mode. Default is UTF-8.
     
    **kwargs
      Passed to :func:`io.open`.
    """
    # realpath doesn't work on Windows: http://bugs.python.org/issue9949
    # Luckily, we only need to resolve the file itself being a symlink, not
    # any of its directories, so this will suffice:
    if os.path.islink(path):
        path = os.path.join(os.path.dirname(path), os.readlink(path))

    dirname, basename = os.path.split(path)
    tmp_dir = tempfile.mkdtemp(prefix=basename, dir=dirname)
    tmp_path = os.path.join(tmp_dir, basename)
    if text:
        fileobj = io.open(tmp_path, 'w', encoding=encoding, **kwargs)
    else:
        fileobj = io.open(tmp_path, 'wb', **kwargs)

    try:
        yield fileobj
    except:
        fileobj.close()
        shutil.rmtree(tmp_dir)
        raise

    # Flush to disk
    fileobj.flush()
    os.fsync(fileobj.fileno())

    # Written successfully, now rename it
    fileobj.close()

    # Copy permission bits, access time, etc.
    try:
        _copy_metadata(path, tmp_path)
    except OSError:
        # e.g. the file didn't already exist. Ignore any failure to copy metadata
        pass

    if os.name == 'nt' and os.path.exists(path):
        # Rename over existing file doesn't work on Windows
        os.remove(path)

    os.rename(tmp_path, path)
    shutil.rmtree(tmp_dir)


class FileManagerMixin(object):
    """
    Mixin for ContentsAPI classes that interact with the filesystem.

    Provides facilities for reading, writing, and copying both notebooks and
    generic files.

    Shared by FileContentsManager and FileCheckpoints.

    Note
    ----
    Classes using this mixin must provide the following attributes:

    root_dir : unicode
        A directory against against which API-style paths are to be resolved.

    log : logging.Logger
    """

    @contextmanager
    def open(self, os_path, *args, **kwargs):
        """wrapper around io.open that turns permission errors into 403"""
        with self.perm_to_403(os_path):
            with io.open(os_path, *args, **kwargs) as f:
                yield f

    @contextmanager
    def atomic_writing(self, os_path, *args, **kwargs):
        """wrapper around atomic_writing that turns permission errors to 403"""
        with self.perm_to_403(os_path):
            with atomic_writing(os_path, *args, **kwargs) as f:
                yield f

    @contextmanager
    def perm_to_403(self, os_path=''):
        """context manager for turning permission errors into 403."""
        try:
            yield
        except (OSError, IOError) as e:
            if e.errno in {errno.EPERM, errno.EACCES}:
                # make 403 error message without root prefix
                # this may not work perfectly on unicode paths on Python 2,
                # but nobody should be doing that anyway.
                if not os_path:
                    os_path = str_to_unicode(e.filename or 'unknown file')
                path = to_api_path(os_path, root=self.root_dir)
                raise HTTPError(403, u'Permission denied: %s' % path)
            else:
                raise

    def _copy(self, src, dest):
        """copy src to dest

        like shutil.copy2, but log errors in copystat
        """
        shutil.copyfile(src, dest)
        try:
            shutil.copystat(src, dest)
        except OSError:
            self.log.debug("copystat on %s failed", dest, exc_info=True)

    def _get_os_path(self, path):
        """Given an API path, return its file system path.

        Parameters
        ----------
        path : string
            The relative API path to the named file.

        Returns
        -------
        path : string
            Native, absolute OS path to for a file.

        Raises
        ------
        404: if path is outside root
        """
        root = os.path.abspath(self.root_dir)
        os_path = to_os_path(path, root)
        if not (os.path.abspath(os_path) + os.path.sep).startswith(root):
            raise HTTPError(404, "%s is outside root contents directory" % path)
        return os_path

    def _read_notebook(self, os_path, as_version=4):
        """Read a notebook from an os path."""
        with self.open(os_path, 'r', encoding='utf-8') as f:
            try:
                return nbformat.read(f, as_version=as_version)
            except Exception as e:
                raise HTTPError(
                    400,
                    u"Unreadable Notebook: %s %r" % (os_path, e),
                )

    def _save_notebook(self, os_path, nb):
        """Save a notebook to an os_path."""
        with self.atomic_writing(os_path, encoding='utf-8') as f:
            nbformat.write(nb, f, version=nbformat.NO_CONVERT)

    def _read_file(self, os_path, format):
        """Read a non-notebook file.

        os_path: The path to be read.
        format:
          If 'text', the contents will be decoded as UTF-8.
          If 'base64', the raw bytes contents will be encoded as base64.
          If not specified, try to decode as UTF-8, and fall back to base64
        """
        if not os.path.isfile(os_path):
            raise HTTPError(400, "Cannot read non-file %s" % os_path)

        with self.open(os_path, 'rb') as f:
            bcontent = f.read()

        if format is None or format == 'text':
            # Try to interpret as unicode if format is unknown or if unicode
            # was explicitly requested.
            try:
                return bcontent.decode('utf8'), 'text'
            except UnicodeError:
                if format == 'text':
                    raise HTTPError(
                        400,
                        "%s is not UTF-8 encoded" % os_path,
                        reason='bad format',
                    )
        return base64.encodestring(bcontent).decode('ascii'), 'base64'

    def _save_file(self, os_path, content, format):
        """Save content of a generic file."""
        if format not in {'text', 'base64'}:
            raise HTTPError(
                400,
                "Must specify format of file contents as 'text' or 'base64'",
            )
        try:
            if format == 'text':
                bcontent = content.encode('utf8')
            else:
                b64_bytes = content.encode('ascii')
                bcontent = base64.decodestring(b64_bytes)
        except Exception as e:
            raise HTTPError(
                400, u'Encoding error saving %s: %s' % (os_path, e)
            )

        with self.atomic_writing(os_path, text=False) as f:
            f.write(bcontent)

import os
import re
import sys
from subprocess import Popen, PIPE
from collections import namedtuple
# When dropping Python 2.7 support we should consider switching to
# `shutil.which`.
from distutils.spawn import find_executable

from jedi.cache import memoize_method
from jedi.evaluate.compiled.subprocess import get_subprocess, \
    EvaluatorSameProcess, EvaluatorSubprocess

import parso

_VersionInfo = namedtuple('VersionInfo', 'major minor micro')

_SUPPORTED_PYTHONS = ['3.6', '3.5', '3.4', '3.3', '2.7']


class InvalidPythonEnvironment(Exception):
    pass


class _BaseEnvironment(object):
    @memoize_method
    def get_grammar(self):
        version_string = '%s.%s' % (self.version_info.major, self.version_info.minor)
        return parso.load_grammar(version=version_string)


class Environment(_BaseEnvironment):
    def __init__(self, path, executable):
        self._base_path = os.path.abspath(path)
        self._executable = os.path.abspath(executable)
        self.version_info = self._get_version()

    def _get_version(self):
        try:
            process = Popen([self._executable, '--version'], stdout=PIPE, stderr=PIPE)
            stdout, stderr = process.communicate()
            retcode = process.poll()
            if retcode:
                raise InvalidPythonEnvironment()
        except OSError:
            raise InvalidPythonEnvironment()

        # Until Python 3.4 wthe version string is part of stderr, after that
        # stdout.
        output = stdout + stderr
        match = re.match(br'Python (\d+)\.(\d+)\.(\d+)', output)
        if match is None:
            raise InvalidPythonEnvironment("--version not working")

        return _VersionInfo(*[int(m) for m in match.groups()])

    def __repr__(self):
        version = '.'.join(str(i) for i in self.version_info)
        return '<%s: %s in %s>' % (self.__class__.__name__, version, self._base_path)

    def get_evaluator_subprocess(self, evaluator):
        return EvaluatorSubprocess(evaluator, self._get_subprocess())

    def _get_subprocess(self):
        return get_subprocess(self._executable)

    @memoize_method
    def get_sys_path(self):
        # It's pretty much impossible to generate the sys path without actually
        # executing Python. The sys path (when starting with -S) itself depends
        # on how the Python version was compiled (ENV variables).
        # If you omit -S when starting Python (normal case), additionally
        # site.py gets executed.
        return self._get_subprocess().get_sys_path()


class DefaultEnvironment(Environment):
    def __init__(self):
        super(DefaultEnvironment, self).__init__(sys.prefix, sys.executable)

    def _get_version(self):
        return _VersionInfo(*sys.version_info[:3])


class InterpreterEnvironment(_BaseEnvironment):
    def __init__(self):
        self.version_info = _VersionInfo(*sys.version_info[:3])

    def get_evaluator_subprocess(self, evaluator):
        return EvaluatorSameProcess(evaluator)

    def get_sys_path(self):
        return sys.path


def _get_virtual_env_from_var():
    var = os.environ.get('VIRTUAL_ENV')
    if var is not None:
        if var == sys.prefix:
            return DefaultEnvironment()

        try:
            return create_environment(var)
        except InvalidPythonEnvironment:
            pass


def get_default_environment():
    """
    Tries to return an active Virtualenv. If there is no VIRTUAL_ENV variable
    set it will return the latest Python version installed on the system. This
    makes it possible to use as many new Python features as possible when using
    autocompletion and other functionality.
    """
    virtual_env = _get_virtual_env_from_var()
    if virtual_env is not None:
        return virtual_env

    for environment in find_python_environments():
        return environment


def find_virtualenvs(paths=None, **kwargs):
    """
    :param paths: A list of paths in your file system to be scanned for
        Virtualenvs. It will search in these paths and potentially execute the
        Python binaries. Also the VIRTUAL_ENV variable will be checked if it
        contains a valid Virtualenv.
    :param safe: Default True. In case this is False, it will allow this
        function to execute potential `python` environments. An attacker might
        be able to drop an executable in a path this function is searching by
        default. If the executable has not been installed by root, it will not
        be executed.
    """
    def py27_comp(paths=None, safe=True):
        if paths is None:
            paths = []

        _used_paths = set()

        # Using this variable should be safe, because attackers might be able
        # to drop files (via git) but not environment variables.
        virtual_env = _get_virtual_env_from_var()
        if virtual_env is not None:
            yield virtual_env
            _used_paths.add(virtual_env._base_path)

        for directory in paths:
            if not os.path.isdir(directory):
                continue

            directory = os.path.abspath(directory)
            for path in os.listdir(directory):
                path = os.path.join(directory, path)
                if path in _used_paths:
                    # A path shouldn't be evaluated twice.
                    continue
                _used_paths.add(path)

                try:
                    executable = _get_executable_path(path, safe=safe)
                    yield Environment(path, executable)
                except InvalidPythonEnvironment:
                    pass

    return py27_comp(paths, **kwargs)


def find_python_environments():
    """
    Ignores virtualenvs and returns the different Python version environments.

    The environments are sorted from latest to oldest Python version.
    """
    current_version = '%s.%s' % (sys.version_info.major, sys.version_info.minor)
    for version_string in _SUPPORTED_PYTHONS:
        if version_string == current_version:
            yield DefaultEnvironment()
        else:
            try:
                yield get_python_environment('python' + version_string)
            except InvalidPythonEnvironment:
                pass


def get_python_environment(python_name):
    exe = find_executable(python_name)
    if exe is None:
        raise InvalidPythonEnvironment("This executable doesn't exist.")
    path = os.path.dirname(os.path.dirname(exe))
    return Environment(path, exe)


def create_environment(path):
    """
    Make it possible to create an environment by hand.
    """
    # Since this path is provided by the user, just use unsafe execution.
    return Environment(path, _get_executable_path(path, safe=False))


def from_executable(executable):
    path = os.path.dirname(os.path.dirname(executable))
    return Environment(path, executable)


def _get_executable_path(path, safe=True):
    """
    Returns None if it's not actually a virtual env.
    """

    if os.name == 'nt':
        bin_name = 'Scripts'
        extension = '.exe'
    else:
        bin_name = 'bin'
        extension = ''
    bin_folder = os.path.join(path, bin_name)
    activate = os.path.join(bin_folder, 'activate')
    python = os.path.join(bin_folder, 'python' + extension)
    if not all(os.path.exists(p) for p in (activate, python)):
        raise InvalidPythonEnvironment("One of bin/activate and bin/python is missing.")

    if safe and not _is_safe(python):
        raise InvalidPythonEnvironment("The python binary is potentially unsafe.")
    return python


def _is_safe(executable_path):
    real_path = os.path.realpath(executable_path)
    if _is_admin():
        # In case we are root or are part of Windows, just be conservative and
        # only execute known paths.
        # TODO add a proper Windows path.
        return real_path.startswith('/usr/bin')

    uid = os.stat(real_path).st_uid
    # The interpreter needs to be owned by root. This means that it wasn't
    # written by a user and therefore attacking Jedi is not as simple.
    # The attack could look like the following:
    # 1. A user clones a repository.
    # 2. The repository has an inocent looking folder called foobar. jedi
    #    searches for the folder and executes foobar/bin/python --version if
    #    there's also a foobar/bin/activate.
    # 3. The bin/python is obviously not a python script but a bash script or
    #    whatever the attacker wants.
    return uid == 0


def _is_admin():
    try:
        return os.getuid() == 0
    except AttributeError:
        return False  # Windows

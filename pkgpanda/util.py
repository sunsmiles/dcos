import hashlib
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
from contextlib import contextmanager, ExitStack
from itertools import chain
from multiprocessing import Process
from shutil import rmtree, which
from subprocess import check_call

import requests
import teamcity
import yaml
from teamcity.messages import TeamcityServiceMessages

from pkgpanda.exceptions import FetchError, ValidationError


json_prettyprint_args = {
    "sort_keys": True,
    "indent": 2,
    "separators": (',', ':')
}


def variant_str(variant):
    """Return a string representation of variant."""
    if variant is None:
        return ''
    return variant


def variant_name(variant):
    """Return a human-readable string representation of variant."""
    if variant is None:
        return '<default>'
    return variant


def variant_prefix(variant):
    """Return a filename prefix for variant."""
    if variant is None:
        return ''
    return variant + '.'


def download(out_filename, url, work_dir):
    assert os.path.isabs(out_filename)
    assert os.path.isabs(work_dir)
    work_dir = work_dir.rstrip('/')

    # Strip off whitespace to make it so scheme matching doesn't fail because
    # of simple user whitespace.
    url = url.strip()

    # Handle file:// urls specially since requests doesn't know about them.
    try:
        if url.startswith('file://'):
            src_filename = url[len('file://'):]
            if not os.path.isabs(src_filename):
                src_filename = work_dir + '/' + src_filename
            shutil.copyfile(src_filename, out_filename)
        else:
            # Download the file.
            with open(out_filename, "w+b") as f:
                r = requests.get(url, stream=True)
                if r.status_code == 301:
                    raise Exception("got a 301")
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=4096):
                    f.write(chunk)
    except Exception as fetch_exception:
        rm_passed = False

        # try / except so if remove fails we don't get an exception during an exception.
        # Sets rm_passed to true so if this fails we can include a special error message in the
        # FetchError
        try:
            os.remove(out_filename)
            rm_passed = True
        except Exception:
            pass

        raise FetchError(url, out_filename, fetch_exception, rm_passed) from fetch_exception


def download_atomic(out_filename, url, work_dir):
    assert os.path.isabs(out_filename)
    tmp_filename = out_filename + '.tmp'
    try:
        download(tmp_filename, url, work_dir)
        os.rename(tmp_filename, out_filename)
    except FetchError:
        try:
            os.remove(tmp_filename)
        except:
            pass
        raise


def extract_tarball(path, target):
    """Extract the tarball into target.

    If there are any errors, delete the folder being extracted to.
    """
    # TODO(cmaloney): Validate extraction will pass before unpacking as much as possible.
    # TODO(cmaloney): Unpack into a temporary directory then move into place to
    # prevent partial extraction from ever laying around on the filesystem.
    try:
        assert os.path.exists(path), "Path doesn't exist but should: {}".format(path)
        check_call(['mkdir', '-p', target])
        check_call(['tar', '-xf', path, '-C', target])
    except:
        # If there are errors, we can't really cope since we are already in an error state.
        rmtree(target, ignore_errors=True)
        raise


def load_json(filename):
    try:
        with open(filename) as f:
            return json.load(f)
    except ValueError as ex:
        raise ValueError("Invalid JSON in {0}: {1}".format(filename, ex)) from ex


class YamlParseError(Exception):
    pass


def load_yaml(filename):
    try:
        with open(filename) as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as ex:
        raise YamlParseError("Invalid YAML in {}: {}".format(filename, ex)) from ex


def make_file(name):
    with open(name, 'a'):
        pass


def write_json(filename, data):
    with open(filename, "w+") as f:
        return json.dump(data, f, **json_prettyprint_args)


def write_string(filename, data):
    with open(filename, "w+") as f:
        return f.write(data)


def load_string(filename):
    with open(filename) as f:
        return f.read().strip()


def json_prettyprint(data):
    return json.dumps(data, **json_prettyprint_args)


def if_exists(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except FileNotFoundError:
        return None


def sha1(filename):
    hasher = hashlib.sha1()

    with open(filename, 'rb') as fh:
        while 1:
            buf = fh.read(4096)
            if not buf:
                break
            hasher.update(buf)

    return hasher.hexdigest()


def expect_folder(path, files):
    path_contents = os.listdir(path)
    assert set(path_contents) == set(files)


def expect_fs(folder, contents):
    if isinstance(contents, list):
        expect_folder(folder, contents)
    elif isinstance(contents, dict):
        expect_folder(folder, contents.keys())

        for path in iter(contents):
            if contents[path] is not None:
                expect_fs(os.path.join(folder, path), contents[path])
    else:
        raise ValueError("Invalid type {0} passed to expect_fs".format(type(contents)))


def make_tar(result_filename, change_folder):
    tar_cmd = ["tar", "--numeric-owner", "--owner=0", "--group=0"]
    if which("pxz"):
        tar_cmd += ["--use-compress-program=pxz", "-cf"]
    else:
        tar_cmd += ["-cJf"]
    tar_cmd += [result_filename, "-C", change_folder, "."]
    check_call(tar_cmd)


def rewrite_symlinks(root, old_prefix, new_prefix):
    # Find the symlinks and rewrite them from old_prefix to new_prefix
    # All symlinks not beginning with old_prefix are ignored because
    # packages may contain arbitrary symlinks.
    for root_dir, dirs, files in os.walk(root):
        for name in chain(files, dirs):
            full_path = os.path.join(root_dir, name)
            if os.path.islink(full_path):
                # Rewrite old_prefix to new_prefix if present.
                target = os.readlink(full_path)
                if target.startswith(old_prefix):
                    new_target = os.path.join(new_prefix, target[len(old_prefix) + 1:].lstrip('/'))
                    # Remove the old link and write a new one.
                    os.remove(full_path)
                    os.symlink(new_target, full_path)


def check_forbidden_services(path, services):
    """Check if package contains systemd services that may break DC/OS

    This functions checks the contents of systemd's unit file dirs and
    throws the exception if there are reserved services inside.

    Args:
        path: path where the package contents are
        services: list of reserved services to look for

    Raises:
        ValidationError: Reserved serice names were found inside the package
    """
    services_dir_regexp = re.compile(r'dcos.target.wants(?:_.+)?')
    forbidden_srv_set = set(services)
    pkg_srv_set = set()

    for direntry in os.listdir(path):
        if not services_dir_regexp.match(direntry):
            continue
        pkg_srv_set.update(set(os.listdir(os.path.join(path, direntry))))

    found_units = forbidden_srv_set.intersection(pkg_srv_set)

    if found_units:
        msg = "Reverved unit names found: " + ','.join(found_units)
        raise ValidationError(msg)


def run(cmd, *args, **kwargs):
    proc = subprocess.Popen(cmd, *args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    stdout, stderr = proc.communicate()
    print("STDOUT: ", stdout.decode('utf-8'))
    print("STDERR: ", stderr.decode('utf-8'))

    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)

    assert len(stderr) == 0
    return stdout.decode('utf-8')


def launch_server(directory):
    os.chdir("resources/repo")
    httpd = socketserver.TCPServer(
        ("", 8000),
        http.server.SimpleHTTPRequestHandler)
    httpd.serve_forever()


class TestRepo:

    def __init__(self, repo_dir):
        self.__dir = repo_dir

    def __enter__(self):
        self.__server = Process(target=launch_server, args=(self.__dir))
        self.__server.start()

    def __exit__(self, exc_type, exc_value, traceback):
        self.__server.join()


def resources_test_dir(path):
    assert not path.startswith('/')
    return "pkgpanda/test_resources/{}".format(path)


class MessageLogger:
    """Abstraction over TeamCity Build Messages

    When pkgpanda is ran in a TeamCity environment additional meta-messages will be output to stdout
    such that TeamCity can provide improved status reporting, log line highlighting, and failure
    reporting. When pkgpanda is ran in an environment other than TeamCity all meta-messages will
    silently be omitted.

    TeamCity docs: https://confluence.jetbrains.com/display/TCD10/Build+Script+Interaction+with+TeamCity
    """
    def __init__(self):
        self.loggers = []
        if teamcity.is_running_under_teamcity():
            self.loggers.append(TeamcityServiceMessages())
        else:
            self.loggers.append(PrintLogger())

    def _custom_message(self, text, status, error_details='', flow_id=None):
        for log in self.loggers:
            log.customMessage(text, status, errorDetails=error_details, flowId=flow_id)

    @contextmanager
    def _block(self, log, name, flow_id):
        log.blockOpened(name, flowId=flow_id)
        log.progressMessage(name)
        yield
        log.blockClosed(name, flowId=flow_id)

    @contextmanager
    def scope(self, name, flow_id=None):
        """
        Creates a new scope for TeamCity messages. This method is intended to be called in a ``with`` statement

        :param name: The name of the scope
        :param flow_id: Optional flow id that can be used if ``name`` can be non-unique
        """
        with ExitStack() as stack:
            for log in self.loggers:
                stack.enter_context(self._block(log, name, flow_id))
            yield

    def normal(self, text, flow_id=None):
        self._custom_message(text=text, status='NORMAL', flow_id=flow_id)

    def warning(self, text, flow_id=None):
        self._custom_message(text=text, status='WARNING', flow_id=flow_id)

    def error(self, text, flow_id=None, error_details=''):
        self._custom_message(text=text, status='ERROR', flow_id=flow_id, error_details=error_details)

    def failure(self, text, flow_id=None):
        self._custom_message(text=text, status='FAILURE', flow_id=flow_id)


class PrintLogger:
    def customMessage(self, text, status, errorDetails='', flowId=None):  # noqa: N802, N803
        print("{}: {} {}".format(status, text, errorDetails))

    def progressMessage(self, message):  # noqa: N802, N803
        pass

    def blockOpened(self, name, flowId=None):  # noqa: N802, N803
        print("starting: {}".format(name))

    def blockClosed(self, name, flowId=None):  # noqa: N802, N803
        print("completed: {}".format(name))


logger = MessageLogger()

import os
import tarfile
import zipfile
import re
import urllib
import urllib2
import platform
import subprocess
import json
import shutil
import cStringIO
from glob import glob
from default_platform import default_platform

# Master table of dependency types.

# A dependency definition can specify 'type' to inherit definitions from one of these.
# String values can depend on other string values from the dependency. For example,
# if 'name' is defined as 'Example' then '${name}.exe' will expand to 'Example.exe'.
# It does not matter which order the values are defined.
# String values can also depend on boolean values. For example, the string
# '${test-value?yes-result:no-result}' will get the value of the string named
# 'yes-result' if 'test-value' is a true boolean value, and the string named
# 'no-result' if 'test-value' is a false boolean value.

# The principle string values that must be defined are 'archive-path' to point to the
# .tar.gz file with the dependency's binaries, 'dest' to specify where to untar it,
# and 'configure-args' to specify the list of arguments to pass to waf.

# In order for source control fetching to work, the string 'source-git' should point
# to the git repo and 'tag' should identify the git tag that corresponds to the
# fetched binaries.

DEPENDENCY_TYPES = {
    # Label a dependency with the 'ignore' type to prevent it being considered at all.
    # Can be useful to include comments. (Json has no comment syntax.)
    'ignore' : {
        'ignore': True     # This causes the entire dependency entry to be ignored. Useful for comments.
        },

    # Openhome dependencies generally have an associated git repo to allow us to
    # fetch source code. They also have a different directory layout to accomodate
    # the large number of versions created by CI builds.
    #
    # An openhome dependency, at minimum, must define:
    #     name
    #     version
    #
    # Commonly overridden:
    #     archive-suffix
    #     platform-specific
    #     configure-args
    'openhome' : {
        'archive-extension': '.tar.gz',
        'archive-prefix': '',
        'archive-suffix': '',
        'binary-repo': 'http://openhome.org/releases/artifacts',
        'archive-directory': '${binary-repo}/${name}/',
        'archive-filename': '${archive-prefix}${name}-${version}-${archive-platform}${archive-suffix}${archive-extension}',
        'remote-archive-path': '${archive-directory}${archive-filename}',
        'use-local-archive': False,
        'archive-path': '${use-local-archive?local-archive-path:remote-archive-path}',
        'source-path': '${linn-git-user}@core.linn.co.uk:/home/git',
        'repo-name': '${name}',
        'source-git': '${source-path}/${repo-name}.git',
        'tag': '${repo-name}_${version}',
        'any-platform': 'AnyPlatform',
        'platform-specific': True,
        'archive-platform': '${platform-specific?platform:any-platform}',
        'dest': 'dependencies/${archive-platform}/',
        'configure-args': [],
        'strip-archive-dirs': 0
        },

    # External dependencies generally don't have a git repo, and even if they do,
    # it won't conform to our conventions.
    #
    # An external dependency, at minimum, must define:
    #     name
    #     archive-filename
    #
    # Commonly overriden:
    #     platform-specific
    #     configure-args
    'external' : {
        'binary-repo': 'http://openhome.org/releases/artifacts',
        'source-git': None,
        'any-platform': 'AnyPlatform',
        'platform-specific': True,
        'archive-platform': '${platform-specific?platform:any-platform}',
        'archive-path': '${binary-repo}/${archive-platform}/${archive-filename}',
        'dest': 'dependencies/${archive-platform}/',
        'configure-args': [],
        'strip-archive-dirs': 0
        },
    }









def default_log(logfile=None):
    return logfile if logfile is not None else open(os.devnull, "w")

def windows_program_exists(program):
    return subprocess.call(["which", "/q", program], shell=False)==0

def other_program_exists(program):
    return subprocess.call(["/bin/sh", "-c", "command -v "+program], shell=False, stdout=open(os.devnull), stderr=open(os.devnull))==0

program_exists = windows_program_exists if platform.platform().startswith("Windows") else other_program_exists



def scp(source, target):
    program = None
    for p in ["scp", "pscp"]:
        if program_exists(p):
            program = p
            break
    if program is None:
        raise "Cannot find scp (or pscp) in the path."
    subprocess.check_call([program, source, target])


def open_file_url(url):
    smb = False
    if url.startswith("smb://"):
        url = url[6:]
        smb = True
    elif url.startswith("file://"):
        url = url[7:]
    path = urllib.url2pathname(url).replace(os.path.sep, "/")
    if path[0]=='/':
        if path[1]=='/':
            # file:////hostname/path/file.ext
            # Bad remote path.
            remote = True
            legacy = True
            final_path = path.replace("/", os.path.sep)
        else:
            # file:///path/file.ext
            # Good local path.
            remote = False
            legacy = False
            if smb:
                raise Exception("Bad smb:// path")
            final_path = path[1:].replace("/", os.path.sep)
    else:
        if path[0].isalpha() and path[1] == ':':
            # file:///x:/foo/bar/baz
            # Good absolute local path.
            remote = False
            legacy = False
            final_path = path.replace('/', os.path.sep)
        else:
            # file://hostname/path/file.ext
            # Good remote path.
            remote = True
            legacy = False
            final_path = "\\\\" + path.replace("/", os.path.sep)
    if smb and (legacy or not remote):
        raise Exception("Bad smb:// path. Use 'smb://hostname/path/to/file.ext'")
    if (smb or remote) and not platform.platform().startswith("Windows"):
        raise Exception("SMB file access not supported on non-Windows platforms.")
    return open(final_path, "rb")

def urlopen(url):
    fileobj = urllib2.urlopen(url)
    try:
        contents = fileobj.read()
        return cStringIO.StringIO(contents)
    finally:
        fileobj.close()

def get_opener_for_path(path):
    if path.startswith("file:") or path.startswith("smb:"):
        return open_file_url
    if re.match("[^\W\d]{2,8}:", path):
        return urlopen
    return lambda fname: open(fname, mode="rb")

def is_trueish(value):
    if hasattr(value, "upper"):
        value = value.upper()
    return value in [1, "1", "YES", "Y", "TRUE", "ON", True]

class EnvironmentExpander(object):
    # template_regex matches 
    template_regex = re.compile(r"""
        (?x)                                # Enable whitespace and comments
        (?P<dollar>\$\$)|                   # Match $$
        (?P<word>\$[a-zA-Z_][a-zA-Z_0-9]*)| # Match $word
        (?P<parens>\$\{[^}]*\})             # Match ${any-thing}
        """)
    def __init__(self, env_dict):
        self.env_dict = env_dict
        self.cache = {}
        self.expandset = set()
    def __getitem__(self, key):
        return self.expand(key)
    def getraw(self, key):
        return self.env_dict[key]
    def __contains__(self, key):
        return key in self.env_dict
    def keys(self):
        return self.env_dict.keys()
    def values(self):
        return [self.expand(key) for key in self.keys()]
    def items(self):
        return [(key, self.expand(key)) for key in self.keys()]
    def expand(self, key):
        if key in self.cache:
            return self.cache[key]
        if key in self.expandset:
            raise ValueError("Recursive expansion for key:", key)
        self.expandset.add(key)
        result = self._expand(key)
        self.cache[key] = result
        self.expandset.remove(key)
        return result
    def _expand(self, key):
        if key not in self.env_dict:
            raise KeyError("Key undefined:", key)
        value = self.env_dict[key]
        return self._expandvalue(value)
    def _expandvalue(self, value):
        if isinstance(value, (str, unicode)):
            return self.template_regex.sub(self.replacematch, value)
        elif isinstance(value, (list, tuple)):
            return [self._expandvalue(x) for x in value]
        elif isinstance(value, dict):
            return dict((k, self.expandvalue(v)) for (k,v) in value.items())
        return value
    def replacematch(self, match):
        if match.group('dollar'):
            return '$'
        key = None
        if match.group('word'):
            key = match.group('word')[1:]
        if match.group('parens'):
            key = match.group('parens')[2:-1]
        assert key is not None
        key = key.strip()
        if '?' in key:
            return self.expandconditional(key)
        return self.expand(key)
    def expandconditional(self, key):
        if '?' not in key:
            raise ValueError('conditional must be of form ${condition?result:alternative}')
        condition, rest = key.split('?', 1)
        if ':' not in rest:
            raise ValueError('conditional must be of form ${condition?result:alternative}')
        primary, alternative = rest.split(':', 1)
        condition, primary, alternative = [x.strip() for x in [condition, primary, alternative]]
        try:
            conditionvalue = self.expand(condition)
        except KeyError:
            conditionvalue = False
        if is_trueish(conditionvalue):
            return self.expand(primary)
        return self.expand(alternative)

def openarchive(name, fileobj):
    memoryfile = cStringIO.StringIO(fileobj.read())
    if os.path.splitext(name)[1].upper() in ['.ZIP', '.NUPKG', '.JAR']:
        return zipfile.ZipFile(memoryfile, "r")
    else:
        return tarfile.open(name=name, fileobj=memoryfile, mode="r:*")

def extract_archive(archive, local_path, strip_dirs=0):
    # The general idea is to mutate the in-memory archive, changing the
    # path of files to remove their prefix directories, before invoking
    # extractall. This can solve the problem of archives that include
    # a top-level directory whose name includes a variable value like
    # version-number, which forces us to change assembly references in
    # every project for every minor change.
    if strip_dirs > 0:
        if not isinstance(archive, tarfile.TarFile):
            raise Exception('Cannot strip leading directories from zip archives.')
        for entry in archive:
            entry.name = '/'.join(entry.name.split('/')[strip_dirs:])
    archive.extractall(local_path)


class Dependency(object):
    def __init__(self, name, environment, logfile=None, has_overrides=False):
        self.expander = EnvironmentExpander(environment)
        self.logfile = default_log(logfile)
        self.has_overrides = has_overrides
    def fetch(self):
        remote_path = self.expander.expand('archive-path')
        local_path = os.path.abspath(self.expander.expand('dest'))
        strip_dirs = self.expander.expand('strip-archive-dirs')
        self.logfile.write("Fetching '%s'\n  from '%s'\n" % (self.name, remote_path))
        try:
            opener = get_opener_for_path(remote_path)
            remote_file = opener(remote_path)
            archive = openarchive(name=remote_path, fileobj=remote_file)
        except IOError:
            self.logfile.write("  FAILED\n")
            return False
        try:
            os.makedirs(local_path)
        except OSError:
            # We get an error if the directory exists, which we are happy to
            # ignore. If something worse went wrong, we will find out very
            # soon when we try to extract the files.
            pass
        self.logfile.write("  unpacking to '%s'\n" % (local_path,))
        extract_archive(archive, local_path, strip_dirs)
        archive.close()
        remote_file.close()
        self.logfile.write("  OK\n")
        return True
    @property
    def name(self):
        return self['name']
    def __getitem__(self, key):
        return self.expander.expand(key)
    def __contains__(self, key):
        return key in self.expander
    def items(self):
        return self.expander.items()
    def checkout(self):
        name = self['name']
        sourcegit = self['source-git']
        if sourcegit is None:
            self.logfile.write('No git repo defined for {0}.\n'.format(name))
            return False
        self.logfile.write("Fetching source for '%s'\n  into '%s'\n" % (name, os.path.abspath('../'+name)))
        tag = self['tag']
        try:
            if not os.path.exists('../'+name):
                self.logfile.write('  git clone {0} {1}\n'.format(sourcegit, name))
                subprocess.check_call(['git', 'clone', sourcegit, name], cwd='..', shell=True)
            elif not os.path.isdir('../'+name):
                self.logfile.write('Cannot checkout {0}, because directory ../{0} already exists\n'.format(name))
                return False
            else:
                self.logfile.write('  git fetch origin\n')
                subprocess.check_call(['git', 'fetch', 'origin'], cwd='../'+name, shell=True)
            self.logfile.write("  git checkout {0}\n".format(tag))
            subprocess.check_call(['git', 'checkout', tag], cwd='../'+name, shell=True)
        except subprocess.CalledProcessError as cpe:
            self.logfile.write(str(cpe)+'\n')
            return False
        return True
    def expand_remote_path(self):
        return self.expander.expand('archive-path')
    def expand_local_path(self):
        return self.expander.expand('dest')
    def expand_configure_args(self):
        return self.expander.expand('configure-args')


class DependencyCollection(object):
    def __init__(self, env, logfile=None):
        self.logfile = default_log(logfile)
        self.base_env = env
        self.dependency_types = DEPENDENCY_TYPES
        self.dependencies = {}
    def create_dependency(self, dependency_definition, overrides={}):
        defn = dependency_definition
        env = {}
        env.update(self.base_env)
        if 'type' in defn:
            dep_type = defn['type']
            env.update(self.dependency_types[dep_type])
        env.update(defn)
        env.update(overrides)
        if 'ignore' in env and env['ignore']:
            return
        if 'name' not in env:
            raise ValueError('Dependency definition contains no name')
        name = env['name']
        new_dependency = Dependency(name, env, logfile=self.logfile, has_overrides=len(overrides) > 0)
        self.dependencies[name] = new_dependency
    def __contains__(self, key):
        return key in self.dependencies
    def __getitem__(self, key):
        return self.dependencies[key]
    def items(self):
        return self.dependencies.items()
    def _filter(self, subset=None):
        if subset is None:
            return self.dependencies.values()
        missing_dependencies = [name for name in subset if name not in self.dependencies]
        if len(missing_dependencies) > 0:
            raise Exception("No entries in dependency file named: " + ", ".join(missing_dependencies) + ".")
        return [self.dependencies[name] for name in subset]
    def get_args(self, subset=None):
        dependencies = self._filter(subset)
        configure_args=sum((d.expand_configure_args() for d in dependencies), [])
        return configure_args
    def fetch(self, subset=None):
        dependencies = self._filter(subset)
        failed_dependencies = []
        for d in dependencies:
            if not d.fetch():
                failed_dependencies.append(d.name)
        if failed_dependencies:
            self.logfile.write("Failed to fetch some dependencies: " + ' '.join(failed_dependencies) + '\n')
            return False
        return True
    def checkout(self, subset=None):
        dependencies = self._filter(subset)
        failed_dependencies = []
        for d in dependencies:
            if not d.checkout():
                failed_dependencies.append(d.name)
        if failed_dependencies:
            self.logfile.write("Failed to check out some dependencies: " + ' '.join(failed_dependencies) + '\n')
            return False
        return True

def read_json_dependencies(dependencyfile, overridefile, env, logfile):
    collection = DependencyCollection(env, logfile=logfile)
    dependencies = json.load(dependencyfile)
    overrides = json.load(overridefile)
    overrides_by_name = dict((dep['name'], dep) for dep in overrides)
    for d in dependencies:
        name = d['name']
        override = overrides_by_name.get(name,{})
        collection.create_dependency(d, override)
    return collection

def read_json_dependencies_from_filename(dependencies_filename, overrides_filename, env, logfile):
    dependencyfile = open(dependencies_filename, "r")
    with open(dependencies_filename) as dependencyfile:
        if overrides_filename is not None and os.path.isfile(overrides_filename):
            with open(overrides_filename) as overridesfile:
                return read_json_dependencies(dependencyfile, overridesfile, env, logfile)
        else:
            return read_json_dependencies(dependencyfile, cStringIO.StringIO('[]'), env, logfile)

def cli(args):
    if platform.system() != "Windows":
        args = ["mono", "--runtime=v4.0.30319"] + args
    subprocess.check_call(args, shell=False)

def clean_directories(directories):
    """Remove the specified directories, trying very hard not to remove
    anything if a failure occurs."""

    # Some explanation is in order. Windows locks DLLs while they are in
    # use. You can't just unlink them like in Unix and create a new
    # directory entry in their place - the lock isn't just on the file
    # contents, but on the directory entry (and the parent's directory
    # entry, etc.)
    # The scenario we really want to avoid is to start deleting stuff
    # and then fail half-way through with a random selection of files
    # deleted. It's preferable to fail before any file has actually been
    # deleted, so that a user can, for example, decide that they don't
    # really want to run a fetch after all, rather than leaving them in
    # a state where they're forced to close down the app with the locks
    # (probably Visual Studio) and run another fetch.
    # We achieve this by first doing a bunch of top-level directory
    # renames. These will generally fail if any of the subsequent deletes
    # would have failed. If one fails, we just undo the previous renames
    # and report an error. It's not bulletproof, but it should be good
    # enough for the most common scenarios.

    try:
        directories = list(directories)
        moved = []
        try:
            lastdirectory = None
            for directory in directories:
                if not os.path.isdir(directory):
                    continue
                newname = directory + '.deleteme'
                lastdirectory = directory
                os.rename(directory, newname)
                lastdirectory = None
                moved.append((directory, newname))
        except:
            for original, newname in reversed(moved):
                os.rename(newname, original)
            raise
        for original, newname in moved:
            shutil.rmtree(newname)
    except Exception as e:
        if lastdirectory is not None:
            raise Exception("Failed to remove directory '{0}'. Try closing applications that might be using it. (E.g. Visual Studio.)".format(lastdirectory))
        else:
            raise Exception("Failed to remove directory. Try closing applications that might be using it. (E.g. Visual Studio.)\n"+str(e))

def fetch_dependencies(dependency_names=None, platform=None, env=None, fetch=True, nuget=True, clean=True, source=False, logfile=None, list_details=False, local_overrides=True, verbose=False):
    '''
    Fetch all the dependencies defined in projectdata/dependencies.json and in
    projectdata/packages.config.
    platform:
        Name of target platform. E.g. 'Windows-x86', 'Linux-x64', 'Mac-x64'...
    env:
        Extra variables referenced by the dependencies file.
    fetch:
        True to fetch the listed dependencies, False to skip.
    nuget:
        True to fetch nuget packages listed in packages.config, False to skip.
    clean:
        True to clean out directories before fetching, False to skip.
    source:
        True to fetch source for the listed dependencies, False to skip.
    logfile:
        File-like object for log messages.
    '''
    if env is None:
        env = {}
    if platform is not None:
        env['platform'] = platform
    if 'platform' not in env:
        platform = env['platform'] = default_platform()
    if platform is None:
        raise Exception('Platform not specified and unable to guess.')
    if clean and not list_details:
        clean_dirs = []
        if fetch:
            clean_dirs += [
                'dependencies/AnyPlatform',
                'dependencies/'+platform]
        if nuget:
            clean_dirs += ['dependencies/nuget']
        clean_directories(clean_dirs)


    overrides_filename = '../dependency_overrides.json' if local_overrides else None
    dependencies = read_json_dependencies_from_filename('projectdata/dependencies.json', overrides_filename, env=env, logfile=logfile)
    if list_details:
        for name, dependency in dependencies.items():
            print "Dependency '{0}':".format(name)
            print "    fetches from:     {0!r}".format(dependency['archive-path'])
            print "    unpacks to:       {0!r}".format(dependency['dest'])
            print "    local override:   {0}".format("YES (see '../dependency_overrides.json')" if dependency.has_overrides else 'no')
            if verbose:
                print "    all keys:"
                for key, value in sorted(dependency.items()):
                    print "        {0} = {1!r}".format(key, value)
            print ""
    else:
        if fetch:
            dependencies.fetch(dependency_names)
        if nuget:
            nuget_exe = os.path.normpath(list(glob('dependencies/AnyPlatform/NuGet.[0-9]*/NuGet.exe'))[0])
            cli([nuget_exe, 'install', 'projectdata/packages.config', '-OutputDirectory', 'dependencies/nuget'])
        if source:
            dependencies.checkout(dependency_names)
    return dependencies



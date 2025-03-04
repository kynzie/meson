# Copyright 2013-2021 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from .base import ExternalDependency, DependencyException, sort_libpaths, DependencyTypeName
from ..mesonlib import LibType, MachineChoice, OptionKey, OrderedSet, PerMachine, Popen_safe
from ..programs import find_external_program, ExternalProgram
from .. import mlog
from pathlib import PurePath
import re
import os
import shlex
import typing as T

if T.TYPE_CHECKING:
    from ..environment import Environment

class PkgConfigDependency(ExternalDependency):
    # The class's copy of the pkg-config path. Avoids having to search for it
    # multiple times in the same Meson invocation.
    class_pkgbin: PerMachine[T.Union[None, bool, ExternalProgram]] = PerMachine(None, None)
    # We cache all pkg-config subprocess invocations to avoid redundant calls
    pkgbin_cache: T.Dict[
        T.Tuple[ExternalProgram, T.Tuple[str, ...], T.FrozenSet[T.Tuple[str, str]]],
        T.Tuple[int, str, str]
    ] = {}

    def __init__(self, name: str, environment: 'Environment', kwargs: T.Dict[str, T.Any], language: T.Optional[str] = None) -> None:
        super().__init__(DependencyTypeName('pkgconfig'), environment, kwargs, language=language)
        self.name = name
        self.is_libtool = False
        # Store a copy of the pkg-config path on the object itself so it is
        # stored in the pickled coredata and recovered.
        self.pkgbin: T.Union[None, bool, ExternalProgram] = None

        # Only search for pkg-config for each machine the first time and store
        # the result in the class definition
        if PkgConfigDependency.class_pkgbin[self.for_machine] is False:
            mlog.debug('Pkg-config binary for %s is cached as not found.' % self.for_machine)
        elif PkgConfigDependency.class_pkgbin[self.for_machine] is not None:
            mlog.debug('Pkg-config binary for %s is cached.' % self.for_machine)
        else:
            assert PkgConfigDependency.class_pkgbin[self.for_machine] is None
            mlog.debug('Pkg-config binary for %s is not cached.' % self.for_machine)
            for potential_pkgbin in find_external_program(
                    self.env, self.for_machine, 'pkgconfig', 'Pkg-config',
                    environment.default_pkgconfig, allow_default_for_cross=False):
                version_if_ok = self.check_pkgconfig(potential_pkgbin)
                if not version_if_ok:
                    continue
                if not self.silent:
                    mlog.log('Found pkg-config:', mlog.bold(potential_pkgbin.get_path()),
                             '(%s)' % version_if_ok)
                PkgConfigDependency.class_pkgbin[self.for_machine] = potential_pkgbin
                break
            else:
                if not self.silent:
                    mlog.log('Found Pkg-config:', mlog.red('NO'))
                # Set to False instead of None to signify that we've already
                # searched for it and not found it
                PkgConfigDependency.class_pkgbin[self.for_machine] = False

        self.pkgbin = PkgConfigDependency.class_pkgbin[self.for_machine]
        if self.pkgbin is False:
            self.pkgbin = None
            msg = 'Pkg-config binary for machine %s not found. Giving up.' % self.for_machine
            if self.required:
                raise DependencyException(msg)
            else:
                mlog.debug(msg)
                return

        assert isinstance(self.pkgbin, ExternalProgram)
        mlog.debug('Determining dependency {!r} with pkg-config executable '
                   '{!r}'.format(name, self.pkgbin.get_path()))
        ret, self.version, _ = self._call_pkgbin(['--modversion', name])
        if ret != 0:
            return

        self.is_found = True

        try:
            # Fetch cargs to be used while using this dependency
            self._set_cargs()
            # Fetch the libraries and library paths needed for using this
            self._set_libs()
        except DependencyException as e:
            mlog.debug(f"pkg-config error with '{name}': {e}")
            if self.required:
                raise
            else:
                self.compile_args = []
                self.link_args = []
                self.is_found = False
                self.reason = e

    def __repr__(self) -> str:
        s = '<{0} {1}: {2} {3}>'
        return s.format(self.__class__.__name__, self.name, self.is_found,
                        self.version_reqs)

    def _call_pkgbin_real(self, args: T.List[str], env: T.Dict[str, str]) -> T.Tuple[int, str, str]:
        assert isinstance(self.pkgbin, ExternalProgram)
        cmd = self.pkgbin.get_command() + args
        p, out, err = Popen_safe(cmd, env=env)
        rc, out, err = p.returncode, out.strip(), err.strip()
        call = ' '.join(cmd)
        mlog.debug(f"Called `{call}` -> {rc}\n{out}")
        return rc, out, err

    @staticmethod
    def setup_env(env: T.MutableMapping[str, str], environment: 'Environment', for_machine: MachineChoice,
                  extra_path: T.Optional[str] = None) -> None:
        extra_paths: T.List[str] = environment.coredata.options[OptionKey('pkg_config_path', machine=for_machine)].value[:]
        if extra_path and extra_path not in extra_paths:
            extra_paths.append(extra_path)
        sysroot = environment.properties[for_machine].get_sys_root()
        if sysroot:
            env['PKG_CONFIG_SYSROOT_DIR'] = sysroot
        new_pkg_config_path = ':'.join([p for p in extra_paths])
        env['PKG_CONFIG_PATH'] = new_pkg_config_path

        pkg_config_libdir_prop = environment.properties[for_machine].get_pkg_config_libdir()
        if pkg_config_libdir_prop:
            new_pkg_config_libdir = ':'.join([p for p in pkg_config_libdir_prop])
            env['PKG_CONFIG_LIBDIR'] = new_pkg_config_libdir
        # Dump all PKG_CONFIG environment variables
        for key, value in env.items():
            if key.startswith('PKG_'):
                mlog.debug(f'env[{key}]: {value}')

    def _call_pkgbin(self, args: T.List[str], env: T.Optional[T.Dict[str, str]] = None) -> T.Tuple[int, str, str]:
        # Always copy the environment since we're going to modify it
        # with pkg-config variables
        if env is None:
            env = os.environ.copy()
        else:
            env = env.copy()

        assert isinstance(self.pkgbin, ExternalProgram)
        PkgConfigDependency.setup_env(env, self.env, self.for_machine)

        fenv = frozenset(env.items())
        targs = tuple(args)
        cache = PkgConfigDependency.pkgbin_cache
        if (self.pkgbin, targs, fenv) not in cache:
            cache[(self.pkgbin, targs, fenv)] = self._call_pkgbin_real(args, env)
        return cache[(self.pkgbin, targs, fenv)]

    def _convert_mingw_paths(self, args: T.List[str]) -> T.List[str]:
        '''
        Both MSVC and native Python on Windows cannot handle MinGW-esque /c/foo
        paths so convert them to C:/foo. We cannot resolve other paths starting
        with / like /home/foo so leave them as-is so that the user gets an
        error/warning from the compiler/linker.
        '''
        if not self.env.machines.build.is_windows():
            return args
        converted = []
        for arg in args:
            pargs: T.Tuple[str, ...] = tuple()
            # Library search path
            if arg.startswith('-L/'):
                pargs = PurePath(arg[2:]).parts
                tmpl = '-L{}:/{}'
            elif arg.startswith('-I/'):
                pargs = PurePath(arg[2:]).parts
                tmpl = '-I{}:/{}'
            # Full path to library or .la file
            elif arg.startswith('/'):
                pargs = PurePath(arg).parts
                tmpl = '{}:/{}'
            elif arg.startswith(('-L', '-I')) or (len(arg) > 2 and arg[1] == ':'):
                # clean out improper '\\ ' as comes from some Windows pkg-config files
                arg = arg.replace('\\ ', ' ')
            if len(pargs) > 1 and len(pargs[1]) == 1:
                arg = tmpl.format(pargs[1], '/'.join(pargs[2:]))
            converted.append(arg)
        return converted

    def _split_args(self, cmd: str) -> T.List[str]:
        # pkg-config paths follow Unix conventions, even on Windows; split the
        # output using shlex.split rather than mesonlib.split_args
        return shlex.split(cmd)

    def _set_cargs(self) -> None:
        env = None
        if self.language == 'fortran':
            # gfortran doesn't appear to look in system paths for INCLUDE files,
            # so don't allow pkg-config to suppress -I flags for system paths
            env = os.environ.copy()
            env['PKG_CONFIG_ALLOW_SYSTEM_CFLAGS'] = '1'
        ret, out, err = self._call_pkgbin(['--cflags', self.name], env=env)
        if ret != 0:
            raise DependencyException('Could not generate cargs for %s:\n%s\n' %
                                      (self.name, err))
        self.compile_args = self._convert_mingw_paths(self._split_args(out))

    def _search_libs(self, out: str, out_raw: str) -> T.Tuple[T.List[str], T.List[str]]:
        '''
        @out: PKG_CONFIG_ALLOW_SYSTEM_LIBS=1 pkg-config --libs
        @out_raw: pkg-config --libs

        We always look for the file ourselves instead of depending on the
        compiler to find it with -lfoo or foo.lib (if possible) because:
        1. We want to be able to select static or shared
        2. We need the full path of the library to calculate RPATH values
        3. De-dup of libraries is easier when we have absolute paths

        Libraries that are provided by the toolchain or are not found by
        find_library() will be added with -L -l pairs.
        '''
        # Library paths should be safe to de-dup
        #
        # First, figure out what library paths to use. Originally, we were
        # doing this as part of the loop, but due to differences in the order
        # of -L values between pkg-config and pkgconf, we need to do that as
        # a separate step. See:
        # https://github.com/mesonbuild/meson/issues/3951
        # https://github.com/mesonbuild/meson/issues/4023
        #
        # Separate system and prefix paths, and ensure that prefix paths are
        # always searched first.
        prefix_libpaths: OrderedSet[str] = OrderedSet()
        # We also store this raw_link_args on the object later
        raw_link_args = self._convert_mingw_paths(self._split_args(out_raw))
        for arg in raw_link_args:
            if arg.startswith('-L') and not arg.startswith(('-L-l', '-L-L')):
                path = arg[2:]
                if not os.path.isabs(path):
                    # Resolve the path as a compiler in the build directory would
                    path = os.path.join(self.env.get_build_dir(), path)
                prefix_libpaths.add(path)
        # Library paths are not always ordered in a meaningful way
        #
        # Instead of relying on pkg-config or pkgconf to provide -L flags in a
        # specific order, we reorder library paths ourselves, according to th
        # order specified in PKG_CONFIG_PATH. See:
        # https://github.com/mesonbuild/meson/issues/4271
        #
        # Only prefix_libpaths are reordered here because there should not be
        # too many system_libpaths to cause library version issues.
        pkg_config_path: T.List[str] = self.env.coredata.options[OptionKey('pkg_config_path', machine=self.for_machine)].value
        pkg_config_path = self._convert_mingw_paths(pkg_config_path)
        prefix_libpaths = OrderedSet(sort_libpaths(list(prefix_libpaths), pkg_config_path))
        system_libpaths: OrderedSet[str] = OrderedSet()
        full_args = self._convert_mingw_paths(self._split_args(out))
        for arg in full_args:
            if arg.startswith(('-L-l', '-L-L')):
                # These are D language arguments, not library paths
                continue
            if arg.startswith('-L') and arg[2:] not in prefix_libpaths:
                system_libpaths.add(arg[2:])
        # Use this re-ordered path list for library resolution
        libpaths = list(prefix_libpaths) + list(system_libpaths)
        # Track -lfoo libraries to avoid duplicate work
        libs_found: OrderedSet[str] = OrderedSet()
        # Track not-found libraries to know whether to add library paths
        libs_notfound = []
        libtype = LibType.STATIC if self.static else LibType.PREFER_SHARED
        # Generate link arguments for this library
        link_args = []
        for lib in full_args:
            if lib.startswith(('-L-l', '-L-L')):
                # These are D language arguments, add them as-is
                pass
            elif lib.startswith('-L'):
                # We already handled library paths above
                continue
            elif lib.startswith('-l'):
                # Don't resolve the same -lfoo argument again
                if lib in libs_found:
                    continue
                if self.clib_compiler:
                    args = self.clib_compiler.find_library(lib[2:], self.env,
                                                           libpaths, libtype)
                # If the project only uses a non-clib language such as D, Rust,
                # C#, Python, etc, all we can do is limp along by adding the
                # arguments as-is and then adding the libpaths at the end.
                else:
                    args = None
                if args is not None:
                    libs_found.add(lib)
                    # Replace -l arg with full path to library if available
                    # else, library is either to be ignored, or is provided by
                    # the compiler, can't be resolved, and should be used as-is
                    if args:
                        if not args[0].startswith('-l'):
                            lib = args[0]
                    else:
                        continue
                else:
                    # Library wasn't found, maybe we're looking in the wrong
                    # places or the library will be provided with LDFLAGS or
                    # LIBRARY_PATH from the environment (on macOS), and many
                    # other edge cases that we can't account for.
                    #
                    # Add all -L paths and use it as -lfoo
                    if lib in libs_notfound:
                        continue
                    if self.static:
                        mlog.warning('Static library {!r} not found for dependency {!r}, may '
                                     'not be statically linked'.format(lib[2:], self.name))
                    libs_notfound.append(lib)
            elif lib.endswith(".la"):
                shared_libname = self.extract_libtool_shlib(lib)
                shared_lib = os.path.join(os.path.dirname(lib), shared_libname)
                if not os.path.exists(shared_lib):
                    shared_lib = os.path.join(os.path.dirname(lib), ".libs", shared_libname)

                if not os.path.exists(shared_lib):
                    raise DependencyException('Got a libtools specific "%s" dependencies'
                                              'but we could not compute the actual shared'
                                              'library path' % lib)
                self.is_libtool = True
                lib = shared_lib
                if lib in link_args:
                    continue
            link_args.append(lib)
        # Add all -Lbar args if we have -lfoo args in link_args
        if libs_notfound:
            # Order of -L flags doesn't matter with ld, but it might with other
            # linkers such as MSVC, so prepend them.
            link_args = ['-L' + lp for lp in prefix_libpaths] + link_args
        return link_args, raw_link_args

    def _set_libs(self) -> None:
        env = None
        libcmd = ['--libs']

        if self.static:
            libcmd.append('--static')

        libcmd.append(self.name)

        # Force pkg-config to output -L fields even if they are system
        # paths so we can do manual searching with cc.find_library() later.
        env = os.environ.copy()
        env['PKG_CONFIG_ALLOW_SYSTEM_LIBS'] = '1'
        ret, out, err = self._call_pkgbin(libcmd, env=env)
        if ret != 0:
            raise DependencyException('Could not generate libs for %s:\n%s\n' %
                                      (self.name, err))
        # Also get the 'raw' output without -Lfoo system paths for adding -L
        # args with -lfoo when a library can't be found, and also in
        # gnome.generate_gir + gnome.gtkdoc which need -L -l arguments.
        ret, out_raw, err_raw = self._call_pkgbin(libcmd)
        if ret != 0:
            raise DependencyException('Could not generate libs for %s:\n\n%s' %
                                      (self.name, out_raw))
        self.link_args, self.raw_link_args = self._search_libs(out, out_raw)

    def get_pkgconfig_variable(self, variable_name: str, kwargs: T.Dict[str, T.Union[str, T.List[str]]]) -> str:
        options = ['--variable=' + variable_name, self.name]

        if 'define_variable' in kwargs:
            definition = kwargs.get('define_variable', [])
            if not isinstance(definition, list):
                raise DependencyException('define_variable takes a list')

            if len(definition) != 2 or not all(isinstance(i, str) for i in definition):
                raise DependencyException('define_variable must be made up of 2 strings for VARIABLENAME and VARIABLEVALUE')

            options = ['--define-variable=' + '='.join(definition)] + options

        ret, out, err = self._call_pkgbin(options)
        variable = ''
        if ret != 0:
            if self.required:
                raise DependencyException('dependency %s not found:\n%s\n' %
                                          (self.name, err))
        else:
            variable = out.strip()

            # pkg-config doesn't distinguish between empty and non-existent variables
            # use the variable list to check for variable existence
            if not variable:
                ret, out, _ = self._call_pkgbin(['--print-variables', self.name])
                if not re.search(r'^' + variable_name + r'$', out, re.MULTILINE):
                    if 'default' in kwargs:
                        assert isinstance(kwargs['default'], str)
                        variable = kwargs['default']
                    else:
                        mlog.warning(f"pkgconfig variable '{variable_name}' not defined for dependency {self.name}.")

        mlog.debug(f'Got pkgconfig variable {variable_name} : {variable}')
        return variable

    def check_pkgconfig(self, pkgbin: ExternalProgram) -> T.Optional[str]:
        if not pkgbin.found():
            mlog.log(f'Did not find pkg-config by name {pkgbin.name!r}')
            return None
        try:
            p, out = Popen_safe(pkgbin.get_command() + ['--version'])[0:2]
            if p.returncode != 0:
                mlog.warning('Found pkg-config {!r} but it failed when run'
                             ''.format(' '.join(pkgbin.get_command())))
                return None
        except FileNotFoundError:
            mlog.warning('We thought we found pkg-config {!r} but now it\'s not there. How odd!'
                         ''.format(' '.join(pkgbin.get_command())))
            return None
        except PermissionError:
            msg = 'Found pkg-config {!r} but didn\'t have permissions to run it.'.format(' '.join(pkgbin.get_command()))
            if not self.env.machines.build.is_windows():
                msg += '\n\nOn Unix-like systems this is often caused by scripts that are not executable.'
            mlog.warning(msg)
            return None
        return out.strip()

    def extract_field(self, la_file: str, fieldname: str) -> T.Optional[str]:
        with open(la_file, encoding='utf-8') as f:
            for line in f:
                arr = line.strip().split('=')
                if arr[0] == fieldname:
                    return arr[1][1:-1]
        return None

    def extract_dlname_field(self, la_file: str) -> T.Optional[str]:
        return self.extract_field(la_file, 'dlname')

    def extract_libdir_field(self, la_file: str) -> T.Optional[str]:
        return self.extract_field(la_file, 'libdir')

    def extract_libtool_shlib(self, la_file: str) -> T.Optional[str]:
        '''
        Returns the path to the shared library
        corresponding to this .la file
        '''
        dlname = self.extract_dlname_field(la_file)
        if dlname is None:
            return None

        # Darwin uses absolute paths where possible; since the libtool files never
        # contain absolute paths, use the libdir field
        if self.env.machines[self.for_machine].is_darwin():
            dlbasename = os.path.basename(dlname)
            libdir = self.extract_libdir_field(la_file)
            if libdir is None:
                return dlbasename
            return os.path.join(libdir, dlbasename)
        # From the comments in extract_libtool(), older libtools had
        # a path rather than the raw dlname
        return os.path.basename(dlname)

    def log_tried(self) -> str:
        return self.type_name

    def get_variable(self, *, cmake: T.Optional[str] = None, pkgconfig: T.Optional[str] = None,
                     configtool: T.Optional[str] = None, internal: T.Optional[str] = None,
                     default_value: T.Optional[str] = None,
                     pkgconfig_define: T.Optional[T.List[str]] = None) -> T.Union[str, T.List[str]]:
        if pkgconfig:
            kwargs: T.Dict[str, T.Union[str, T.List[str]]] = {}
            if default_value is not None:
                kwargs['default'] = default_value
            if pkgconfig_define is not None:
                kwargs['define_variable'] = pkgconfig_define
            try:
                return self.get_pkgconfig_variable(pkgconfig, kwargs)
            except DependencyException:
                pass
        if default_value is not None:
            return default_value
        raise DependencyException(f'Could not get pkg-config variable and no default provided for {self!r}')

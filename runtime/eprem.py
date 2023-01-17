import argparse
import collections.abc
import contextlib
import datetime
import json
import os
import pathlib
import shutil
import subprocess
import types
import typing

try:
    import yaml
    _HAVE_YAML = True
except ModuleNotFoundError:
    _HAVE_YAML = False


PathLike = typing.Union[str, os.PathLike]


def fullpath(p: PathLike):
    """Expand and resolve the given path."""
    return pathlib.Path(p).expanduser().resolve()


class LogKeyError(KeyError):
    """The log has no entry for a given key."""


class RunKeyError(Exception):
    """The log entry does not include a given key."""


class ReadTypeError(Exception):
    """There is no support for reading a given file type."""


class RunLog(collections.abc.Mapping):
    """Mapping-based interface to an EPREM project log."""

    def __init__(
        self,
        path: PathLike,
        branches: typing.Iterable[str]=None,
        **common
    ) -> None:
        """Create a new project log.
        
        Parameters
        ----------
        path : path-like
            The path at which to create the log file. May be relative to the
            current directory.

        branches : iterable of strings, optional
            The branch names, if any, in the project.

        common
            Key-value pairs of attributes that are common to all runs.
        """
        self._path = fullpath(path)
        self._branches = tuple(branches or [])
        self.dump(common)

    def __len__(self) -> int:
        """Called for len(self)."""
        return len(self._asdict)

    def __iter__(self) -> typing.Iterator[str]:
        """Called for iter(self)."""
        return iter(self._asdict)

    def __getitem__(self, __k: str):
        """Get metadata for a run."""
        if __k in self._asdict:
            return self._asdict[__k]
        raise LogKeyError(f"Unknown run {__k!r}")

    def create(self, key: str, source=None, filetype: str=None):
        """Create a new entry in this log file."""
        contents = self._asdict.copy()
        if source is None:
            contents[key] = {}
        contents.update({key: self._normalize_source(source, filetype)})
        self.dump(contents)
        return self

    def _normalize_source(self, source, filetype):
        """Convert `source` into an appropriate dictionary."""
        if source is None:
            return {}
        if isinstance(source, typing.Mapping):
            return dict(source)
        if isinstance(source, str):
            return self._read_from_file(source, filetype)
        raise TypeError(f"Unrecognized source type: {type(source)}")

    def load(self, source: str, filetype: str):
        """Create a new entry in this log file from a file."""
        self.dump(self._read_from_file(source, filetype))
        return self

    def _read_from_file(self, source, filetype):
        """Update the current contents from the `source` file."""
        contents = self._asdict.copy()
        loader = self._source_loader(filetype or pathlib.Path(source).suffix)
        with pathlib.Path(source).open('r') as fp:
            if loaded := loader(fp):
                contents.update(loaded)
        return contents

    def _source_loader(self, filetype: str):
        """Get a format-specific file-reader."""
        if filetype.lower().lstrip('.') == 'yaml':
            if _HAVE_YAML:
                return yaml.safe_load
            raise ReadTypeError("No support for reading YAML") from None
        if filetype.lower().lstrip('.') == 'json':
            return json.load
        raise ValueError(
            f"Unknown file type: {filetype!r}"
        ) from None

    def append(self, target: str, key: str, metadata):
        """Append metadata to `target`."""
        contents = self._asdict.copy()
        try:
            record = contents[target]
        except KeyError as err:
            raise LogKeyError(
                f"Cannot append to unknown run {target!r}"
            ) from err
        record[key] = metadata
        self.dump(contents)
        return self

    def mv(self, source: str, target: str):
        """Rename `source` to `target` in this log file."""
        current = self._asdict.copy()
        try:
            run = current[source]
        except KeyError as err:
            raise LogKeyError(
                f"Cannot rename unknown run {source!r}"
            ) from err
        updated = {k: v for k, v in current.items() if k != source}
        if not self._branches:
            directory = pathlib.Path(run['directory']).parent / target
            updated[target] = {
                k: v if k != 'directory' else str(directory)
                for k, v in run.items()
            }
        else:
            updated[target] = {}
            for branch in self._branches:
                old = run[branch]
                directory = pathlib.Path(old['directory']).parent / target
                updated[target][branch] = {
                    k: v if k != 'directory' else str(directory)
                    for k, v in old.items()
                }
        self.dump(updated)
        return self

    def rm(self, *targets: str, branch: str=None):
        """Remove the target run(s) from this log file."""
        current = self._asdict.copy()
        if target := next((t for t in targets if t not in current), None):
            raise LogKeyError(
                f"Cannot remove unknown run {target!r}"
            ) from None
        updated = {k: v for k, v in current.items() if k not in targets}
        if branch not in self._branches:
            raise LogKeyError(
                f"Cannot remove runs from unknown branch {branch!r}"
            ) from None
        if branch:
            for key, info in current.items():
                if key in targets:
                    updated[key] = {
                        k: v for k, v in info.items() if k != branch
                    }
        self.dump(updated)
        return self

    @property
    def _asdict(self) -> typing.Dict[str, typing.Dict[str, typing.Any]]:
        """Internal dictionary representing the current contents."""
        with self.path.open('r') as fp:
            return dict(json.load(fp))

    def dump(self, contents):
        """Write `contents` to this log file."""
        with self.path.open('w') as fp:
            json.dump(contents, fp, indent=4, sort_keys=True)

    @property
    def name(self):
        """The name of this log file.
        
        Same as `RunLog.path.name`.
        """
        return self.path.name

    @property
    def path(self):
        """The path to this log file."""
        return self._path

    def __str__(self) -> str:
        """A simplified representation of this object."""
        return str(self._asdict)

    def __repr__(self) -> str:
        """An unambiguous representation of this object."""
        return f"{self.__class__.__qualname__}({self.path})"


class PathOperationError(Exception):
    """This operation is not allowed on the given path(s)."""


class ProjectExistsError(Exception):
    """A project with this name already exists."""


_P = typing.TypeVar('_P', bound=pathlib.Path)


class _ProjectInit(typing.Mapping):
    """A mapping of `~Project` initialization attributes."""

    _kwargs = {
        'branches': {'type': tuple, 'default': ()},
        'config': {'type': str, 'default': 'eprem.cfg'},
        'output': {'type': str, 'default': 'eprem.log'},
        'rundir': {'type': str, 'default': 'runs'},
        'logstem': {'type': str, 'default': 'runs'},
    }

    def __init__(self, root, **kwargs) -> None:
        """Create a new instance."""
        self._attrs = {
            key: this['type'](kwargs.get(key) or this['default'])
            for key, this in self._kwargs.items()
        }
        self._attrs['root'] = str(root)
        self._path = None
        self._root = None
        self._branches = None
        self._config = None
        self._output = None
        self._rundir = None
        self._logname = None
        self._logstem = None

    @property
    def path(self):
        """A fully qualified path to the project root directory."""
        if self._path is None:
            self._path = fullpath(self.root)
        return self._path

    @property
    def root(self):
        """The project root directory."""
        if self._root is None:
            self._root = str(self._attrs['root'])
        return self._root

    @property
    def branches(self) -> typing.Tuple[str]:
        """The distinct project branches, if any."""
        if self._branches is None:
            self._branches = tuple(self._attrs['branches'])
        return self._branches

    @property
    def config(self):
        """The name of the standard project configuration file."""
        if self._config is None:
            self._config = str(self._attrs['config'])
        return self._config

    @property
    def output(self):
        """The name of the standard project output log."""
        if self._output is None:
            # Ensure a string file name, even if the initialization argument was
            # a path. We don't want to write to an arbitrary location on disk!
            self._output = pathlib.Path(self._attrs['output']).name
        return self._output

    @property
    def rundir(self):
        """The name of the standard project run directory."""
        if self._rundir is None:
            self._rundir = str(self._attrs['rundir'])
        return self._rundir

    @property
    def logname(self):
        """The name of the project-wide log file."""
        if self._logname is None:
            self._logname = pathlib.Path(
                self.logstem
            ).with_suffix('.json').name
        return self._logname

    @property
    def logstem(self):
        """The name (without suffix) of the project-wide log file."""
        if self._logstem is None:
            self._logstem = str(self._attrs['logstem'])
        return self._logstem

    def __len__(self) -> int:
        """Called for len(self)."""
        return len(self._attrs)

    def __iter__(self) -> typing.Iterable[str]:
        """Called for iter(self)."""
        return iter(self._attrs)

    def __getitem__(self, __k: str):
        """Key-based access to attribute values."""
        if __k in self._attrs:
            return self._attrs[__k]
        raise KeyError(f"Unknown attribute {__k!r}")


ProjectType = typing.TypeVar('ProjectType', bound='Project')


class Project:
    """Interface to an EPREM runtime project."""

    @typing.overload
    def __init__(
        self,
        root: typing.Union[str, pathlib.Path],
    ) -> None: ...

    @typing.overload
    def __init__(
        self,
        root: typing.Union[str, pathlib.Path],
        branches: typing.Iterable[str]=None,
        config: str=None,
        output: str=None,
        rundir: str=None,
        logname: str=None,
    ) -> None: ...

    database = pathlib.Path('.eprem-runtime.json')

    def __init__(self, root, **kwargs):
        """Initialize a new project."""
        self._isvalid = False
        attrs = self._init_attrs(root, kwargs)
        self._log = None
        self._name = None
        self._branches = None
        directories = [
            attrs.path / branch / attrs.rundir
            for branch in attrs.branches or ['']
        ]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        self._log = RunLog(
            attrs.path / attrs.logname,
            branches=attrs.branches,
            config=attrs.config,
            output=attrs.output,
        )
        self._attrs = attrs
        self._directories = directories
        self._isvalid = True

    def _init_attrs(self, root: pathlib.Path, kwargs: dict):
        """Initialize arguments from input or the database."""
        path = fullpath(root)
        if path.exists() and kwargs:
            existing = (
                f"{self.__class__.__qualname__}"
                f"({os.path.relpath(path)!r})"
            )
            raise ProjectExistsError(
                f"The project {path.name!r} already exists in {path.parent}. "
                f"You can access the existing project via {existing}"
            )
        key = str(path)
        if path.exists():
            with self.database.open('r') as fp:
                existing = dict(json.load(fp))
            return _ProjectInit(**existing[key])
        path.mkdir(parents=True)
        init = _ProjectInit(root=path, **kwargs)
        with self.database.open('w') as fp:
            json.dump({key: dict(init)}, fp, indent=4, sort_keys=True)
        return init

    def remove(self):
        """Delete this project."""
        shutil.rmtree(self.root)
        with self.database.open('r') as fp:
            current = dict(json.load(fp))
        updated = {
            k: v for k, v in current.items()
            if k != str(self.root)
        }
        with self.database.open('w') as fp:
            json.dump(updated, fp, indent=4, sort_keys=True)
        self._isvalid = False

    def run(
        self: ProjectType,
        config: str,
        name: str=None,
        subset: typing.Union[str, typing.Iterable[str]]=None,
        nprocs: int=None,
        environment: typing.Dict[str, str]=None,
        silent: bool=False,
    ) -> ProjectType:
        """Set up and execute a new EPREM run within this project."""
        directories = (
            {subset} if isinstance(subset, str)
            else set(subset or ())
        )
        self.log.create(name)
        for path in self._make_paths(name, directories):
            branch = path.parent.parent
            mpirun = _locate('mpirun', branch, environment or {})
            eprem = _locate('eprem', branch, environment or {})
            shutil.copy(config, path / self._attrs.config)
            command = (
                "nice -n 10 ionice -c 2 -n 3 "
                f"{mpirun} --mca btl_base_warn_component_unused 0 "
                f"-n {nprocs or 1} {eprem} eprem.cfg"
            )
            output = path / self._attrs.output
            now = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
            with output.open('w') as stdout:
                process = subprocess.Popen(
                    command,
                    shell=True,
                    cwd=path,
                    stdout=stdout,
                    stderr=subprocess.STDOUT,
                )
                if not silent:
                    print(f"\n[{process.pid}]")
                    print(f"started at {now}")
                process.wait()
                if not silent:
                    print(f"created {name} in branch {branch.name!r}")
            if not silent and process.returncode:
                print(f"WARNING: Process exited with {process.returncode}\n")
            logentry = {
                'mpirun': str(mpirun),
                'eprem': str(eprem),
                'directory': str(path),
                'time': datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
            }
            self.log.append(name, branch.name, logentry)

    def mv(
        self: ProjectType,
        source: str,
        target: str,
        subset: typing.Union[str, typing.Iterable[str]]=None,
        silent: bool=False,
    ) -> ProjectType:
        """Rename an existing EPREM run within this project."""
        directories = (
            {subset} if isinstance(subset, str)
            else set(subset or ())
        )
        paths = self._rename_paths(source, target, directories)
        if not paths:
            if not silent:
                print(f"Nothing to rename for {source!r}")
            return
        branches = [path[0].parent.parent.name for path in paths]
        for branch in branches:
            self.log.mv(source, target, branch)
        if not silent:
            print(f"Updated {self.log.path}")
            underline(f"{self.root}")
            for branch in branches:
                print(f"[{source} -> {target}] in {branch}")

    def rm(
        self: ProjectType,
        run: str,
        subset: typing.Union[str, typing.Iterable[str]]=None,
        silent: bool=False,
    ) -> ProjectType:
        """Remove an existing EPREM run from this project."""
        directories = (
            {subset} if isinstance(subset, str)
            else set(subset or ())
        )
        paths = self._remove_paths(run, directories)
        if not paths:
            if not silent:
                print(f"Nothing to remove for {run!r}")
        branches = [path.parent.parent.name for path in paths]
        targets = [path.name for path in paths]
        for branch in branches:
            self.log.rm(*targets, branch)
        if not silent:
            print(f"Updated {self.log.path}")
            underline(f"{self.root}")
            for branch in branches:
                for target in targets:
                    print(f"removed {target} from {branch}")

    def show(self: ProjectType, *runs: str):
        """Display information about this project or the named run(s)."""

    def _make_paths(self, name: str, subset: typing.Iterable[str]):
        """Create the target subdirectory in each branch.

        Returns
        -------
        list of paths
            A list whose members are the full paths to the directory in which to
            create a requested EPREM run.

        Notes
        -----
        * Intended for use by `~eprem.Project`.
        """
        rundirs = self._get_rundirs(subset)
        paths = [rundir / name for rundir in rundirs]
        action = self._make_paths_check(*paths)
        for path in paths:
            action(path)
        return paths

    def _make_paths_check(self, *paths: pathlib.Path):
        """Return a function to create paths only if safe to do so."""
        def action(path: pathlib.Path):
            path.mkdir(parents=True)
        for path in paths:
            if path.exists():
                raise PathOperationError(
                    f"Cannot create {path}: already exists"
                ) from None
        return action

    def _rename_paths(self, src: str, dst: str, subset: typing.Iterable[str]):
        """Rename `src` to `dst` in all subdirectories.

        Returns
        -------
        list of tuples of paths
            A list of tuples whose members are full paths to the
            source and destination directories, respectively, in the standard
            sense of path-renaming operations.

        Notes
        -----
        * Intended for use by `~eprem.Project`.
        """
        rundirs = self._get_rundirs(subset)
        pairs = [(rundir / src, rundir / dst) for rundir in rundirs]
        action = self._rename_paths_check(*pairs)
        for pair in pairs:
            action(*pair)
        return pairs

    def _rename_paths_check(self, *pairs: typing.Tuple[_P, _P]):
        """Return a function to rename paths only if safe to do so."""
        def action(old: _P, new: _P):
            old.rename(new)
        for old, new in pairs:
            if not old.exists():
                raise PathOperationError(
                    f"Cannot rename {old}: does not exist"
                ) from None
            if not old.is_dir():
                raise PathOperationError(
                    f"Cannot rename {old}: not a directory"
                ) from None
            if new.exists():
                raise PathOperationError(
                    f"Renaming {old.name!r} to {new.name!r} would "
                    f"overwrite {new}."
                ) from None
        return action

    def _remove_paths(self, name: str, subset: typing.Iterable[str]):
        """Remove `target` from all subdirectories.

        Returns
        -------
        list of paths
            A list whose members are the full paths to the EPREM runtime
            directory to remove.

        Notes
        -----
        * Intended for use by `~eprem.Project`.
        """
        rundirs = self._get_rundirs(subset)
        paths = [
            path for rundir in rundirs
            for path in rundir.glob(name)
        ]
        action = self._remove_paths_check(*paths)
        for path in paths:
            action(path)
        return paths

    def _remove_paths_check(self, *paths: pathlib.Path):
        """Return a function to remove paths only if safe to do so."""
        def action(path: pathlib.Path):
            shutil.rmtree(path)
        for path in paths:
            if not path.exists():
                raise PathOperationError(
                    f"Cannot remove {path}: does not exist"
                ) from None
            if not path.is_dir():
                raise PathOperationError(
                    f"Cannot remove {path}: not a directory"
                ) from None
        return action

    def _get_rundirs(self, subset: typing.Iterable[str]):
        """Build an appropriate collection of runtime directories."""
        if not subset:
            return self.directories
        return [d for d in self.directories if d.parent.name in subset]

    @property
    def log(self):
        """The log of runs in this project."""
        return self._log

    @property
    def name(self):
        """The name of this project.
        
        Same as `Project.root.name` or `Project.base.name`.
        """
        if self._name is None:
            self._name = self.root.name
        return self._name

    @property
    def branches(self):
        """The names of project branches, if any."""
        if self._branches is None:
            self._branches = self._attrs.branches
        return self._branches

    @property
    def directories(self):
        """The full path to each run directory."""
        return self._directories

    @property
    def base(self):
        """Alias for `Project.root`."""
        return self.root

    @property
    def root(self):
        """The top-level directory of this project."""
        return self._attrs.path

    def __bool__(self) -> bool:
        """True if this is a valid project."""
        return self._isvalid

    def __eq__(self, other) -> bool:
        """True if two projects have the same initializing attributes."""
        if isinstance(other, Project):
            return self._attrs == other._attrs
        return NotImplemented

    def __str__(self) -> str:
        """A simplified representation of this object."""
        return self.name

    def __repr__(self) -> str:
        """An unambiguous representation of this object."""
        return f"{self.__class__.__qualname__}({self.root})"


def _locate(
    name: str,
    path: pathlib.Path,
    environment: typing.Dict[str, str]
) -> pathlib.Path:
    """Compute an appropriate path to the named element.

    Notes
    -----
    * Intended for use by `~eprem.Project`.
    * This function will attempt to create a full path (resolving links as
      necessary) based on `environment` or from `path / name`. If neither exist,
      it will return `name` as-is, thereby allowing calling code to default to
      the searching the system path.
    """
    location = environment.get(name) or path / name
    it = fullpath(os.path.realpath(location))
    return it if it.exists() else pathlib.Path(shutil.which(name))


def underline(text: str):
    """Print underlined text."""
    dashes = '-' * len(text)
    print(f"\n{text}")
    print(dashes)


def spawn():
    """Set up and execute a new EPREM run."""


def rename():
    """Rename an existing EPREM run."""


def remove():
    """Remove an existing EPREM run."""


def doc2help(func: types.FunctionType):
    """Convert a function docstring to CLI help text."""
    doclines = func.__doc__.split('\n')
    summary = doclines[0]
    return summary[0].lower() + summary[1:]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description="Support for operations on EPREM runs.",
    )
    parser.add_argument(
        '-l',
        '--logfile',
        help=(
            "path to the relevant log file"
            "\n(default: runs.json)"
        ),
    )
    parser.add_argument(
        '-v',
        '--verbose',
        help="print runtime messages",
        action='store_true',
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        '--spawn',
        help=doc2help(spawn),
        nargs=2,
        metavar=('CONFIG', 'TARGET'),
    )
    mode_group.add_argument(
        '--rename',
        help=doc2help(rename),
        nargs=2,
        metavar=('SOURCE', 'TARGET'),
    )
    mode_group.add_argument(
        '--remove',
        help=doc2help(remove),
        metavar='TARGET',
    )
    cli = vars(parser.parse_args())

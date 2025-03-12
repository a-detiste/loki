# (C) Copyright 2018- ECMWF.
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.

from operator import attrgetter
from pathlib import Path
import networkx as nx

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, *args, **kwargs):  # pylint: disable=unused-argument
        return iterable

from loki.logging import warning
from loki.tools import as_tuple, find_paths
from loki.jit_build.compiler import _default_compiler
from loki.jit_build.obj import Obj
from loki.jit_build.workqueue import workqueue, wait_and_check


__all__ = ['Lib']


class Lib:
    """
    A library object linked from multiple compiled objects (:class:`Obj`).

    Note, either :param objs: or the arguments :param pattern: and
    :param source_dir: are required to generated the necessary dependencies.

    :param name: Name of the resulting library (without leading ``lib``).
    :param shared: Flag indicating a shared library build.
    :param objs: List of :class:`Obj` objects that define the objects to link.
    :param pattern: A glob pattern that determines the objects to link.
    :param source_dir: A file path to find objects on when resolving glob patterns.
    :param ignore: A (list of) glob patterns definig file to ignore when
                   generating dependencies from a glob pattern.
    """

    def __init__(self, name, shared=True, objs=None, pattern=None, source_dir=None, ignore=None):
        self.name = name
        self.shared = shared

        if objs is not None:
            self.objs = objs

        else:
            # Generate object list by globbing the source_dir according to pattern
            if source_dir is None:
                raise RuntimeError(f'No source directory found for pattern expansion in {self}')

            obj_paths = find_paths(directory=source_dir, pattern=pattern, ignore=ignore)
            self.objs = [Obj(source_path=p) for p in obj_paths]

        if len(self.objs) == 0:
            warning(f'{self}:: Empty dependency list: {self.objs}')

    def __repr__(self):
        return f'Lib<{self.name}>'

    def build(self, builder=None, logger=None, compiler=None, shared=None,
              force=False, include_dirs=None, external_objs=None):
        """
        Build the source objects and create target library.
        """
        compiler = compiler or builder.compiler
        logger = logger or builder.logger
        shared = self.shared if shared is None else shared
        build_dir = builder.build_dir
        workers = builder.workers

        suffix = '.so' if shared else '.a'
        target = (build_dir/(f'lib{self.name}')).with_suffix(suffix)

        # Establish file-modified times
        t_time = target.stat().st_mtime if target.exists() else None
        o_paths = [o.source_path for o in self.objs]
        if any(p is None for p in o_paths):
            o_time = None
        else:
            o_time = max(p.stat().st_mtime for p in o_paths)

        # Skip the build if up-to-date...
        if not force and t_time is not None and o_time is not None \
           and t_time > o_time:
            logger.info(f'{self} up-to-date, skipping...')
            return

        logger.info(f'Building {self} (workers={workers})')

        # Generate the dependncy graph implied by .mod files
        dep_graph = builder.get_dependency_graph(self.objs, depgen=attrgetter('dependencies'))

        # Execute the object build in parallel via a queue of worker processes
        with workqueue(workers=workers, logger=logger) as q:

            # Traverse the dependency tree in reverse topological order
            topo_nodes = list(reversed(list(nx.topological_sort(dep_graph))))
            for obj in tqdm(topo_nodes):
                if obj.source_path and obj.q_task is None:

                    # Wait for dependencies to complete before scheduling item
                    for dep in obj.obj_dependencies:
                        wait_and_check(dep.q_task, logger=logger)

                    # Schedule object compilation on the workqueue
                    obj.build(builder=builder, compiler=compiler, logger=logger,
                              workqueue=q, force=force, include_dirs=include_dirs)

            # Ensure all build tasks have finished
            for obj in dep_graph.nodes:
                if obj.q_task is not None:
                    wait_and_check(obj.q_task, logger=logger)

        # Link the final library
        objs = [Path(o).resolve() for o in external_objs or []]
        objs += [(build_dir/obj.name).with_suffix('.o') for obj in self.objs]
        logger.debug(f'Linking {self} ({len(objs)} objects)')
        compiler.link(target=target, objs=objs, shared=shared)

    def wrap(self, modname, builder, sources=None, libs=None, lib_dirs=None, kind_map=None):
        """
        Wrap the compiled library using ``f90wrap`` and return the loaded module.

        :param sources: List of source files to wrap for Python access.
        """
        items = as_tuple(Obj(source_path=s) for s in as_tuple(sources))
        build_dir = builder.build_dir
        compiler = builder.compiler or _default_compiler

        sourcepaths = [str(i.source_path) for i in items]
        compiler.f90wrap(modname=modname, source=sourcepaths, cwd=str(build_dir), kind_map=kind_map)

        # Execute the second-level wrapper (f2py-f90wrap)
        wrappers = [f'f90wrap_{item.source_path.stem}.f90' for item in items]
        wrappers += ['f90wrap_toplevel.f90']  # Include the generic wrapper
        wrappers = [w for w in wrappers if (build_dir/w).exists()]

        libs = [self.name] + (libs or [])
        lib_dirs = [str(build_dir.absolute())] + (lib_dirs or [])
        compiler.f2py(modname=modname, source=wrappers,
                      libs=libs, lib_dirs=lib_dirs, cwd=str(build_dir))

        return builder.load_module(modname)

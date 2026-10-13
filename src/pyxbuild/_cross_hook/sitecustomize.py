"""Loaded into the PEP 517 build process by ``pyxbuild.crossbuild`` (via PYTHONPATH).

Replaces setuptools' ``build_ext.build_extension`` and ``build_clib.build_libraries``
with versions that compile C/C++ sources for Windows x64 with ``zig cc``
against the headers and import library of the *target* CPython. Everything
else (setup.py logic, Cython code generation, packaging) runs unchanged on
the host. Only active when PYXBUILD_CROSS is set.
"""

import json
import os


def _install() -> None:
    cfg = json.loads(os.environ["PYXBUILD_CROSS"])
    zig = cfg["zig"]
    target = cfg["target"]
    include = cfg["include"]
    libdir = cfg["libdir"]
    pylib = cfg["pylib"]
    ext_suffix = cfg["ext_suffix"]

    import subprocess
    import sysconfig

    host_includes = {sysconfig.get_paths()[k] for k in ("include", "platinclude")}
    case_shims = cfg["case_shims"]
    cxx_suffixes = (".cpp", ".cc", ".cxx", ".c++")

    def portable(args):
        # MSVC-style switches ("/O2", "/EHsc") make no sense for clang; keep
        # absolute POSIX paths (extra objects, static libraries)
        return [a for a in (args or [])
                if not (a.startswith("/") and len(a) < 16 and not os.path.exists(a))]

    def run(cmd):
        print("pyxbuild-cross:", " ".join(cmd), flush=True)
        subprocess.run(cmd, check=True)

    def compile_sources(sources, build_temp, macros, undef, include_dirs, extra):
        objects = []
        for src in sources:
            obj = os.path.join(build_temp, os.path.splitdrive(src)[1].lstrip("/\\") + ".obj")
            os.makedirs(os.path.dirname(obj), exist_ok=True)
            tool = "c++" if src.lower().endswith(cxx_suffixes) else "cc"
            # MS_WIN64: PC/pyconfig.h sets it for MinGW only since 3.12
            cmd = [*zig, tool, "-target", target, "-c", "-O2", "-DNDEBUG", "-DMS_WIN64"]
            for name, value in macros or []:
                cmd.append(f"-D{name}" if value is None else f"-D{name}={value}")
            cmd += [f"-U{name}" for name in undef or []]
            cmd += [f"-I{d}" for d in [*(include_dirs or []), include]]
            cmd += ["-idirafter", case_shims]
            cmd += portable(extra)
            cmd += [src, "-o", obj]
            run(cmd)
            objects.append(obj)
        return objects

    def build_extension(self, ext):
        sources = self.swig_sources(list(ext.sources), ext)
        ext_path = self.get_ext_fullpath(ext.name)
        os.makedirs(os.path.dirname(ext_path) or ".", exist_ok=True)
        # build_ext-level include dirs, minus the host interpreter's headers
        host = {os.path.normcase(os.path.abspath(p)) for p in host_includes}
        includes = [*(ext.include_dirs or []),
                    *[d for d in (self.include_dirs or [])
                      if os.path.normcase(os.path.abspath(d)) not in host]]
        objects = compile_sources(sources, self.build_temp, ext.define_macros,
                                  ext.undef_macros, includes, ext.extra_compile_args)
        is_cxx = any(s.lower().endswith(cxx_suffixes) for s in sources) or ext.language == "c++"
        cmd = [*zig, "c++" if is_cxx else "cc", "-target", target, "-shared", "-o", ext_path]
        cmd += objects + list(ext.extra_objects or [])
        # self.libraries / self.library_dirs carry build_clib output
        cmd += [f"-L{d}" for d in [*(ext.library_dirs or []), *(self.library_dirs or []),
                                   self.build_temp, libdir]]
        cmd += [f"-l{lib}" for lib in [*(ext.libraries or []), *(self.libraries or [])]]
        cmd += [os.path.join(libdir, pylib + ".lib")]
        cmd += portable(ext.extra_link_args)
        cmd.append("-s")
        out_dir = os.path.dirname(ext_path) or "."
        before = set(os.listdir(out_dir))
        run(cmd)
        # the linker leaves an import library and a .pdb next to the .pyd
        for junk in set(os.listdir(out_dir)) - before:
            if junk.lower().endswith((".lib", ".pdb")):
                os.remove(os.path.join(out_dir, junk))

    def build_libraries(self, libraries):
        for name, info in libraries:
            objects = compile_sources(info.get("sources", []), self.build_temp,
                                      info.get("macros"), None, info.get("include_dirs"),
                                      info.get("cflags"))
            out = os.path.join(self.build_clib, f"lib{name}.a")
            os.makedirs(self.build_clib, exist_ok=True)
            run([*zig, "ar", "rcs", out, *objects])

    def get_ext_filename(self, fullname):
        ext = getattr(self, "ext_map", {}).get(fullname)
        # abi3 extensions on Windows are plain ".pyd"
        suffix = ".pyd" if ext is not None and getattr(ext, "py_limited_api", False) \
            else ext_suffix
        return os.path.join(*fullname.split(".")) + suffix

    # Patch the command classes when the build imports them, not earlier:
    # importing setuptools from sitecustomize changes how its distutils shim
    # initialises and breaks some setup.py files (zstandard). Projects that
    # subclass distutils' commands directly (PyYAML) need the base classes
    # patched as well.
    patches = {
        "setuptools.command.build_ext": {"build_ext": {"build_extension": build_extension,
                                                       "get_ext_filename": get_ext_filename}},
        "setuptools.command.build_clib": {"build_clib": {"build_libraries": build_libraries}},
        "setuptools._distutils.command.build_ext": {
            "build_ext": {"build_extension": build_extension,
                          "get_ext_filename": get_ext_filename}},
        "setuptools._distutils.command.build_clib": {
            "build_clib": {"build_libraries": build_libraries}},
    }
    # setuptools' _distutils_hack serves "distutils" from setuptools/_distutils
    # under the "distutils.*" names: separate module objects (Cython's build_ext)
    patches["distutils.command.build_ext"] = patches["setuptools._distutils.command.build_ext"]
    patches["distutils.command.build_clib"] = patches["setuptools._distutils.command.build_clib"]

    import importlib.abc
    import importlib.machinery
    import sys

    class _Patcher(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name not in patches:
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is None or spec.loader is None:
                return None
            loader = spec.loader
            exec_module = loader.exec_module

            def patched_exec(module, _exec=exec_module, _name=name):
                _exec(module)
                for cls_name, attrs in patches[_name].items():
                    cls = getattr(module, cls_name)
                    for attr, func in attrs.items():
                        setattr(cls, attr, func)

            loader.exec_module = patched_exec
            return spec

    sys.meta_path.insert(0, _Patcher())


if os.environ.get("PYXBUILD_CROSS"):
    _install()

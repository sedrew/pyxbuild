"""Package data of a local dependency, checked by [tool.pyxbuild] require."""

from importlib.resources import files


def settings() -> str:
    return files(__name__).joinpath("data/settings.yaml").read_text(encoding="utf-8")

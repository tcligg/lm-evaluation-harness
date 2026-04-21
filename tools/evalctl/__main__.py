"""Entry point so `python -m evalctl` and the bazel py_binary both work."""

from tools.evalctl.cli import app


if __name__ == "__main__":
    app()

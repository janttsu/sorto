from __future__ import annotations

import pytest

from sorto.cli import main


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as ei:
        main(["--help"])
    assert ei.value.code == 0


def test_run_requires_root() -> None:
    with pytest.raises(SystemExit) as ei:
        main(["run"])
    assert ei.value.code == 2

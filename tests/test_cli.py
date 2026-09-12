"""cli.py: entry point, --version, dispatch (implemented + unimplemented).

``statusline`` and ``status`` are implemented in Phase 2; the rest still
report their implementing phase.
"""

import pytest

from cc_continuity import __version__, cli


def test_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_exits_2_with_help(capsys):
    assert cli.main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    "command",
    ["hook", "rotate", "checkpoint", "validate", "doctor",
     "install", "uninstall"],
)
def test_unimplemented_subcommand_reports_phase(command, capsys):
    assert cli.main([command]) == 2
    err = capsys.readouterr().err
    assert command in err
    assert "Phase" in err

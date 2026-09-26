import json
import shutil
from pathlib import Path

import pytest

from gu_eco_events import cli

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = "2026-09-26"


def fx(name: str) -> str:
    return str(FIXTURES / name)


class Runner:
    """Runs the real CLI in-process and returns (exit code, stdout JSON lines)."""

    def __init__(self, tmp_path: Path, capsys):
        self.tmp = tmp_path
        self.capsys = capsys
        self.state = tmp_path / "state.json"
        self.out = tmp_path / "public"

    def __call__(self, *argv: str):
        self.capsys.readouterr()
        code = cli.main(list(argv))
        captured = self.capsys.readouterr()
        lines = [json.loads(l) for l in captured.out.splitlines() if l.startswith("{")]
        return code, lines, captured

    def run(self, fixture: str, mode: str = "dry-run", today: str = TODAY):
        return self(
            "run", "--fixtures", fx(fixture), "--state", str(self.state),
            "--out-dir", str(self.out), "--today", today, "--mode", mode,
        )

    def build(self, fixture: str, out_dir: Path | None = None, today: str = TODAY):
        return self(
            "build", "--fixtures", fx(fixture), "--state", str(self.state),
            "--out-dir", str(out_dir or self.out), "--today", today,
        )

    def use_prior(self):
        """Install the previously published feed + state snapshot."""
        shutil.copy(FIXTURES / "prior" / "state.json", self.state)
        self.out.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / "prior" / "eco-events.ics", self.out / "eco-events.ics")


def summary(lines):
    return next(l for l in lines if "planned" in l)


def plans(lines):
    return [l for l in lines if "plan" in l]


@pytest.fixture
def runner(tmp_path, capsys):
    return Runner(tmp_path, capsys)

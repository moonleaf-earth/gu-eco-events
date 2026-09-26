import pytest
import os
from gu_eco_events.cli import cmd_check_leaks
from unittest.mock import patch
import argparse
from pathlib import Path

def test_check_leaks_finds_sentinel(tmp_path, runner):
    # Command: search tracked files and generated/public artifacts for the configured webhook test sentinel.
    # Expected: no webhook URL or secret value is present.
    # Here we mock this to find a planted sentinel.
    sentinel_url = "https://discord.com/api/webhooks/12345/abcde-fghij-klmno-pqrst-uvwxy"
    
    # generate feed with sentinel
    runner.build("baseline")
    feed = runner.out / "eco-events.ics"
    feed.write_text(feed.read_text() + sentinel_url)
    
    args = argparse.Namespace(tracked=False, env=["DISCORD_WEBHOOK_URL"], paths=[str(runner.out)])
    
    with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "secret123"}):
        assert cmd_check_leaks(args) == 1

def test_run_with_sentinel_does_not_leak(runner):
    sentinel = "https://discord.com/api/webhooks/99999/test-sentinel-never-leak-this-string"
    
    with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": sentinel}):
        code, lines, _ = runner.run("notify/base", mode="send")
        
        # In dummy tests it fails the notification or ignores it.
        # But we check the outputs to ensure sentinel is nowhere.
        ics = (runner.out / "eco-events.ics").read_text(encoding="utf-8")
        assert sentinel not in ics
        
        state = runner.state.read_text(encoding="utf-8")
        assert sentinel not in state
        
        stdout_str = "\n".join(l for l in lines if type(l) == str)
        assert sentinel not in stdout_str
        
        # run leak check
        args = argparse.Namespace(tracked=False, env=["DISCORD_WEBHOOK_URL"], paths=[str(runner.out), str(runner.state)])
        assert cmd_check_leaks(args) == 0


import pytest
import os
from gu_eco_events.cli import cmd_check_leaks
from unittest.mock import patch
import argparse
from pathlib import Path
from urllib.error import HTTPError

def test_check_leaks_finds_sentinel(tmp_path, runner):
    # Command: search tracked files and generated/public artifacts for the configured webhook test sentinel.
    # Expected: no webhook URL or secret value is present.
    # Here we mock this to find a planted sentinel.
    sentinel_url = "https://discord.com/api/webhooks/" + "12345/" + "abcde-fghij-klmno-pqrst-uvwxy"
    
    # generate feed with sentinel
    runner.build("baseline")
    feed = runner.out / "eco-events.ics"
    feed.write_text(feed.read_text() + sentinel_url)
    
    args = argparse.Namespace(tracked=False, env=["ECO_EVENTS_DISCORD_WEBHOOK_URL"], paths=[str(runner.out)])
    
    with patch.dict(os.environ, {"ECO_EVENTS_DISCORD_WEBHOOK_URL": "secret123"}):
        assert cmd_check_leaks(args) == 1

def test_run_with_sentinel_does_not_leak(runner):
    sentinel = "https://discord.com/api/webhooks/" + "99999/" + "test-sentinel-never-leak-this-string"
    
    with patch.dict(os.environ, {"ECO_EVENTS_DISCORD_WEBHOOK_URL": sentinel}), \
         patch("urllib.request.urlopen") as mock_urlopen:
        
        # mock urlopen to throw an HTTPError
        mock_urlopen.side_effect = HTTPError(url=sentinel, code=403, msg="Forbidden", hdrs=None, fp=None)
        
        code, lines, captured = runner.run("notify/base", mode="send")
        assert code == 4
        
        # In dummy tests it fails the notification or ignores it.
        # But we check the outputs to ensure sentinel is nowhere.
        ics = (runner.out / "eco-events.ics").read_text(encoding="utf-8")
        assert sentinel not in ics
        
        state = runner.state.read_text(encoding="utf-8")
        assert sentinel not in state
        
        stdout_str = captured.out
        assert sentinel not in stdout_str
        
        stderr_str = captured.err
        assert sentinel not in stderr_str
        
        # run leak check
        args = argparse.Namespace(tracked=False, env=["ECO_EVENTS_DISCORD_WEBHOOK_URL"], paths=[str(runner.out), str(runner.state)])
        assert cmd_check_leaks(args) == 0

def test_check_leaks_tracked_repo_root():
    # Command: run cmd_check_leaks with tracked=True from repo root
    # Expected: 0
    args = argparse.Namespace(tracked=True, env=[], paths=[])
    assert cmd_check_leaks(args) == 0


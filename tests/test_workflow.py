import pytest
import json
from pathlib import Path
import yaml

def test_publish_workflow():
    wf_path = Path(".github/workflows/publish.yml")
    wf = yaml.safe_load(wf_path.read_text())
    
    # Assert schedule and workflow_dispatch
    triggers = wf.get(True, wf.get("on", {}))
    assert "schedule" in triggers
    assert len(triggers["schedule"]) == 1
    assert triggers["schedule"][0]["cron"] == "0 4 * * 1"
    assert "workflow_dispatch" in triggers
    
    # Top-level permissions
    assert wf["permissions"]["contents"] == "read"
    
    # Jobs
    jobs = wf["jobs"]
    assert "build" in jobs
    assert "deploy" in jobs
    
    # Build job
    build = jobs["build"]
    assert build["permissions"]["contents"] == "write"
    step_names = [s.get("name") for s in build["steps"]]
    
    # tests run before pipeline and publication
    test_idx = step_names.index("Run tests")
    pipe_idx = step_names.index("Run pipeline build")
    assert test_idx < pipe_idx
    
    # check secret is not echoed (can't fully verify, but we can check run commands)
    for s in build["steps"]:
        if s.get("run"):
            assert "echo $ECO_EVENTS_DISCORD_WEBHOOK_URL" not in s["run"]
            assert "echo ${{ secrets.ECO_EVENTS_DISCORD_WEBHOOK_URL }}" not in s["run"]
            
    # Deploy job
    deploy = jobs["deploy"]
    assert deploy["needs"] == "build"
    assert deploy["environment"]["name"] == "github-pages"
    assert deploy["permissions"]["pages"] == "write"
    assert deploy["permissions"]["id-token"] == "write"


def test_publish_workflow_failure_summary():
    wf = yaml.safe_load(Path(".github/workflows/publish.yml").read_text())
    build = wf["jobs"]["build"]
    steps = {s.get("name"): s for s in build["steps"]}

    notify_step = steps["Run pipeline notify"]
    assert "NOTIFY_FAILED_CHANNELS" in notify_step["run"]
    assert "failed_channels" in notify_step["run"]
    assert build["outputs"]["notify_failed_channels"] == "${{ steps.notify.outputs.NOTIFY_FAILED_CHANNELS }}"

    fail = next(s for s in wf["jobs"]["deploy"]["steps"] if s.get("name") == "Fail if notify failed")
    assert fail["env"]["FAILED_CHANNELS"] == "${{ needs.build.outputs.notify_failed_channels }}"
    assert "FAILED_CHANNELS" in fail["run"]

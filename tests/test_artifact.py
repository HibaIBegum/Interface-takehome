"""Artifact schema and recorder: record a (scripted) discovery run, check the artifact, round-trip it."""

import json

import pytest
from pydantic import ValidationError

from conftest import MOCK_PASSWORD, MOCK_USER, post_json
from cua.agent.loop import DiscoveryAgent, Limits, Outcome
from cua.artifact.recorder import AppCatalog, RecordingError, normalize_route, record_run, to_template
from cua.artifact.schema import CapabilityArtifact, UrlMatches, route_regex
from cua.artifact.store import ArtifactStore, StoreError
from cua.observability.runlog import RunLog
from cua.policy.gate import PolicyConfig, PolicyGate
from cua.surface.base import Strategy
from test_agent_loop import LOGIN, PARAMS, ScriptedDecider, call, click, extract, fill


def arm_then(mock_server, fault, step):
    """A scripted step that first arms a fault in the mock app, then decides as `step` would."""
    def run(screen):
        post_json(f"{mock_server}/__faults", {"fault": fault})
        return step(screen)
    return run


@pytest.fixture
def recorded_run(surface, mock_server, tmp_path):
    """A successful run that had to dismiss an unexpected system notice on the member page."""
    policy = PolicyConfig.load()
    script = LOGIN + [
        fill(r"label='Member ID'", "{{member_id}}"),
        arm_then(mock_server, "interstitial", click(r"button 'Search'")),
        click(r"button 'OK'"),
        extract("savings_balance", r"cell row 'Savings', column 'Balance'"),
        call("done", summary="Savings balance is $5,230.17"),
    ]
    log = RunLog(tmp_path / "run-1", policy.redactor({"password": MOCK_PASSWORD, "username": MOCK_USER}), echo=False)
    agent = DiscoveryAgent(surface=surface, gate=PolicyGate(policy, base_url=mock_server),
                           decider=ScriptedDecider(script), log=log)
    result = agent.run(goal="look up member {member_id} and read their savings balance",
                       entry_url=f"{mock_server}/login", params=PARAMS, limits=Limits(max_steps=15, timeout_s=60))
    assert result.outcome is Outcome.DONE
    return log.dir


@pytest.fixture
def artifact(recorded_run):
    return record_run(recorded_run, capability_id="lookup-savings-balance", name="Look up savings balance",
                      catalog=AppCatalog.load())


# ---------------------------------------------------------------- recorder

def test_steps_are_templated_and_incidental_recovery_is_dropped(artifact):
    assert [(s.action, s.value_template) for s in artifact.steps] == [
        ("navigate", "/login"),
        ("fill", "{username}"),
        ("fill", "{password}"),
        ("click", None),
        ("fill", "{member_id}"),
        ("click", None),
        ("extract", None),  # the OK click on the system notice is not a step
    ]
    assert [s.id for s in artifact.steps] == ["s01", "s02", "s03", "s04", "s05", "s06", "s07"]
    assert artifact.steps[6].output == "savings_balance"
    assert "dropped 1" in artifact.provenance.note


def test_inputs_outputs_and_description(artifact):
    inputs = {p.name: p for p in artifact.inputs}
    assert inputs["member_id"].pattern == r"\d{6}" and not inputs["member_id"].sensitive
    assert inputs["password"].sensitive and inputs["username"].sensitive
    assert [(o.name, o.type, o.source_step) for o in artifact.outputs] == [("savings_balance", "currency", "s07")]
    assert "Read-only." in artifact.capability.description
    assert "MEMBER_NOT_FOUND" in artifact.capability.description
    assert artifact.capability.status == "draft"
    assert artifact.provenance.models == ["scripted"]


def test_locator_candidates_are_ranked_with_coords_last(artifact):
    for step in artifact.steps:
        if step.target is None:
            continue
        strategies = [r.candidate.strategy for r in step.target.candidates]
        assert 2 <= len(strategies) <= 4, step
        assert strategies[-1] is Strategy.COORDS and Strategy.COORDS not in strategies[:-1]
        assert all(r.why for r in step.target.candidates)
    member_id = artifact.steps[4].target.candidates
    assert [(r.candidate.strategy, r.candidate.value) for r in member_id[:2]] == [
        (Strategy.LABEL, "Member ID"), (Strategy.CSS, '[name="mid"]')]
    balance = artifact.steps[6].target.candidates[0].candidate
    assert (balance.strategy, balance.value, balance.column) == (Strategy.TEXT_NEAR, "Savings", "Balance")


def test_recorded_candidates_resolve_on_the_live_screen(artifact, surface):
    """The run ended on the member page: every structural candidate of the extract step must hit one element."""
    step = artifact.steps[6]
    boxes = set()
    for ranked in step.target.candidates[:-1]:
        resolved = surface.resolve(step.target.to_target().model_copy(update={"candidates": [ranked.candidate]}))
        boxes.add(json.dumps(resolved.locator.bounding_box(), sort_keys=True))
    assert len(boxes) == 1


def test_routes_are_normalized_into_checkpoints(artifact):
    search_click = artifact.steps[5].checkpoint
    assert search_click == UrlMatches(route="/app/member?m=:member_id", frame_path=["main"])
    assert artifact.steps[6].precondition == UrlMatches(route="/app/member?m=:member_id", frame_path=["main"])
    sign_on = artifact.steps[3].checkpoint
    assert UrlMatches(route="/desk", frame_path=[]) in sign_on.conditions
    assert artifact.target_app.entry_route == "/login"


def test_known_error_signatures_are_included(artifact):
    codes = {s.outcome_code: s.classification for s in artifact.error_signatures}
    assert codes["MEMBER_NOT_FOUND"] == "business_outcome"
    assert codes["INVALID_INPUT"] == "business_outcome"
    assert codes["PERMISSION_DENIED"] == "business_outcome"
    assert codes["SESSION_EXPIRED"] == "recoverable"
    assert codes["INTERSTITIAL"] == "recoverable"
    assert codes["SERVER_ERROR"] == "hard_failure"
    assert artifact.sign_in_steps == ["s01", "s02", "s03", "s04"]


def test_route_normalization_and_templates():
    assert normalize_route("http://h/member/12345", {"12345": "member_id"}) == "/member/:member_id"
    assert normalize_route("http://h/app/subacct/review?t=3f9a0c1d2e4b5a69", {}) == "/app/subacct/review?t=:id"
    assert normalize_route("http://h/app/search", {}) == "/app/search"
    assert route_regex("/app/member?m=:member_id").match("/app/member?m=100236")
    assert not route_regex("/app/member?m=:member_id").match("/app/member?m=1&x=2")
    assert to_template("{{member_id}}", {}) == "{member_id}"
    assert to_template("100234", {"100234": "member_id"}) == "{member_id}"
    assert to_template("Rainy {Day}", {}) == "Rainy {{Day}}"


def test_failed_runs_are_not_recorded(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"run_id": "x", "goal": "g", "entry_url": "http://h/",
                                                   "params": [], "started_at": "t"}))
    (tmp_path / "steps.jsonl").write_text("")
    (tmp_path / "result.json").write_text(json.dumps({"outcome": "stuck"}))
    with pytest.raises(RecordingError, match="did not succeed"):
        record_run(tmp_path, capability_id="x-y", name="x", catalog=AppCatalog.load())


# ---------------------------------------------------------------- acceptance: round trip, no secrets

def test_schema_round_trip(artifact):
    assert CapabilityArtifact.model_validate_json(artifact.model_dump_json()) == artifact
    assert CapabilityArtifact.model_json_schema()["properties"]["schema_version"]["const"] == "1.0"


def test_store_round_trip_and_no_raw_sensitive_values(artifact, tmp_path, recorded_run):
    store = ArtifactStore(tmp_path / "artifacts")
    path = store.save(artifact, PolicyConfig.load().redactor())
    assert path.name == "v1.json" and path.with_suffix(".md").exists()
    assert store.load("lookup-savings-balance") == artifact
    for file in (path, path.with_suffix(".md")):
        text = file.read_text()
        assert MOCK_PASSWORD not in text and MOCK_USER not in text
    assert "{password}" in path.read_text()
    with pytest.raises(StoreError, match="already exists"):
        store.save(artifact, PolicyConfig.load().redactor())


# ---------------------------------------------------------------- schema guards

def _mutate(artifact, fn):
    data = artifact.model_dump(mode="json")
    fn(data)
    return data


@pytest.mark.parametrize("breaks,message", [
    (lambda d: d["steps"][0].update(value_template="/login?pw={password}"), "may only be typed"),
    (lambda d: d["steps"][4].update(value_template="{account}"), "undeclared input"),
    (lambda d: d["outputs"][0].update(source_step="s02"), "source_step"),
    (lambda d: d["error_signatures"][5].update(recovery=None), "recoverable"),
    (lambda d: d["steps"][1].update(value_template=None), "needs a value_template"),
    (lambda d: d["steps"][2].update(id="s02"), "unique"),
    (lambda d: d.update(schema_version="2.0"), "schema_version"),
    (lambda d: d.update(sign_in_steps=["s02"]), "prefix"),
    (lambda d: d.update(sign_in_steps=[]), "needs sign_in_steps"),
])
def test_schema_rejects_inconsistent_artifacts(artifact, breaks, message):
    with pytest.raises(ValidationError, match=message):
        CapabilityArtifact.model_validate(_mutate(artifact, breaks))


def test_store_refuses_an_artifact_that_redaction_would_change(artifact, tmp_path):
    """A literal account number typed into a field must become a (sensitive) input, not be silently rewritten."""
    steps = list(artifact.steps)
    steps[4] = steps[4].model_copy(update={"value_template": "000123456789"})
    leaky = artifact.model_copy(update={"steps": steps})
    with pytest.raises(StoreError, match=r"sensitive-looking data at \$\.steps\[4\]\.value_template"):
        ArtifactStore(tmp_path).save(leaky, PolicyConfig.load().redactor())
    assert not (tmp_path / artifact.capability.id).exists()

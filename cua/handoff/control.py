"""Who controls the live browser session, and the handoff protocol between automation and a human.

    AGENT --(trigger)--> AWAITING_HUMAN --take--> HUMAN --resume--> AGENT
                          AWAITING_HUMAN --resume/approve/deny--> AGENT
                          any --abort--> ABORTED (terminal)

Only the owner acts: the policy gate refuses automation actions unless the state is AGENT, and the
human's actions are only recorded while the state is HUMAN. Every transition is logged with who and why.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol

from pydantic import BaseModel

from cua.observability.runlog import RunLog
from cua.policy.gate import ApprovalDecision, ApprovalRequest
from cua.surface.base import Surface

from .intervention import Command, HumanActionRecorder, InterventionRequest, Trigger, now_iso, write_request


class ControlState(str, Enum):
    AGENT = "agent"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN = "human"
    ABORTED = "aborted"


_ALLOWED = {
    (ControlState.AGENT, ControlState.AWAITING_HUMAN),
    (ControlState.AWAITING_HUMAN, ControlState.HUMAN),
    (ControlState.AWAITING_HUMAN, ControlState.AGENT),
    (ControlState.HUMAN, ControlState.AGENT),
}


class InvalidTransition(Exception):
    pass


class Transition(BaseModel):
    at: str
    from_state: ControlState
    to_state: ControlState
    by: str
    why: str


class Controller:
    def __init__(self, log: RunLog):
        self.state = ControlState.AGENT
        self.log = log
        self.history: list[Transition] = []

    def automation_may_act(self) -> bool:
        return self.state is ControlState.AGENT

    def transition(self, to: ControlState, *, by: str, why: str) -> None:
        allowed = (self.state, to) in _ALLOWED or (to is ControlState.ABORTED and self.state is not to)
        if not allowed:
            raise InvalidTransition(f"{self.state.value} -> {to.value} is not allowed")
        record = Transition(at=now_iso(), from_state=self.state, to_state=to, by=by, why=why)
        self.history.append(record)
        self.state = to
        self.log.step({"event": "control_transition", **record.model_dump(mode="json")})
        self.log.echo(f"   control: {record.from_state.value} -> {record.to_state.value} by {by} ({why})")


class Operator(Protocol):
    """The human's console. `next_command` must keep calling `pump` while it waits, so the browser stays live."""

    name: str

    def show(self, request: InterventionRequest) -> None: ...

    def say(self, message: str) -> None: ...

    def next_command(self, pump) -> str: ...


class HandoffOutcome(BaseModel):
    command: Command  # resume | approve | deny | abort
    human_actions: int


_COMMANDS: dict[Trigger, list[Command]] = {
    "approval": ["approve", "deny", "abort"],
    "stuck": ["take", "resume", "abort"],
    "request_human": ["take", "resume", "abort"],
    "replay_failure": ["take", "resume", "abort"],
}


class HandoffSession:
    """One live browser, one controller, one operator. Used by discovery, replay and the approval hook."""

    def __init__(self, *, surface: Surface, log: RunLog, operator: Operator, subject: str,
                 controller: Controller | None = None):
        self.surface = surface
        self.log = log
        self.operator = operator
        self.subject = subject
        self.controller = controller or Controller(log)
        self.recorder = HumanActionRecorder(log)
        self.requests = 0
        surface.enable_capture(self.recorder)

    def request(self, *, trigger: Trigger, reason: str, step_id: str | None) -> HandoffOutcome:
        self.requests += 1
        allowed = _COMMANDS[trigger]
        screenshot = self.surface.screenshot(self.log.dir / f"intervention-{self.requests}.png")
        frames = {}
        try:
            frames = {"/".join(f.path) or "top": f.url for f in self.surface.observe().frames}
        except Exception:  # noqa: BLE001 - a request must go out even if the page is mid-navigation
            pass
        request = InterventionRequest(
            run_id=self.log.dir.name, subject=self.subject, step_id=step_id, trigger=trigger, reason=reason,
            current_url=self.surface.current_url(), frames=frames, screenshot=screenshot.name,
            allowed_commands=allowed, requested_at=now_iso(),
        )
        write_request(self.log, request, self.requests)
        self.operator.show(InterventionRequest.model_validate(self.log.redactor.obj(request.model_dump(mode="json"))))
        self.controller.transition(ControlState.AWAITING_HUMAN, by="automation", why=f"{trigger}: {reason}")
        before = len(self.recorder.actions)

        def pump() -> None:
            self.surface.pump_events(250)

        while True:
            command = self.operator.next_command(pump).strip().lower()
            state = self.controller.state
            if command not in allowed:
                self.operator.say(f"'{command}' is not available here; choose one of {allowed}")
            elif command == "abort":
                self.recorder.close()
                self.controller.transition(ControlState.ABORTED, by=self.operator.name, why="operator aborted")
                return HandoffOutcome(command="abort", human_actions=len(self.recorder.actions) - before)
            elif command == "take" and state is ControlState.AWAITING_HUMAN:
                self.controller.transition(ControlState.HUMAN, by=self.operator.name, why="took control")
                self.recorder.open()
                self.operator.say("You have control of the browser window. Type 'resume' when done.")
            elif command == "resume" and state in (ControlState.HUMAN, ControlState.AWAITING_HUMAN):
                self.surface.flush_capture()  # a field still being edited has not fired `change` yet
                pump()  # deliver the human's last events while they still own the session
                self.recorder.close()
                self.controller.transition(ControlState.AGENT, by=self.operator.name, why="resumed automation")
                return HandoffOutcome(command="resume", human_actions=len(self.recorder.actions) - before)
            elif command in ("approve", "deny") and state is ControlState.AWAITING_HUMAN:
                self.controller.transition(ControlState.AGENT, by=self.operator.name, why=f"{command}d the action")
                return HandoffOutcome(command=command, human_actions=0)
            else:
                self.operator.say(f"'{command}' is not valid while control is {state.value}")

    # ------------------------------------------------------------ the approval hook for the policy gate

    def approver(self, request: ApprovalRequest) -> ApprovalDecision:
        outcome = self.request(trigger="approval", step_id=None,
                               reason=f"approve {request.action} {request.target} on {request.page}? "
                                      f"({request.reason}; {request.context})")
        return ApprovalDecision(approved=outcome.command == "approve", by=f"operator {self.operator.name}",
                                by_human=True, note=outcome.command)

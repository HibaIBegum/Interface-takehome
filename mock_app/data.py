"""In-memory member data and sub-account business rules. All data is fake."""

from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

SUBACCOUNT_TYPES: dict[str, str] = {
    "SHR": "Share Savings",
    "MMA": "Money Market",
    "HOL": "Holiday Club",
    "CD12": "12-Month Share Certificate",
}
MIN_OPENING_DEPOSIT: dict[str, Decimal] = {"CD12": Decimal("1000.00")}

_AMOUNT_RE = re.compile(r"^\d{1,7}(\.\d{1,2})?$")
_NICKNAME_RE = re.compile(r"^[A-Za-z0-9 ]{1,20}$")


@dataclass
class SubAccount:
    type_code: str
    nickname: str
    balance: Decimal
    reference: str

    @property
    def type_label(self) -> str:
        return SUBACCOUNT_TYPES[self.type_code]


@dataclass
class Member:
    member_id: str
    name: str
    ssn_last4: str
    savings: Decimal
    checking: Decimal
    restricted: bool = False
    sub_accounts: list[SubAccount] = field(default_factory=list)

    @property
    def masked_ssn(self) -> str:
        return f"***-**-{self.ssn_last4}"


@dataclass(frozen=True)
class Draft:
    token: str
    member_id: str
    type_code: str
    nickname: str
    amount: Decimal

    @property
    def type_label(self) -> str:
        return SUBACCOUNT_TYPES[self.type_code]


@dataclass(frozen=True)
class Receipt:
    reference: str
    member_id: str
    member_name: str
    type_code: str
    nickname: str
    amount: Decimal

    @property
    def type_label(self) -> str:
        return SUBACCOUNT_TYPES[self.type_code]


class InsufficientFunds(Exception):
    pass


def _seed() -> dict[str, Member]:
    members = [
        Member("100234", "Jane Q. Testmember", "0001", Decimal("5230.17"), Decimal("812.40")),
        Member("100235", "John Sampleton", "0002", Decimal("250.00"), Decimal("1990.05")),
        Member("100236", "Maria Placeholder", "0003", Decimal("18400.00"), Decimal("45.99")),
        Member("100237", "Robert Restricted", "0004", Decimal("7000.00"), Decimal("300.00"), restricted=True),
        Member("100238", "Ada Fictional", "0005", Decimal("1200.50"), Decimal("600.00")),
    ]
    return {m.member_id: m for m in members}


def validate_subaccount(member: Member, type_code: str, nickname: str, amount_raw: str) -> tuple[Decimal | None, dict[str, str]]:
    """Return (parsed amount, field errors). Errors are keyed by form field name."""
    errors: dict[str, str] = {}
    if type_code not in SUBACCOUNT_TYPES:
        errors["typ"] = "Please select a sub-account type."
    if not _NICKNAME_RE.match(nickname):
        errors["nick"] = "Nickname is required: 1-20 letters, digits or spaces."

    amount: Decimal | None = None
    if not _AMOUNT_RE.match(amount_raw):
        errors["amt"] = "Initial deposit must be a dollar amount, e.g. 250.00"
    else:
        try:
            amount = Decimal(amount_raw)
        except InvalidOperation:
            errors["amt"] = "Initial deposit must be a dollar amount, e.g. 250.00"
    if amount is not None and "amt" not in errors:
        minimum = MIN_OPENING_DEPOSIT.get(type_code)
        if amount <= 0:
            errors["amt"] = "Initial deposit must be greater than zero."
        elif minimum is not None and amount < minimum:
            errors["amt"] = f"Minimum opening deposit for {SUBACCOUNT_TYPES[type_code]} is ${minimum:,.2f}."
        elif amount > member.savings:
            errors["amt"] = "Insufficient funds: initial deposit exceeds available Savings balance."
    return (amount if not errors else None), errors


class Bank:
    """All mutable app state. One instance per app, so tests are isolated."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.members: dict[str, Member] = _seed()
        self.drafts: dict[str, Draft] = {}
        self.submitted: dict[str, str] = {}  # draft token -> reference (idempotent submit)
        self.receipts: dict[str, Receipt] = {}
        self._next_ref = 240101

    def get(self, member_id: str) -> Member | None:
        return self.members.get(member_id.strip())

    def create_draft(self, member_id: str, type_code: str, nickname: str, amount: Decimal) -> Draft:
        draft = Draft(secrets.token_hex(8), member_id, type_code, nickname, amount)
        self.drafts[draft.token] = draft
        return draft

    def submit(self, token: str) -> tuple[Receipt | None, bool]:
        """Commit a draft. Returns (receipt, was_duplicate). (None, False) if the token is unknown."""
        with self._lock:
            if token in self.submitted:
                return self.receipts[self.submitted[token]], True
            draft = self.drafts.get(token)
            if draft is None:
                return None, False
            member = self.members[draft.member_id]
            if draft.amount > member.savings:
                raise InsufficientFunds()
            reference = f"SA-{self._next_ref}"
            self._next_ref += 1
            member.savings -= draft.amount
            member.sub_accounts.append(SubAccount(draft.type_code, draft.nickname, draft.amount, reference))
            receipt = Receipt(reference, member.member_id, member.name, draft.type_code, draft.nickname, draft.amount)
            self.receipts[reference] = receipt
            self.submitted[token] = reference
            del self.drafts[token]
            return receipt, False

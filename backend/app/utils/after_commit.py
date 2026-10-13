from collections.abc import Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

_CALLBACKS = "transaction_commit_callbacks"


def run_after_commit(session: Session, callback: Callable[[], None]) -> None:
    if _CALLBACKS not in session.info:
        session.info[_CALLBACKS] = {}
        event.listen(session, "after_commit", _committed)
        event.listen(session, "after_soft_rollback", _rolled_back)
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:
        raise RuntimeError("A transaction is required for an after-commit callback")
    session.info[_CALLBACKS].setdefault(transaction, []).append(callback)


def _committed(session: Session) -> None:
    pending: dict[SessionTransaction, list[Callable[[], None]]] = session.info[_CALLBACKS]
    transaction = session.get_nested_transaction() or session.get_transaction()
    callbacks = pending.pop(transaction, [])
    if transaction is not None and transaction.nested:
        pending.setdefault(transaction.parent, []).extend(callbacks)
    else:
        pending.clear()
        for callback in callbacks:
            callback()


def _rolled_back(session: Session, previous_transaction: Any) -> None:
    pending = session.info[_CALLBACKS]
    if previous_transaction.parent is None:
        pending.clear()
    else:
        pending.pop(previous_transaction, None)

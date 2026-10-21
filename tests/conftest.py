import asyncio

import pytest

from shadowclaw.execution.approval import FailClosedPolicy
from shadowclaw.execution.checkpoint import SqliteCheckpointStore
from shadowclaw.execution.config import Config
from shadowclaw.execution.orchestrator import Orchestrator
from shadowclaw.execution.trajectory import SqliteTrajectoryStore
from shadowclaw.execution.units import UnitRegistry


def run_coro(coro):
    return asyncio.run(coro)


@pytest.fixture
def make_orchestrator(tmp_path):
    """工厂夹具：给定注册表（与可选策略/在场设置）造一个隔离的编排器。"""
    db = tmp_path / "test.db"

    def _make(registry: UnitRegistry, **kwargs) -> Orchestrator:
        config = Config(db_path=db, harness_dir=tmp_path / "harness")
        return Orchestrator(
            registry=registry,
            trajectory_store=SqliteTrajectoryStore(db),
            checkpoint_store=SqliteCheckpointStore(db),
            config=config,
            approval_policy=kwargs.get("approval_policy")
            or FailClosedPolicy(),
            attended=kwargs.get("attended", False),
            ask_handler=kwargs.get("ask_handler"))

    _make.db_path = db
    return _make


@pytest.fixture
def store_of(tmp_path):
    def _store(db_path=None) -> SqliteTrajectoryStore:
        return SqliteTrajectoryStore(db_path or (tmp_path / "test.db"))
    return _store

"""事实板单写者：提议不直接生效，落板才进快照；同陈述去重。"""
from shadowclaw.execution.facts import FactBoard


def test_propose_does_not_take_effect_until_commit():
    board = FactBoard()
    fact = board.propose("事实一", source_unit="u1", step_id="s1")
    assert board.snapshot() == []          # 只提议：任何人都看不到
    assert board.committed() == []
    assert board.commit(fact) is True
    assert board.snapshot() == ["事实一"]


def test_commit_dedupes_same_statement():
    board = FactBoard()
    f1 = board.propose("重复事实", source_unit="u1", step_id="s1")
    f2 = board.propose("重复事实", source_unit="u2", step_id="s2")
    assert board.commit(f1) is True
    assert board.commit(f2) is False       # 已落板同陈述：不再重复入板
    assert board.snapshot() == ["重复事实"]


def test_restore_roundtrip_for_checkpoint():
    board = FactBoard()
    board.commit(board.propose("甲", source_unit="u", step_id="s"))
    exported = board.export_committed()
    board2 = FactBoard()
    board2.restore(exported)
    assert board2.snapshot() == ["甲"]
    f = board2.propose("乙", source_unit="u", step_id="s2")
    assert f.id == 2                        # id 续号不冲突

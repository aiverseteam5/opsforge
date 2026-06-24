"""Guard against a silently dropped table — pure metadata, no DB."""

from __future__ import annotations

from opsforge.models import ALL_TABLES, Base


def test_all_expected_tables_present():
    defined = set(Base.metadata.tables.keys())
    assert defined == set(ALL_TABLES), defined.symmetric_difference(set(ALL_TABLES))


def test_table_count():
    assert len(ALL_TABLES) == 21

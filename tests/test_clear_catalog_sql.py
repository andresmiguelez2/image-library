from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FUNCTION_START = "CREATE OR REPLACE FUNCTION public.clear_catalog()"
FUNCTION_END = "$clear_catalog$;"


def _function_definition(sql: str) -> str:
    start = sql.index(FUNCTION_START)
    end = sql.index(FUNCTION_END, start) + len(FUNCTION_END)
    return sql[start:end]


def test_fresh_schema_and_migration_define_the_same_clear_function():
    bootstrap = (ROOT / "scripts" / "init_db.sql").read_text()
    migration = (ROOT / "scripts" / "003_clear_catalog.sql").read_text()

    assert _function_definition(bootstrap) == _function_definition(migration)


def test_clear_function_is_explicit_safe_and_handles_optional_location_table():
    sql = (ROOT / "scripts" / "003_clear_catalog.sql").read_text()
    definition = _function_definition(sql)
    upper_definition = definition.upper()

    assert "SECURITY INVOKER" in upper_definition
    assert "TO_REGCLASS('PUBLIC.IMAGE_LOCATIONS') IS NOT NULL" in upper_definition
    assert "TO_REGCLASS(FORMAT('PUBLIC.%I', REQUIRED_TABLE)) IS NULL" in upper_definition
    assert "RAISE EXCEPTION" in upper_definition
    assert "DELETE FROM PUBLIC.IMAGE_LOCATIONS" in upper_definition
    for table in ("face_match_decisions", "faces", "persons", "images", "sources"):
        assert f"DELETE FROM PUBLIC.{table.upper()}" in upper_definition
    assert "ALTER SEQUENCE" in upper_definition
    assert "TRUNCATE" not in upper_definition
    assert "CASCADE" not in upper_definition

    assert "SELECT PUBLIC.CLEAR_CATALOG()" not in upper_definition

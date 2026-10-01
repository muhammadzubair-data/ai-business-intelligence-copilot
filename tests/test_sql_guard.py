import pytest

from bi_copilot.sql_guard import GuardError

BLOCK = ["DROP TABLE fact_orders", "DELETE FROM dim_customer", "UPDATE dim_product SET list_price = 0",
         "SELECT 1; DROP TABLE dim_date", "SELECT * FROM read_csv('/etc/passwd')", "COPY dim_customer TO '/tmp/x.csv'",
         "ATTACH '/tmp/x.db' AS x", "PRAGMA database_list", "CREATE TABLE t AS SELECT 1", "SELECT getenv('HOME')",
         "SELECT no_such_column FROM v_sales_lines", "SELECT * FROM not_a_table", "INSTALL httpfs",
         "SELECT * FROM x.main.fact_orders", "SELECT * FROM glob('/*')"]


@pytest.mark.parametrize("sql", BLOCK)
def test_malicious_sql_is_blocked(db, sql):
    with pytest.raises(GuardError):
        db.guard.check(sql)


def test_legitimate_sql_passes_and_is_limited(db):
    res = db.guard.check("WITH a AS (SELECT region, net_line_amount FROM v_sales_lines) SELECT region, SUM(net_line_amount) FROM a GROUP BY 1")
    assert "LIMIT" in res.sql and res.tables == ["v_sales_lines"]
    df, rec = db.run("SELECT region, COUNT(*) n FROM v_sales_lines GROUP BY 1", guarded=True)
    assert len(df) == 4 and rec.rows == 4


def test_connection_is_read_only(db):
    with pytest.raises(Exception):
        db.con.execute("CREATE TABLE hacked AS SELECT 1")

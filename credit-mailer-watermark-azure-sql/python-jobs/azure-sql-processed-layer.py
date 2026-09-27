"""Fill the processed zone: dim_client in full, fact_mailer incrementally, one commit at the end.

The Azure counterpart of ``glue-jobs/redshift-processed-layer.py``, against Azure SQL Database::

    raw_zone.client_attributes                      ->  processed_zone.dim_client   (full merge)

    raw_zone.mail_offers
      joined to raw_zone.client_attributes    for risk_band
      joined to processed_zone.dim_offer_arm  for arm_id    ->  processed_zone.fact_mailer
                                                                (incremental, by wave)

The design is the AWS job's and so is most of the code: the fact's column table, the arm-band
join, the ``MAX(wave)`` watermark with its ``COALESCE`` and its ``>=``, the MERGE generator, the
arm-grid check and the stage builder are all imported from it. Why the fact's watermark is an
ordinal read from the fact itself rather than a second Table Storage entity, why ``>=`` re-merges
the newest wave every run and why that is a no-op under the ``(client_id, wave)`` key, why
``bad_account`` stays NULL: all argued there and all unchanged here.

What this file owns is the handful of statements whose text T-SQL does not accept.

WHAT T-SQL CHANGED, STATEMENT BY STATEMENT
------------------------------------------
``CREATE TEMP TABLE x AS SELECT ...``
    becomes ``SELECT ... INTO #x FROM ...``. A ``#`` table is private to the session and dropped
    when the connection closes, which is what TEMP meant on Redshift.

``(ca.female = 1)`` and ``(ca.edhi = 1)``
    are not values in T-SQL -- a comparison may appear in a WHERE, not in a SELECT list -- and
    there is no BOOLEAN to hold one. The target columns are nullable BIT, and the expression is a
    CASE with two WHENs and no ELSE::

        CAST(CASE WHEN ca.female = 1 THEN 1 WHEN ca.female <> 1 THEN 0 END AS BIT)

    which is the comparison's three-valued truth table written out: 1 -> 1, any other number ->
    0, and NULL -> NULL, because a NULL makes both WHENs unknown and there is no ELSE to supply a
    default. The obvious shorter form, ``CASE WHEN ca.female = 1 THEN 1 ELSE 0 END``, is the one
    that loses the distinction the AWS job keeps on purpose: ``edhi`` is self-reported, and "did
    not say" is not "not more educated".

``ORDER BY n DESC LIMIT 10``
    in the arm-miss diagnostic becomes ``SELECT TOP 10 ... ORDER BY n DESC``.

``BEGIN TRANSACTION;`` / ``COMMIT;`` / ``ROLLBACK;`` as text
    are gone. The AWS job sends them because DuckDB's ``cursor()`` duplicates the connection and
    only statement text reaches the right session. pyodbc's cursor is a cursor on the connection,
    and its implicit transaction is ended by ``conn.commit()`` / ``conn.rollback()`` -- one
    mechanism, because mixing the two nests the transaction and the commit then keeps nothing.

The MERGE text is the AWS generator's unchanged: it already ends in ``;``, which T-SQL requires.

WHAT SQL SERVER ADDS
--------------------
The fact's ``(client_id, wave)`` PRIMARY KEY is enforced here. On Redshift it is informational --
the planner reads it and nothing checks it -- so a duplicate mailer would have merged in silently.
The NOT NULL columns are enforced on both engines, so a mailer with no arm fails the MERGE in
either place. The pre-MERGE checks -- the arm count, the staged-versus-source parity, the arm-miss
count -- still run first and still name the cause; the key is now a second line behind them
rather than a declaration the planner reads.

    python python-jobs/azure-sql-processed-layer.py --self-check
    python python-jobs/azure-sql-processed-layer.py --dry-run
    python python-jobs/azure-sql-processed-layer.py
"""

import argparse
import logging

import azure_common as az

LOG = logging.getLogger("azure_sql_processed_layer")

AWS = az.load_aws_job("redshift-processed-layer.py", "aws_redshift_processed_layer")

# Imported, not restated: the translation from raw names to business names for the fact, the
# arm-band join, the watermark subquery, the MERGE generator and the stage/scalar helpers.
FACT_MAILER_SELECT = AWS.FACT_MAILER_SELECT
FACT_MAILER_KEYS = AWS.FACT_MAILER_KEYS
DIM_CLIENT_KEYS = AWS.DIM_CLIENT_KEYS
TREATMENT_FLAGS = AWS.TREATMENT_FLAGS
WAVE_WATERMARK = AWS.WAVE_WATERMARK
ARM_BAND_JOIN = AWS.ARM_BAND_JOIN
projection = AWS.projection
merge_sql = AWS.merge_sql
scalar = AWS.scalar
build_stage = AWS.build_stage
check_arm_grid = AWS.check_arm_grid

STAGE_DIM_CLIENT = "#stage_dim_client"
STAGE_FACT_MAILER = "#stage_fact_mailer"


def bit_of(column):
    """The T-SQL spelling of ``(column = 1)``: 1, 0, or NULL when the column is NULL. No ELSE."""
    return ("CAST(CASE WHEN {0} = 1 THEN 1 WHEN {0} <> 1 THEN 0 END AS BIT)"
            .format(column))


# The AWS job's DIM_CLIENT_SELECT, with the two comparisons rewritten and everything else taken
# from it by position -- so a column added there without being added here fails the self-check
# rather than silently dropping out of the dimension.
_T_SQL_EXPRESSIONS = {"is_female": bit_of("ca.female"), "is_more_educated": bit_of("ca.edhi")}
DIM_CLIENT_SELECT = [(name, _T_SQL_EXPRESSIONS.get(name, expression))
                     for name, expression in AWS.DIM_CLIENT_SELECT]


def stage_dim_client_sql():
    """Every client in the CRM snapshot, every run: no event time, so no watermark."""
    return ("SELECT\n"
            "%s\n"
            "INTO %s\n"
            "FROM raw_zone.client_attributes ca;" % (projection(DIM_CLIENT_SELECT),
                                                     STAGE_DIM_CLIENT))


def stage_fact_mailer_sql():
    """The mailers at or after the fact's own high-water wave, banded into their price arms.

    The AWS job's statement with ``INTO #stage_fact_mailer`` where Redshift had ``CREATE TEMP
    TABLE ... AS``. Inner join to the client snapshot, LEFT join to the arm grid so a rate outside
    every band arrives as a countable NULL rather than a missing row.
    """
    return ("SELECT\n"
            "%s\n"
            "INTO %s\n"
            "FROM raw_zone.mail_offers mo\n"
            "JOIN raw_zone.client_attributes ca\n"
            "     ON ca.client_id = mo.client_id\n"
            "LEFT JOIN processed_zone.dim_offer_arm arm\n"
            "     ON %s\n"
            "WHERE mo.wave >= %s;" % (projection(FACT_MAILER_SELECT), STAGE_FACT_MAILER,
                                      ARM_BAND_JOIN, WAVE_WATERMARK))


def check_fact_stage(cursor, staged):
    """The AWS job's two pre-MERGE checks, with the diagnostic's LIMIT written as TOP.

    Staged rows must equal the mail_offers rows in the same wave window -- fewer means a mailer
    whose client is missing from the snapshot, more means two arms overlap -- and no staged row
    may have a NULL arm_id. Both are counted before the MERGE so the error names the cause.
    """
    source = scalar(cursor, "SELECT COUNT(*) FROM raw_zone.mail_offers mo "
                            "WHERE mo.wave >= %s;" % WAVE_WATERMARK)
    if staged != source:
        raise ValueError("staged %s rows from %s mail_offers rows in the same wave window. "
                         "Fewer means the join to client_attributes dropped mailers whose "
                         "client is not in the CRM snapshot; more means two arms in one risk "
                         "band overlap and a mailer matched both" % (staged, source))

    missed = scalar(cursor, "SELECT COUNT(*) FROM %s WHERE arm_id IS NULL;" % STAGE_FACT_MAILER)
    if missed:
        az.run(cursor, "SELECT TOP 10 risk_band, offer_rate, COUNT(*) AS n FROM %s "
                       "WHERE arm_id IS NULL GROUP BY risk_band, offer_rate "
                       "ORDER BY n DESC;" % STAGE_FACT_MAILER, log=LOG)
        offending = ", ".join("%s %s (n=%s)" % tuple(row) for row in cursor.fetchall())
        raise ValueError("%s of %s staged mailers matched no arm in dim_offer_arm. The arm grid "
                         "is declared, not derived -- widen the outermost cut points rather "
                         "than dropping the rows. Worst offenders: %s"
                         % (missed, staged, offending))
    LOG.info("every one of the %s staged mailers matched exactly one arm", staged)


def self_check():
    """The AWS job's assertions about the column tables, then the T-SQL rewrites. No database."""
    # 1. The fact's column table, the merge keys, the unfilled treatment flags and the watermark
    #    predicate -- asserted by the AWS job's own self-check, since they are its objects.
    AWS.self_check()

    # 2. dim_client is the AWS column table with exactly two expressions replaced.
    assert [n for n, _ in DIM_CLIENT_SELECT] == [n for n, _ in AWS.DIM_CLIENT_SELECT], \
        "dim_client's target columns differ from the AWS job's"
    changed = [n for (n, e), (_, a) in zip(DIM_CLIENT_SELECT, AWS.DIM_CLIENT_SELECT) if e != a]
    assert changed == ["is_female", "is_more_educated"], changed

    # 3. The BIT expression keeps NULL: two WHENs, no ELSE. An ELSE is the edit that would make
    #    "did not say" read as "no".
    for name in changed:
        expression = dict(DIM_CLIENT_SELECT)[name]
        assert "ELSE" not in expression.upper(), "%s has an ELSE, so a NULL becomes 0" % name
        assert expression.count("WHEN") == 2 and "AS BIT" in expression

    # 4. No Redshift-only spelling survives in the statements this file sends.
    for sql in [stage_dim_client_sql(), stage_fact_mailer_sql()]:
        assert "CREATE TEMP TABLE" not in sql and "\nINTO #stage_" in sql
        assert "LIMIT" not in sql.upper()
    for target, stage, keys, select in [
            ("processed_zone.dim_client", STAGE_DIM_CLIENT, DIM_CLIENT_KEYS, DIM_CLIENT_SELECT),
            ("processed_zone.fact_mailer", STAGE_FACT_MAILER, FACT_MAILER_KEYS,
             FACT_MAILER_SELECT)]:
        merge = merge_sql(target, stage, keys, select)
        assert merge.endswith(";"), "%s's MERGE is not terminated, and T-SQL requires it" % target
        assert "USING %s AS source" % stage in merge

    LOG.info("self-check passed: the AWS column tables and watermark predicate, dim_client's two "
             "flags as BIT with no ELSE, SELECT ... INTO # in place of CREATE TEMP TABLE, and "
             "every MERGE terminated")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", default=az.DEFAULT_DATABASE,
                        help="SQL database (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="build both staging tables and run every check against them, then "
                             "roll back. Merges nothing and commits nothing")
    parser.add_argument("--self-check", action="store_true",
                        help="assert the column tables and the T-SQL rewrites, then exit")
    args, _ = parser.parse_known_args()

    if args.self_check:
        self_check()
        return

    conn = None
    try:
        conn = az.connect_sql(args.database)
        cursor = conn.cursor()

        check_arm_grid(cursor)

        clients = build_stage(cursor, STAGE_DIM_CLIENT, stage_dim_client_sql())
        LOG.info("staged %s clients for dim_client (full snapshot, no watermark: "
                 "client_attributes has no event time to filter on)", clients)

        through = scalar(cursor, "SELECT %s;" % WAVE_WATERMARK)
        staged = build_stage(cursor, STAGE_FACT_MAILER, stage_fact_mailer_sql())
        LOG.info("fact_mailer holds waves up to %s, so %s mailers are staged from wave %s "
                 "onward -- '>=' re-merges the newest wave every run, which the "
                 "(client_id, wave) key makes a no-op", through, staged, through)
        check_fact_stage(cursor, staged)

        if args.dry_run:
            conn.rollback()
            LOG.info("dry run complete: %s client rows and %s mailer rows staged and checked, "
                     "nothing merged and nothing committed", clients, staged)
            return

        az.run(cursor, merge_sql("processed_zone.dim_client", STAGE_DIM_CLIENT,
                                 DIM_CLIENT_KEYS, DIM_CLIENT_SELECT), log=LOG)
        az.run(cursor, merge_sql("processed_zone.fact_mailer", STAGE_FACT_MAILER,
                                 FACT_MAILER_KEYS, FACT_MAILER_SELECT), log=LOG)
        conn.commit()
        LOG.info("committed: dim_client merged from %s staged clients, fact_mailer merged from "
                 "%s staged mailers, both in one transaction", clients, staged)
    except Exception:
        az.rollback(conn, log=LOG)
        # Re-raised: a non-zero exit is what stops the chain before the refinery reads a
        # half-merged fact.
        raise
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()

"""Path 3 -- the bandit sub-refinery over the mailer star, reading and writing Azure SQL Database.

The Azure counterpart of ``glue-jobs/glue-refinery-path3.py``, and the thinnest of the four ports,
because that job was already written so that almost nothing in it knows which warehouse it is
talking to. Steps 4-10, the verdict badges, the contextual Beta-Bernoulli Thompson sampler, the
SNIPS evaluation and its bootstrap are all imported from it and run unchanged::

      processed_zone.fact_mailer   x   processed_zone.dim_offer_arm
  ->  Step 4   Imputation      OVERRIDE -- native missingness kept as a latent state, no fill
  ->  Step 5   Diagnostics     APPLIES  -- exposure and reward per (band, wave, arm); positivity
  ->  Step 6   Topology        N/A      -- an ordinal batch index has no cycle to encode
  ->  Step 7   Feature Eng     APPLIES  -- the interaction frame is the (band x arm) cell
  ->  Step 8   Pruning         BANNED   -- variance pruning deletes the rare arms first
  ->  Step 9   Regularisation  BANNED   -- L1 shrinks a thin arm back onto the prior
  ->  Step 10  Scaling         BANNED   -- a standardised count is not a Beta parameter
  ->  engine   Contextual Beta-Bernoulli Thompson sampling, one batched round per wave
  ->  processed_zone.bandit_posterior  +  processed_zone.bandit_policy_value

Every argument for those verdicts -- why there is no gamma, why the propensities are estimated,
why SNIPS with an estimated propensity collapses to a direct-method estimate, why the two random
streams are spawned apart -- is in that module's docstring, beside the code that acts on it.

WHAT THE MOVE TO T-SQL TOUCHED, WHICH IS NOTHING IN THE ARITHMETIC
------------------------------------------------------------------
Every statement the AWS job sends was checked against T-SQL, because a statement that parses and
means something slightly different is the failure a port produces:

*   The one aggregate is ``SUM(took_up)`` over SMALLINT with ``COUNT(*)`` beside it, and the rate
    is divided in Python. There is no ``AVG`` anywhere, and that is not a T-SQL change: T-SQL's
    ``AVG`` over an integer column returns an integer -- measured, ``AVG`` of 1 and 2 is 1 -- and
    Redshift's does the same. DuckDB, the sibling's local warehouse, returns a double, so a local
    rehearsal there would never have shown that a take-up rate computed as ``AVG(took_up)`` is 0
    for every arm and every band on either production engine, with the posterior still a valid
    Beta.
*   ``information_schema.columns`` answers Step 6 in lower case because the database's default
    collation is case-insensitive; its ``data_type`` values are SQL Server's (``smallint``,
    ``decimal``, ``bigint``, ``varchar``), and none of them contains a time token, so Step 6 is
    N/A here on the same measurement.
*   The result write is ``DELETE`` then one multi-row ``INSERT ... VALUES``. T-SQL caps a row
    constructor at 1,000 rows; the posterior writes 18 per wave loaded, 54 at most, and the cap is
    asserted before the statement is built rather than discovered as error 10738.
*   The DECIMAL(10,8) and DECIMAL(12,2) literals are rendered at the declared scale by the AWS
    ``values_clause()``, so SQL Server stores exactly the digits the job printed.

Transaction control is ``conn.commit()`` / ``conn.rollback()`` on pyodbc's implicit transaction,
so the DELETE and the INSERT land together or not at all -- which the AWS docstring notes was true
on Redshift and not on DuckDB, where each statement committed as it ran.

    python python-jobs/refinery-path3.py --self-check
    python python-jobs/refinery-path3.py --dry-run
    python python-jobs/refinery-path3.py
"""

import argparse
import logging

import azure_common as az

LOG = logging.getLogger("refinery_path3_azure")

AWS = az.load_aws_job("glue-refinery-path3.py", "aws_glue_refinery_path3")

# The job's public surface, re-exported so the ported tests address THIS module and would notice
# if the Azure job ever stopped using the declared grid or the shared arithmetic.
BANDS = AWS.BANDS
ARM_BASE = AWS.ARM_BASE
ARMS_PER_BAND = AWS.ARMS_PER_BAND
CUTS = AWS.CUTS
PRIOR_A, PRIOR_B = AWS.PRIOR_A, AWS.PRIOR_B
DEFAULT_SEED = AWS.DEFAULT_SEED
DEFAULT_DRAWS = AWS.DEFAULT_DRAWS
DEFAULT_REPLICATES = AWS.DEFAULT_REPLICATES
RUN_ID_PATTERN = AWS.RUN_ID_PATTERN
arm_index_for_rate = AWS.arm_index_for_rate
make_generators = AWS.make_generators
thompson_probabilities = AWS.thompson_probabilities
posterior_sd = AWS.posterior_sd
run_rounds = AWS.run_rounds
snips = AWS.snips
bootstrap_differences = AWS.bootstrap_differences
evaluate_policy = AWS.evaluate_policy
load_arm_grid = AWS.load_arm_grid
load_cells = AWS.load_cells
write_results = AWS.write_results

# T-SQL's limit on the rows of one table value constructor (error 10738 past it).
TSQL_VALUES_LIMIT = 1000


def check_values_limit(posterior_rows, policy_rows):
    """Refuse a result set that one multi-row INSERT cannot carry in T-SQL."""
    for label, rows in (("bandit_posterior", posterior_rows),
                        ("bandit_policy_value", policy_rows)):
        if len(rows) > TSQL_VALUES_LIMIT:
            raise ValueError("%s would take %s rows in one VALUES list; T-SQL allows %s. Batch "
                             "the INSERT before widening the grid" % (label, len(rows),
                                                                      TSQL_VALUES_LIMIT))


def self_check():
    """The AWS job's arithmetic assertions, then the one T-SQL constraint on the write."""
    # 1. The conjugate update and its batch-invariance, the binner against all 18 bands and both
    #    sides of every cut, SNIPS against the hand-computed case, independent generators. These
    #    are the functions this job runs, so their own self-check is this job's self-check.
    AWS.self_check()

    # 2. The largest result this job can write fits one T-SQL VALUES list: 18 arms x 3 waves.
    most = len(BANDS) * ARMS_PER_BAND * 3
    assert most <= TSQL_VALUES_LIMIT, most
    check_values_limit([{}] * most, [{}] * 6)
    try:
        check_values_limit([{}] * (TSQL_VALUES_LIMIT + 1), [])
    except ValueError:
        pass
    else:                                                           # pragma: no cover
        raise AssertionError("a VALUES list past T-SQL's limit was accepted")

    # 3. The write renders literals at the DDL's scale, so SQL Server stores the printed digits.
    row = dict((column, 0.123456789) for column in AWS.POSTERIOR_COLUMNS)
    row.update({"run_id": "x", "risk_band": "HIGH", "through_wave": 1, "arm_id": 1,
                "arm_index": 0, "pulls": 1, "rewards": 0})
    rendered = AWS.values_clause([row], AWS.POSTERIOR_COLUMNS)
    assert "0.12345679" in rendered and "0.12," in rendered, rendered

    LOG.info("self-check passed: the AWS job's arithmetic, a result set that fits one T-SQL "
             "VALUES list, and literals rendered at the declared DECIMAL scales")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", default=az.DEFAULT_DATABASE,
                        help="SQL database (default: %(default)s)")
    parser.add_argument("--run-id", default=None,
                        help="identifies this run in both result tables (VARCHAR(32)). Derived "
                             "from the star when omitted: through-wave-<highest wave loaded>")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help="seed for the two generators (default: %(default)s)")
    parser.add_argument("--draws", type=int, default=DEFAULT_DRAWS,
                        help="posterior draws per Thompson probability (default: %(default)s)")
    parser.add_argument("--bootstrap-replicates", type=int, default=DEFAULT_REPLICATES,
                        help="resamples per evaluated cell (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="read the star, run every step and the engine, write nothing")
    parser.add_argument("--self-check", action="store_true",
                        help="assert the pure arithmetic and exit; no database")
    args, _ = parser.parse_known_args()

    if args.self_check:
        self_check()
        return
    if args.draws < 1 or args.bootstrap_replicates < 1:
        parser.error("--draws and --bootstrap-replicates must both be at least 1")
    if args.run_id is not None and not RUN_ID_PATTERN.match(args.run_id):
        parser.error("--run-id must be 1-32 characters of letters, digits, dot, dash or "
                     "underscore: it is half the primary key of both result tables and it is "
                     "written into SQL text")

    thompson, bootstrap = make_generators(args.seed)

    conn = None
    try:
        conn = az.connect_sql(args.database)
        cursor = conn.cursor()

        grid = load_arm_grid(cursor)
        cells, waves = load_cells(cursor, grid)
        run_id = args.run_id or "through-wave-{}".format(waves[-1])
        LOG.info("run-id %s | seed %s | %s draws | %s bootstrap replicates%s", run_id,
                 args.seed, "{:,}".format(args.draws), "{:,}".format(args.bootstrap_replicates),
                 " | DRY RUN, nothing will be written" if args.dry_run else "")

        # The same order as the AWS main(): the Thompson generator is consumed in it, so the
        # Monte-Carlo columns match that job's for the same seed, draws and replicates.
        AWS.step4_native_missingness(cursor, waves)
        AWS.step5_diagnostics(cells, waves)
        AWS.step6_topology(cursor, waves)
        AWS.step7_interaction_frame(cells, waves)
        AWS.step8_pruning_banned(cells, waves)
        AWS.step9_regularisation_banned()
        posterior_rows, policy, alpha, beta = run_rounds(cells, waves, thompson, args.draws,
                                                         run_id)
        AWS.step10_scaling_banned(alpha, beta)
        policy_rows = evaluate_policy(cells, waves, policy, bootstrap,
                                      args.bootstrap_replicates, run_id)

        if args.dry_run:
            conn.rollback()
            LOG.info("dry run complete: %s posterior rows and %s policy rows computed and "
                     "discarded", len(posterior_rows), len(policy_rows))
            return

        check_values_limit(posterior_rows, policy_rows)
        write_results(cursor, run_id, posterior_rows, policy_rows)
        conn.commit()
        LOG.info("Path 3 complete -- Steps 4-10 applied, excused or banned with reasons, and the "
                 "posterior and its off-policy evaluation are in processed_zone under run_id %s, "
                 "DELETE and INSERT committed together", run_id)
    except Exception:
        # Guarded, as the AWS job guards its own: pyodbc's rollback with nothing open is a no-op,
        # but a rollback on a connection that has itself failed can raise, and a raise here would
        # replace the error being handled. See azure_common.rollback().
        az.rollback(conn, log=LOG)
        raise
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()

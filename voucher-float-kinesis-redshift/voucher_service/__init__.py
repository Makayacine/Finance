"""The gift-voucher service: the source system whose events the pipeline carries to Redshift.

    config.py     codes, the 30-day term, limits, the three seeded users
    timeutil.py   fixed-width UTC text, and a clock the simulator can move
    db.py         the SQLite schema, write transactions, the outbox writer
    auth.py       Argon2id passwords, HS256 JWTs, role checks
    errors.py     one generic phrase per error status
    app.py        the REST endpoints: login, issue, redeem -- and /graphql
    schema.py     the GraphQL reads: voucher, liability, cohort
    warehouse.py  where liability and cohort are read from: DuckDB locally, Redshift on AWS
    sweep.py      the expiry sweep

Every state change writes an outbox row in the transaction that makes it. Nothing here talks to
AWS; the relay in ``kinesis-producer/`` reads the outbox and publishes it.
"""

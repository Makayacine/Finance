"""The WSGI entry point for the one you deploy: ``gunicorn voucher_service.wsgi:app``.

Everything comes from the environment:

    VOUCHER_DB_PATH               the service's SQLite file                     required
    VOUCHER_JWT_SECRET            the HS256 signing key                         set it
    VOUCHER_ISSUER_PASSWORD       the three users' passwords, hashed with       set them
    VOUCHER_MERCHANT_PASSWORD     Argon2id at start-up
    VOUCHER_FINANCE_PASSWORD
    VOUCHER_REDSHIFT_WORKGROUP    Redshift Serverless, for liability and cohort
    VOUCHER_REDSHIFT_DATABASE
    VOUCHER_WAREHOUSE_PATH        or a DuckDB file instead, as the local run builds

With neither warehouse set, ``voucher`` still answers and the other two queries report the
warehouse unavailable.
"""

import os

from . import config
from .app import create_app
from .warehouse import DuckDBWarehouse, RedshiftDataWarehouse


def warehouse_from(environ):
    if environ.get("VOUCHER_REDSHIFT_WORKGROUP"):
        return RedshiftDataWarehouse(database=environ["VOUCHER_REDSHIFT_DATABASE"],
                                     workgroup=environ["VOUCHER_REDSHIFT_WORKGROUP"],
                                     region=environ.get("AWS_REGION"))
    if environ.get("VOUCHER_WAREHOUSE_PATH"):
        return DuckDBWarehouse(environ["VOUCHER_WAREHOUSE_PATH"])
    return None


def build(environ=os.environ):
    return create_app(environ["VOUCHER_DB_PATH"], jwt_secret=config.jwt_secret(environ),
                      users=config.seed_users(environ), warehouse=warehouse_from(environ))


app = build() if "VOUCHER_DB_PATH" in os.environ else None

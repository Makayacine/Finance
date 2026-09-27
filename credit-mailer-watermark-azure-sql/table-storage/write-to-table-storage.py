"""Seed the externalised watermark table in Azure Table Storage: two entities, one per source table.

The Azure counterpart of ``credit-mailer-watermark-glue-redshift/dynamodb/write-to-dynamo.py``, and
it does not restate the configuration: the two records are imported from that file's
``CONFIGURATIONS``, which stays the one place they are written down. What is new is only the shape
Table Storage needs them in and the write that puts them there.

    PartitionKey    RowKey              load_column    last_extracted_value
    credit_mailer   mail_offers         wave           (absent until the first extract sets it)
    credit_mailer   client_attributes   (absent)       (absent, and it stays that way)

THE DYNAMODB NULL BECOMES AN ABSENT PROPERTY
--------------------------------------------
DynamoDB stores a Python None as ``{'NULL': True}``, a typed value the extraction job reads back
as None. Table Storage has no null type. A property whose value is None is not stored at all --
measured against Azurite, the entity comes back without the key -- so ``client_attributes`` has
no ``load_column`` property and a freshly seeded ``mail_offers`` has no ``last_extracted_value``.
The entities below leave those properties out explicitly rather than handing None to the SDK to
drop, so the absence is visible in this file instead of implied by the wire format; and the
extraction job reads both with ``.get()``, which turns the absence back into the None the AWS
job's logic expects.

That absence is load-bearing twice over, and the second time is a trap:

1.  An absent ``last_extracted_value`` is what tells the extractor that a first incremental run has
    no predicate and takes everything. That is the DynamoDB NULL's job, unchanged.
2.  **Resetting it therefore requires REPLACE, not MERGE.** An upsert in MERGE mode updates the
    properties it is given and leaves the others alone -- and a property "given" as None is not
    given at all. So a MERGE-mode reset of ``mail_offers`` after run 3 would leave
    ``last_extracted_value = '3'`` exactly where it was, report success, and turn the next run 1 of
    the demo into a run 4. Measured against Azurite before this file was written. REPLACE swaps
    the whole entity, which is what DynamoDB's PutItem did and what a reset means.

BOTH ROWS IN ONE TRANSACTION
----------------------------
Both entities share the partition ``credit_mailer``, so they go in one entity-group transaction:
the seed or reset lands whole or not at all. The AWS seeder's ``batch_writer`` could not promise
that -- a DynamoDB batch write is not atomic -- and at two rows the difference is small, but it
is the difference between "the reset happened" and "half of it did".

THE TABLE IS NOT CREATED UNLESS ASKED
-------------------------------------
Same position as the AWS seeder: a seeder that creates tables holds a right it has no other use
for, and on Azure the table belongs to whatever provisions the storage account. ``--create-table``
exists for Azurite, which starts empty on every container start and has no provisioning step to
belong to; it is idempotent, and it is what the local commands and the test harness pass.

    python table-storage/write-to-table-storage.py --create-table
    python table-storage/write-to-table-storage.py --reset
"""

import argparse
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJECT, "python-jobs"))

import azure_common as az  # noqa: E402  (the path above is what makes it importable)

LOG = logging.getLogger("write_to_table_storage")

# The declared configuration, imported from the AWS seeder rather than copied from it.
CONFIGURATIONS = az.load_aws_file("dynamodb/write-to-dynamo.py",
                                  "aws_write_to_dynamo").CONFIGURATIONS


def entity(record):
    """One DynamoDB-shaped record as a Table Storage entity. None-valued properties are left out.

    ``table_name`` becomes the RowKey rather than a property beside it, because it IS the key --
    carrying it twice would give the entity two places to disagree about which table it
    describes.
    """
    shaped = {"PartitionKey": az.CONFIG_PARTITION,
              "RowKey": az.check_row_key(record["table_name"])}
    for name, value in record.items():
        if name != "table_name" and value is not None:
            shaped[name] = value
    return shaped


def write(table=az.CONFIG_TABLE, create_table=False):
    """Write both entities in one transaction, replacing whatever was there. Returns them."""
    from azure.data.tables import TableServiceClient, UpdateMode

    if create_table:
        TableServiceClient.from_connection_string(
            az.connection_string(az.STORAGE_CONNECTION_ENV)).create_table_if_not_exists(table)
    entities = [entity(record) for record in CONFIGURATIONS]
    az.config_table(table).submit_transaction(
        [("upsert", item, {"mode": UpdateMode.REPLACE}) for item in entities])
    return entities


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--table", default=az.CONFIG_TABLE,
                        help="Table Storage table name (default: %(default)s)")
    parser.add_argument("--create-table", action="store_true",
                        help="create the table if it does not exist. For Azurite, which starts "
                             "empty; on Azure the table belongs to the storage account's "
                             "provisioning")
    parser.add_argument("--reset", action="store_true",
                        help="put both rows back to no last_extracted_value so the four-run demo "
                             "can be run again. Same REPLACE write as a plain seed -- the seeded "
                             "state IS the reset state -- so this only changes the log line")
    args = parser.parse_args()

    entities = write(args.table, args.create_table)
    LOG.info("%s: %s entities written to table %s in one transaction",
             "reset" if args.reset else "seeded", len(entities), args.table)
    for item in entities:
        LOG.info("   %-18s load_column=%-6s last_extracted_value=%s -> %s", item["RowKey"],
                 item.get("load_column"), item.get("last_extracted_value"),
                 "incremental" if item.get("load_column") else "full_load on every run")


if __name__ == "__main__":
    main()

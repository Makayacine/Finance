"""Create the landing-zone container in Azurite, if it is not there already.

None of the jobs creates the container it writes to or reads from, for the reason the AWS
extraction job gives about its bucket: a job that creates the thing it writes into holds a right it
uses on no normal day. On Azure the container belongs to whatever provisions the storage account.
Azurite starts empty on every container start and has no provisioning step, so this script is that
step, locally. It is idempotent.

    python local-development/create_container.py
"""

import argparse
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "python-jobs"))

import azure_common as az  # noqa: E402  (the path above is what makes it importable)

LOG = logging.getLogger("create_container")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--container", default=az.DEFAULT_CONTAINER,
                        help="container to create (default: %(default)s)")
    args = parser.parse_args()

    client = az.blob_container(args.container)
    if client.exists():
        LOG.info("container %s already exists", args.container)
    else:
        client.create_container()
        LOG.info("created container %s", args.container)


if __name__ == "__main__":
    main()

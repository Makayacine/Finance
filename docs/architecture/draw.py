"""Draw the four architecture diagrams in the root README with the official AWS service icons.

Needs the `diagrams` package and Graphviz's `dot` on PATH. From the repository root:

    python docs/architecture/draw.py
"""
from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.analytics import Athena, Glue
from diagrams.aws.compute import ECS, Fargate, Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.integration import Eventbridge, StepFunctions
from diagrams.aws.storage import S3
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.network import Internet

OUT = Path(__file__).parent


def diagram(name):
    # a white background keeps the black labels readable in GitHub's dark theme too
    return Diagram("", filename=str(OUT / name), show=False, direction="LR", outformat="png",
                   graph_attr={"bgcolor": "white", "pad": "0.4", "nodesep": "0.7", "ranksep": "0.9",
                               "splines": "spline"},  # the default ortho routing detaches edge labels
                   node_attr={"fontsize": "13"}, edge_attr={"fontsize": "12"})


with diagram("sba-credit-risk"):
    landing = S3("S3 landing\nnew/ + _READY")
    rule = Eventbridge("EventBridge\nkey = new/_READY")
    with Cluster("Step Functions"):
        validity = Fargate("ECS Fargate\ndata validity")
        model = Fargate("ECS Fargate\ncredit model\nSBA + FDIC chains")
        fail = StepFunctions("Fail state\nnames the cause")
        validity >> Edge(label="gate passed") >> model
        validity >> Edge(label="gate failed", style="dashed", color="firebrick") >> fail
    landing >> rule >> validity
    model >> Dynamodb("DynamoDB\nresults")

with diagram("latvia-company-credit"):
    raw = S3("S3 raw_zone/\n73 quarterly batches")
    tables = S3("Delta Lake: companies,\nproceedings, statements")
    raw >> Glue("Glue 5.0\nconditional MERGE") >> tables
    tables >> Edge(label="VERSION AS OF\neach 30 September") >> Glue("Glue 5.0\nWoE scorecard") \
        >> S3("Delta Lake:\nscored_companies")

with diagram("sovereign-curves"):
    sources = Internet("FedInvest, GSW, auctions,\nNY Fed, National Treasury")
    ci = GithubActions("GitHub Actions\ntest every PR,\ndeploy main via OIDC")
    with Cluster("AWS"):
        cron = Eventbridge("EventBridge\nweekdays")
        fetch = Lambda("Lambda\nfetch")
        raw = S3("S3 raw/")
        load = Glue("Glue Python Shell\nclean to dirty,\nflags, watermark")
        lake = S3("S3 lake/\nParquet")
        engine = ECS("ECS task: curve engine\nbootstrap, Svensson,\nDV01, key rates")
        athena = Athena("Athena")
        cron >> fetch >> raw >> load >> lake
        lake >> Edge(label="job SUCCEEDED") >> engine >> athena
    sources >> Edge(tailport="e", headport="n") >> fetch  # leave from the icon's side, not through its label
    ci >> Edge(style="dashed", color="gray") >> [fetch, load, engine]

with diagram("crypto-ticks"):
    raw = S3("S3 raw/\n340,971,834 trades")
    bars = Glue("Glue 4.0\n5-second bars, Steps 1-3")
    curated = S3("S3 curated/\n1,607,040 bars")
    with Cluster("The fork: one Glue job per path"):
        paths = [Glue("Path 3\nbandit"), Glue("Path 2\ncategorical"), Glue("Path 1\ncontinuous")]  # LR stacks bottom-up
    shell = Glue("Glue Python Shell")
    raw >> bars >> curated >> paths
    paths >> shell >> Dynamodb("DynamoDB")

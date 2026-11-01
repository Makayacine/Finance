"""The one list that drives the pipeline: entities, folders, file counts, dtypes and the gate.

Everything downstream reads ``ENTITIES``. The validator walks it to find the landing folders and
the dtype dict for each file, the gate compares its ``expected_files`` with what arrived, the
staging script maps source files onto it, and the model container asks it where each table
lives. Adding a sixth entity is one entry here and nothing anywhere else -- the state machine in
``step-functions/step-function.json`` does not name a single entity, because its Choice reads
one boolean the validator computes from this list.

    sba_loans            3 files   the SBA 7(a) FOIA loan book, one CSV per span of fiscal years
    fdic_failures        1 file    header: one row per failure or assistance event
    fdic_financials      2 files   child: quarterly filings of failed banks + every bank at year-end
    fdic_institutions    1 file    dimension: one row per CERT, names only, never a feature
    sba_recovery_rates   1 file    dimension: SBA's two published recovery tables, tidied

THE DTYPE RULE, AND THE ONE WIDENING IT ALLOWS
----------------------------------------------

The validator reads each file with pandas' own inference and compares the inferred dtype of
every declared column with the declaration, exactly. Two consequences shape the dicts below:

*   **A nullable integer is declared ``float64``.** pandas cannot hold a missing value in an
    ``int64`` column, so an integer column with one blank infers ``float64`` and an ``int64``
    declaration would reject a perfectly good file. ``CERT`` in the failures table is the
    clearest case: 488 events predate the certificate numbering and carry no CERT at all.
*   **A ``float64`` declaration also accepts ``int64``.** The converse happens file by file:
    ``LocationID`` has blanks in the FY2000-2009 and FY2010-2019 files and none in the
    FY2020-Present file, so the same column infers ``float64`` in two files and ``int64`` in the
    third. That is one column with one meaning, and the widening is the only one allowed. A
    string where a number was declared, or a number where a string was declared, still fails.

**Codes that look like numbers are read as strings.** A ZIP code is an identifier, and inference
turns ``02134`` into ``2134`` the moment a file happens to hold no letters in that column -- which
the FY2020-Present file does for ``BankZip``. ``string_columns`` forces those to ``str`` on read,
so the declared type and the meaning agree in every file.

Borrower name, street, city and ZIP sit in the SBA files and are listed in ``never_read``: a file
may carry them, the validator does not load them, and no other column outside the dict is
allowed. The model container drops them the same way. Some 7(a) borrowers are sole proprietors,
and those four columns would identify them.
"""

from dataclasses import dataclass, field

# The SBA FOIA extract is a snapshot. Every "by the as-of date" in this project means this day.
AS_OF = "2026-09-30"

LANDING = "new"  # new/<entity>/ is where an upload lands
ARCHIVE = "archived"  # archived/<entity>/ is where the model container leaves a processed batch
RESULTS = "results"  # results/<chain>/ holds the loan- and bank-level outputs
READY_MARKER = "new/_READY"  # written last by the upload; the EventBridge rule matches this key
RESULTS_TABLE = "sba_credit_risk_results"  # DynamoDB: partition key `chain`, sort key `item`

BORROWER_COLUMNS = ("BorrName", "BorrStreet", "BorrCity", "BorrZip")


@dataclass(frozen=True)
class Entity:
    name: str
    role: str
    expected_files: int
    source_glob: str  # relative to the data root the staging script is pointed at
    dtypes: dict
    string_columns: tuple = ()
    never_read: tuple = ()
    shared_source: bool = field(default=False)  # True: always staged from data/, not the root

    @property
    def landing(self):
        return f"{LANDING}/{self.name}"

    @property
    def ready(self):
        return self.name

    @property
    def archived(self):
        return f"{ARCHIVE}/{self.name}"


F, I, S = "float64", "int64", "str"

SBA_LOAN_DTYPES = {
    "AsOfDate": S, "Program": S, "LocationID": F, "BorrState": S,
    "BankName": S, "BankFDICNumber": F, "BankNCUANumber": F, "BankStreet": S, "BankCity": S,
    "BankState": S, "BankZip": S,
    "GrossApproval": F, "SBAGuaranteedApproval": F, "ApprovalDate": S, "ApprovalFY": I,
    "FirstDisbursementDate": S, "ProcessingMethod": S, "InitialInterestRate": F,
    "FixedorVariableInterestInd": S, "TermInMonths": F, "NaicsCode": F, "NaicsDescription": S,
    "FranchiseCode": S, "FranchiseName": S, "ProjectCounty": S, "ProjectState": S,
    "SBADistrictOffice": S, "CongressionalDistrict": F, "BusinessType": S, "BusinessAge": S,
    "LoanStatus": S, "PaidInFullDate": S, "ChargeOffDate": S, "GrossChargeOffAmount": F,
    "RevolverStatus": S, "JobsSupported": F, "CollateralInd": S, "SoldSecMrktInd": S,
}

FDIC_FAILURE_DTYPES = {
    "CERT": F, "NAME": S, "CITY": S, "PSTALP": S, "FAILDATE": S, "FAILYR": I, "RESTYPE": S,
    "RESTYPE1": S, "SAVR": S, "QBFDEP": F, "QBFASSET": F, "COST": F, "COSTMOSTRECENTASOF": S,
    "COMMENTS": S,
}

FDIC_FINANCIAL_DTYPES = {
    "CERT": I, "REPDTE": I, "ASSET": I, "DEP": F, "DEPINS": F, "DEPUNINS": F, "LNLSNET": F,
    "LNLSDEPR": F, "EQV": F, "ROA": F, "NIMY": F, "NCLNLSR": F, "NTLNLSR": F, "RBCRWAJ": F,
}

FDIC_INSTITUTION_DTYPES = {
    "CERT": I, "NAME": S, "CITY": S, "STALP": S, "BKCLASS": S, "CHARTER": I, "ACTIVE": I,
    "ESTYMD": S, "INSDATE": S, "ENDEFYMD": S,
}

RECOVERY_DTYPES = {
    "table": S, "program": S, "cohort_year": I, "fiscal_year": I, "rate_pct": F,
    "cohort_total_pct": F,
}

ENTITIES = (
    Entity("sba_loans", "loan book", 3, "sba/FOIA_7a_*.csv", SBA_LOAN_DTYPES,
           string_columns=("BankZip", "FranchiseCode"), never_read=BORROWER_COLUMNS),
    Entity("fdic_failures", "header", 1, "fdic/failures.csv", FDIC_FAILURE_DTYPES),
    Entity("fdic_financials", "child", 2, "fdic/financials_*.csv", FDIC_FINANCIAL_DTYPES),
    Entity("fdic_institutions", "dimension", 1, "fdic/institutions.csv", FDIC_INSTITUTION_DTYPES),
    Entity("sba_recovery_rates", "dimension", 1, "sba_recovery_rates.csv", RECOVERY_DTYPES,
           shared_source=True),
)


def entity(name):
    for candidate in ENTITIES:
        if candidate.name == name:
            return candidate
    raise KeyError(name)

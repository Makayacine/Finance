"""Every rule the chain applies, in one place: who counts, what a default is, when accounts exist.

The stage script, both Glue jobs, the tests and the notebook all import from here, so a rule is
changed once and every reader sees the change.

WHO COUNTS
----------
Companies only: the register types whose name is a firm's name and not a person's. Individual
merchants (IK), individual and family enterprises (IND, GIM) and farms and fishing holdings (ZEM,
ZVJ) carry their owners' names and are dropped on read, before anything is stored, together with
associations, foundations, churches, unions, parties, the media register and foreign branches.

WHAT A DEFAULT IS
-----------------
The first start of an insolvency, legal-protection or out-of-court legal-protection proceeding
that was not dismissed or left without action: Basel's "bankruptcy or similar protection". The
seven excluded resolution codes are the dismissal and left-without-action codes of the three
forms in the Register's own code sheet (insolvency_proceeding_kolonnu_apzimejumi.xlsx). The
insolvency-only label is the sensitivity.

WHEN ACCOUNTS EXIST
-------------------
``created_at`` is a real filing time only from 2021-01-18. Every statement the Register published
before then -- all 1,152,314 of them, for fiscal years 2006 to 2020 and 99.87% of them FY2019 or
earlier -- carries a stamp from the bulk load of 2020-08-11 to 2021-01-17, at a median of 17,534
a day on its 73 busiest days. Such a statement is dated nine months after its period end, the
embargo, and never later than its stamp, which is an upper bound on when it was filed. Nine
months is measured: of companies' accounts for December year ends in FY2020-24, 91.6-95.1% were
filed by 30 September.
"""

from datetime import date

# --- who counts -------------------------------------------------------------------------------
# SIA private limited company, AS joint-stock company, SE European company, KB cooperative
# society, PS general partnership, KS limited partnership.
COMPANY_TYPES = ("SIA", "AS", "SE", "KB", "PS", "KS")

# --- what a default is ------------------------------------------------------------------------
FORMS = ("INSOLVENCY", "LEGAL_PROTECTION", "OUT_OF_COURT_LEGAL_PROTECTION")
# M01 insolvency application dismissed, M12 left without action; T07 and T18 legal-protection
# application dismissed, T19 left without action; A09 out-of-court application dismissed, A12
# left without action.
DISMISSED = ("M01", "M12", "T07", "T18", "T19", "A09", "A12")

# --- money -------------------------------------------------------------------------------------
LVL_PER_EUR = 0.702804  # the fixed conversion rate of 1 January 2014; statements to FY2013 are LVL
SCALE = {"ONES": 1.0, "THOUSANDS": 1000.0}  # rounded_to_nearest

# --- when accounts exist ------------------------------------------------------------------------
BULK_LOAD_END = date(2021, 1, 18)  # first day on which created_at is a filing time
EMBARGO_MONTHS = 9
OBSERVATION_MONTH, OBSERVATION_DAY = 10, 1  # 1 October: nine months after a 31 December year end
HORIZON_MONTHS = 12
MAX_ACCOUNTS_AGE_MONTHS = 24  # the latest statement whose period ended at most this long ago

FIRST_OBSERVATION_YEAR = 2009  # the first statements in the file are for FY2008

# --- out-of-time windows, by observation year ----------------------------------------------------
TRAIN = (2009, 2017)
VALIDATE = (2018, 2020)
TEST = (2021, 2025)  # every statement used here carries a real filing time or predates the window
TRAIN_SENSITIVITY = (2011, 2017)  # 2009 and 2010 have 15.0% and 29.4% accounts coverage

# --- the replay --------------------------------------------------------------------------------
FIRST_BATCH = date(2008, 12, 31)  # everything dated on or before it arrives in the first batch

# --- source files: the only columns ever read ------------------------------------------------------
# The register's name, address and reregistration columns and the insolvency file's debtor
# identity columns are never in these lists, so no reader ever parses them.
REGISTER_COLUMNS = ("regcode", "type", "registered", "terminated", "closed")
PROCEEDING_COLUMNS = (
    "proceeding_id", "debtor_registration_number", "proceeding_started_on",
    "proceeding_ended_on", "proceeding_form", "proceeding_type",
    "proceeding_resolution_type", "entry_created_on",
)
STATEMENT_COLUMNS = (
    "id", "file_id", "legal_entity_registration_number", "source_schema", "source_type", "year",
    "year_started_on", "year_ended_on", "employees", "rounded_to_nearest", "currency", "created_at",
)
BALANCE_COLUMNS = (
    "statement_id", "cash", "accounts_receivable", "inventories", "total_current_assets",
    "fixed_assets", "intangible_assets", "total_non_current_assets", "total_assets",
    "current_liabilities", "non_current_liabilities", "provisions", "equity",
)
INCOME_COLUMNS = (
    "statement_id", "net_turnover", "by_nature_labour_expenses",
    "by_nature_depreciation_expenses", "interest_expenses", "income_before_income_taxes",
    "net_income",
)
AMOUNTS = BALANCE_COLUMNS[1:] + INCOME_COLUMNS[1:]

# Group (consolidated) accounts: a parent's statement for the whole group, filed beside its own.
CONSOLIDATED_SOURCE_TYPES = ("UKGP",)
CONSOLIDATED_SCHEMAS = ("DokUKGP2008v1",)

# The source file each lakehouse table is built from, and its raw_zone folder name.
SOURCES = {
    "companies": ("register",),
    "proceedings": ("insolvency_legal_person_proceeding",),
    "statements": ("financial_statements", "balance_sheets", "income_statements"),
}
SOURCE_COLUMNS = {
    "register": REGISTER_COLUMNS,
    "insolvency_legal_person_proceeding": PROCEEDING_COLUMNS,
    "financial_statements": STATEMENT_COLUMNS,
    "balance_sheets": BALANCE_COLUMNS,
    "income_statements": INCOME_COLUMNS,
}

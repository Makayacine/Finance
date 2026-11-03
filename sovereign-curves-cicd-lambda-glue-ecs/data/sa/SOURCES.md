# Sources

Source: National Treasury of the Republic of South Africa.

| File | What | From |
| --- | --- | --- |
| `nt_auctions.csv` | every fixed-rate government bond auction from 6 April 2010 to 29 September 2026: 2,386 rows on 811 auction dates, one row per bond auctioned, the figures as published, `quality_flags` where a check fails | the Treasury's 16 yearly files of fixed-rate bond auction results, 2010-13 to 2026-27: <https://investor.treasury.gov.za/Debt%20Operations%20and%20Data/Historical%20Results/Fixed-rate%20bonds/> |
| `nt_bonds.csv` | the 35 fixed-rate bonds listed in the 17 debt schedules read: coupon, initial issue date and price, interest dates as listed, the coupon cycle used, redemption dates, the leg a three-legged bond is priced on, nominal outstanding | the "Schedule of domestic government bonds" as at 31 March of each year from 2011 to 2026, and as at 30 September 2026: <https://investor.treasury.gov.za/Debt%20Operations%20and%20Data/Schedule%20of%20Domestic%20Debt/> |

Both files were written on 3 November 2026 by `local-development/sa_download.py`, which fetched the
Treasury's files and parsed them with the Glue job's own parsers.

The National Treasury permits its material to be downloaded and copied for educational and
non-commercial use, provided it is not modified or misrepresented, the National Treasury is
acknowledged as its source, and copyright notices stay intact. The Treasury's spreadsheets and PDFs
are therefore not in this repository, every row names the Treasury in its `source` column, and no
figure is altered: a figure that fails a check keeps its value and gains a flag. Two things are
restated without changing a figure: yields that a file gives as fractions (0.0757) are given in
percent, with `yield_unit = fraction` on the row, and the 2010-13 file's amounts in R million are
given in rand. The bond label as published is kept in `bond_label` beside the code read from it.

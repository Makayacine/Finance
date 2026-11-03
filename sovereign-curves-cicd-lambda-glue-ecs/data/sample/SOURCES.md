# Sources

The sample is October 2026, every business day, and the two crisis dates of the truth check,
15 September 2008 and 16 March 2020, and the two monthly South African series the auction curve is
checked against. The National Treasury's own rows are in `data/sa/`, with their terms. Each file is gzipped and sits at the key the fetch Lambda
writes it to in the raw bucket, so `local-development/run_local.py` stages it unchanged.

| Folder | What | Publisher | Licence |
| --- | --- | --- | --- |
| `raw/fedinvest/` | FedInvest end-of-day security prices, the page as returned for each date, 24 dates (12 October 2026, Columbus Day, has no prices) | US Department of the Treasury, Bureau of the Fiscal Service: <https://www.treasurydirect.gov/GA-FI/FedInvest/selectSecurityPriceDate> | US government work, public domain |
| `raw/auctions/` | Treasury Securities Auctions Data, the 1,765 auction records of the 965 CUSIPs on those pages | US Department of the Treasury, Fiscal Data: <https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query> | US government work, public domain |
| `raw/gsw/` | The Federal Reserve Board's Treasury yield curve (Gurkaynak, Sack and Wright), `feds200628.csv` as fetched on 2 November 2026, with its preamble and header and the 19 rows for the sample's dates; the file's last date is 23 October 2026 | Board of Governors of the Federal Reserve System: <https://www.federalreserve.gov/data/yield-curve-tables/feds200628.csv> | US government work, public domain |
| `raw/nyfed/` | SOFR, BGCR, TGCR and the SOFR averages and index for each sample date, as the Markets Data API returns them | Federal Reserve Bank of New York: <https://markets.newyorkfed.org/api/rates/secured/all/search.json> | the New York Fed's Terms of Use, below |
| `raw/oecd/` | Long-term interest rates for South Africa (IRLT), monthly from January 2010, the SDMX file as fetched on 3 November 2026 | OECD, Financial market dataset (DSD_STES@DF_FINMARK), source the South African Reserve Bank: <https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/ZAF.M.IRLT.PA.....> | the OECD's terms: any use, with the citation below |
| `raw/imf/` | Government bond yield for South Africa, monthly from January 2010, as FRED distributes it (series INTGSBZAM193N), fetched on 3 November 2026 | International Monetary Fund, International Financial Statistics: <https://fred.stlouisfed.org/series/INTGSBZAM193N> | the IMF's terms: publishing and distribution with the source named; commercial reuse needs the IMF's permission |

## Federal Reserve Bank of New York

© 2026 Federal Reserve Bank of New York. Content from the New York Fed subject to the Terms of
Use at newyorkfed.org.

The SOFR, BGCR, TGCR and SOFR Averages and Index data is subject to the Terms of Use posted at newyorkfed.org. The New
York Fed is not responsible for publication of the SOFR, BGCR, TGCR and SOFR Averages and Index data by Vince Mbanze, does
not sanction or endorse any particular republication, and has no liability for your use.

The Secured Overnight Financing Rate (SOFR) Data and Broad General Collateral Rate (BGCR) Data
are calculated using data provided under a license granted to the New York Fed by DTCC Solutions
LLC ("Solutions"), an affiliate of The Depository Trust & Clearing Corporation. Solutions, its
affiliates, and third parties from which they obtained data have no liability for the content of
this material.

The rates are as published, unmodified.

## OECD

OECD (2026), Financial market (DSD_STES@DF_FINMARK), long-term interest rates, South Africa,
South African Reserve Bank,
https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/ZAF.M.IRLT.PA.....
(accessed on 3 November 2026).

## International Monetary Fund

Source: International Monetary Fund, International Financial Statistics, government bond yield for
South Africa, via FRED series INTGSBZAM193N, https://fred.stlouisfed.org/series/INTGSBZAM193N.

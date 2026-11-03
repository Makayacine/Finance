"""Treasury curves and rate risk on top of the fixed income course's FIA module.

``FIA.py`` is vendored unchanged. The modules here add what it leaves out: calendar dates,
accrued interest, security static, a bucketed bootstrap, a Svensson fit and key-rate risk; and,
for South Africa, the JSE bond pricing formula, the National Treasury's auction files and debt
schedule, and a curve per auction.
"""

🐂 Bulls, Banks & Bollinger 📈
Quantitative Analysis of South African Bank Stocks

Project Overview
This project provides a comprehensive quantitative analysis of five major South African bank stocks listed on the JSE: Standard Bank (SBK.JO), FirstRand (FSR.JO), Nedbank (NED.JO), Absa Group (ABG.JO), and Capitec (CPI.JO). The analysis covers data acquisition, exploratory data analysis (EDA) including statistical and visual insights, and the backtesting of classical technical trading strategies. A key focus is on understanding return distributions, assessing risk metrics, and evaluating strategy performance with the crucial inclusion of transaction costs.

The notebook is designed to be a clear, step-by-step narrative, making the complex financial analysis accessible and easy to follow.

Objectives
Data Acquisition & Cleaning: Download 10 years of historical stock data (Close prices and Volume) from Yahoo Finance and perform robust cleaning to handle missing values, spurious zeros/negatives, and extreme outliers.

Exploratory Data Analysis (EDA):

Analyze daily return distributions (mean, standard deviation, skewness, kurtosis).

Perform Jarque-Bera normality tests to understand the shape of return distributions.

Visualize return distributions using histograms and QQ-plots.

Compute and visualize the correlation matrix between bank stock returns.

Calculate key risk metrics: Value at Risk (VaR), Conditional Value at Risk (CVaR), and Annualized Sharpe Ratios.

Strategy Backtesting:

Implement and backtest two classical technical trading strategies:

Moving Average (MA) Crossover (7-day / 21-day): A trend-following strategy.

Bollinger Bands (20-day, ±2σ): A mean-reversion strategy.

Evaluate strategy performance using metrics such as total return, annualized return, annualized volatility, Sharpe Ratio, and Maximum Drawdown.

Crucially, incorporate realistic transaction costs (0.10% per round-trip trade) into all strategy performance calculations for a practical assessment.

Performance Comparison: Compare the performance of the active trading strategies against a passive Buy & Hold benchmark.

Key Findings & Conclusions
Capitec's Outperformance: Capitec consistently demonstrated a unique profile with significantly higher annualized mean returns and the only positive Sharpe Ratio under a Buy & Hold strategy. Its high kurtosis indicates frequent extreme movements, but its returns historically compensated for this risk. It also showed lower correlation with other banks, suggesting diversification benefits.

Active Strategies Underperformed: Both the Moving Average Crossover and Bollinger Bands strategies generally failed to outperform the Buy & Hold benchmark for most banks, especially after accounting for transaction costs.

The MA Crossover often resulted in substantial negative net returns and Sharpe Ratios, indicating it's not well-suited for these assets or market conditions with the chosen parameters.

The Bollinger Bands strategy, while sometimes better than MA, also struggled to generate consistent positive risk-adjusted returns after transaction costs.

Critical Impact of Transaction Costs: The analysis vividly highlights how even a small transaction cost (0.10%) can severely erode profitability, turning theoretically positive returns into negative net results. This underscores the necessity of robust strategies with high win rates and/or low trading frequency to overcome real-world trading expenses.

Non-Normal Returns: The Jarque-Bera tests and QQ-plots confirmed that the daily returns for all bank stocks are not normally distributed, exhibiting "fat tails." This emphasizes the importance of using risk metrics like CVaR, which provide a more conservative view of potential losses in extreme market conditions.

How to Run the Notebook
To run this analysis on your local machine, follow these steps:

Prerequisites
Python 3.8+

pip (Python package installer) or conda (Anaconda/Miniconda)

Installation
Clone the Repository:

git clone https://github.com/Makayacine/Finance.git
cd "Finance/Bulls,Banks & Bollinger" # Navigate to the correct subdirectory

Create a Virtual Environment (Recommended):
Using conda:

conda create -n bulls_banks_bollinger python=3.9
conda activate bulls_banks_bollinger

Using venv:

python -m venv venv
.\venv\Scripts\activate # On Windows
source venv/bin/activate # On macOS/Linux

Install Dependencies:
Install all required Python libraries using pip:

pip install pandas numpy yfinance matplotlib seaborn scipy statsmodels tabulate

Running the Notebook
Start Jupyter Notebook:

jupyter notebook

Open the Notebook:
Your web browser will open, showing the Jupyter interface. Navigate to the directory where you saved Bulls, Banks & Bollinger.ipynb and open it.

Run Cells:
Execute the notebook cells sequentially from top to bottom. You can do this by clicking Cell -> Run All or by pressing Shift + Enter on each cell.

Project Structure
Bulls, Banks & Bollinger.ipynb: The main Jupyter Notebook containing all the analysis.

ma_crossover_performance.csv: Exported performance summary for the MA Crossover strategy.

bollinger_bands_performance.csv: Exported performance summary for the Bollinger Bands strategy.

buy_hold_performance.csv: Exported performance summary for the Buy & Hold benchmark.

risk_metrics_summary.csv: Exported summary of VaR, CVaR, and Sharpe Ratios.

README.md: This file.

Future Work
Parameter Optimization: Conduct a systematic grid search or use optimization algorithms to find optimal parameters for both strategies that maximize risk-adjusted returns.

Advanced Strategy Variations: Explore dynamic exit conditions, volatility filters, or combining multiple indicators for more robust signals.

Portfolio Construction: Investigate building diversified portfolios of these bank stocks using techniques like risk parity or minimum variance optimization.

Alternative Data Sources: Explore using data sources that reliably provide Adjusted Close prices for more accurate long-term return analysis.

Out-of-Sample Testing: Validate strategy performance on unseen data to prevent overfitting.

Author
Vince Mbanze

License
This project is open-source and available under the MIT License.

````markdown
# 🐂 Bulls, Banks & Bollinger 📈  
**Quantitative Analysis of South African Bank Stocks**

---

## 🔗 Contents  
1. [Project Overview](#project-overview)  
2. [Objectives](#objectives)  
3. [Key Findings & Conclusions](#key-findings--conclusions)  
4. [How to Run the Notebook](#how-to-run-the-notebook)  
5. [Project Structure](#project-structure)  
6. [Future Work](#future-work)  
7. [Author](#author)  
8. [License](#license)  

---

## 📝 Project Overview  
This project provides a comprehensive quantitative analysis of five major South African bank stocks listed on the JSE:  
- Standard Bank (`SBK.JO`)  
- FirstRand (`FSR.JO`)  
- Nedbank (`NED.JO`)  
- Absa Group (`ABG.JO`)  
- Capitec (`CPI.JO`)  

The analysis covers:  
- Data acquisition & cleaning  
- Exploratory Data Analysis (EDA)  
- Backtesting of technical trading strategies with transaction costs  

---

## 🎯 Objectives  

1. **Data Acquisition & Cleaning**  
   - Download 10 years of historical Close & Volume from Yahoo Finance  
   - Handle missing values, spurious zeros, and outliers  

2. **Exploratory Data Analysis (EDA)**  
   - Daily return distributions (mean, σ, skewness, kurtosis)  
   - Jarque–Bera normality tests  
   - Histograms & QQ-plots  
   - Correlation matrix of returns  
   - VaR, CVaR, and annualized Sharpe ratios  

3. **Strategy Backtesting**  
   - **MA Crossover (7/21-day)** – trend following  
   - **Bollinger Bands (20-day ±2 σ)** – mean reversion  
   - Include realistic transaction costs (0.10% per round-trip)  
   - Metrics: total return, annualized return, volatility, Sharpe, max drawdown  

4. **Performance Comparison**  
   - Compare active strategies vs. Buy & Hold benchmark  

---

## ✅ Key Findings & Conclusions  

- **Capitec’s Outperformance**  
  - Highest annualized mean returns, only positive Sharpe under Buy & Hold  
  - High kurtosis ⇒ fat tails but compensated by returns  
  - Lower correlation with other banks ⇒ diversification benefit  

- **Active Strategies Underperformed**  
  - MA Crossover often produced negative net returns after costs  
  - Bollinger Bands slightly better but inconsistent post-costs  

- **Transaction Costs Matter**  
  - 0.10% per trade erodes theoretical gains  
  - Highlights need for high win-rate or low-frequency strategies  

- **Non-Normal Returns & Tail Risk**  
  - Returns exhibit fat tails ⇒ CVaR preferred for risk assessment  

---

## 🚀 How to Run the Notebook  

### 1. Prerequisites  
- Python 3.8+  
- pip or conda  

### 2. Clone the Repository  
```bash
git clone https://github.com/Makayacine/Finance.git
cd "Finance/Bulls, Banks & Bollinger"
````

### 3. Create & Activate Virtual Environment

```bash
# Conda
conda create -n bulls_banks_bollinger python=3.9
conda activate bulls_banks_bollinger

# or venv
python -m venv venv
.\venv\Scripts\activate   # Windows
source venv/bin/activate   # macOS/Linux
```

### 4. Install Dependencies

```bash
pip install pandas numpy yfinance matplotlib seaborn scipy statsmodels tabulate
```

### 5. Run the Notebook

```bash
jupyter notebook
```

Open `Bulls, Banks & Bollinger.ipynb` and run all cells.

---

## 📁 Project Structure

```
Bulls, Banks & Bollinger/  
├── Bulls, Banks & Bollinger.ipynb  
├── README.md  
```

---

## 🔭 Future Work

* **Parameter Optimization:** Grid search or Bayesian methods
* **Advanced Signals:** Dynamic exit rules, volatility filters
* **Portfolio Construction:** Risk parity, minimum variance
* **Alternative Data:** Adjusted Close, macro indicators
* **Out-of-Sample Testing:** Prevent overfitting

---

## ✍️ Author

Vince Mbanze


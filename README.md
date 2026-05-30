# Alpaca_AI

Alpaca_AI is a Python toolkit for Alpaca paper trading experiments. It includes safe order helpers, intraday setup scanning, breakout strategy backtesting, and an automated Git/GitHub deployment workflow.

> This project is for research and paper trading only. It is not financial advice and does not guarantee trading performance.

## Features

- Alpaca paper trading client setup through environment variables
- Market buy workflow with trailing stop protection
- Thread-safe symbol trade guard to avoid duplicate local orders
- API rate limiter with retry handling for rate-limit responses
- Intraday momentum, gap, volume spike, ATR, and liquidity filters
- Long-only breakout strategy backtesting with Pandas
- Paper-trade execution helpers with risk controls
- GitHub Actions CI for linting and tests
- `deploy.sh` / `deploy.ps1` scripts for Conventional Commit based pushes

## Project Structure

```text
.
├── alpaca_trading_agent.py      # Alpaca credentials, safe trade executor, order helpers
├── intraday_scanner.py          # Intraday setup scanner and signal scoring
├── paper_trade_from_scan.py     # Paper-trade execution from scan results
├── strategy_backtest.py         # CSV-based breakout strategy backtester
├── tests/                       # Unit tests
├── requirements.txt             # Runtime dependencies
├── pytest.ini                   # Pytest import path config
├── deploy.sh                    # Mac/Linux automated commit and push script
├── deploy.ps1                   # Windows automated commit and push script
└── .github/workflows/main.yml   # GitHub Actions CI
```

## Requirements

- Python 3.9+
- Alpaca paper trading account
- Git and GitHub SSH access

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install pytest ruff
```

## Environment Variables

Create a local `.env` file:

```bash
ALPACA_API_KEY=your_alpaca_api_key
ALPACA_SECRET_KEY=your_alpaca_secret_key
```

`.env` is ignored by Git and must not be committed.

## Usage

Check Alpaca account status:

```bash
python alpaca_trading_agent.py
```

Run a CSV breakout backtest:

```bash
python strategy_backtest.py path/to/ohlcv.csv --trades-out output/trades.csv
```

Run tests:

```bash
pytest -q
```

Run lint checks:

```bash
ruff check .
```

## Automated Version Control

Mac/Linux:

```bash
./deploy.sh
```

Windows PowerShell:

```powershell
.\deploy.ps1
```

Commit messages must follow Conventional Commits:

```text
feat(scanner): add new setup filter
fix(ci): configure pytest import path
docs: update README
chore: update dependencies
```

## CI

GitHub Actions runs on pushes and pull requests to `main` or `master`:

- Install Python dependencies
- Run `ruff check .`
- Run `pytest -q`

## Safety Notes

- The default Alpaca client is configured for paper trading.
- Keep `.env`, API keys, private keys, and generated runtime state out of Git.
- Review orders manually before adapting this code for any real-money workflow.
- Backtests and scans are historical or rule-based tools; they do not predict future returns.

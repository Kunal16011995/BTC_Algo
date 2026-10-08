# Systematic Trading Backtest Framework

Use Robert Carver's systematic trading framework from `C:\Dhan Algo\Systematic Trading Rules\Systematic Trading PDF.pdf` as the default standard for every trading strategy backtested in this project.

## Core principles

- Prefer simple, explainable, hypothesis-driven rules.
- Treat backtesting as research, not proof of future profitability.
- Separate trading rules from position sizing and portfolio allocation.
- Standardize forecasts and position sizes using volatility.
- Optimize for robustness, diversification, and low turnover—not the best historical result.
- Evaluate all strategies after realistic trading costs.
- Never hard-code timeframe, session, stop, target, or direction for a generic framework. Treat them as **strategy parameters** to be justified by the paper and tested for robustness.

## Required strategy definition

Before backtesting any strategy, capture:

1. Market edge or hypothesis
2. Instruments and universe
3. Timeframe and holding period
4. Entry rules
5. Exit rules
6. Parameters and why they were chosen
7. Expected market regime
8. Long-only, short-only, or both
9. Starting capital
10. Commission, spread, slippage, and other cost assumptions

## Project-specific defaults for this repo

The following defaults apply to **every** strategy backtested in this project unless explicitly changed by the user for a specific test:

- **Instrument:** BTCUSD (BTCUSD inverse perpetual on Bybit) or BTCUSDT linear perpetual when the paper/data source requires it.
- **Starting capital for research backtests:** $10,000 USD unless the user specifies otherwise.
- **Forward-testing capital:** use the actual equity available in the live/demo broker account, divided as required by the strategy variants. Do not use a fixed $10k for live or paper-forward KPIs unless the user explicitly requests it.
- **Cost assumptions:** Bybit BTCUSD/BTCUSDT perpetual trading fees.
  - **Default realistic assumption:** 0.055% taker on entry and 0.055% taker on exit for market orders on the demo/retail tier.
  - Only use a lower maker-entry assumption if the strategy explicitly places limit orders and you can verify fills.
  - Include slippage (observed vs intended entry/exit price) when reporting live results.
  - Include funding costs if the holding period crosses funding intervals.
  - Document the exact fee tier and any slippage assumption used.

The following are **not** project defaults. They must be taken from the research paper or treated as optimisation parameters:

- Timeframe (e.g., M5, M15, H1, H4, daily).
- Trading session filter (e.g., London, NY, overlap, 24-hour).
- Direction (long-only, short-only, both).
- Stop-loss %, take-profit %, runner distances, proximity thresholds.
- Any other rule-specific parameter (lookback, body threshold, FVG lookforward, etc.).

## Parameter discovery and optimisation

For every new strategy, define a clear set of candidate parameters and test them **without overfitting**.

### Parameters to consider

Always ask whether the paper's rules depend on any of the following. If they do, treat them as candidate parameters:

| Category | Examples |
|----------|----------|
| Time structure | Bar timeframe, session filter, lookback/lookforward windows. |
| Direction | Long-only, short-only, both. |
| Entry filter | Body-ratio thresholds, momentum-leg definitions, FVG/proximity windows. |
| Risk/exit | Stop-loss %, TP1 %, runner distances, trailing-stop logic, daily loss cap %. |
| Higher-timeframe | HTF proximity %, HTF source (EMA, POC, swing levels). |
| Cost model | Maker/taker split, fixed per-trade cost, funding. |

### Optimisation process

1. **Start with the paper's recommended values** as the baseline.
2. **Define a small, economically sensible range** around each key parameter (e.g., ±20%). Do not scan a massive grid and pick the best Sharpe.
3. **Run an in-sample / out-of-sample split**:
   - Fit or verify on the in-sample period (e.g., 2019–2023).
   - Test on the out-of-sample period (e.g., 2024–today).
4. **Run a parameter sensitivity sweep** on the OOS period: change one parameter at a time or use small joint variations. The edge should not collapse.
5. **Use walk-forward analysis** when enough data exists to confirm stability across different regimes.
6. **Choose the parameter set for robustness**, not the highest historical Sharpe. Prefer sets with:
   - Stable Sharpe/CAGR across parameter perturbations.
   - Acceptable drawdown and turnover.
   - Lower cost drag.
7. **Document every parameter tested**, the range, and why the final set was selected.

### What to avoid

- Avoid selecting parameters by scanning large grids and choosing the best result.
- Avoid parameters that work only in one regime or one instrument.
- Avoid excessive turnover that consumes more than one-third of the pre-cost Sharpe.

## Default data sources

Use free historical data first, but document the exact symbol, provider, timezone, and any proxy limitations in every backtest.

- **Primary BTC data source:** Bybit public API (`BTCUSDT` or `BTCUSD` linear/inverse perpetual klines). Document the maximum available history (approximately 2020–today for Bybit BTCUSDT).
- **Alternative / cross-check:** Binance `BTCUSDT` public klines or Binance Vision archives (history starts in 2017).
- **Daily data:** Yahoo Finance `BTC-USD` for long daily history.
- **XAUUSD-related strategies:** use `GC=F` gold futures as the default free XAUUSD proxy; cross-check with `GLD` when useful. If the paper requires true spot XAUUSD or intraday spot data, use user-provided broker/MT4/MT5 export data or another documented spot source.
- Cross-check data for gaps, timezone misalignment, missing candles, and abnormal prices before running the backtest.

If the paper does not define a rule, parameter, timeframe, or cost assumption precisely enough to reproduce the strategy, flag the ambiguity before backtesting. Any assumption needed to fill a gap must be explicitly documented in the backtest output.

## Backtest discipline

- Use distinct fitting and testing periods.
- Prefer rolling or expanding out-of-sample tests.
- Keep tested rule variations limited and purposeful.
- Pool related instruments when appropriate to reduce instrument-specific overfitting.
- Use enough history to judge the strategy; preferably 10+ years when data is available.
- Avoid survivorship bias, lookahead bias, unrealistic fills, and ignored corporate actions.

## Forecast rules

- Forecasts should be scaled, not binary.
- Rescale raw forecasts so the average absolute value is approximately `10`.
- Cap individual and combined forecasts at `-20` to `+20` unless the user explicitly defines another range.
- When combining multiple rules, document forecast weights and ensure positive weights sum to 100%.
- Apply a diversification multiplier when combining uncorrelated forecasts, with a normal maximum of `2.5`.

## Volatility targeting and position sizing

- Define the annual volatility target before sizing positions.
- Convert annual cash volatility target to daily using approximately `/ 16`.
- Estimate instrument volatility from historical returns, normally using EWMA or a fixed lookback such as 25–36 trading days.
- Position size must account for forecast strength, instrument volatility, block value, currency, and portfolio weight.
- Avoid oversized positions in unusually low-volatility instruments.
- Document the exact position-sizing formula used.

## Portfolio construction

- Check correlations across instruments and strategies.
- Prefer diversified combinations over concentrated bets.
- Apply instrument diversification multipliers when appropriate.
- Use position inertia to avoid tiny unnecessary trades; by default, do not trade if the current position is within 10% of target.
- Check liquidity, skew, minimum trade size, and instrument access.

## Cost and turnover requirements

- Include realistic commissions, spreads, slippage, and borrow costs where relevant.
- Report turnover and annualized cost drag.
- Be skeptical if costs consume more than one third of expected pre-cost Sharpe ratio.
- Prefer lower-frequency designs when costs materially reduce net performance.

## Required backtest output

Every backtest summary should include:

- Strategy hypothesis
- Instruments and date range
- Rules and parameters
- In-sample/out-of-sample methodology
- Forecast scaling and caps
- Volatility target and position-sizing method
- Cost assumptions
- Number of trades and turnover
- CAGR
- Annualized volatility
- Net Sharpe ratio
- Maximum drawdown
- Recovery time
- Win rate
- Profit factor
- Return skew
- Exposure
- Parameter sensitivity
- Main overfitting risks
- Verdict: reject, iterate, or paper trade

Also save equity-curve image files for **every strategy variant** in the backtest output folder, normally as JPG. Each image should include a legend with strategy names, the starting-capital reference line, a clear date axis, and final equity values. If final equity values span a wide range, use a log-scale y-axis or provide both linear and log versions.

## Forward testing and live trading

- Run the strategy in **virtual paper mode** first. Compare live/paper performance to the backtest before risking capital.
- When moving to live execution:
  - Start with one variant on a demo account.
  - Use the exact same parameters selected in the OOS test.
  - Record real closed-PnL and compare to backtested costs.
  - Monitor for slippage, partial fills, and API/rate-limit issues.
- Keep a clear log of every live trade and any deviation from the backtest rules.

## Operational lessons (apply to every strategy)

### Leverage and margin

- Fixed-dollar position sizing can produce a notional position larger than the allocated cash.
- For live/paper trading, set leverage so that `intended position size <= allocated capital × leverage`.
- Cap live position size to `allocated capital × leverage` and log when capping occurs.
- Default to 1× leverage only if the position-sizing formula guarantees `position size <= allocated capital`.

### API resilience and rate limits

- Public market-data endpoints (e.g., Bybit `/v5/market/kline`) can return rate-limit errors (`10006`).
- Implement retry logic with exponential backoff:
  - At least 6 retries.
  - Initial backoff ≥ 5 seconds.
  - Cap backoff at 60 seconds.
- Add a small delay (e.g., 0.5s) between consecutive API calls in the same loop to avoid bursts.
- Cache history when possible; do not refetch the full history more often than necessary.
- Prefer deriving higher-timeframe bars from lower-timeframe bars (e.g., derive H1 from M15 by resampling) instead of making separate klines requests. This cuts public API calls in half.

### Live trade reconciliation

- Do not rely on a single order ID to match live entries and exits.
- Broker stop-loss / take-profit fills often receive a **new order ID**.
- Match closed-PnL records to open live trades by:
  - Quantity (exact)
  - Average entry price (within small tolerance)
  - Opposite closing side
  - Exit time after entry time
- Persist open live trades to local storage so restarts do not lose track of positions.

### Session filters

- Add a session-filter parameter to the strategy engine (e.g., `Asia`, `London`, `Overlap`, `New York`, or `None` for all).
- Treat session as an optimisation dimension: test each session independently and compare net Sharpe, drawdown, and trade frequency.
- Document the session windows used (UTC and local time).

### Required trade-level KPIs

Every backtest and forward-test dashboard must report, at minimum:

- Best Win
- Worst Loss
- Best R:R
- Worst R:R
- Avg Win
- Avg Loss
- Consecutive Wins
- Consecutive Losses

Compute R multiples from the strategy’s recorded risk amount when available; fall back to `PnL / intended risk per trade` for live/paper trades where exact risk is not stored.

## Decision standard

Reject or iterate when the strategy has:

- No clear economic rationale
- Poor out-of-sample performance
- Excessive parameter sensitivity
- Unrealistic cost assumptions
- Too few trades to judge reliability
- Heavy dependence on one instrument, period, or regime
- Excessive turnover or cost drag
- Negative skew that is unexplained or unmanaged
- Position sizes that are unstable or impractical to execute

Do not present a strategy as promising unless the net, out-of-sample results are credible and the main risks are explicitly documented.

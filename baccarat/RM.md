-CSV re-sequence

                 -----------------Summary of Improvements----------------------------
Feature	                                                            Implementation
Progression selection	Dropdown in Settings: Default (Martingale-like), Fibonacci, D’Alembert.
Weighted decision                   	Exponential decay over last 20 decisive results, threshold 0.2.
Trailing stop loss	     	Stops when drawdown exceeds configured percentage (default 25%).
Profit target		Configurable, stops auto modes when reached.
CD score modulation		Reduces bet if CD score > threshold (default 2000) by factor (0.5).
Loss streak cooldown	Skips one round after N consecutive losses (default 3).
Long streak reduction	Cuts bet by 30% when same result appears 4+ times in a row.
Tie handling		After a Green (tie), bets opposite of previous result.
UI feedback		Progression display shows current state (e.g., Fibonacci: 5+8=13).

All new settings are saved in bacart_settings.json and survive restarts. The script remains fully compatible with existing calibration and CSV logging.

-Added strategies follow_streak, opposite_streak, majority, alternate, randomize, follow_trend, pattern_follow, weighted
-Removed cd_score_threshold and cd_bet_reduction_factor from DEFAULT_SETTINGS.
-Flow fixed
-Cursor changed to crosshair

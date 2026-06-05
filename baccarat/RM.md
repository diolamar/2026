Key Improvements Implemented


---Configurable Progression & Limits---
	->Added Settings dialog (button next to Exit) to edit progression steps, max bet, stop loss, and auto idle seconds.
	->Settings saved to bacart_settings.json.
	->Auto betting checks max_bet and stop_loss; triggers emergency stop if breached.

---Individual Point Recalibration---
	->New button Recalibrate Point opens a list of all points; select one and click on the frozen screen to update only that point.

---Non‑Blocking Bet Clicks---
	->_place_bet now uses after() to stagger clicks instead of time.sleep(), preventing UI freeze.

---Keyboard Emergency Stop---
	->Pressing Escape stops all automation, clears pending bets, and halts monitoring.

---Bounds Checking in _resolve_bet---
	->Verifies that pending_bet_basis_len is within valid range before accessing snapshot.sequence.

---Improved Color Matching Constants---
	->Soft‑matching thresholds now come from settings (can be adjusted in JSON if needed; not exposed in UI for simplicity but can be added).

---Added Color Enum (for future use, not fully integrated but present).---

---UI Enhancements---
	->Added Settings button.
	->Progression display warns if current step exceeds max bet.

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

->Added strategies follow_streak, opposite_streak, majority, alternate, randomize, follow_trend, pattern_follow, weighted
->Removed cd_score_threshold and cd_bet_reduction_factor from DEFAULT_SETTINGS.
->Flow fixed
->Cursor changed to crosshair
->json files relocated at files folder
->Removed Clear, Save, Load buttons, anmake calibration auto save and auto load.
->Added human like clicking, offset click to PLR and BNR
->follow pattern, it skips 3-5 rounds randomly after lose streak reaches 6
->It skips also after 3 lose streaks once only 
-Added logging in terminal
-Fixed .txt file when in compliling with exe not found.




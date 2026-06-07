import csv
import random
import sys
from pathlib import Path
from datetime import datetime, timedelta

SCRIPT_DIR = Path(__file__).parent.resolve()
OUTPUT_CSV = SCRIPT_DIR / "results_generated.csv"

PROB_BLUE = 0.45
PROB_RED = 0.45
PROB_GREEN = 0.10
PROGRESSION_STEPS = [10, 20, 40, 80, 150, 290, 460, 900, 1400, 2200, 3800, 6000, 10000]
LOSS_STREAK_COOLDOWN = 3
PATTERN_FOLLOW_SKIP_MIN = 3
PATTERN_FOLLOW_SKIP_MAX = 5

# ---------- Strategy Functions ----------
def fibonacci_step(progression_idx):
    fib = [1,1,2,3,5,8,13,21,34,55,89,144,233]
    idx = min(progression_idx, len(fib)-1)
    return fib[idx] * 10

def dalembert_step(progression_idx, base=10):
    return base + progression_idx * 10

def choose_side(history, strategy="follow_streak"):
    if not history:
        return random.choice(("PLR", "BNR"))

    if strategy == "follow_streak":
        last = history[-1]
        return "PLR" if last == "Blue" else "BNR"
    elif strategy == "opposite_streak":
        last = history[-1]
        return "BNR" if last == "Blue" else "PLR"
    elif strategy == "majority":
        recent = history[-6:]
        blue = sum(1 for x in recent if x == "Blue")
        red = len(recent) - blue
        if blue > red:
            return "PLR"
        elif red > blue:
            return "BNR"
        else:
            return random.choice(("PLR", "BNR"))
    elif strategy == "alternate":
        return "PLR" if (len(history) % 2 == 0) else "BNR"
    elif strategy == "randomize":
        return random.choice(("PLR", "BNR"))
    elif strategy == "follow_trend":
        recent = history[-5:]
        blue = sum(1 for x in recent if x == "Blue")
        red = len(recent) - blue
        if blue > red:
            return "PLR"
        elif red > blue:
            return "BNR"
        else:
            return random.choice(("PLR", "BNR"))
    elif strategy == "pattern_follow":
        if len(history) >= 3:
            last_three = history[-3:]
            if last_three[0] == last_three[2] and last_three[0] != last_three[1]:
                last = history[-1]
                return "BNR" if last == "Blue" else "PLR"
        if len(history) >= 2 and history[-1] == history[-2]:
            last = history[-1]
            return "PLR" if last == "Blue" else "BNR"
        last = history[-1]
        return "PLR" if last == "Blue" else "BNR"
    else:
        return random.choice(("PLR", "BNR"))

def get_bet_amount(progression_idx, progression_type):
    step = min(progression_idx, len(PROGRESSION_STEPS)-1)
    if progression_type == "martingale":
        return PROGRESSION_STEPS[step]
    elif progression_type == "fibonacci":
        return fibonacci_step(progression_idx)
    else:
        return dalembert_step(progression_idx)

def generate_synthetic_row(
    counter,
    profit,
    progression_idx,
    last_non_tie,
    consecutive_losses,
    loss_limit,
    progression_type,
    side_strategy,
    skip_blocks_left,
    cooldown_skip_active,
    pattern_skip_armed,
    resolved_bets,
):
    r = random.random()
    if r < PROB_BLUE:
        outcome = "Blue"
    elif r < PROB_BLUE + PROB_RED:
        outcome = "Red"
    else:
        outcome = "Green"

    side = ""
    amount = 0
    event = ""
    profit_change = 0
    new_progression = progression_idx
    new_consecutive_losses = consecutive_losses
    result_text = outcome
    note = ""
    new_skip_blocks_left = skip_blocks_left
    new_cooldown_skip_active = cooldown_skip_active
    new_pattern_skip_armed = pattern_skip_armed
    new_resolved_bets = resolved_bets

    if side_strategy == "pattern_follow":
        if consecutive_losses < LOSS_STREAK_COOLDOWN:
            new_cooldown_skip_active = False

        if (
            LOSS_STREAK_COOLDOWN > 0
            and consecutive_losses == LOSS_STREAK_COOLDOWN
            and not cooldown_skip_active
        ):
            event = "skip"
            note = f"loss streak {consecutive_losses} cooldown"
            new_cooldown_skip_active = True
        elif skip_blocks_left > 0:
            event = "skip"
            note = f"pattern_follow streak skip ({skip_blocks_left} left)"
            new_skip_blocks_left = skip_blocks_left - 1
        elif consecutive_losses >= 6 and pattern_skip_armed:
            new_skip_blocks_left = random.randint(PATTERN_FOLLOW_SKIP_MIN, PATTERN_FOLLOW_SKIP_MAX)
            event = "skip"
            note = f"pattern_follow streak skip ({new_skip_blocks_left} left)"
            new_skip_blocks_left -= 1
            new_pattern_skip_armed = False
        else:
            side = choose_side(last_non_tie, side_strategy)
            amount = get_bet_amount(progression_idx, progression_type)
            expected = "Blue" if side == "PLR" else "Red"

            new_resolved_bets += 1
            if outcome == "Green":
                event = "tie"
                profit_change = 0
                new_progression = progression_idx
                new_consecutive_losses = 0
                result_text = "Green"
            elif outcome == expected:
                event = "win"
                profit_change = amount
                new_progression = 0
                new_consecutive_losses = 0
                result_text = outcome
                new_cooldown_skip_active = False
                new_skip_blocks_left = 0
                new_pattern_skip_armed = False
            else:
                event = "lose"
                profit_change = -amount
                new_consecutive_losses = consecutive_losses + 1
                new_progression = min(progression_idx + 1, len(PROGRESSION_STEPS)-1)
                result_text = outcome
                if new_consecutive_losses >= 6:
                    new_pattern_skip_armed = True
    else:
        # For other strategies, no skip logic
        side = choose_side(last_non_tie, side_strategy)
        amount = get_bet_amount(progression_idx, progression_type)
        expected = "Blue" if side == "PLR" else "Red"

        new_resolved_bets += 1
        if outcome == "Green":
            event = "tie"
            profit_change = 0
            new_progression = progression_idx
            new_consecutive_losses = 0
            result_text = "Green"
        elif outcome == expected:
            event = "win"
            profit_change = amount
            new_progression = 0
            new_consecutive_losses = 0
            result_text = outcome
        else:
            event = "lose"
            profit_change = -amount
            new_consecutive_losses = consecutive_losses + 1
            new_progression = min(progression_idx + 1, len(PROGRESSION_STEPS)-1)
            result_text = outcome

    profit += profit_change
    new_history = last_non_tie[:]
    if outcome != "Green":
        new_history.append(outcome)

    # Optional random notes for non-skip events
    if event != "skip" and not note and random.random() < 0.1:
        if event == "win":
            note = random.choice(["streak blue->plr", "tie blue->plr", "blue 3-1->plr"])
        elif event == "lose":
            note = random.choice(["streak red->bnr", "tie red->bnr", "red 1-3->bnr"])
        else:
            note = random.choice(["cd red", "cooldown wait_reset cd=red"])

    box_num = (counter - 1) % (6*18)
    row_num = (box_num % 6) + 1
    col = (box_num // 6) + 1
    round_box = f"R{row_num}C{col}"
    timestamp = (datetime.now() + timedelta(seconds=counter * random.randint(20, 40))).strftime("%Y-%m-%d %H:%M:%S")
    hit_rate = 0.0

    # FIX: use new_progression (after the round) instead of progression_idx (old)
    row = (
        timestamp,
        counter,
        round_box,
        result_text,
        side,
        amount,
        event,
        profit,
        new_progression,
        new_resolved_bets,
        hit_rate,
        note,
    )
    return (
        row,
        new_progression,
        new_history,
        profit,
        new_consecutive_losses,
        note,
        new_skip_blocks_left,
        new_cooldown_skip_active,
        new_pattern_skip_armed,
        new_resolved_bets,
    )

def generate_synthetic(output_csv, num_rows, stop_loss, take_profit, loss_limit,
                       progression_type, side_strategy):
    rows = []
    profit = 0
    progression_idx = 0
    history = []
    consecutive_losses = 0
    stop_reason = None
    skip_blocks_left = 0
    cooldown_skip_active = False
    pattern_skip_armed = False
    resolved_bets = 0

    for counter in range(1, num_rows + 1):
        (
            row,
            new_prog,
            new_hist,
            new_profit,
            new_losses,
            note,
            new_skip_blocks,
            new_cooldown_skip_active,
            new_pattern_skip_armed,
            new_resolved_bets,
        ) = generate_synthetic_row(
            counter, profit, progression_idx, history, consecutive_losses,
            loss_limit, progression_type, side_strategy, skip_blocks_left,
            cooldown_skip_active, pattern_skip_armed, resolved_bets
        )
        triggered = False
        if new_profit <= stop_loss:
            stop_reason = f"stop loss reached (profit={new_profit} <= {stop_loss})"
            triggered = True
            if not note:
                note = f"stop loss at {new_profit}"
            row = list(row)
            row[-1] = note
            row = tuple(row)
        elif new_profit >= take_profit:
            stop_reason = f"take profit reached (profit={new_profit} >= {take_profit})"
            triggered = True
            if not note:
                note = f"take profit at {new_profit}"
            row = list(row)
            row[-1] = note
            row = tuple(row)
        elif new_losses >= loss_limit:
            stop_reason = f"loss limit reached ({new_losses} consecutive losses)"
            triggered = True
            if not note:
                note = f"loss limit reached ({new_losses})"
            row = list(row)
            row[-1] = note
            row = tuple(row)

        rows.append(row)
        progression_idx = new_prog
        history = new_hist
        profit = new_profit
        consecutive_losses = new_losses
        skip_blocks_left = new_skip_blocks
        cooldown_skip_active = new_cooldown_skip_active
        pattern_skip_armed = new_pattern_skip_armed
        resolved_bets = new_resolved_bets

        if triggered:
            break

    with open(output_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp","counter","round_box","result","bet_side","bet_amount",
                         "event","profit_total","progression_step","resolved_bets","hit_rate","note"])
        writer.writerows(rows)

    print(f"Generated {len(rows)} rows -> {output_csv}")
    if stop_reason:
        print(f"Stopped because: {stop_reason}")
    else:
        print(f"Completed {num_rows} rows without hitting stop conditions.")

if __name__ == "__main__":
    num = int(input("Rounds to simulate (20000): ") or "20000")
    stop_loss = int(input("Stop loss (default -5000): ") or "-5000")
    take_profit = int(input("Take profit (default 2000): ") or "2000")
    loss_limit = int(input("Loss limit (reset after N losses, default 10): ") or "10")

    print("\n--- Strategy Options ---")
    prog_type = input("Progression (martingale/fibonacci/dalembert) [martingale]: ").lower() or "martingale"

    print("\nSide selection strategies:")
    print("1. follow_streak")
    print("2. opposite_streak")
    print("3. majority")
    print("4. alternate")
    print("5. randomize")
    print("6. follow_trend")
    print("7. pattern_follow (skip once at 3 losses, then skip random 3-5 rounds after 6+ losses)")
    side_choice = input("Enter number (1-7) [default 1]: ") or "1"
    side_map = {
        "1": "follow_streak",
        "2": "opposite_streak",
        "3": "majority",
        "4": "alternate",
        "5": "randomize",
        "6": "follow_trend",
        "7": "pattern_follow"
    }
    side_sel = side_map.get(side_choice, "follow_streak")

    print(f"\nSimulating with: stop_loss={stop_loss}, take_profit={take_profit}, loss_limit={loss_limit}")
    print(f"Progression={prog_type}, side_strategy={side_sel}")

    generate_synthetic(OUTPUT_CSV, num, stop_loss, take_profit, loss_limit,
                       prog_type, side_sel)


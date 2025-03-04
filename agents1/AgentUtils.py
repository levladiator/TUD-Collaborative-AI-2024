import logging

def compute_collected_adjustments(victim_name: str, base_adjustment: float):
    if "mildly" in victim_name:
        return base_adjustment, base_adjustment
    elif "critically" in victim_name:
        return base_adjustment * 2, base_adjustment * 2

def log_info(use: bool, message: str):
    logging.basicConfig(level=logging.INFO)
    if use:
        logging.info(message)

def calculate_wait_time(distance_to_human: str, taskTrustBelief: dict) -> int:
    willingness = taskTrustBelief['willingness'] + 1
    competence = taskTrustBelief['competence'] + 1
    multiplier = 1.5 if distance_to_human == 'close' else 2

    # Willingness to help is more important than competence
    # The time increases quadratically with the trustworthiness
    # We have a minimum wait time of 10 seconds
    # The wait time will be bigger if the human is far away and smaller if the human is close.
    return max(10, int(multiplier * ((2 * willingness + competence) ** 2)))

def is_waiting_over(started_waiting_tick: float, current_tick: float, wait_time):
    return current_tick - started_waiting_tick >= wait_time
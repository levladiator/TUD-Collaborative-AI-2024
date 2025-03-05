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

def calculate_wait_time(distance_to_human: str, taskTrustBelief: dict, must_be_done_together=False) -> int:
    # We normalize the trust to be in [1,3] in order to make our function strictly increasing
    willingness = taskTrustBelief['willingness'] + 2
    competence = taskTrustBelief['competence'] + 2
    multiplier = 1.5 if distance_to_human == 'close' else 2
    if must_be_done_together:
        multiplier *= 1.5

    # Willingness to help is more important than competence
    # The time increases quadratically with the trustworthiness in order to break linearity
    # We have a minimum wait time of 10 seconds and a maximum of 30
    # The wait time will be bigger if the human is far away and smaller if the human is close.
    return min(max(10, int(multiplier * ((2 * willingness + competence) ** 1.5))), 30)

def is_waiting_over(started_waiting_tick: float, current_tick: float, wait_time):
    return current_tick - started_waiting_tick >= wait_time
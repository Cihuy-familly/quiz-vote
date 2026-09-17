#!/usr/bin/env python3
"""
Load generator for quiz-vote app.
Generates random votes at a configurable rate.
"""
import os
import time
import random
import requests
import logging
import json
import sys

# Config from env vars
VOTE_API_URL = os.environ.get("VOTE_API_URL", "http://vote-api:8000")
VOTES_PER_SECOND = float(os.environ.get("VOTES_PER_SECOND", "0.5"))
ERROR_RATE = float(os.environ.get("ERROR_RATE", "0.0"))
LATENCY_MAX = float(os.environ.get("LATENCY_MAX", "0.0"))  # max simulated latency

# Structured JSON logging
logging.basicConfig(
    level=logging.INFO,
    format='{"time": "%(asctime)s", "service": "load-generator", "level": "%(levelname)s", "message": %(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%S"
)
logger = logging.getLogger(__name__)

def fetch_quizzes():
    try:
        r = requests.get(f"{VOTE_API_URL}/api/quizzes", timeout=5)
        if r.status_code == 200:
            return r.json()
    except requests.RequestException as e:
        logger.warning(json.dumps({"text": f"Failed to fetch quizzes: {e}"}))
    return []

def send_vote(quiz_id, option_id):
    try:
        # Simulate latency
        if LATENCY_MAX > 0:
            time.sleep(random.uniform(0, LATENCY_MAX))

        # Simulate errors
        if random.random() < ERROR_RATE:
            logger.info(json.dumps({"text": f"Vote NOT sent (simulated error) for quiz {quiz_id} option {option_id}"}))
            return False

        r = requests.post(
            f"{VOTE_API_URL}/api/quizzes/{quiz_id}/vote",
            json={"option_id": option_id},
            timeout=5
        )
        if r.status_code == 200:
            logger.info(json.dumps({"text": f"Vote sent: quiz={quiz_id}, option={option_id}"}))
            return True
        else:
            logger.warning(json.dumps({"text": f"Vote failed: status={r.status_code}, quiz={quiz_id}"}))
            return False
    except requests.RequestException as e:
        logger.error(json.dumps({"text": f"Vote error: {e}"}))
        return False

def main():
    logger.info(json.dumps({"text": f"Load generator started. Target: {VOTE_API_URL}, Rate: {VOTES_PER_SECOND} votes/s"}))

    while True:
        quizzes = fetch_quizzes()
        if quizzes:
            quiz = random.choice(quizzes)
            options = quiz.get("options", [])
            if options:
                option = random.choice(options)
                send_vote(quiz["id"], option["id"])
        else:
            logger.info(json.dumps({"text": "No quizzes found, waiting 5 seconds..."}))
            time.sleep(5)

        # Wait according to the configured rate
        time.sleep(1.0 / VOTES_PER_SECOND)

if __name__ == "__main__":
    main()
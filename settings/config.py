"""Configuration settings for the Vote Efficiency LLM."""

# Blockchain configuration
BLOCKCHAIN_CHOICE = "STEEM"  # Options: "HIVE" or "STEEM"
CURATOR = "karja"

log_level = "INFO"

log_file_path = "log.txt"

# Node configurations
STEEM_NODES = [
    "https://api.steemit.com",
    "https://api.justyy.com",
    "https://api.moecki.online"
]

HIVE_NODES = [
    "https://api.deathwing.me",
    "https://api.hive.blog",
    "https://api.openhive.network",
]

# Model configuration
MODE_CHOICES = ["TRAINING", "TESTING", "PRODUCTION"]
OPERATION_MODE = "TRAINING"  # Options: "TRAINING", "TESTING", "PRODUCTION"
TEST_SIZE = 0.2
MAX_RESULTS = 1000

# Directory configuration
DIRECTORIES = ['models', 'reports']
MODEL_DIR = 'models'
REPORT_DIR = 'reports'

steem_domain = 'https://steemit.com'
hive_domain = 'https://peakd.com'
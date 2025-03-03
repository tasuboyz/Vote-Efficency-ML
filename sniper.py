import json
import requests
import logging
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from settings.logging_config import logger
from settings.config import (
    BLOCKCHAIN_CHOICE, STEEM_NODES, HIVE_NODES, 
    CURATOR, MODE_CHOICES, OPERATION_MODE, steem_domain, hive_domain
)
from utils.beem_requests import BlockchainConnector
from database.db_manager import DatabaseManager
from xgboost import XGBClassifier, XGBRegressor

class VoteSniper:
    def __init__(self, config_path):
        """Initialize vote sniper with configuration."""
        with open(config_path, 'r') as file:
            config = json.load(file)
            
        self.admin_id = config["admin_id"]
        self.TOKEN = config["TOKEN"]
        self.steem_curator = config["steem_curator"]
        self.hive_curator = config["hive_curator"]
        
        # Initialize blockchain connector and database
        self.beem = BlockchainConnector(BLOCKCHAIN_CHOICE)
        self.db = DatabaseManager()
        
        # Load ML models
        self.clf_model = XGBClassifier()
        self.reg_model = XGBRegressor()
        self.clf_model.load_model('models/classifier_model.json')
        self.reg_model.load_model('models/regressor_model.json')
        
        # Initialize tracking
        self.last_check_time = defaultdict(lambda: datetime.now(timezone.utc))
        self.published_posts = set()

    def get_posts(self, usernames, platform, max_age_minutes=5):
        """Get recent posts for monitored users."""
        post_links = []
        current_time = datetime.now(timezone.utc)
        logger.info(f"Checking posts for {len(usernames)} users on {platform}")

        for username in usernames:
            try:
                post = self.beem.get_author_post(username, platform)
                
                created_time = post['created']
                created_time = datetime.strptime(post['created'], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
                
                post_age = current_time - created_time
                age_minutes = post_age.total_seconds() / 60
                
                if age_minutes <= max_age_minutes and post['url'] not in self.published_posts:
                    # Get post features and optimal delay for prediction
                    author_stats = self.db.get_author_stats(username, platform)
                    optimal_delay = self.db.get_optimal_delay(username, platform)
                    
                    if author_stats and optimal_delay:
                        features = {
                            'author_avg_efficiency': author_stats['avg_efficiency'],
                            'author_reputation': author_stats['reputation'],
                            'author_avg_payout': author_stats['avg_payout'],
                            'vote_delay': optimal_delay['recent_good_delay']
                        }
                        
                        # Make vote decision prediction
                        clf_features = [features[f] for f in ['author_avg_efficiency', 'author_reputation', 'author_avg_payout']]
                        vote_decision = self.clf_model.predict([clf_features])[0]
                        
                        # If vote decision is positive, predict efficiency
                        if vote_decision == 1:
                            reg_features = [features[f] for f in ['author_avg_efficiency', 'author_reputation', 'author_avg_payout', 'vote_delay']]
                            predicted_efficiency = self.reg_model.predict([reg_features])[0]
                            
                            post_links.append({
                                'url': post['url'],
                                'author': username,
                                'created': created_time,
                                'optimal_delay': optimal_delay['recent_good_delay'],
                                'predicted_efficiency': predicted_efficiency,
                                'best_historical_efficiency': optimal_delay['best_efficiency']
                            })
                            self.published_posts.add(post['url'])
                            logger.info(
                                f"Found voteable post: {post['url']}\n"
                                f"Optimal delay: {optimal_delay['recent_good_delay']} minutes\n"
                                f"Predicted efficiency: {predicted_efficiency:.2f}%"
                            )
                        
            except Exception as e:
                logger.error(f"Error processing posts for {username}: {str(e)}")
                continue

        return post_links

    def process_votes(self):
        """Main loop for monitoring and voting on posts."""
        while True:
            try:
                # Get monitored users from database
                steem_users = self.db.get_all_authors("STEEM")
                hive_users = self.db.get_all_authors("HIVE")
                
                logger.info(f"Monitoring {len(steem_users)} STEEM users and {len(hive_users)} HIVE users")
                
                # Process one platform at a time to avoid timeouts
                if steem_users:
                    try:
                        posts = self.get_posts(
                            [user['author_name'] for user in steem_users], 
                            "STEEM"
                        )
                        self._process_platform_posts(posts, "STEEM")
                    except Exception as e:
                        logger.error(f"Error processing STEEM posts: {str(e)}")
                
                if hive_users:
                    try:
                        posts = self.get_posts(
                            [user['author_name'] for user in hive_users], 
                            "HIVE"
                        )
                        self._process_platform_posts(posts, "HIVE")
                    except Exception as e:
                        logger.error(f"Error processing HIVE posts: {str(e)}")
                
                time.sleep(15)  # Check every 15 seconds
                
            except Exception as e:
                logger.error(f"Error in main loop: {str(e)}")
                time.sleep(60)  # Wait longer on error

    def _process_platform_posts(self, posts, platform):
        """Process posts for a specific platform."""
        if not posts:
            return
            
        for post in posts:
            try:
                curator = self.steem_curator if platform == "STEEM" else self.hive_curator
                voting_power = self.beem.calculate_voting_power(curator)
                url = f"{steem_domain}{post['url']}" if platform == "STEEM" else f"{hive_domain}{post['url']}"
                
                # Calculate when to vote based on optimal delay
                created_time = post['created']
                optimal_delay = post['optimal_delay']
                target_vote_time = created_time + timedelta(minutes=optimal_delay)
                time_until_vote = target_vote_time - datetime.now(timezone.utc)
                minutes_until_vote = time_until_vote.total_seconds() / 60
                
                message = (
                    f"[{platform}] Found voteable post!\n"
                    f"Author: {post['author']}\n"
                    f"VP: {voting_power}%\n"
                    f"URL: {url}\n"
                    f"Optimal delay: {optimal_delay} minutes\n"
                    f"Predicted efficiency: {post['predicted_efficiency']:.2f}%\n"
                    f"Best historical: {post['best_historical_efficiency']:.2f}%\n"
                    f"Voting in: {minutes_until_vote:.1f} minutes"
                )
                self.send_telegram_message(self.TOKEN, self.admin_id, message)
                
                if voting_power > 89:
                    if minutes_until_vote > 0:
                        logger.info(f"Waiting {minutes_until_vote:.1f} minutes before voting...")
                        time.sleep(minutes_until_vote * 60)
                    
                    permlink = self.beem.get_permlink(url)
                    self.beem.like_steem_post(
                        voter=curator,
                        voted=post['author'],
                        permlink=permlink,
                        weight=100
                    )
                    logger.info(f"Voted on {url} after optimal delay")
                else:
                    self.send_telegram_message(
                        self.TOKEN, 
                        self.admin_id, 
                        f"⚠️ VP too low ({voting_power}%), skipping vote"
                    )
                    
            except Exception as e:
                logger.error(f"Error processing post {post['url']}: {str(e)}")
                continue

    def send_telegram_message(self, bot_token, chat_id, message):
        """Send notification via Telegram."""
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            data = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            response = requests.post(url, json=data)
            return response.json()
        except Exception as e:
            logger.error(f"Telegram notification failed: {str(e)}")
            return False

if __name__ == '__main__':
    CONFIG_PATH = "config.json"
    sniper = VoteSniper(CONFIG_PATH)
    sniper.process_votes()
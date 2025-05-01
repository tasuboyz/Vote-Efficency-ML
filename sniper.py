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
                    
                    # Per i post nuovi, non ci saranno votanti importanti immediatamente
                    # Utilizziamo i dati storici e il modello predittivo
                    important_voters = []
                    use_historical_data = True
                    
                    # Verifica se il post ha già voti importanti (improbabile se è molto nuovo)
                    try:
                        # Tentiamo di analizzare i votanti, ma non ci aspettiamo risultati
                        important_voters = self.beem.get_post_voters(post['url'], min_importance=1.0)
                        
                        if important_voters:
                            logger.info(f"Sorprendentemente, trovati {len(important_voters)} votanti importanti su un post nuovo: {post['url']}")
                            voting_window = self._calculate_optimal_voting_window(important_voters)
                            
                            if voting_window:
                                use_historical_data = False
                                logger.info(f"Usando la finestra di voto dai votanti esistenti: {voting_window}")
                                
                                # Usa la finestra di voto ottimale se disponibile
                                if optimal_delay:
                                    # Bilanciamo tra l'ottimo storico e la finestra basata sui votanti
                                    weighted_delay = optimal_delay['recent_good_delay'] * 0.3 + voting_window['optimal_delay'] * 0.7
                                    optimal_delay['recent_good_delay'] = max(round(weighted_delay), 2)  # Minimo 2 minuti
                        else:
                            logger.info(f"Nessun votante importante trovato per il post nuovo {post['url']} - normale per un post recente")
                    except Exception as voter_error:
                        logger.warning(f"Could not analyze voters (expected for new posts): {str(voter_error)}")
                    
                    # Per i post nuovi senza voti importanti, utilizziamo una strategia basata sulla storia dell'autore
                    if use_historical_data and optimal_delay:
                        logger.info(f"Utilizzando dati storici per {post['url']} - Delay ottimale: {optimal_delay['recent_good_delay']} minuti")
                        
                        # Affiniamo il timing con il modello predittivo
                        # Cerchiamo di identificare i votanti abituali di questo autore
                        try:
                            # Verifica se abbiamo dati storici su quali whale votano questo autore
                            author_frequent_voters = self._get_author_frequent_voters(username, platform)
                            
                            if author_frequent_voters:
                                logger.info(f"Trovati {len(author_frequent_voters)} votanti frequenti per {username}")
                                voter_info = "\n".join([f"- {v['voter']}: {v['vote_count']} voti, media delay: {v['avg_delay_minutes']:.1f} min" 
                                            for v in author_frequent_voters[:3]])
                                logger.info(f"Top votanti abituali per {username}:\n{voter_info}")
                                
                                # Calcola un delay ottimale basato sui votanti abituali
                                avg_delay = sum(v['avg_delay_minutes'] for v in author_frequent_voters[:5]) / min(5, len(author_frequent_voters))
                                # Imposta un valore minimo e massimo ragionevole
                                optimal_voting_delay = max(min(avg_delay * 0.8, 30), 5)
                                logger.info(f"Delay calcolato dai votanti abituali: {optimal_voting_delay:.1f} minuti")
                                
                                # Bilanciamo con i dati storici dell'autore
                                if optimal_delay:
                                    optimal_delay['recent_good_delay'] = round((optimal_delay['recent_good_delay'] + optimal_voting_delay) / 2)
                                    logger.info(f"Delay finale bilanciato: {optimal_delay['recent_good_delay']} minuti")
                        except Exception as e:
                            logger.warning(f"Errore nell'analisi dei votanti abituali: {str(e)}")
                    
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
                            
                            # Aggiungi info sui votanti importanti al post
                            post_data = {
                                'url': post['url'],
                                'author': username,
                                'created': created_time,
                                'optimal_delay': optimal_delay['recent_good_delay'],
                                'predicted_efficiency': predicted_efficiency,
                                'best_historical_efficiency': optimal_delay['best_efficiency'],
                                'important_voters': important_voters,
                                'is_new_post': use_historical_data,  # Flag per indicare se è un post nuovo senza votanti
                                'frequent_voters': author_frequent_voters if 'author_frequent_voters' in locals() else []
                            }
                            
                            post_links.append(post_data)
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
        
    def _get_author_frequent_voters(self, author, platform):
        """Analizza i votanti abituali di un autore in base ai dati storici."""
        try:
            # Qui potremmo fare una query al database per trovare i post precedenti dell'autore
            # e analizzare chi sono i votanti abituali e con quale timing
            
            # Versione semplificata: utilizziamo i dati già in cache
            voter_stats = {}
            
            # Analizza la cache dei votanti per trovare pattern
            for key, voters_data in self.beem._voters_cache.items():
                if author.lower() in key.lower():
                    for voter in voters_data:
                        voter_name = voter['voter']
                        delay = voter['vote_delay_minutes']
                        importance = voter['importance']
                        
                        if voter_name not in voter_stats:
                            voter_stats[voter_name] = {
                                'voter': voter_name,
                                'vote_count': 1,
                                'delays': [delay],
                                'importance': importance
                            }
                        else:
                            voter_stats[voter_name]['vote_count'] += 1
                            voter_stats[voter_name]['delays'].append(delay)
                            voter_stats[voter_name]['importance'] = max(voter_stats[voter_name]['importance'], importance)
            
            # Calcola le medie dei delay e ordina per frequenza di voto
            frequent_voters = []
            for voter_name, stats in voter_stats.items():
                if stats['vote_count'] >= 2:  # Considera solo votanti che hanno votato almeno due volte
                    stats['avg_delay_minutes'] = sum(stats['delays']) / len(stats['delays'])
                    frequent_voters.append(stats)
            
            # Ordina per conteggio dei voti (frequenza) decrescente
            frequent_voters.sort(key=lambda x: x['vote_count'], reverse=True)
            return frequent_voters
            
        except Exception as e:
            logger.error(f"Errore nell'analisi dei votanti abituali per {author}: {str(e)}")
            return []

    def _calculate_optimal_voting_window(self, voters):
        """Calculate optimal voting window based on important voters data."""
        if not voters:
            return None
            
        # Sort voters by vote delay (ascending)
        sorted_voters = sorted(voters, key=lambda x: x['vote_delay_minutes'])
        
        # Get optimal voting window based on when important voters voted
        if sorted_voters:
            # Optimal window is right before the first important voter
            first_important_voter = sorted_voters[0]
            voting_window_end = max(first_important_voter['vote_delay_minutes'] - 1, 5)  # 1 minuto prima, ma minimo 5 minuti
            voting_window_start = max(voting_window_end - 5, 5)  # 5 minuti prima della fine della finestra, ma minimo 5 minuti
            
            # Calcola un delay ottimale che è più vicino alla fine della finestra (70% verso end, 30% verso start)
            # Questo bilancia il vantaggio di essere tra i primi votanti e evitare la penalità di curation
            optimal_delay = voting_window_start + (voting_window_end - voting_window_start) * 0.7
            
            return {
                'start': voting_window_start,
                'end': voting_window_end,
                'optimal_delay': optimal_delay
            }
        
        return None

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
                
                # Prepara informazioni sui votanti importanti per la notifica
                voter_info = ""
                if 'important_voters' in post and post['important_voters']:
                    # Ottieni i top 3 votanti per importanza
                    top_voters = sorted(post['important_voters'], key=lambda x: x['importance'], reverse=True)[:3]
                    voter_info = "\n\nTop voters:\n"
                    for v in top_voters:
                        voter_info += f"- {v['voter']} (importance: {v['importance']:.2f}, delay: {v['vote_delay_minutes']} min)\n"
                
                message = (
                    f"[{platform}] Found voteable post!\n"
                    f"Author: {post['author']}\n"
                    f"VP: {voting_power}%\n"
                    f"URL: {url}\n"
                    f"Optimal delay: {optimal_delay} minutes\n"
                    f"Predicted efficiency: {post['predicted_efficiency']:.2f}%\n"
                    f"Best historical: {post['best_historical_efficiency']:.2f}%\n"
                    f"Voting in: {minutes_until_vote:.1f} minutes"
                    f"{voter_info}"
                )
                self.send_telegram_message(self.TOKEN, self.admin_id, message)
                
                if voting_power > 89:
                    if minutes_until_vote > 0:
                        logger.info(f"Waiting {minutes_until_vote:.1f} minutes before voting...")
                        time.sleep(minutes_until_vote * 60)
                    
                    # Verifica nuovamente che non ci sia stato un voto importante nel frattempo
                    if minutes_until_vote > 2:  # Solo se stiamo aspettando più di 2 minuti
                        try:
                            current_voters = self.beem.get_post_voters(post['url'], min_importance=1.0, use_cache=False)
                            updated_window = self._calculate_optimal_voting_window(current_voters)
                            if updated_window:
                                new_optimal_delay = updated_window['optimal_delay']
                                # Se il nuovo ritardo ottimale è significativamente diverso, aggiustiamo
                                if abs(new_optimal_delay - optimal_delay) > 2:
                                    logger.info(f"Important voter detected during waiting! Adjusting vote timing.")
                                    adjusted_msg = f"⚠️ Vote timing adjusted based on new important voters. Voting now!"
                                    self.send_telegram_message(self.TOKEN, self.admin_id, adjusted_msg)
                                    # Votiamo subito se è passato il tempo minimo
                                    if (datetime.now(timezone.utc) - created_time).total_seconds() / 60 >= 5:
                                        break
                        except Exception as recheck_error:
                            logger.warning(f"Failed to recheck voters: {str(recheck_error)}")
                    
                    permlink = self.beem.get_permlink(url)
                    self.beem.like_steem_post(
                        voter=curator,
                        voted=post['author'],
                        permlink=permlink,
                        weight=100
                    )
                    
                    # Registra questa votazione nel database per migliorare le future previsioni
                    try:
                        actual_delay = (datetime.now(timezone.utc) - created_time).total_seconds() / 60
                        logger.info(f"Voted on {url} after {actual_delay:.1f} minutes delay (target was {optimal_delay})")
                        
                        # Aggiorna il database con questa votazione per apprendimento futuro
                        self.db.update_voting_delay(
                            author_name=post['author'],
                            platform=platform,
                            vote_delay=actual_delay,
                            efficiency=post['predicted_efficiency'],
                            post_url=post['url']
                        )
                        
                        # Notifica del voto completato con successo
                        success_msg = f"✅ Vote on {url} successful!\nActual delay: {actual_delay:.1f} min"
                        self.send_telegram_message(self.TOKEN, self.admin_id, success_msg)
                    except Exception as db_error:
                        logger.error(f"Failed to update database: {str(db_error)}")
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
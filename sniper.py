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
    CURATOR, MODE_CHOICES, OPERATION_MODE, steem_domain, hive_domain,
    TEST_MODE
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
        
        # Mostra avviso se in modalità test
        if TEST_MODE:
            test_mode_message = (
                "\n" + "=" * 80 + "\n" +
                "MODALITÀ TEST ATTIVA: I voti verranno simulati ma non inviati alla blockchain\n" +
                "=" * 80 + "\n"
            )
            logger.warning(test_mode_message)
            print(test_mode_message)
            
            # Invia notifica Telegram se configurato
            try:
                self.send_telegram_message(
                    self.TOKEN, 
                    self.admin_id, 
                    "⚠️ <b>AVVISO: MODALITÀ TEST ATTIVA</b>\n\n" +
                    "Il sistema è in esecuzione in modalità di test.\n" +
                    "I voti verranno analizzati e simulati ma non saranno inviati alla blockchain."
                )
            except Exception:
                pass  # Ignora errori nell'invio della notifica Telegram

    def get_posts(self, usernames, platform, max_age_minutes=5):
        """Get recent posts for monitored users."""
        post_links = []
        current_time = datetime.now(timezone.utc)
        logger.info(f"Checking posts for {len(usernames)} users on {platform}")

        for username in usernames:
            try:
                post = self.beem.get_author_post(username, platform)
                
                created_time = datetime.strptime(post['created'], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
                
                post_age = current_time - created_time
                age_minutes = post_age.total_seconds() / 60
                
                if age_minutes <= max_age_minutes and post['url'] not in self.published_posts:
                    # Get post features and optimal delay for prediction
                    author_stats = self.db.get_author_stats(username, platform)
                    optimal_delay = self.db.get_optimal_delay(username, platform)
                    
                    # Usiamo la nuova analisi dei voti recenti per questo autore
                    recent_optimal = self._analyze_recent_voting_patterns(username, platform)
                    if recent_optimal:
                        logger.info(f"Utilizzando ritardo ottimale basato sui voti recenti: {recent_optimal} minuti")
                        
                        # Aggiorniamo il delay ottimale con i dati recenti
                        if optimal_delay:
                            # Diamo più peso (60%) all'analisi recente rispetto al valore storico (40%)
                            optimal_delay['recent_good_delay'] = round(recent_optimal * 0.6 + optimal_delay['recent_good_delay'] * 0.4)
                            logger.info(f"Delay finale dopo bilanciamento con dati storici: {optimal_delay['recent_good_delay']} minuti")
                    
                    # Per i post nuovi, non ci saranno votanti importanti immediatamente
                    # Utilizziamo i dati storici e il modello predittivo
                    important_voters = []
                    use_historical_data = True
                    
                    # Verifica se il post ha già voti importanti (improbabile se è molto nuovo)
                    try:
                        # Tentiamo di analizzare i votanti, ma non ci aspettiamo risultati
                        important_voters = self.beem.get_post_voters(post['url'], min_importance=1.0)
                        
                        if important_voters:
                            logger.info(f"Trovati {len(important_voters)} votanti importanti su un post nuovo: {post['url']}")
                            voting_window = self._calculate_optimal_voting_window(important_voters)
                            
                            if voting_window:
                                use_historical_data = False
                                logger.info(f"Usando la finestra di voto dai votanti esistenti: {voting_window}")
                                
                                # Usa la finestra di voto ottimale se disponibile
                                if optimal_delay:
                                    # Diamo MOLTO più peso (80%) ai votanti attuali rispetto ai dati storici (20%)
                                    weighted_delay = optimal_delay['recent_good_delay'] * 0.2 + voting_window['optimal_delay'] * 0.8
                                    optimal_delay['recent_good_delay'] = max(round(weighted_delay), 2)  # Minimo 2 minuti
                        else:
                            logger.info(f"Nessun votante importante trovato per il post nuovo {post['url']} - normale per un post recente")
                    except Exception as voter_error:
                        logger.warning(f"Could not analyze voters (expected for new posts): {str(voter_error)}")
                    
                    # Per i post nuovi senza voti importanti, utilizziamo una strategia basata sulla storia dell'autore
                    if use_historical_data and optimal_delay:
                        logger.info(f"Utilizzando dati storici per {post['url']} - Delay ottimale: {optimal_delay['recent_good_delay']} minuti")
                        
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
                                # Consideriamo solo i votanti più frequenti (max 5)
                                avg_delay = sum(v['avg_delay_minutes'] for v in author_frequent_voters[:5]) / min(5, len(author_frequent_voters))
                                # Imposta un valore massimo inferiore a prima (25 invece di 30)
                                optimal_voting_delay = max(min(avg_delay * 0.8, 25), 5)
                                logger.info(f"Delay calcolato dai votanti abituali: {optimal_voting_delay:.1f} minuti")
                                
                                # Diamo più peso ai votanti abituali (60%) rispetto ai dati storici (40%)
                                if optimal_delay:
                                    optimal_delay['recent_good_delay'] = round(optimal_voting_delay * 0.6 + optimal_delay['recent_good_delay'] * 0.4)
                                    logger.info(f"Delay finale bilanciato: {optimal_delay['recent_good_delay']} minuti")
                        except Exception as e:
                            logger.warning(f"Errore nell'analisi dei votanti abituali: {str(e)}")
                    
                    if author_stats and optimal_delay:
                        # Impostiamo un limite massimo assoluto al delay per evitare di votare troppo tardi
                        optimal_delay['recent_good_delay'] = min(optimal_delay['recent_good_delay'], 25)
                        
                        # Prepariamo le features per il modello di classificazione
                        # Questo modello decide SE votare in base alle caratteristiche dell'autore
                        features = {
                            'author_avg_efficiency': author_stats['avg_efficiency'],
                            'author_reputation': author_stats['reputation'],
                            'author_avg_payout': author_stats['avg_payout'],
                            'vote_delay': optimal_delay['recent_good_delay']
                        }
                        
                        # IMPORTANTE: Prima di tutto, il classificatore decide SE votare o meno
                        clf_features = [features[f] for f in ['author_avg_efficiency', 'author_reputation', 'author_avg_payout']]
                        vote_decision = self.clf_model.predict([clf_features])[0]
                        
                        # Solo se il classificatore dice di votare, procediamo con la predizione dell'efficienza
                        if vote_decision == 1:
                            logger.info(f"Il modello di classificazione ha deciso di votare per {post['url']}")
                            
                            # Prediciamo l'efficienza attesa
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
                        else:
                            logger.info(f"Il modello di classificazione ha deciso di NON votare per {post['url']}")
                        
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
        """
        Calcola la finestra di voto ottimale basata sui dati dei votanti importanti.
        Considera sia l'importanza che il timing dei votanti.
        """
        if not voters:
            return None
            
        # Ordina i votanti per delay (ascendente)
        sorted_by_timing = sorted(voters, key=lambda x: x['vote_delay_minutes'])
        
        # Ordina i votanti per importanza (discendente)
        sorted_by_importance = sorted(voters, key=lambda x: x['importance'], reverse=True)
        
        logger.info(f"Analisi votanti: trovati {len(voters)} votanti importanti")
        
        # Mostra i primi 3 votanti per timing e per importanza nei log
        timing_info = ", ".join([f"{v['voter']} ({v['vote_delay_minutes']} min)" 
                               for v in sorted_by_timing[:3]])
        importance_info = ", ".join([f"{v['voter']} (importanza {v['importance']:.1f})" 
                                  for v in sorted_by_importance[:3]])
        
        logger.info(f"Top 3 per timing: {timing_info}")
        logger.info(f"Top 3 per importanza: {importance_info}")
        
        # Prendi i top votanti (primi 5 per importanza)
        top_important_voters = sorted_by_importance[:5]
        
        # Calcola il delay del primo votante per timing
        first_voter_delay = sorted_by_timing[0]['vote_delay_minutes'] if sorted_by_timing else None
        
        # Calcola il delay medio dei top votanti per importanza
        top_delays = [v['vote_delay_minutes'] for v in top_important_voters]
        top_importance_min_delay = min(top_delays) if top_delays else None
        
        # Strategia avanzata: consideriamo sia il primo votante che i votanti più importanti
        if first_voter_delay is not None and top_importance_min_delay is not None:
            # Se il primo votante è lontano dai votanti più importanti, 
            # potremmo voler bilanciare i due valori
            if top_importance_min_delay - first_voter_delay > 3:
                # Il primo votante per importanza arriva molto dopo il primo per timing
                logger.info(f"Votanti importanti arrivano significativamente dopo ({top_importance_min_delay} min) " +
                          f"rispetto al primo votante ({first_voter_delay} min)")
                
                # Calcola un delay ottimale che bilancia i due valori
                # Se i top votanti arrivano molto dopo, diamo comunque più peso
                # al loro timing per massimizzare l'efficienza
                weight_for_top_important = 0.7  # 70% del peso per i votanti top
                optimal_raw = (first_voter_delay * (1 - weight_for_top_important) + 
                             top_importance_min_delay * weight_for_top_important)
                
                # Arrotondiamo e assicuriamo che sia almeno 5 minuti
                optimal_delay = max(round(optimal_raw - 1), 5)  # -1 per anticipare comunque di 1 minuto
                
                # Calcoliamo la finestra di voto
                voting_window_end = optimal_delay
                voting_window_start = max(voting_window_end - 3, 5)  # 3 minuti prima della fine della finestra, ma minimo 5 minuti
                
                logger.info(f"Bilanciamento tra timing e importanza: selezionato delay ottimale di {optimal_delay} minuti")
            else:
                # I votanti importanti votano vicino al primo votante
                # In questo caso, anticipiamo leggermente il primo votante
                voting_window_end = max(first_voter_delay - 1, 5)  # 1 minuto prima, ma minimo 5 minuti
                voting_window_start = max(voting_window_end - 3, 5)  # 3 minuti prima della fine della finestra, ma minimo 5 minuti
                optimal_delay = voting_window_start + (voting_window_end - voting_window_start) * 0.7
                
                logger.info(f"Votanti importanti arrivano subito: ottimizzato per anticiparli tutti a {optimal_delay:.1f} minuti")
        elif first_voter_delay is not None:
            # Se abbiamo solo il primo votante per timing
            voting_window_end = max(first_voter_delay - 1, 5)  # 1 minuto prima, ma minimo 5 minuti
            voting_window_start = max(voting_window_end - 3, 5)  # 3 minuti prima della fine della finestra, ma minimo 5 minuti
            optimal_delay = voting_window_start + (voting_window_end - voting_window_start) * 0.7
        else:
            # Caso improbabile: non abbiamo informazioni sui delay
            return None
        
        # Log dettagliati sulla strategia di timing
        logger.info(f"Finestra di voto calcolata: {voting_window_start}-{voting_window_end} minuti")
        logger.info(f"Delay ottimale raccomandato: {optimal_delay:.1f} minuti")
        
        return {
            'start': voting_window_start,
            'end': voting_window_end,
            'optimal_delay': optimal_delay
        }

    def _analyze_recent_voting_patterns(self, author_name, platform):
        """
        Analizza i dati di voto recenti per l'autore con focus sui votanti importanti.
        L'obiettivo è anticipare specificamente i votanti importanti, non votare genericamente presto.
        """
        try:
            # Otteniamo l'informazione sui votanti frequenti di questo autore
            frequent_voters = self._get_author_frequent_voters(author_name, platform)
            
            # Se abbiamo votanti frequenti importanti, utilizziamo i loro tempi per decidere
            important_voter_timing = None
            
            if frequent_voters:
                important_voters = [v for v in frequent_voters if v.get('importance', 0) >= 1.0]
                if important_voters:
                    # Calcola la media del timing dei votanti importanti
                    delays = []
                    for voter in important_voters:
                        delays.extend(voter['delays'])
                    
                    if delays:
                        # Ordina i delay e prendi il primo quartile (per anticipare la maggior parte dei voti)
                        delays.sort()
                        first_quartile_index = max(0, len(delays) // 4 - 1)
                        important_voter_timing = delays[first_quartile_index]
                        
                        # Per sicurezza, anticipa di 1-2 minuti rispetto al timing del primo quartile
                        important_voter_timing = max(important_voter_timing - 2, 5)
                        
                        logger.info(f"📊 Timing basato sui votanti importanti per {author_name}: {important_voter_timing} minuti " +
                                   f"(anticipato rispetto al primo quartile di {len(delays)} voti storici)")
                        
                        # Ritorna immediatamente questo valore se lo abbiamo trovato
                        return important_voter_timing
            
            # Fallback alla strategia precedente se non abbiamo informazioni sui votanti importanti
            last_optimal = self.db.get_last_optimal_delay(author_name, platform)
            recent_votes = self.db.get_recent_voting_history(author_name, platform, limit=5)
            
            if not recent_votes and not last_optimal:
                logger.info(f"Nessun dato storico per {author_name}, utilizzando delay predefinito di 10 minuti")
                return 10
                
            total_weight = 0
            weighted_sum = 0
            
            for i, vote in enumerate(recent_votes):
                weight = (len(recent_votes) - i)**2
                weighted_sum += vote['vote_delay'] * weight
                total_weight += weight
            
            avg_recent_delay = weighted_sum / total_weight if total_weight > 0 else None
            
            base_delay = 10
            
            if last_optimal is not None:
                base_delay = max(round(last_optimal * 0.7), 5)
                logger.info(f"Utilizzo ultimo ottimale per {author_name} con riduzione: {last_optimal} → {base_delay}")
            elif avg_recent_delay is not None:
                base_delay = max(round(avg_recent_delay * 0.7), 5)
                logger.info(f"Utilizzo media recente per {author_name} con riduzione: {avg_recent_delay:.1f} → {base_delay}")
            
            return min(base_delay, 20)
            
        except Exception as e:
            logger.error(f"Errore nell'analisi dei voti recenti per {author_name}: {str(e)}")
            return 10

    def _calculate_optimal_vote_weight(self, post_age_minutes, target_delay, important_voters_delay=None):
        """
        Calcola il peso ottimale del voto in base al timing.
        
        Args:
            post_age_minutes: Età attuale del post in minuti
            target_delay: Ritardo target ottimale calcolato
            important_voters_delay: Ritardo medio dei votanti importanti se disponibile
            
        Returns:
            Tuple (peso_voto, delay_consigliato, messaggio)
        """
        # Valori di riferimento per i limiti di tempo critici
        MIN_FULL_WEIGHT_AGE = 5.0  # Età minima per peso voto 100%
        CRITICAL_TIMING = 4.8      # Votanti importanti sotto questo valore richiedono decisioni critiche
        ANTICIPATION_MARGIN = 0.5  # Margine di anticipo rispetto ai votanti importanti (minuti)
        
        # Caso 1: Il post ha già più di 5 minuti, possiamo votare con peso pieno
        if post_age_minutes >= MIN_FULL_WEIGHT_AGE:
            return 100, 0, "Voto con peso pieno (post > 5 min)"
            
        # Caso 2: Post giovane senza votanti importanti imminenti o non ci sono informazioni sui votanti
        if not important_voters_delay:
            remaining = MIN_FULL_WEIGHT_AGE - post_age_minutes
            return 0, remaining, f"Attendo {remaining:.1f} minuti per raggiungere i 5 minuti (peso pieno)"
            
        # Caso 3: Abbiamo informazioni sui votanti importanti
        # Calcola quanto tempo manca all'arrivo dei votanti importanti
        time_until_important = max(0, important_voters_delay - post_age_minutes)
        
        # Se i votanti importanti arriveranno dopo i 5 minuti
        if important_voters_delay > MIN_FULL_WEIGHT_AGE:
            # Verifichiamo se conviene aspettare i 5 minuti o fino a poco prima dell'arrivo dei votanti
            time_until_full_weight = MIN_FULL_WEIGHT_AGE - post_age_minutes
            
            # Se manca poco ai 5 minuti, è meglio aspettare per votare con peso pieno
            if time_until_full_weight <= 2.0:
                return 0, time_until_full_weight, f"Attendo solo {time_until_full_weight:.1f} minuti per votare con peso pieno"
                
            # Se manca tanto ai 5 minuti ma i votanti arrivano subito dopo, 
            # aspetta comunque fino ai 5 minuti per votare a peso pieno
            if important_voters_delay - MIN_FULL_WEIGHT_AGE < 1.5:
                return 0, time_until_full_weight, f"Attendo {time_until_full_weight:.1f} minuti per votare con peso pieno (votanti attesi poco dopo)"
                
            # Se i votanti arrivano significativamente dopo i 5 minuti,
            # vota poco prima del loro arrivo previsto
            target_wait = max(time_until_full_weight, time_until_important - ANTICIPATION_MARGIN)
            return 0, target_wait, f"Attendo {target_wait:.1f} minuti per votare poco prima dei votanti importanti (previsti a {important_voters_delay:.1f} min)"
            
        # Caso 4: I votanti importanti arriveranno prima dei 5 minuti
        else:
            # Se il post è già quasi ai 5 minuti, meglio votare con peso ridotto
            if post_age_minutes >= CRITICAL_TIMING:
                return 90, 0, f"Voto subito con peso 90% (quasi ai 5 min, votanti previsti a {important_voters_delay:.1f} min)"
                
            # Se i votanti sono imminenti (entro 1 minuto)
            if time_until_important <= 1.0:
                # Vota subito con peso proporzionale all'età del post
                time_ratio = post_age_minutes / MIN_FULL_WEIGHT_AGE
                weight = int(60 + (time_ratio * 30))  # 60-90% dipendendo dall'età
                return weight, 0, f"Voto immediato con peso {weight}% (votanti imminenti a {important_voters_delay:.1f} min)"
                
            # Se c'è ancora tempo prima dell'arrivo dei votanti importanti
            # aspetta finché non sono quasi arrivati
            wait_time = max(0, time_until_important - ANTICIPATION_MARGIN)
            
            # Se dopo l'attesa il post avrà superato i 5 minuti, aspetta solo fino ai 5 minuti
            expected_age_after_wait = post_age_minutes + wait_time
            if expected_age_after_wait >= MIN_FULL_WEIGHT_AGE:
                wait_time = max(0, MIN_FULL_WEIGHT_AGE - post_age_minutes)
                return 0, wait_time, f"Attendo {wait_time:.1f} minuti per votare con peso pieno"
            
            # Altrimenti, aspetta fino a poco prima dell'arrivo dei votanti
            return 0, wait_time, f"Attendo {wait_time:.1f} minuti per votare poco prima dei votanti importanti (previsti a {important_voters_delay:.1f} min)"
        
        # Caso predefinito (non dovrebbe mai arrivare qui)
        remaining = MIN_FULL_WEIGHT_AGE - post_age_minutes
        return 0, remaining, f"Attendo {remaining:.1f} minuti per peso voto ottimale"

    def _optimize_vote_weight_by_voting_power(self, vote_weight, voting_power):
        """
        Ottimizza il peso del voto in base alla potenza di voto.
        
        Args:
            vote_weight: Peso del voto calcolato
            voting_power: Potenza di voto attuale
        
        Returns:
            Peso del voto ottimizzato
        """
        if voting_power > 95:
            return max(vote_weight - 10, 0)  # Riduci il peso del voto di 10% se VP > 95%
        elif voting_power > 90:
            return max(vote_weight - 5, 0)  # Riduci il peso del voto di 5% se VP > 90%
        return vote_weight

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
                
                # Calcola l'età attuale del post
                current_age_minutes = (datetime.now(timezone.utc) - created_time).total_seconds() / 60
                
                # Analizza i votanti importanti
                important_voters_delay = None
                if 'frequent_voters' in post and post['frequent_voters']:
                    important_voters = [v for v in post['frequent_voters'] if v.get('importance', 0) >= 1.0]
                    if important_voters:
                        # Calcola la media dei tempi dei votanti importanti
                        delays = []
                        for voter in important_voters:
                            if 'avg_delay_minutes' in voter:
                                delays.append(voter['avg_delay_minutes'])
                        
                        if delays:
                            # Utilizziamo il valore più basso per essere più aggressivi
                            important_voters_delay = min(delays)
                
                # Calcola il peso del voto ottimale in base al timing
                vote_weight, wait_minutes, decision_reason = self._calculate_optimal_vote_weight(
                    current_age_minutes, 
                    optimal_delay, 
                    important_voters_delay
                )
                
                # Ottimizza il peso del voto in base alla potenza di voto
                original_weight = vote_weight
                vote_weight = self._optimize_vote_weight_by_voting_power(vote_weight, voting_power)
                
                # Aggiorna il messaggio di decisione se il peso è stato cambiato
                if vote_weight != original_weight and vote_weight > 0:
                    decision_reason += f" (peso ridotto da {original_weight}% a {vote_weight}% per VP alto)"
                
                # Ottimizza ulteriormente il peso in base al valore Steem Power rispetto ai whale
                if vote_weight > 0 and 'important_voters' in post and post['important_voters']:
                    try:
                        from utils.vote import calculate_optimal_weight_by_power
                        
                        # Ottieni il valore stimato del tuo voto al 100%
                        curator = self.steem_curator if platform == "STEEM" else self.hive_curator
                        curator_account = self.beem.get_account_info(curator)
                        
                        # Calcola il valore del voto a peso pieno
                        vote_value_result = self.beem.calculate_vote_value(
                            curator=curator,
                            weight=10000,  # Peso pieno per il calcolo
                            voting_power=voting_power
                        )
                        
                        steem_value = vote_value_result.get('steem_value', 0)
                        
                        if steem_value > 0:
                            # Ottieni il peso ottimale basato sul rapporto con i whale
                            original_sp_weight = vote_weight
                            vote_weight = calculate_optimal_weight_by_power(
                                curator_steem_value=steem_value,
                                important_voters_data=post['important_voters'],
                                base_weight=vote_weight
                            )
                            
                            # Aggiorna il messaggio se il peso è cambiato
                            if vote_weight != original_sp_weight:
                                decision_reason += f" (peso ulteriormente ottimizzato a {vote_weight}% per bilanciare con i whale)"
                                logger.info(f"Peso voto ottimizzato per SP: {original_sp_weight}% → {vote_weight}%")
                    except Exception as e:
                        logger.warning(f"Errore nell'ottimizzare il peso per SP: {e}")
                
                # Prepara informazioni sui votanti per la notifica
                voter_info = ""
                frequent_voter_info = ""
                all_frequent_voters_info = ""
                
                # Aggiunge informazioni sui votanti frequenti e importanti dell'autore
                if 'frequent_voters' in post and post['frequent_voters']:
                    # Prima mostriamo i votanti abituali importanti (whales)
                    important_frequent = [v for v in post['frequent_voters'] if v.get('importance', 0) >= 1.0]
                    if important_frequent:
                        frequent_voter_info = "\n\n📊 Votanti importanti abituali:\n"
                        for v in important_frequent[:5]:  # Mostra fino a 5 votanti importanti
                            avg_delay = v['avg_delay_minutes']
                            frequent_voter_info += f"- {v['voter']} ({v['vote_count']} voti, arrivo previsto: {avg_delay:.1f} min)\n"
                    
                    # Poi aggiungiamo una sezione per tutti i votanti abituali
                    # Ordina per frequenza di voto (discendente)
                    all_frequent = sorted(post['frequent_voters'], key=lambda x: x['vote_count'], reverse=True)
                    if all_frequent:
                        all_frequent_voters_info = "\n\n👥 Elenco completo votanti abituali:\n"
                        for v in all_frequent[:15]:  # Mostra fino a 15 votanti abituali
                            avg_delay = v['avg_delay_minutes']
                            importance_marker = "🐳" if v.get('importance', 0) >= 1.0 else ""
                            all_frequent_voters_info += f"- {v['voter']}{importance_marker}: {v['vote_count']} voti, media {avg_delay:.1f} min\n"
                
                # Aggiunge informazioni sui votanti attuali se presenti
                if 'important_voters' in post and post['important_voters']:
                    top_voters = sorted(post['important_voters'], key=lambda x: x['importance'], reverse=True)[:3]
                    voter_info = "\n\n🔍 Votanti importanti già presenti:\n"
                    for v in top_voters:
                        voter_info += f"- {v['voter']} (importanza: {v['importance']:.2f}, delay: {v['vote_delay_minutes']} min)\n"
                
                weight_info = f"\nPeso voto: {vote_weight}%" if vote_weight > 0 else ""
                timing_info = f"\nAttesa: {wait_minutes:.1f} minuti" if wait_minutes > 0 else "\nVoto immediato"
                
                message = (
                    f"[{platform}] 🔍 Trovato post votabile!\n"
                    f"Author: {post['author']}\n"
                    f"VP: {voting_power}%\n"
                    f"URL: {url}\n"
                    f"Età attuale: {current_age_minutes:.1f} minuti\n"
                    f"Ritardo ottimale: {optimal_delay} minuti\n"
                    f"Efficienza prevista: {post['predicted_efficiency']:.2f}%\n"
                    f"Decisione: {decision_reason}{weight_info}{timing_info}"
                    f"{voter_info}"
                    f"{frequent_voter_info}"
                    f"{all_frequent_voters_info}"
                )
                self.send_telegram_message(self.TOKEN, self.admin_id, message)
                
                if voting_power > 89:
                    # Se dobbiamo aspettare in base alla strategia di peso
                    if wait_minutes > 0:
                        logger.info(f"In attesa {wait_minutes:.1f} minuti per ottimizzare il peso del voto...")
                        
                        # Suddividiamo l'attesa in intervalli per monitorare attivamente
                        wait_seconds = int(wait_minutes * 60)
                        check_interval = 30  # Verifica ogni 30 secondi (aumentiamo la frequenza)
                        
                        created_timestamp = created_time.timestamp()
                        original_voters = post['important_voters'] if 'important_voters' in post else []
                        original_voter_names = {v['voter'] for v in original_voters}
                        
                        for i in range(0, wait_seconds, check_interval):
                            time_to_wait = min(check_interval, wait_seconds - i)
                            if time_to_wait <= 0:
                                break
                                
                            time.sleep(time_to_wait)
                            
                            # Ricalcola l'età attuale e il peso ottimale
                            current_age = (datetime.now(timezone.utc).timestamp() - created_timestamp) / 60
                            new_weight, _, new_reason = self._calculate_optimal_vote_weight(
                                current_age, optimal_delay, important_voters_delay
                            )
                            
                            # Verifica se siamo pronti a votare
                            if new_weight > 0:
                                logger.info(f"Condizioni cambiate! Possiamo votare con peso {new_weight}%: {new_reason}")
                                vote_weight = new_weight
                                break
                                
                            # Verifica se sono arrivati nuovi votanti importanti
                            try:
                                current_voters = self.beem.get_post_voters(post['url'], min_importance=1.0, use_cache=False)
                                
                                # Se ci sono nuovi votanti importanti, votiamo immediatamente
                                if current_voters:
                                    current_voter_names = {v['voter'] for v in current_voters}
                                    new_voters = current_voter_names - original_voter_names
                                    
                                    if new_voters:
                                        logger.info(f"⚠️ Rilevati nuovi votanti importanti: {', '.join(new_voters)}!")
                                        alert_msg = f"⚠️ ATTENZIONE! Nuovi votanti importanti: {', '.join(new_voters)}"
                                        self.send_telegram_message(self.TOKEN, self.admin_id, alert_msg)
                                        
                                        # Se il post ha almeno 4.5 minuti, votiamo con peso ridotto
                                        if current_age >= 4.5:
                                            vote_weight = max(70, int(current_age / 5 * 100))
                                            break
                                        # Altrimenti incrementiamo l'attesa ma aumentiamo la frequenza di check
                                        else:
                                            check_interval = 10  # Check ogni 10 secondi
                            except Exception as e:
                                logger.warning(f"Errore nel monitoraggio votanti: {str(e)}")
                    
                    # Votiamo con il peso calcolato
                    if vote_weight > 0:
                        permlink = self.beem.get_permlink(url)
                        logger.info(f"Voto su {url} con peso {vote_weight}%")
                        
                        # Notifica che stiamo per votare
                        notification = f"🗳️ Voto su {url}\nPeso: {vote_weight}%"
                        self.send_telegram_message(self.TOKEN, self.admin_id, notification)
                        
                        # Esegui il voto
                        self.beem.like_steem_post(
                            voter=curator,
                            voted=post['author'],
                            permlink=permlink,
                            weight=vote_weight
                        )
                        
                        # Registra questa votazione nel database
                        try:
                            actual_delay = (datetime.now(timezone.utc) - created_time).total_seconds() / 60
                            logger.info(f"Votato {url} dopo {actual_delay:.1f} minuti con peso {vote_weight}%")
                            
                            # Aggiorna il database (qui l'efficienza sarà proporzionale al peso del voto)
                            scaled_efficiency = post['predicted_efficiency'] * (vote_weight / 100)
                            self.db.update_voting_delay(
                                author_name=post['author'],
                                platform=platform,
                                vote_delay=actual_delay,
                                efficiency=scaled_efficiency,
                                post_url=post['url']
                            )
                            
                            success_msg = f"✅ Voto su {url} completato!\nRitardo: {actual_delay:.1f} min, Peso: {vote_weight}%"
                            self.send_telegram_message(self.TOKEN, self.admin_id, success_msg)
                        except Exception as db_error:
                            logger.error(f"Errore nell'aggiornamento del database: {db_error}")
                    else:
                        logger.info(f"Voto su {url} saltato per strategia di timing")
                        skip_msg = f"⏭️ Voto su {url} saltato per strategia di timing"
                        self.send_telegram_message(self.TOKEN, self.admin_id, skip_msg)
                else:
                    self.send_telegram_message(
                        self.TOKEN, 
                        self.admin_id, 
                        f"⚠️ VP troppo basso ({voting_power}%), voto saltato"
                    )
                    
            except Exception as e:
                logger.error(f"Errore nell'elaborazione del post {post['url'] if 'url' in post else 'sconosciuto'}: {str(e)}")
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
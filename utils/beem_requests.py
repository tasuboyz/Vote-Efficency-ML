import time
import random
import requests
import os
import pickle
from datetime import datetime, timezone, timedelta
from beem import Steem, Hive
from beem.account import Account
from settings.config import HIVE_NODES, STEEM_NODES, BLOCKCHAIN_CHOICE, MAX_RESULTS
from settings.logging_config import logger
from beem.comment import Comment
from beem.vote import ActiveVotes, Vote
from settings.keys import steem_posting_key, hive_posting_key
import json 

class BlockchainConnector:
    def __init__(self, blockchain_type="HIVE"):
        """Initialize blockchain connector with specified type."""
        self.blockchain_type = blockchain_type
        self.nodes = HIVE_NODES if blockchain_type == "HIVE" else STEEM_NODES
        self.working_node = self.get_working_node()
        self.blockchain = self._initialize_blockchain()
        self.power_symbol = "HP" if blockchain_type == "HIVE" else "SP"
        
        # Inizializza la cache dei voti
        self._voters_cache = {}
        self._cache_path = os.path.join("database", f"voters_cache_{blockchain_type.lower()}.pkl")
        self._load_cache()

    def _initialize_blockchain(self):
        """Initialize blockchain instance with working node."""        
        return Hive(keys=[hive_posting_key], node=self.working_node) if self.blockchain_type == "HIVE" else Steem(keys=[steem_posting_key], node=self.working_node)

    def convert_vests_to_power(self, amount):
        """Convert vesting shares to HP/SP based on blockchain type."""        
        try:
            if isinstance(self.blockchain, Hive):
                return self.blockchain.vests_to_hp(float(amount))
            elif isinstance(self.blockchain, Steem):
                return self.blockchain.vests_to_sp(float(amount))
            else:
                logger.error("Unsupported blockchain")
                return 0
        except Exception as e:
            logger.error(f"Error converting vesting shares to power: {e}")
            return 0

    def test_node(self, node_url):
        """Test if a node is responsive and functioning correctly."""        
        try:
            headers = {'Content-Type': 'application/json'}
            payload = {
                "jsonrpc": "2.0",
                "method": "condenser_api.get_dynamic_global_properties",
                "params": [],
                "id": 1
            }
            
            response = requests.post(node_url, json=payload, headers=headers, timeout=5)
            
            if response.status_code == 200:
                result = response.json()
                if 'result' in result:
                    logger.info(f"Node {node_url} is working properly")
                    return True
                
            logger.warning(f"Node {node_url} returned invalid response")
            return False
            
        except requests.exceptions.RequestException as e:
            logger.warning(f"Node {node_url} test failed: {str(e)}")
            return False

    def get_working_node(self):
        """Get a working node from the configured list with fallback mechanism."""        
        working_nodes = []
        
        for node in self.nodes:
            if self.test_node(node):
                working_nodes.append(node)
        
        if not working_nodes:
            raise Exception(f"No working {self.blockchain_type} nodes found!")
        
        selected_node = random.choice(working_nodes)
        logger.info(f"Selected {self.blockchain_type} node: {selected_node}")
        return selected_node

    def switch_to_backup_node(self):
        """Switch to a different working node, avoiding the current one."""        
        backup_nodes = [node for node in self.nodes if node != self.working_node]
        random.shuffle(backup_nodes)  # Randomize the order of backup nodes
        working_backup_nodes = []
        
        # First collect all working backup nodes
        for node in backup_nodes:
            if self.test_node(node):
                working_backup_nodes.append(node)
        
        if not working_backup_nodes:
            raise Exception(f"No working backup {self.blockchain_type} nodes available!")
        
        # Select a random working backup node
        new_node = random.choice(working_backup_nodes)
        logger.info(f"Switching from {self.working_node} to backup node: {new_node}")
        self.working_node = new_node
        self.blockchain = self._initialize_blockchain()
        return True

    def get_account_history(self, account_name, max_retries=3, delay=1):
        """Get account history with retry logic for node failures and node switching."""        
        # Create a list of all available nodes except current one
        available_nodes = [node for node in self.nodes if node != self.working_node]
        # Add current node at the beginning
        all_nodes = [self.working_node] + available_nodes
        
        for node in all_nodes:
            try:
                # Switch to new node
                self.working_node = node
                self.blockchain = self._initialize_blockchain()
                account = Account(account_name, blockchain_instance=self.blockchain)
                logger.info(f"Trying node: {node}")
                
                history_data = []
                curation_count = 0
                
                for h in account.history_reverse():
                    if h['type'] == 'curation_reward':
                        history_data.append(h)
                        curation_count += 1
                        if curation_count >= MAX_RESULTS:
                            logger.info(f"Collected {curation_count} curation rewards from {node}")
                            return history_data, self.blockchain
                
                logger.info(f"Collected all available curation rewards: {curation_count} from {node}")
                return history_data, self.blockchain
                
            except Exception as e:
                logger.warning(f"Failed to get account history from node {node}: {str(e)}")
                continue
        
        raise Exception(f"Failed to get account history after trying all nodes")
    
    def get_author_post(self, author, platform):
        data = {
            "jsonrpc": "2.0",
            "method": "condenser_api.get_discussions_by_blog",
            "params": [{"tag": author, "limit": 1}],
            "id": 1
        }
        headers = {'Content-Type': 'application/json'}
        response = requests.post(self.working_node, headers=headers, data=json.dumps(data), timeout=5)
        response.raise_for_status()
        result = response.json().get('result', [])
        return result[0]
    
    def get_account_info(self, account_name):
        return Account(account_name, blockchain_instance=self.blockchain)
    
    def calculate_voting_power(self, account_name):
        accout = Account(account_name, blockchain_instance=self.blockchain)
        return accout.get_voting_power()
    
    def get_permlink(self, post_url):
        comment = Comment(post_url, blockchain_instance=self.blockchain)
        permlink = comment.permlink
        return permlink
    
    def get_author(self, post_url):
        comment = Comment(post_url, blockchain_instance=self.blockchain)
        author = comment.author
        return author
    
    def like_steem_post(self, voter, voted, permlink, weight=20):

        account = Account(voter, blockchain_instance=self.blockchain)
        comment = Comment(authorperm=f"@{voted}/{permlink}", blockchain_instance=self.blockchain)
        comment.vote(weight, account=account)    
    
    def get_reward_fund(self, fund_name="post"):
        """Get reward fund information directly from the blockchain.
        
        Args:
            fund_name (str): Name of the reward fund, typically "post"
            
        Returns:
            dict: Reward fund data with relevant information
        """
        try:
            headers = {'Content-Type': 'application/json'}
            payload = {
                "jsonrpc": "2.0",
                "method": "condenser_api.get_reward_fund",
                "params": [fund_name],
                "id": 1
            }
            
            response = requests.post(self.working_node, json=payload, headers=headers, timeout=5)
            
            if response.status_code == 200:
                result = response.json()
                if 'result' in result:
                    # Convert amounts to a more usable format
                    reward_data = result['result']
                    return reward_data
                    
            logger.warning(f"Failed to get reward fund data from {self.working_node}")
            self.switch_to_backup_node()
            return self.get_reward_fund(fund_name)  # Try again with new node
            
        except Exception as e:
            logger.error(f"Error getting reward fund: {str(e)}")
            self.switch_to_backup_node()
            return self.get_reward_fund(fund_name)  # Try again with new node
    
    def get_current_median_history_price(self):
        """Get the current median price history from the blockchain.
        
        Returns:
            dict: Price data with base and quote values
        """
        try:
            headers = {'Content-Type': 'application/json'}
            payload = {
                "jsonrpc": "2.0",
                "method": "condenser_api.get_current_median_history_price",
                "params": [],
                "id": 1
            }
            
            response = requests.post(self.working_node, json=payload, headers=headers, timeout=5)
            
            if response.status_code == 200:
                result = response.json()
                if 'result' in result:
                    # Parse price data into a usable format
                    price_data = result['result']
                    
                    # Convert price strings to structured data
                    base_parts = price_data['base'].split(' ')
                    quote_parts = price_data['quote'].split(' ')
                    
                    return {
                        'base': {
                            'amount': float(base_parts[0]),
                            'symbol': base_parts[1]
                        },
                        'quote': {
                            'amount': float(quote_parts[0]),
                            'symbol': quote_parts[1]
                        }
                    }
                    
            logger.warning(f"Failed to get price data from {self.working_node}")
            self.switch_to_backup_node()
            return self.get_current_median_history_price()  # Try again with new node
            
        except Exception as e:
            logger.error(f"Error getting current median history price: {str(e)}")
            self.switch_to_backup_node()
            return self.get_current_median_history_price()  # Try again with new node

    def get_dynamic_global_properties(self):
        """Get dynamic global properties from the blockchain.
        
        Returns:
            dict: Global properties data
        """
        try:
            headers = {'Content-Type': 'application/json'}
            payload = {
                "jsonrpc": "2.0",
                "method": "condenser_api.get_dynamic_global_properties",
                "params": [],
                "id": 1
            }
            
            response = requests.post(self.working_node, json=payload, headers=headers, timeout=5)
            
            if response.status_code == 200:
                result = response.json()
                if 'result' in result:
                    props = result['result']
                    
                    # Parse vesting fund and shares to structured data
                    if 'total_vesting_fund_steem' in props:
                        fund_parts = props['total_vesting_fund_steem'].split(' ')
                        props['total_vesting_fund_steem'] = {
                            'amount': float(fund_parts[0]),
                            'symbol': fund_parts[1]
                        }
                    elif 'total_vesting_fund_hive' in props:
                        fund_parts = props['total_vesting_fund_hive'].split(' ')
                        props['total_vesting_fund_steem'] = {  # Use the same key for compatibility
                            'amount': float(fund_parts[0]),
                            'symbol': fund_parts[1]
                        }
                        
                    if 'total_vesting_shares' in props:
                        shares_parts = props['total_vesting_shares'].split(' ')
                        props['total_vesting_shares'] = {
                            'amount': float(shares_parts[0]),
                            'symbol': shares_parts[1]
                        }
                        
                    return props
                    
            logger.warning(f"Failed to get global properties from {self.working_node}")
            self.switch_to_backup_node()
            return self.get_dynamic_global_properties()  # Try again with new node
            
        except Exception as e:
            logger.error(f"Error getting dynamic global properties: {str(e)}")
            self.switch_to_backup_node()
            return self.get_dynamic_global_properties()  # Try again with new node

    def _load_cache(self):
        """Carica la cache dei votanti dal file se esiste."""
        try:
            if os.path.exists(self._cache_path):
                with open(self._cache_path, 'rb') as f:
                    cached_data = pickle.load(f)
                    # Verifica che la cache non sia vecchia (più di 7 giorni)
                    if 'timestamp' in cached_data and (datetime.now() - cached_data['timestamp']).days < 7:
                        self._voters_cache = cached_data.get('voters', {})
                        logger.info(f"Caricati {len(self._voters_cache)} record dalla cache dei votanti")
                    else:
                        logger.info("Cache dei votanti scaduta, verrà rigenerata")
        except Exception as e:
            logger.warning(f"Errore nel caricamento della cache dei votanti: {e}")
            self._voters_cache = {}
    
    def _save_cache(self):
        """Salva la cache dei votanti su file."""
        try:
            # Assicurati che la directory esista
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            
            cache_data = {
                'timestamp': datetime.now(),
                'voters': self._voters_cache
            }
            
            with open(self._cache_path, 'wb') as f:
                pickle.dump(cache_data, f)
            logger.info(f"Salvati {len(self._voters_cache)} record nella cache dei votanti")
        except Exception as e:
            logger.warning(f"Errore nel salvataggio della cache dei votanti: {e}")

    def get_post_voters(self, post_url, min_importance=0.0, use_cache=True):
        """Get the voters of a post sorted by importance (vesting shares or rshares)
        
        Args:
            post_url (str): The URL or identifier of the post
            min_importance (float): Minimum importance threshold to filter voters
            use_cache (bool): Whether to use cached voters data if available
            
        Returns:
            list: List of dictionaries with voter information
        """
        # Check cache first if enabled
        cache_key = f"{post_url}_{min_importance}"
        if use_cache and cache_key in self._voters_cache:
            logger.info(f"Utilizzando dati in cache per {post_url}")
            return self._voters_cache[cache_key]
        
        try:
            # Ottimizzazione: limita il numero di richieste parallele
            start_time = time.time()
            from beem.vote import Vote
            
            # Usa un timeout più breve per evitare blocchi lunghi
            comment = Comment(post_url, blockchain_instance=self.blockchain)
            # Ottiene i dati completi del post
            comment_data = comment.json()
            
            # Estrai la data di creazione del post e assicurati che abbia timezone UTC
            post_created = comment_data.get('created')
            if isinstance(post_created, str):
                post_created = datetime.strptime(post_created, '%Y-%m-%dT%H:%M:%S')
                # Assicurati che post_created sia timezone-aware (UTC)
                if post_created.tzinfo is None:
                    post_created = post_created.replace(tzinfo=timezone.utc)
            
            # Ottiene i voti con i dettagli completi
            active_votes = comment_data.get('active_votes', [])
            if not active_votes and hasattr(comment, 'get_active_votes'):
                active_votes = comment.get_active_votes()
            
            logger.info(f"Trovati {len(active_votes)} voti per il post {post_url}")
            
            # Ottimizzazione: limita il numero di voti da analizzare per post con molti voti
            max_votes_to_process = 50  # Imposta un limite ragionevole
            if len(active_votes) > max_votes_to_process:
                # Ordina preliminarmente per rshares se disponibili
                if 'rshares' in active_votes[0]:
                    active_votes.sort(key=lambda v: float(v.get('rshares', 0)), reverse=True)
                active_votes = active_votes[:max_votes_to_process]
                logger.info(f"Limitata analisi ai top {max_votes_to_process} voti per {post_url}")
            
            # Get voters data
            voters_data = []
            processed_voters = 0
            
            # Processa i voti più significativi (in batch per maggiore efficienza)
            for vote_data in active_votes:
                try:
                    voter_name = vote_data['voter']
                    processed_voters += 1
                    
                    # Prima prova a ottenere rshares direttamente dal voto (più veloce)
                    vote_rshares = float(vote_data.get('rshares', 0))
                    
                    # Se non ci sono rshares significativi, passa al votante successivo (ottimizzazione)
                    if vote_rshares < 1000000 and processed_voters > 10:
                        continue
                    
                    # Estrai informazioni dirette dal voto quando disponibili
                    vote_percent = float(vote_data.get('percent', 0))
                    
                    # Determina quando è avvenuto il voto
                    vote_time = vote_data.get('time')
                    if isinstance(vote_time, str):
                        vote_time = datetime.strptime(vote_time, '%Y-%m-%dT%H:%M:%S')
                        if vote_time.tzinfo is None:
                            vote_time = vote_time.replace(tzinfo=timezone.utc)
                    
                    # Se non abbiamo il timestamp nel voto base, prova con l'oggetto Vote (più lento)
                    if not vote_time:
                        try:
                            vote = Vote(voter_name, post_url, blockchain_instance=self.blockchain)
                            vote_time = vote.time
                            if vote_time.tzinfo is None:
                                vote_time = vote_time.replace(tzinfo=timezone.utc)
                            
                            if not vote_rshares or vote_rshares == 0:
                                vote_rshares = float(vote.rshares)
                            
                            if not vote_percent or vote_percent == 0:
                                vote_percent = vote.weight
                        except Exception as vote_error:
                            # Se fallisce anche questo, usa una stima
                            if 'last_update' in vote_data:
                                vote_time = vote_data.get('last_update')
                                if isinstance(vote_time, str):
                                    vote_time = datetime.strptime(vote_time, '%Y-%m-%dT%H:%M:%S')
                                    if vote_time.tzinfo is None:
                                        vote_time = vote_time.replace(tzinfo=timezone.utc)
                            else:
                                # Ultimo tentativo: usa il timestamp attuale
                                vote_time = datetime.now(timezone.utc)
                    
                    # Calcola il ritardo del voto in minuti
                    vote_delay_minutes = int((vote_time - post_created).total_seconds() / 60)
                    
                    # Calcola l'importanza usando rshares direttamente se disponibili
                    importance = vote_rshares / 1e12  # Normalizza per leggibilità
                    
                    # Solo se l'importanza è troppo bassa, ottieni ulteriori informazioni sull'account
                    vests = 0
                    reputation = 0
                    
                    if importance < min_importance and processed_voters <= 10:
                        try:
                            # Ottimizzazione: ottieni informazioni sull'account solo se necessario
                            voter_account = Account(voter_name, blockchain_instance=self.blockchain)
                            vests = float(voter_account['vesting_shares'].amount) + float(voter_account['received_vesting_shares'].amount) - float(voter_account['delegated_vesting_shares'].amount)
                            importance = max(importance, vests / 1e6)  # Usa il valore maggiore tra rshares e vests
                            reputation = voter_account.get_reputation()
                        except Exception as e:
                            logger.debug(f"Non è stato possibile ottenere dettagli completi per {voter_name}: {e}")
                    
                    if importance >= min_importance or vote_rshares >= min_importance * 1e12:
                        voters_data.append({
                            'voter': voter_name,
                            'weight': vote_percent,
                            'rshares': vote_rshares,
                            'vesting_shares': vests,
                            'importance': importance,
                            'vote_time': vote_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(vote_time, 'strftime') else vote_time,
                            'vote_delay_minutes': vote_delay_minutes,
                            'reputation': reputation
                        })
                except Exception as e:
                    logger.warning(f"Error processing voter {vote_data.get('voter', 'unknown')}: {str(e)}")
                    continue
            
            # Sort by importance (vesting shares o rshares)
            voters_data.sort(key=lambda x: x['importance'], reverse=True)
            
            # Logga il tempo totale di esecuzione e i primi votanti importanti
            execution_time = time.time() - start_time
            logger.info(f"Analisi votanti completata in {execution_time:.2f} secondi")
            
            if voters_data:
                top_voters = [f"{v['voter']} (dopo {v['vote_delay_minutes']} min., importanza: {v['importance']:.2f})" 
                            for v in voters_data[:3]]
                logger.info(f"Top votanti per {post_url}: {', '.join(top_voters)}")
            
            # Save to cache if the operation was successful
            if use_cache:
                self._voters_cache[cache_key] = voters_data
                # Save cache every 10 new entries
                if len(self._voters_cache) % 10 == 0:
                    self._save_cache()
            
            return voters_data
            
        except Exception as e:
            logger.error(f"Error getting post voters: {str(e)}")
            return []

    def cleanup(self):
        """Pulisce e salva la cache a fine esecuzione."""
        if self._voters_cache:
            self._save_cache()
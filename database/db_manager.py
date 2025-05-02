import sqlite3
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self, db_path="database/author_stats.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_database()

    def init_database(self):
        """Initialize database with required tables."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Create authors table with platform
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS authors (
                    author_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author_name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    UNIQUE(author_name, platform)
                )
                ''')

                # Create author statistics table
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS author_statistics (
                    stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author_id INTEGER,
                    avg_efficiency REAL,
                    reputation REAL,
                    avg_payout REAL,
                    training_date TIMESTAMP,
                    model_version TEXT,
                    FOREIGN KEY (author_id) REFERENCES authors(author_id)
                )
                ''')

                # Create aggregated statistics table with optimal delay fields included
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS aggregated_statistics (
                    author_id INTEGER,
                    avg_efficiency_all_time REAL,
                    reputation_all_time REAL,
                    avg_payout_all_time REAL,
                    total_trainings INTEGER,
                    last_updated TIMESTAMP,
                    optimal_delay INTEGER DEFAULT 1440,
                    best_efficiency REAL DEFAULT 0,
                    FOREIGN KEY (author_id) REFERENCES authors(author_id),
                    UNIQUE(author_id)
                )
                ''')

                # Create voting_delays table
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS voting_delays (
                    delay_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author_id INTEGER,
                    vote_delay INTEGER,  -- in minutes
                    efficiency REAL,
                    post_url TEXT,
                    voted_at TIMESTAMP,
                    FOREIGN KEY (author_id) REFERENCES authors(author_id)
                )
                ''')

                conn.commit()
                logger.info("Database initialized successfully")

        except sqlite3.Error as e:
            logger.error(f"Database initialization error: {e}")
            raise

    def update_author_stats(self, author_name, efficiency, reputation, payout, model_version, platform):
        """Update author statistics in the database."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Insert or get author with platform
                cursor.execute('''
                INSERT OR IGNORE INTO authors (author_name, platform)
                VALUES (?, ?)
                ''', (author_name, platform))
                
                cursor.execute('''
                SELECT author_id FROM authors 
                WHERE author_name = ? AND platform = ?
                ''', (author_name, platform))
                author_id = cursor.fetchone()[0]

                # Insert new statistics
                cursor.execute('''
                INSERT INTO author_statistics 
                (author_id, avg_efficiency, reputation, avg_payout, training_date, model_version)
                VALUES (?, ?, ?, ?, ?, ?)
                ''', (author_id, efficiency, reputation, payout, 
                     datetime.now(), model_version))

                # Update aggregated statistics
                cursor.execute('''
                INSERT INTO aggregated_statistics 
                (author_id, avg_efficiency_all_time, reputation_all_time, 
                 avg_payout_all_time, total_trainings, last_updated)
                SELECT 
                    author_id,
                    AVG(avg_efficiency),
                    AVG(reputation),
                    AVG(avg_payout),
                    COUNT(*),
                    CURRENT_TIMESTAMP
                FROM author_statistics
                WHERE author_id = ?
                GROUP BY author_id
                ON CONFLICT(author_id) DO UPDATE SET
                    avg_efficiency_all_time = excluded.avg_efficiency_all_time,
                    reputation_all_time = excluded.reputation_all_time,
                    avg_payout_all_time = excluded.avg_payout_all_time,
                    total_trainings = excluded.total_trainings,
                    last_updated = CURRENT_TIMESTAMP
                ''', (author_id,))

                conn.commit()

        except sqlite3.Error as e:
            logger.error(f"Error updating author stats: {e}")
            raise

    def update_voting_delay(self, author_name, platform, vote_delay, efficiency, post_url):
        """Record a new voting delay and its efficiency."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Get author_id
                cursor.execute('''
                SELECT author_id FROM authors 
                WHERE author_name = ? AND platform = ?
                ''', (author_name, platform))
                result = cursor.fetchone()
                
                if result:
                    author_id = result[0]
                    
                    # Insert new voting delay record
                    cursor.execute('''
                    INSERT INTO voting_delays 
                    (author_id, vote_delay, efficiency, post_url, voted_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ''', (author_id, vote_delay, efficiency, post_url))
                    
                    # Update optimal delay if this vote was more efficient
                    cursor.execute('''
                    UPDATE aggregated_statistics
                    SET optimal_delay = ?,
                        best_efficiency = ?
                    WHERE author_id = ? 
                    AND (best_efficiency < ? OR best_efficiency IS NULL)
                    ''', (vote_delay, efficiency, author_id, efficiency))
                    
                    conn.commit()
                    
        except sqlite3.Error as e:
            logger.error(f"Error updating voting delay: {e}")
            raise

    def get_optimal_delay(self, author_name, platform):
        """Get the optimal voting delay for an author."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                SELECT 
                    a.author_name,
                    ag.optimal_delay,
                    ag.best_efficiency,
                    (
                        SELECT AVG(vd.vote_delay)
                        FROM voting_delays vd
                        WHERE vd.author_id = a.author_id
                        AND vd.efficiency >= (ag.best_efficiency * 0.8)
                        ORDER BY vd.voted_at DESC
                        LIMIT 5
                    ) as recent_good_delay
                FROM authors a
                JOIN aggregated_statistics ag ON a.author_id = ag.author_id
                WHERE a.author_name = ? AND a.platform = ?
                ''', (author_name, platform))
                
                result = cursor.fetchone()
                if result:
                    return {
                        'author_name': result[0],
                        'optimal_delay': result[1],
                        'best_efficiency': result[2],
                        'recent_good_delay': result[3] or result[1]  # fallback to optimal if no recent
                    }
                return None
                
        except sqlite3.Error as e:
            logger.error(f"Error getting optimal delay: {e}")
            return None

    def get_author_stats(self, author_name, platform):
        """Retrieve author statistics from database."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                SELECT 
                    a.author_name,
                    a.platform,
                    ag.avg_efficiency_all_time,
                    ag.reputation_all_time,
                    ag.avg_payout_all_time,
                    ag.total_trainings,
                    ag.last_updated
                FROM authors a
                JOIN aggregated_statistics ag ON a.author_id = ag.author_id
                WHERE a.author_name = ? AND a.platform = ?
                ''', (author_name, platform))
                
                result = cursor.fetchone()
                if result:
                    return {
                        'author_name': result[0],
                        'platform': result[1],
                        'avg_efficiency': result[2],
                        'reputation': result[3],
                        'avg_payout': result[4],
                        'total_trainings': result[5],
                        'last_updated': result[6]
                    }
                return None

        except sqlite3.Error as e:
            logger.error(f"Error retrieving author stats: {e}")
            raise

    def get_all_authors(self, platform):
        """
        Get all authors and their statistics for a specific platform.
        
        Args:
            platform (str): The platform to filter authors ("STEEM" or "HIVE")
            
        Returns:
            list: List of dictionaries containing author information and statistics
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                SELECT 
                    a.author_name,
                    a.platform,
                    ag.avg_efficiency_all_time,
                    ag.reputation_all_time,
                    ag.avg_payout_all_time,
                    ag.total_trainings,
                    ag.last_updated
                FROM authors a
                LEFT JOIN aggregated_statistics ag ON a.author_id = ag.author_id
                WHERE a.platform = ?
                AND ag.last_updated IS NOT NULL
                ORDER BY ag.avg_efficiency_all_time DESC
                ''', (platform,))
                
                columns = [
                    'author_name', 'platform', 'avg_efficiency',
                    'reputation', 'avg_payout', 'total_trainings',
                    'last_updated'
                ]
                
                results = []
                for row in cursor.fetchall():
                    author_data = dict(zip(columns, row))
                    results.append(author_data)
                
                logger.info(f"Retrieved {len(results)} authors for {platform}")
                return results

        except sqlite3.Error as e:
            logger.error(f"Error getting authors for {platform}: {e}")
            return []

    def get_recent_voting_history(self, author_name, platform, limit=5):
        """
        Ottiene la storia dei voti più recenti per un autore specifico.
        
        Args:
            author_name (str): Nome dell'autore
            platform (str): Piattaforma (STEEM o HIVE)
            limit (int): Numero di voti recenti da recuperare
            
        Returns:
            list: Lista di voti recenti con i loro dettagli
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Ottieni l'author_id
                cursor.execute('''
                SELECT author_id FROM authors 
                WHERE author_name = ? AND platform = ?
                ''', (author_name, platform))
                
                result = cursor.fetchone()
                if not result:
                    return []
                    
                author_id = result[0]
                
                # Ottieni i voti più recenti con il loro timing e efficienza
                cursor.execute('''
                SELECT 
                    vote_delay,
                    efficiency,
                    post_url,
                    voted_at
                FROM voting_delays
                WHERE author_id = ?
                ORDER BY voted_at DESC
                LIMIT ?
                ''', (author_id, limit))
                
                columns = ['vote_delay', 'efficiency', 'post_url', 'voted_at']
                votes = []
                
                for row in cursor.fetchall():
                    vote_data = dict(zip(columns, row))
                    votes.append(vote_data)
                
                return votes
                
        except sqlite3.Error as e:
            logger.error(f"Errore nel recupero della storia di voto recente: {e}")
            return []

    def get_last_optimal_delay(self, author_name, platform):
        """
        Ottiene il ritardo di voto ottimale basato solo sull'ultimo post che ha ottenuto una buona efficienza.
        
        Args:
            author_name (str): Nome dell'autore
            platform (str): Piattaforma (STEEM o HIVE)
            
        Returns:
            int: Ritardo ottimale in minuti o None se non disponibile
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Ottieni l'author_id
                cursor.execute('''
                SELECT author_id FROM authors 
                WHERE author_name = ? AND platform = ?
                ''', (author_name, platform))
                
                result = cursor.fetchone()
                if not result:
                    return None
                    
                author_id = result[0]
                
                # Ottieni dal database l'efficienza massima per questo autore
                cursor.execute('''
                SELECT best_efficiency 
                FROM aggregated_statistics 
                WHERE author_id = ?
                ''', (author_id,))
                
                best_result = cursor.fetchone()
                if not best_result or not best_result[0]:
                    return None
                
                best_efficiency = best_result[0]
                threshold = best_efficiency * 0.7  # Considera voti con efficienza al 70% del massimo
                
                # Ottieni il voto più recente che ha raggiunto almeno il 70% della migliore efficienza
                cursor.execute('''
                SELECT vote_delay
                FROM voting_delays
                WHERE author_id = ? AND efficiency >= ?
                ORDER BY voted_at DESC
                LIMIT 1
                ''', (author_id, threshold))
                
                delay_result = cursor.fetchone()
                if delay_result:
                    return delay_result[0]
                
                return None
                
        except sqlite3.Error as e:
            logger.error(f"Errore nel recupero dell'ultimo ritardo ottimale: {e}")
            return None
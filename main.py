from datetime import datetime, timezone
import random
import time
import requests
import json
import os
import pandas as pd
import numpy as np
from pathlib import Path
from beem.account import Account
from beem import Steem, Hive
from beem.nodelist import NodeList
from beem.vote import Vote
from beem.comment import Comment
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, classification_report, precision_score, recall_score, f1_score, confusion_matrix
from xgboost import XGBClassifier, XGBRegressor
from reporting.performance_analyzer import PerformanceAnalyzer
from reporting.excel_reporter import ExcelReporter

from settings.config import (
    BLOCKCHAIN_CHOICE, CURATOR, STEEM_NODES, HIVE_NODES,
    MODE_CHOICES, OPERATION_MODE, TEST_SIZE, MAX_RESULTS,
    DIRECTORIES, MODEL_DIR, REPORT_DIR
)

from settings.logging_config import logger
from utils.beem_requests import BlockchainConnector
from database.db_manager import DatabaseManager
from utils.vote import calculate_vote_value_sync

blockchain_connector = BlockchainConnector(BLOCKCHAIN_CHOICE)
db_manager = DatabaseManager()

def update_efficiency_average(author, current_efficiency, author_efficiency_dict):
    """Aggiorna l'efficienza media di un autore."""
    if author not in author_efficiency_dict:
        author_efficiency_dict[author] = {
            'total': current_efficiency,
            'count': 1,
            'average': current_efficiency
        }
    else:
        author_data = author_efficiency_dict[author]
        author_data['total'] += current_efficiency
        author_data['count'] += 1
        author_data['average'] = author_data['total'] / author_data['count']
    
    return author_efficiency_dict[author]['average']

def update_payout_average(author, current_payout, author_payout_dict):
    """Updates the average payout for an author."""
    if author not in author_payout_dict:
        author_payout_dict[author] = {
            'total': current_payout,
            'count': 1,
            'average': current_payout
        }
    else:
        payout_data = author_payout_dict[author]
        payout_data['total'] += current_payout
        payout_data['count'] += 1
        payout_data['average'] = payout_data['total'] / payout_data['count']
    
    return author_payout_dict[author]['average']

def ensure_directories():
    """Create necessary directories if they don't exist."""
    directories = ['models', 'reports']
    for directory in directories:
        if not os.path.exists(directory):
            os.makedirs(directory)
            logger.info(f"Created directory: {directory}")

# Add this function after ensure_directories()
def load_or_create_model(model_path=None, model_class=None, X_train=None, y_train=None):
    """Load existing model or create new one if it doesn't exist."""
    # For TESTING/PRODUCTION modes - only loading existing models
    if X_train is None or y_train is None:
        try:
            if not model_path:
                clf_path = os.path.join('models', 'classifier_model.json')
                reg_path = os.path.join('models', 'regressor_model.json')
                
                if not (os.path.exists(clf_path) and os.path.exists(reg_path)):
                    logger.error("Model files not found")
                    return None, None
                
                clf_model = XGBClassifier()
                reg_model = XGBRegressor()
                
                clf_model.load_model(clf_path)
                reg_model.load_model(reg_path)
                
                logger.info("Successfully loaded existing models")
                return clf_model, reg_model
            else:
                logger.error("Invalid parameters for model loading")
                return None, None
                
        except Exception as e:
            logger.error(f"Error loading models: {e}")
            return None, None
    
    # For TRAINING mode - creating/updating models with training data
    try:
        if os.path.exists(model_path):
            model = model_class()
            model.load_model(model_path)
            logger.info(f"Loaded existing model from: {model_path}")
            
            # Update model with new data
            model.fit(X_train, y_train, xgb_model=model_path)
            logger.info(f"Updated model with new training data")
        else:
            model = model_class()
            model.fit(X_train, y_train)
            logger.info(f"Created new model as {model_path} did not exist")
        
        return model
    except Exception as e:
        logger.error(f"Error loading/creating model: {e}")
        # Fallback to creating new model
        model = model_class()
        model.fit(X_train, y_train)
        logger.info("Created new model due to error loading existing one")
        return model

def process_data_for_mode(df, mode, clf_model=None, reg_model=None):
    """Process data based on operation mode."""
    classification_features = ['author_avg_efficiency', 'author_reputation', 'author_avg_payout']
    
    if mode == "TRAINING":
        # Save author statistics and voting delays to database
        model_version = datetime.now().strftime("%Y%m%d_%H%M%S")
        for _, row in df.iterrows():
            # Update author stats
            db_manager.update_author_stats(
                author_name=row['Author'],
                efficiency=row['author_avg_efficiency'],
                reputation=row['author_reputation'],
                payout=row['author_avg_payout'],
                model_version=model_version,
                platform=BLOCKCHAIN_CHOICE
            )
            
            # Update voting delays
            db_manager.update_voting_delay(
                author_name=row['Author'],
                platform=BLOCKCHAIN_CHOICE,
                vote_delay=row['vote_delay'],
                efficiency=row['like_efficiency'],
                post_url=row['Post']
            )
        
        # Training mode - use part of data for training, part for testing
        X = df[classification_features]
        y_clf = df['success']
        y_reg = df['like_efficiency']

        X_train, X_test, y_clf_train, y_clf_test = train_test_split(
            X, y_clf, test_size=TEST_SIZE, random_state=42
        )
        
        # For regression, include vote_delay
        X_reg = df[classification_features + ['vote_delay']]
        X_reg_train, X_reg_test, y_reg_train, y_reg_test = train_test_split(
            X_reg, y_reg, test_size=TEST_SIZE, random_state=42
        )

        # Train and save models
        clf_model = train_classifier_model(X_train, y_clf_train, X_test, y_clf_test)
        reg_model = train_regressor_model(X_reg_train, y_reg_train)

        # Generate performance reports
        generate_performance_reports(df, X_test, y_clf_test, clf_model, reg_model)

    elif mode == "TESTING":
        if clf_model is None or reg_model is None:
            raise ValueError("Models must be provided for testing mode")
        
        X = df[classification_features]
        y_clf = df['success']
        
        generate_performance_reports(df, X, y_clf, clf_model, reg_model)

    elif mode == "PRODUCTION":
        if clf_model is None or reg_model is None:
            raise ValueError("Models must be provided for production mode")
        
        X = df[classification_features]
        generate_predictions_report(df, X, clf_model, reg_model)

def train_classifier_model(X_train, y_train, X_test, y_test):
    """Train and evaluate classifier model."""
    model_path = os.path.join('models', 'classifier_model.json')
    clf_model = load_or_create_model(model_path, XGBClassifier, X_train, y_train)
    
    # Evaluate metrics
    y_pred = clf_model.predict(X_test)
    accuracy = accuracy_score(y_test, y_pred)
    class_report = classification_report(y_test, y_pred)
    
    logger.info(f"Classifier Accuracy Score: {accuracy:.4f}")
    logger.info("Classification Report:\n" + class_report)
    
    # Save model
    clf_model.save_model(model_path)
    return clf_model

def train_regressor_model(X_train, y_train):
    """Train regressor model."""
    model_path = os.path.join('models', 'regressor_model.json')
    reg_model = load_or_create_model(model_path, XGBRegressor, X_train, y_train)
    reg_model.save_model(model_path)
    return reg_model

def save_excel_reports(prediction_df, author_stats):
    """Save prediction results and author statistics to Excel file."""
    excel_reporter = ExcelReporter('reports', CURATOR)
    excel_reporter.save_prediction_reports(prediction_df, author_stats)

def save_production_report(prediction_df):
    """Save production predictions to Excel file."""
    excel_reporter = ExcelReporter('reports', CURATOR)
    excel_reporter.save_production_report(prediction_df)

def analyze_performance_results(prediction_df):
    """Analyze and generate detailed performance results."""
    analyzer = PerformanceAnalyzer()
    return analyzer.analyze_performance(prediction_df)

# Modify generate_performance_reports to include performance analysis
def generate_performance_reports(df, X_test, y_test, clf_model, reg_model):
    """Generate comprehensive performance reports."""
    predictions_list = make_predictions(X_test, df, clf_model, reg_model)
    
    # Create prediction DataFrame
    prediction_df = create_prediction_dataframe(df, X_test, y_test, predictions_list)
    
    # Generate author statistics
    author_stats = generate_author_statistics(df)
    
    # Save reports
    save_excel_reports(prediction_df, author_stats)
    
    # Analyze and log detailed performance results
    analyze_performance_results(prediction_df)

def generate_predictions_report(df, X, clf_model, reg_model):
    """Generate production predictions report."""
    predictions_list = make_predictions(X, df, clf_model, reg_model)
    
    # Create simplified prediction DataFrame for production
    prediction_df = pd.DataFrame({
        'Post': df['Post'],
        'Author': df['Author'],
        'vote_decision': [p['vote_decision'] for p in predictions_list],
        'optimal_vote_delay_minutes': [p['optimal_vote_delay_minutes'] for p in predictions_list],
        'predicted_efficiency': [p['predicted_efficiency'] for p in predictions_list]
    })
    
    # Save production report
    save_production_report(prediction_df)

def make_predictions(X_test, df, clf_model, reg_model):
    """Make predictions using both classifier and regressor models."""
    predictions_list = []
    
    for index, row in X_test.iterrows():
        # Create features without vote_delay for classification
        post_features = row[['author_avg_efficiency', 'author_reputation', 'author_avg_payout']].to_frame().T
        author = df.loc[index, 'Author']
        
        # Make vote decision without considering delay
        vote_decision = clf_model.predict(post_features)[0]
        
        if vote_decision == 0:
            vote_decision_result = 0
            optimal_delay = None
            predicted_eff = None
        else:
            # Get optimal delay from database
            optimal_delay_data = db_manager.get_optimal_delay(author, BLOCKCHAIN_CHOICE)
            optimal_delay = optimal_delay_data['recent_good_delay'] if optimal_delay_data else 1440
            
            # Predict efficiency with optimal delay
            modified_features = post_features.copy()
            modified_features["vote_delay"] = optimal_delay
            predicted_eff = reg_model.predict(modified_features)[0]
            vote_decision_result = 1
        
        predictions_list.append({
            "vote_decision": vote_decision_result,
            "optimal_vote_delay_minutes": optimal_delay,
            "predicted_efficiency": predicted_eff
        })
    
    return predictions_list

def create_prediction_dataframe(df, X_test, y_test, predictions_list):
    """Create a DataFrame with predictions and actual values."""
    prediction_df = df.loc[X_test.index, ['Post', 'Author', 'like_efficiency', 'vote_delay']].copy()
    prediction_df = prediction_df.rename(columns={'vote_delay': 'actual_vote_delay_minutes'})
    
    # Add actual and predicted values
    prediction_df['real_success'] = y_test.values
    prediction_df['vote_decision'] = [p['vote_decision'] for p in predictions_list]
    prediction_df['optimal_vote_delay_minutes'] = [p['optimal_vote_delay_minutes'] for p in predictions_list]
    prediction_df['predicted_efficiency'] = [p['predicted_efficiency'] for p in predictions_list]
    
    # Calculate delay difference
    prediction_df['delay_difference_minutes'] = prediction_df.apply(
        lambda row: row['optimal_vote_delay_minutes'] - row['actual_vote_delay_minutes'] 
        if row['optimal_vote_delay_minutes'] is not None else None,
        axis=1
    )
    
    return prediction_df

def generate_author_statistics(df):
    """Generate comprehensive author statistics."""
    author_stats = pd.DataFrame({
        'Author': df['Author'].unique(),
        'Total_Posts': df.groupby('Author').size(),
        'Avg_Efficiency': df.groupby('Author')['like_efficiency'].mean(),
        'Success_Rate': df.groupby('Author')['success'].mean() * 100,
        'Avg_Payout': df.groupby('Author')['author_avg_payout'].mean(),
        'Avg_Vote_Delay': df.groupby('Author')['vote_delay'].mean(),
        'Reputation': df.groupby('Author')['author_reputation'].first()
    }).reset_index(drop=True)
    
    return author_stats

# Modify the main function to properly handle models
def main():
    if OPERATION_MODE not in MODE_CHOICES:
        raise ValueError(f"Invalid mode. Choose from {MODE_CHOICES}")

    logger.info(f"Starting data processing in {OPERATION_MODE} mode...")
    
    # Load models if needed
    clf_model, reg_model = None, None
    if OPERATION_MODE in ["TESTING", "PRODUCTION"]:
        clf_model, reg_model = load_or_create_model()
        if clf_model is None or reg_model is None:
            logger.error("Required models not found for testing/production mode")
            return
    
    # Initialize blockchain connection
    try:
        blockchain = blockchain_connector.blockchain
        power_symbol = blockchain_connector.power_symbol
        logger.info(f"Connected to {BLOCKCHAIN_CHOICE} node: {blockchain_connector.working_node}")
    except Exception as e:
        logger.error(f"Failed to initialize blockchain connection: {str(e)}")
        return

    # Add retry decorator for blockchain operations
    def retry_on_failure(max_retries=3, delay=1):
        def decorator(func):
            def wrapper(*args, **kwargs):
                nonlocal blockchain
                for attempt in range(max_retries):
                    try:
                        return func(*args, **kwargs)
                    except Exception as e:
                        if attempt == max_retries - 1:
                            raise
                        logger.warning(f"Operation failed, retrying... ({attempt + 1}/{max_retries})")
                        time.sleep(delay)
                        try:
                            new_node = blockchain_connector.get_working_node(BLOCKCHAIN_CHOICE)
                            blockchain = Hive(node=new_node) if BLOCKCHAIN_CHOICE == "HIVE" else Steem(node=new_node)
                            logger.info(f"Switched to new node: {new_node}")
                        except Exception as e:
                            logger.warning(f"Failed to switch node: {str(e)}")
            return wrapper
        return decorator

    @retry_on_failure()
    def get_post_data(post_identifier):
        return Comment(post_identifier, blockchain_instance=blockchain)

    @retry_on_failure()
    def get_vote_data(vote_identifier):
        return Vote(vote_identifier, blockchain_instance=blockchain)

    # Initialize data collection
    account = Account(CURATOR, blockchain_instance=blockchain)
    author_efficiency_dict = {}
    author_payout_dict = {}
    data = {
        'voting_power': [], 
        'vote_delay': [], 
        'reward': [],
        'efficiency': [], 
        'author_avg_efficiency': [], 
        'success': [],
        'author_reputation': [], 
        'Post': [], 
        'Author': [],
        'like_efficiency': [], 
        'author_avg_payout': [],
        'optimal_delay': []  # Add this line
    }

    # Collect historical data
    count = 0
    try:
        history_data, blockchain = blockchain_connector.get_account_history(CURATOR)
        
        for h in history_data:
            try:
                # Extract post info
                author = h.get('comment_author') or h.get('author')
                permlink = h.get('comment_permlink') or h.get('permlink')
                post_identifier = f"@{author}/{permlink}"
                post = get_post_data(post_identifier)

                # Get post data
                post_data = collect_post_data(
                    post, h, author, post_identifier, CURATOR,
                    blockchain, get_vote_data, author_efficiency_dict,
                    author_payout_dict, power_symbol
                )
                
                # Update data dictionary
                for key, value in post_data.items():
                    data[key].append(value)

            except Exception as e:
                logger.error(f"Error processing {post_identifier}: {str(e)}")
                continue
                
    except Exception as e:
        logger.error(f"Fatal error in history collection: {str(e)}")
        return

    logger.info("Data collection completed. Starting model processing...")

    # Create DataFrame and process based on operation mode
    df = pd.DataFrame(data)
    ensure_directories()
    process_data_for_mode(df, OPERATION_MODE, clf_model, reg_model)
    
    logger.info("Operation completed successfully.")

def collect_post_data(post, history, author, post_identifier, curator, blockchain, 
                     get_vote_data, author_efficiency_dict, author_payout_dict, power_symbol):
    """Collect all necessary data for a single post."""
    # Get basic post info
    author_reputation = post['author_reputation']
    author_payout_token_dollar = float(str(post['author_payout_value']).split()[0])
    avg_payout = update_payout_average(author, author_payout_token_dollar, author_payout_dict)

    # Calculate times
    post_creation_time = post['created']
    vote_identifier = f"{post_identifier}|{curator}"
    vote = get_vote_data(vote_identifier)
    vote_time = vote.time
    age = (vote_time - post_creation_time).total_seconds()
    vote_delay_minutes = age / 60
    
    weight = vote.weight

    curator = blockchain_connector.get_account_info(CURATOR)

    curator_total_vests = float(curator['received_vesting_shares'].amount) + float(curator['vesting_shares'].amount) - float(curator['delegated_vesting_shares'].amount)
    
    # Get vote value using our new function
    vote_value_result = calculate_vote_value_sync(
        blockchain, 
        vote_percent=vote.percent,  # Vote weight in percent
        effective_vests=curator_total_vests,  # Will use curator's vests
        voting_power=10000  # Default full voting power
    )
    
    teoric_reward = vote_value_result['steem_value'] / 2

    steem_vote_value = vote_value_result['steem_value']
    
    # Calculate the actual reward
    reward_amount_vests = float(history['reward']['amount']) / 1e6
    reward_amount = blockchain_connector.convert_vests_to_power(reward_amount_vests)
    
    # Calculate efficiency
    efficiency = (((reward_amount - teoric_reward) / teoric_reward) * 100) if steem_vote_value > 0 else 0
    
    # The rest of your function remains the same
    db_manager.update_voting_delay(
        author_name=author,
        platform=BLOCKCHAIN_CHOICE,
        vote_delay=vote_delay_minutes,
        efficiency=efficiency,
        post_url=post_identifier
    )
    
    # Get optimal delay from database
    optimal_delay_data = db_manager.get_optimal_delay(author, BLOCKCHAIN_CHOICE)
    optimal_delay = optimal_delay_data['recent_good_delay'] if optimal_delay_data else 1440
    
    # Update author efficiency
    avg_efficiency = update_efficiency_average(author, efficiency, author_efficiency_dict)
    
    return {
        'voting_power': vote['percent'] / 100,
        'vote_delay': vote_delay_minutes,
        'optimal_delay': optimal_delay,
        'reward': reward_amount,
        'efficiency': efficiency,
        'author_avg_efficiency': avg_efficiency,
        'success': 1 if efficiency > 80 else 0,
        'author_reputation': author_reputation,
        'Post': post_identifier,
        'Author': author,
        'like_efficiency': efficiency,
        'author_avg_payout': avg_payout
    }

if __name__ == "__main__":
    main()
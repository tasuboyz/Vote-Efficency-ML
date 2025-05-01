import pandas as pd
import os
from settings.logging_config import logger

class ExcelReporter:
    def __init__(self, base_path, curator):
        self.base_path = base_path
        self.curator = curator
        self.filepath = os.path.join(base_path, f'model_performance_{curator}.xlsx')
    
    def save_to_excel(self, data_dict):
        """
        Save multiple dataframes to Excel sheets
        data_dict: Dictionary with sheet_name: dataframe pairs
        """
        with pd.ExcelWriter(self.filepath) as writer:
            for sheet_name, df in data_dict.items():
                df.to_excel(writer, sheet_name=sheet_name, index=False)
        logger.info(f"Excel file saved successfully at: {self.filepath}")
    
    def prepare_rankings_data(self, author_stats):
        """Prepare different rankings from author statistics."""
        return {
            'Top Authors by Efficiency': author_stats.nlargest(10, 'Avg_Efficiency'),
            'Bottom Authors by Efficiency': author_stats.nsmallest(10, 'Avg_Efficiency'),
            'Top Authors by Success Rate': author_stats.nlargest(10, 'Success_Rate'),
            'Bottom Authors by Success Rate': author_stats.nsmallest(10, 'Success_Rate'),
            'Top Authors by Payout': author_stats.nlargest(10, 'Avg_Payout'),
            'Bottom Authors by Payout': author_stats.nsmallest(10, 'Avg_Payout'),
            'Complete Author Stats': author_stats.sort_values('Avg_Efficiency', ascending=False)
        }
    
    def save_prediction_reports(self, prediction_df, author_stats):
        """Save prediction results and author statistics."""
        data_dict = {
            'Predictions': prediction_df,
            **self.prepare_rankings_data(author_stats)
        }
        self.save_to_excel(data_dict)
    
    def save_production_report(self, prediction_df):
        """Save production predictions."""
        production_data = {
            'Production Predictions': prediction_df[
                ['Post', 'Author', 'vote_decision', 
                 'optimal_vote_delay_minutes', 'predicted_efficiency']
            ]
        }
        self.save_to_excel(production_data)
    
    def save_voters_report(self, voters_data):
        """Save important voters analytics.
        
        Args:
            voters_data: List of dictionaries with voter data
        """
        if not voters_data:
            logger.warning("No voters data to save")
            return
        
        # Convert the list to a DataFrame
        df = pd.DataFrame(voters_data)
        
        # Create an Excel file with separated sheets
        voters_filepath = os.path.join(self.base_path, f'important_voters_{self.curator}.xlsx')
        
        with pd.ExcelWriter(voters_filepath) as writer:
            # All voters
            df.to_excel(writer, sheet_name='All Important Voters', index=False)
            
            # Top voters by importance
            top_voters = df.sort_values('importance', ascending=False).head(20)
            top_voters.to_excel(writer, sheet_name='Top 20 by Importance', index=False)
            
            # Early voters (first to vote)
            early_voters = df.sort_values('vote_delay_minutes').head(20)
            early_voters.to_excel(writer, sheet_name='Top 20 Fastest Voters', index=False)
            
            # Frequent voters (most posts voted)
            if 'posts_voted' in df.columns:
                frequent_voters = df.sort_values('posts_voted', ascending=False).head(20)
                frequent_voters.to_excel(writer, sheet_name='Most Frequent Voters', index=False)
            
            # Optimal voting delay statistics
            if len(df) > 0:
                stats = {
                    'Statistic': [
                        'Mean Vote Delay', 
                        'Median Vote Delay',
                        'Min Vote Delay',
                        'Max Vote Delay',
                        'Optimal Vote Window Start',
                        'Optimal Vote Window End'
                    ],
                    'Value': [
                        df['vote_delay_minutes'].mean(),
                        df['vote_delay_minutes'].median(),
                        df['vote_delay_minutes'].min(),
                        df['vote_delay_minutes'].max(),
                        max(df['vote_delay_minutes'].min() - 5, 0),
                        max(df['vote_delay_minutes'].min() - 1, 0)
                    ]
                }
                stats_df = pd.DataFrame(stats)
                stats_df.to_excel(writer, sheet_name='Voting Window Stats', index=False)
                
        logger.info(f"Voters report saved successfully at: {voters_filepath}")
        return voters_filepath
import asyncio
from settings.logging_config import logger
from .beem_requests import BlockchainConnector
from settings.config import CURATOR

# Istanza globale del BlockchainConnector
blockchain_connector = BlockchainConnector(blockchain_type="STEEM")

async def calculate_vote_value(blockchain, vote_percent, effective_vests=None, voting_power=10000):
    """Calculate vote value based on blockchain parameters, similar to the JS implementation."""
    try:
        # Step 1: Get dynamic global properties
        props = blockchain_connector.get_dynamic_global_properties()
        
        # Step 2: Calculate SP/VESTS ratio
        total_vesting_fund_steem = float(props['total_vesting_fund_steem']['amount'])
        total_vesting_shares = float(props['total_vesting_shares']['amount'])
        steem_per_vests = total_vesting_fund_steem / total_vesting_shares
        
        # Step 3: If no vesting shares provided, use current user's
        vesting_shares = effective_vests
        if not vesting_shares:
            # Usiamo blockchain_connector invece di blockchain
            account = blockchain_connector.get_account_info(CURATOR)
            if not account:
                raise Exception('Unable to get account info')
            
            # Ottieni i vesting shares dall'account
            account_vests = float(account['vesting_shares'].amount)
            delegated_out = float(account['delegated_vesting_shares'].amount)
            received_vests = float(account['received_vesting_shares'].amount)
            vesting_shares = account_vests - delegated_out + received_vests
        
        # Step 4: Convert vests to Steem Power
        sp = vesting_shares * steem_per_vests
        
        # Step 5: Calculate 'r' (SP/spv ratio)
        r = sp / steem_per_vests
        
        # Step 6: Calculate 'p' (voting power)
        weight = vote_percent  # Convert percentage to weight (100% = 10000)
        p = (voting_power * weight / 10000 + 49) / 50
        
        # Step 7: Get reward fund con il nuovo metodo - utilizziamo blockchain_connector
        reward_fund = blockchain_connector.get_reward_fund("post")
        
        # Step 8: Calculate rbPrc
        recent_claims = float(reward_fund['recent_claims'])
        # Controlla il formato e adatta di conseguenza
        if 'reward_balance' in reward_fund:
            if isinstance(reward_fund['reward_balance'], str):
                reward_balance = float(reward_fund['reward_balance'].split(' ')[0])
            else:
                reward_balance = float(reward_fund['reward_balance'].amount)
        else:
            raise Exception("Format of reward_fund not recognized")
            
        rb_prc = reward_balance / recent_claims
        
        # Step 9: Get median price con il nuovo metodo - utilizziamo blockchain_connector
        price_info = blockchain_connector.get_current_median_history_price()
        
        base_amount = float(price_info['base']['amount'])
        quote_amount = float(price_info['quote']['amount'])
        steem_to_sbd_rate = base_amount / quote_amount
        
        # Step 10: Apply the official Steem formula
        steem_value = r * p * 100 * rb_prc
        
        # Convert STEEM to USD/SBD using the median price
        usd_value = steem_value * steem_to_sbd_rate
        
        logger.info(f"""Vote Value Calculation:
          - SP: {sp:.3f}
          - Vote Weight: {weight}
          - Voting Power: {voting_power}
          - Price ratio: {steem_to_sbd_rate:.4f}
          - Result: {steem_value:.4f} STEEM (${usd_value:.4f})""")
        
        return {
            "steem_value": float(f"{steem_value:.4f}"),
            "sbd_value": float(f"{usd_value:.4f}"),
            "formula": {
                "r": r,
                "p": p,
                "rb_prc": rb_prc,
                "median": steem_to_sbd_rate
            }
        }
    except Exception as e:
        logger.error(f'Error calculating vote value: {str(e)}')
        return {
            "steem_value": 0,
            "sbd_value": 0,
            "error": str(e)
        }

def calculate_vote_value_sync(blockchain, vote_percent, effective_vests=None, voting_power=10000):
    """Synchronous wrapper for the async calculate_vote_value function."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            calculate_vote_value(blockchain, vote_percent, effective_vests, voting_power)
        )
        return result
    finally:
        loop.close()

def calculate_optimal_weight_by_power(curator_steem_value, important_voters_data, base_weight=100):
    """
    Calcola il peso ottimale del voto in base al rapporto tra il valore del proprio voto
    e il valore stimato dei voti dei whale.
    
    Args:
        curator_steem_value (float): Valore stimato del proprio voto al 100% in STEEM
        important_voters_data (list): Lista di dati sui votanti importanti con la loro importanza
        base_weight (int): Peso di voto base proposto (0-100%)
        
    Returns:
        int: Peso di voto ottimizzato (0-100%)
    """
    if not important_voters_data or curator_steem_value <= 0:
        return base_weight
    
    # Ordina i votanti importanti per importanza (descendente)
    top_voters = sorted(important_voters_data, key=lambda x: x.get('importance', 0), reverse=True)
    
    # Prendi i top 3 votanti più importanti, se disponibili
    top_voters = top_voters[:min(3, len(top_voters))]
    
    # Stima il valore medio dei voti dei whale
    # L'importanza è già normalizzata: rshares / 1e12 o vests / 1e6
    whale_powers = [v.get('importance', 0) * 1e6 for v in top_voters]
    avg_whale_power = sum(whale_powers) / len(whale_powers) if whale_powers else 0
    
    # Converte il potere whale in un valore steem approssimativo
    estimated_whale_vote_value = avg_whale_power * curator_steem_value / 10000  # Stima rough
    
    # Calcola il rapporto tra il valore del proprio voto e quello stimato dei whale
    if estimated_whale_vote_value <= 0:
        # Prima di ritornare il peso base, verifichiamo il valore assoluto del voto
        return optimize_weight_by_absolute_value(curator_steem_value, base_weight)
    
    power_ratio = curator_steem_value / estimated_whale_vote_value
    
    logger.info(f"Rapporto di potenza: {power_ratio:.3f} (tuo voto: {curator_steem_value:.4f} STEEM, " +
                f"whale stimati: {estimated_whale_vote_value:.4f} STEEM)")
    
    # Optimizza il peso del voto in base al rapporto di potenza:
    # - Se il tuo voto vale oltre il 50% di quello di un whale tipico, riduci il peso
    if power_ratio >= 0.5:
        # Formula di riduzione più aggressiva per valori elevati di voto
        if power_ratio >= 1.5:
            reduction_factor = 0.15  # Riduzione severa al 15%
        elif power_ratio >= 1.0:
            reduction_factor = 0.2   # Riduzione molto alta al 20%
        elif power_ratio >= 0.75:
            reduction_factor = 0.35  # Riduzione alta al 35% 
        else:  # power_ratio >= 0.5
            reduction_factor = 0.5   # Riduzione moderata al 50%
            
        # Applica la riduzione
        optimized_weight = int(base_weight * reduction_factor)
        
        # Applica un'ulteriore ottimizzazione basata sul valore assoluto
        optimized_weight = optimize_weight_by_absolute_value(curator_steem_value, optimized_weight)
        
        logger.info(f"Peso voto ridotto da {base_weight}% a {optimized_weight}% " +
                   f"(rapporto di potenza: {power_ratio:.2f})")
        
        return optimized_weight
    
    # Se il tuo voto è più piccolo dei whale, comunque controlla il valore assoluto
    return optimize_weight_by_absolute_value(curator_steem_value, base_weight)

def optimize_weight_by_absolute_value(steem_value, base_weight):
    """
    Ottimizza il peso del voto in base al valore assoluto in STEEM,
    indipendentemente dal confronto con i whale.
    
    Args:
        steem_value (float): Valore del voto a peso 100% in STEEM
        base_weight (int): Peso del voto attuale da ottimizzare
        
    Returns:
        int: Peso ottimizzato (0-100%)
    """
    # Calibrazione molto più aggressiva per account con alto valore di voto
    if steem_value >= 30:  # Estremamente potente (30+ STEEM)
        target_weight = min(int(base_weight * 0.05), 5)  # Massimo 5% del peso base, non più del 5% assoluto
        logger.info(f"Voto estremamente potente ({steem_value:.2f} STEEM): peso ridotto drasticamente a {target_weight}%")
    elif steem_value >= 20:  # Molto potente (20-30 STEEM)
        target_weight = min(int(base_weight * 0.1), 10)  # Massimo 10% del peso base, non più del 10% assoluto
        logger.info(f"Voto molto potente ({steem_value:.2f} STEEM): peso ridotto a {target_weight}%") 
    elif steem_value >= 10:  # Potente (10-20 STEEM)
        target_weight = min(int(base_weight * 0.15), 15)  # Massimo 15% del peso base, non più del 15% assoluto
        logger.info(f"Voto potente ({steem_value:.2f} STEEM): peso ridotto a {target_weight}%")
    elif steem_value >= 5:  # Forte (5-10 STEEM)
        target_weight = min(int(base_weight * 0.25), 25)  # Massimo 25% del peso base, non più del 25% assoluto
        logger.info(f"Voto forte ({steem_value:.2f} STEEM): peso ridotto a {target_weight}%")
    elif steem_value >= 1:  # Moderato (1-5 STEEM)
        target_weight = min(int(base_weight * 0.5), 50)  # Massimo 50% del peso base, non più del 50% assoluto
        logger.info(f"Voto moderato ({steem_value:.2f} STEEM): peso ridotto a {target_weight}%")
    elif steem_value >= 0.5:  # Mite (0.5-1 STEEM) 
        target_weight = min(int(base_weight * 0.7), 70)  # Massimo 70% del peso base, non più del 70% assoluto
        logger.info(f"Voto mite ({steem_value:.2f} STEEM): peso ridotto a {target_weight}%")
    else:
        # Per valori bassi, mantieni il peso originale
        return base_weight
        
    # Assicurati che il peso non scenda sotto il 1% per garantire un voto minimo
    return max(target_weight, 1)
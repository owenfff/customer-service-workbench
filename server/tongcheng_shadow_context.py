"""Associate an immediately following product card with a shadow question only."""
from datetime import datetime


def following_question(question, card):
    """Require adjacent, timestamped inbound text/card packets within five seconds.

    Callers enforce adjacency in the same account and conversation. This is
    shadow enrichment, never permission to reactivate or send a reply task.
    """
    if not question or not card:
        return False
    if question.get('direction') != 'inbound' or question.get('kind') != 'text':
        return False
    if card.get('direction') != 'inbound' or card.get('kind') != 'card' or not card.get('products'):
        return False
    try:
        delta = (datetime.fromisoformat(card['received_at']) - datetime.fromisoformat(question['received_at'])).total_seconds()
        return 0 <= delta <= 5
    except (KeyError, ValueError, TypeError):
        return False

"""
BU Control — Fuzzy name matching utility
Used by all document parsers to match extracted names to stored records
"""

import re

def normalize_name(name):
    """Normalize a company name for comparison."""
    if not name:
        return ""
    # lowercase, remove legal suffixes, punctuation, extra spaces
    name = name.lower().strip()
    # Remove common legal suffixes
    suffixes = [
        r'\bs\.r\.l\.?\b', r'\bs\.p\.a\.?\b', r'\bs\.a\.?\b', r'\bsrl\b',
        r'\bspa\b', r'\bltd\.?\b', r'\bllc\.?\b', r'\binc\.?\b',
        r'\bgmbh\b', r'\bb\.v\.?\b', r'\bs\.a\.s\.?\b', r'\bs\.n\.c\.?\b',
        r'\bsaicfel\b',  # keep meaningful parts
    ]
    for s in suffixes:
        name = re.sub(s, '', name)
    # Remove punctuation and extra spaces
    name = re.sub(r'[^\w\s]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def similarity(a, b):
    """
    Simple similarity score between two strings (0.0 to 1.0).
    Uses token overlap — good enough for company names.
    """
    if not a or not b:
        return 0.0
    na, nb = normalize_name(a), normalize_name(b)
    if na == nb:
        return 1.0
    # Check if one contains the other
    if na in nb or nb in na:
        return 0.9
    # Token overlap
    tokens_a = set(na.split())
    tokens_b = set(nb.split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    jaccard = len(intersection) / len(union)
    # Bonus if first token matches (company name usually starts with the key word)
    first_match = 0.1 if list(tokens_a)[0] == list(tokens_b)[0] else 0
    return min(1.0, jaccard + first_match)

def find_best_match(extracted_name, records, name_field="name", threshold=0.5):
    """
    Find the best matching record for an extracted name.
    
    Returns:
        (record, score, match_type) where match_type is:
        'exact'  — score == 1.0
        'fuzzy'  — score >= threshold
        None     — no match found
    """
    if not extracted_name or not records:
        return None, 0.0, None

    best_record = None
    best_score = 0.0

    for record in records:
        stored_name = record[name_field] if hasattr(record, '__getitem__') else getattr(record, name_field, "")
        score = similarity(extracted_name, stored_name)
        if score > best_score:
            best_score = score
            best_record = record

    if best_score >= 0.99:
        return best_record, best_score, "exact"
    elif best_score >= threshold:
        return best_record, best_score, "fuzzy"
    else:
        return None, best_score, None

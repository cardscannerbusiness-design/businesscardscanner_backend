import re
import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

# Common job titles to help identify designation lines
COMMON_TITLES = [
    "ceo", "cto", "cfo", "coo", "cmo", "founder", "co-founder", "president",
    "director", "manager", "engineer", "developer", "designer", "consultant",
    "architect", "specialist", "coordinator", "administrator", "executive",
    "lead", "head", "vp", "vice president", "partner", "principal", "officer",
    "assistant", "associate", "analyst", "representative", "agent", "broker"
]

# Common company suffixes
COMPANY_SUFFIXES = [
    "inc", "llc", "ltd", "corp", "corporation", "company", "co", "group",
    "holdings", "gmbh", "solutions", "technologies", "technology", "studios",
    "labs", "partners", "consulting", "consultants", "pvt", "private",
    "limited", "llp", "plc", "sa", "ag",
]

# Valid top-level business domain suffixes for website validation
VALID_TLDS = {
    'com', 'org', 'net', 'in', 'co', 'io', 'us', 'uk', 'ca', 'biz', 'info', 
    'ai', 'app', 'edu', 'gov', 'me', 'xyz', 'tech', 'online', 'site', 'fr', 
    'de', 'jp', 'cn', 'br', 'au', 'eu', 'es', 'it', 'nl', 'se', 'no', 'ch'
}

EMAIL_IN_LINE_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
WEBSITE_IN_LINE_REGEX = re.compile(r'(?:https?://|www\.)[^\s]+', re.IGNORECASE)

def is_valid_email(email: str) -> bool:
    """Validate email format, rejecting spacing anomalies and multiple dots."""
    email = email.strip()
    if not email or " " in email or ".." in email:
        return False
    return bool(re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,6}$', email))

def is_valid_website(url: str) -> bool:
    """Validate website domain structure, rejecting invalid TLDs and random decimals (e.g. 0.00)."""
    temp = url.lower().strip()
    
    # Reject common decimal noise patterns
    if re.search(r'\d+\.\d+', temp):
        return False
        
    temp = re.sub(r'^https?:\/\/', '', temp)
    temp = re.sub(r'^www\.', '', temp)
    temp = temp.split('/')[0] # keep host domain only
    
    parts = temp.split('.')
    if len(parts) < 2:
        return False
        
    # Suffix TLD must be alphabetical and in our whitelist of valid business domains
    tld = parts[-1]
    if tld not in VALID_TLDS:
        return False
        
    # Second-level domain must be alphanumeric and at least 2 chars long
    sld = parts[-2]
    if not sld or (not sld.isalnum() and '-' not in sld):
        return False
    if len(sld) < 2:
        return False
        
    return True

def is_valid_phone(phone: str) -> bool:
    """Validate phone number, enforcing minimum length and rejecting decimals."""
    if not phone or "." in phone:
        return False
    # Reject if it has more than one letter (allows for 'x' at the end for extensions, but rejects typos)
    letters = [c for c in phone if c.isalpha()]
    if len(letters) > 1:
        return False
    digits = "".join(c for c in phone if c.isdigit())
    return 7 <= len(digits) <= 15

def clean_text(text: str) -> List[str]:
    """Clean the raw text, returning a list of normalized non-empty lines."""
    lines = text.split('\n')
    cleaned_lines = []
    
    for line in lines:
        # Remove unwanted garbage symbols completely, keep letters, numbers, basic punctuation, '&' and '_'
        line = re.sub(r'[^\w\s\.\,\@\-\+\(\)\:\/\|&_]', '', line)
        line = line.strip()
        
        # Skip empty lines or lines with very few characters (likely noise)
        if len(line) < 3:  # Increased from 2 to 3 to reject garbage short strings
            continue
            
        # Reject decimal fraction garbage lines (e.g. 0.00, 2.990)
        if re.match(r'^\d+\.\d+$', line):
            logger.info(f"[OCR Noise Filter] Discarded decimal garbage line: '{line}'")
            continue
            
        cleaned_lines.append(line)
        
    return cleaned_lines

def extract_emails(lines: List[str]) -> tuple[List[str], List[str]]:
    """Extract emails and return the extracted emails and the remaining lines."""
    emails = []
    remaining_lines = []
    email_pattern = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
    
    for line in lines:
        matches = email_pattern.findall(line)
        if matches:
            for match in matches:
                if is_valid_email(match):
                    emails.append(match.strip())
                else:
                    logger.info(f"[OCR Noise Filter] Rejected invalid email candidate: '{match}'")
            # Remove the emails from the line
            new_line = email_pattern.sub('', line).strip()
            # If line still has useful text after removing email, keep it
            if len(new_line) > 2:
                # Remove prefixes like "Email:" or "E:"
                new_line = re.sub(r'^(e(mail)?|m)\s*[:\-\.]?\s*', '', new_line, flags=re.IGNORECASE).strip()
                if len(new_line) > 2:
                    remaining_lines.append(new_line)
        else:
            remaining_lines.append(line)
            
    # Deduplicate emails (case-insensitive)
    unique_emails = []
    seen = set()
    duplicates_removed = 0
    
    for email in emails:
        norm = email.lower().strip()
        if norm in seen:
            duplicates_removed += 1
        else:
            seen.add(norm)
            unique_emails.append(email)
            
    if duplicates_removed > 0:
        logger.info(f"[Deduplication] Removed {duplicates_removed} duplicate email(s).")
            
    return unique_emails, remaining_lines

def extract_websites(lines: List[str]) -> tuple[List[str], List[str]]:
    """Extract websites and return the extracted websites and the remaining lines."""
    websites = []
    remaining_lines = []
    url_pattern = re.compile(r'(?:https?:\/\/)?(?:www\.)?[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]{2,}(?:\/[^\s]*)?', re.IGNORECASE)
    # "Plot No.1151" / "H.No.12" look like domains to a naive pattern — never strip those.
    false_domain = re.compile(r'\b(?:no|h\.?\s*no|plot|pin|fl|floor|rd|st|ave)\.\d', re.IGNORECASE)
    
    for line in lines:
        # Avoid matching emails as urls
        if '@' in line:
            remaining_lines.append(line)
            continue
            
        matches = url_pattern.findall(line)
        kept_matches = []
        for match in matches:
            if false_domain.search(match) or re.fullmatch(r'[A-Za-z]{1,4}\.\d+', match):
                logger.info(f"[OCR Noise Filter] Rejected address-like website candidate: '{match}'")
                continue
            if is_valid_website(match):
                websites.append(match.strip())
                kept_matches.append(match)
            else:
                logger.info(f"[OCR Noise Filter] Rejected invalid website candidate: '{match}'")

        if kept_matches:
            new_line = line
            for match in kept_matches:
                new_line = new_line.replace(match, '', 1)
            new_line = new_line.strip()
            if len(new_line) > 2:
                new_line = re.sub(r'^(w(eb(site)?)?)\s*[:\-\.]?\s*', '', new_line, flags=re.IGNORECASE).strip()
                if len(new_line) > 2:
                    remaining_lines.append(new_line)
        else:
            remaining_lines.append(line)
            
    # Normalize and deduplicate websites (remove protocol, www, trailing slashes)
    unique_websites = []
    seen_normalized = set()
    duplicates_removed = 0
    
    for url in websites:
        norm = url.lower().strip()
        norm = re.sub(r'^https?:\/\/', '', norm)
        norm = re.sub(r'^www\.', '', norm)
        norm = norm.rstrip('/')
        
        if norm in seen_normalized:
            duplicates_removed += 1
        else:
            seen_normalized.add(norm)
            unique_websites.append(url)
            
    if duplicates_removed > 0:
        logger.info(f"[Deduplication] Removed {duplicates_removed} duplicate website(s).")
            
    return unique_websites, remaining_lines

def extract_phones(lines: List[str]) -> tuple[List[str], List[str]]:
    """Extract all valid phone numbers and return them along with remaining lines."""
    phones = []
    remaining_lines = []
    
    # A broad international and domestic phone number pattern (keeps final digits).
    phone_pattern = re.compile(
        r'(?:(?:\+|00)\d{1,3}[\s.-]?)?(?:\(?\d{2,5}\)?[\s.-]?)?\d{3,5}[\s.-]?\d{3,5}(?:[\s.-]?\d{1,5})?'
    )
    address_context = re.compile(
        r'\b(?:plot|plt|pin|pincode|postal|zip|road|rd|street|st|floor|colony|nagar|'
        r'house|door|sector|block|mouza|address)\b|,\s*\w+.*(pin|india)|\b\d{6}\b',
        re.IGNORECASE,
    )

    def collapse_digit_spaces(text: str) -> str:
        """Ignore formatting spaces between digits so spaced OCR phones keep all digits."""
        return re.sub(r'(\d)[\s.\-]+(?=\d)', r'\1', text)

    def normalize_phone_digits(phone: str) -> str:
        """Preserve digits only; keep leading + / 00 country-code marker unchanged in spirit."""
        trimmed = (phone or "").strip()
        if not trimmed:
            return ""
        has_plus = trimmed.startswith("+") or trimmed.startswith("00") or trimmed.startswith("(+")
        digits = "".join(c for c in trimmed if c.isdigit())
        if not digits:
            return ""
        return f"+{digits}" if has_plus else digits

    def is_false_phone_match(match: str, line: str) -> bool:
        """Avoid treating PIN codes / plot numbers as phone numbers."""
        digits = "".join(c for c in match if c.isdigit())
        if len(digits) == 6 and not match.strip().startswith("+"):
            # 6-digit value on an address-like line is almost always a PIN.
            if address_context.search(line) or re.search(r"[A-Za-z]\s*-\s*" + re.escape(digits), line):
                return True
            if len(digits) == 6 and len(match.strip()) <= 8:
                return True
        if len(digits) < 8:
            return True
        idx = line.find(match)
        before = line[:idx] if idx >= 0 else ""
        if re.search(
            r"(?:plot|plt|pin|pincode|postal|zip|floor|house|door|no\.?|road|street|sector|block)\s*[:.#\-]*\s*$",
            before,
            re.IGNORECASE,
        ):
            return True
        return False

    for line in lines:
        clean_line = re.sub(r'^(tel|phone|mobile|cell|m|p|t|f|office|direct)\s*[:\-\.]?\s*', '', line, flags=re.IGNORECASE).strip()
        # Collapse digit spaces before matching so "98765 43210" keeps all 10 digits.
        match_line = collapse_digit_spaces(clean_line)
        
        matches = phone_pattern.findall(match_line)
        matched_in_line = False
        for match in matches:
            if is_false_phone_match(match, match_line):
                continue
            if is_valid_phone(match):
                phones.append(normalize_phone_digits(match))
                matched_in_line = True
                match_line = match_line.replace(match, '').strip()
            else:
                logger.info(f"[OCR Noise Filter] Rejected invalid phone candidate: '{match}'")
                
        if matched_in_line:
            clean_line = re.sub(r'^(tel|phone|mobile|cell|m|p|t|f|office|direct|support\s*no\.?)\s*[:\-\.]?\s*', '', match_line, flags=re.IGNORECASE).strip()
            # Drop leftover labels like "ID:" after email stripping on other paths.
            if len(clean_line) > 2 and not re.match(r'^(?:id|email|e-?mail|web|www)\s*:?\s*$', clean_line, re.IGNORECASE):
                remaining_lines.append(clean_line)
        else:
            remaining_lines.append(line)
            
    # Normalize and deduplicate phone numbers (suffix comparison)
    unique_phones = []
    seen_normalized = set()
    duplicates_removed = 0
    
    for phone in phones:
        # Preserve leading + / 00 country-code prefix exactly as OCR produced it.
        cleaned = phone.strip()
        if cleaned.startswith("00"):
            cleaned = f"+{cleaned[2:]}"
        # Collapse internal OCR spacing but keep a single space after +CC when present.
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        plus_match = re.match(r"^(\+\d{1,3})\s*(.+)$", cleaned)
        if plus_match:
            local_digits = re.sub(r"\D", "", plus_match.group(2))
            cleaned = f"{plus_match.group(1)} {local_digits}" if local_digits else plus_match.group(1)
        norm = "".join(c for c in cleaned if c.isdigit())
        is_duplicate = False
        for existing in list(seen_normalized):
            # Suffix/prefix matching (e.g. 9876543210 is suffix of +919876543210)
            if norm.endswith(existing) or existing.endswith(norm):
                is_duplicate = True
                duplicates_removed += 1
                if len(norm) > len(existing):
                    # Replace with the more complete number containing country code
                    seen_normalized.remove(existing)
                    seen_normalized.add(norm)
                    for idx, val in enumerate(unique_phones):
                        val_norm = "".join(c for c in val if c.isdigit())
                        if val_norm == existing:
                            unique_phones[idx] = cleaned
                            break
                break
        if not is_duplicate:
            seen_normalized.add(norm)
            unique_phones.append(cleaned)
            
    if duplicates_removed > 0:
        logger.info(f"[Deduplication] Removed {duplicates_removed} duplicate phone number(s).")
        
    return unique_phones, remaining_lines

def is_likely_name_line(line: str) -> bool:
    line = line.strip()
    if not line or len(line) < 2 or len(line) > 60:
        return False
    if EMAIL_IN_LINE_REGEX.search(line) or WEBSITE_IN_LINE_REGEX.search(line):
        return False
    if re.search(r"\+?\d[\d\s\-().]{7,}", line):
        return False
    words = line.split()
    if len(words) < 1 or len(words) > 6:
        return False
    alpha_words = sum(1 for w in words if re.match(r"^[A-Za-z][A-Za-z.\-'']*$", w))
    return alpha_words >= max(1, len(words) - 1)


def infer_name_from_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    local = email.split("@")[0].strip()
    if not local or local.isdigit():
        return ""
    name = re.sub(r"[._+\-]+", " ", local).strip()
    words = [w.capitalize() for w in name.split() if w.isalpha()]
    if 1 <= len(words) <= 5:
        return " ".join(words)
    return ""


# Indian states/regions frequently printed on cards (service areas, addresses).
# These are never a person's name, so exclude them from name candidates.
INDIAN_REGIONS = {
    "himachal", "himachal pradesh", "rajasthan", "uttrakhand", "uttarakhand",
    "uttar pradesh", "madhya pradesh", "andhra pradesh", "arunachal pradesh",
    "jammu", "kashmir", "jammu kashmir", "jammu and kashmir", "jammu & kashmir",
    "kerala", "goa", "punjab", "haryana", "bihar", "gujarat", "maharashtra",
    "karnataka", "tamil nadu", "telangana", "west bengal", "odisha", "assam",
    "sikkim", "ladakh", "delhi", "new delhi", "chhattisgarh", "jharkhand",
    "manipur", "meghalaya", "mizoram", "nagaland", "tripura", "india",
}


def _score_name_candidate(line: str) -> int:
    """Score how likely a line is a person's name (higher = more likely)."""
    if not is_likely_name_line(line):
        return -100
    lower_line = line.lower().strip()
    if lower_line in INDIAN_REGIONS:
        return -100
    if any(re.search(r'\b' + re.escape(suffix) + r'\b', lower_line) for suffix in COMPANY_SUFFIXES):
        return -100
    if any(re.search(r'\b' + re.escape(title) + r'\b', lower_line) for title in COMMON_TITLES):
        return -100
    words = line.split()
    score = 0
    # Person names are usually 2-4 words; single words are weak candidates
    # (often place names, headings, or logo fragments).
    if 2 <= len(words) <= 4:
        score += 40
    elif len(words) == 1:
        score += 5
    if all(re.match(r"^[A-Z][a-z'.\-]+$", w) or (len(w) >= 2 and w.isupper()) for w in words):
        score += 25
    return score


def extract_name_and_designation(lines: List[str]) -> tuple[str, str, List[str]]:
    """Identify the person's name and designation using heuristics."""
    name = ""
    designation = ""

    # 1. First, look for a designation line matching COMMON_TITLES
    designation_idx = -1
    for i, line in enumerate(lines):
        lower_line = line.lower()
        for title in COMMON_TITLES:
            if re.search(r'\b' + re.escape(title) + r'\b', lower_line):
                designation = line
                designation_idx = i
                break
        if designation:
            break

    # 2. Pick the best-scoring name candidate across all lines instead of
    # blindly taking the line before the designation (which can be a stray
    # place name or heading from another column of the card).
    best_score = 0
    for i, line in enumerate(lines):
        if i == designation_idx:
            continue
        score = _score_name_candidate(line)
        if score <= 0:
            continue
        # Names usually sit directly above the title line — small bonus only,
        # so a proper multi-word name elsewhere still wins over a single word.
        if designation_idx > 0 and i == designation_idx - 1:
            score += 30
        if score > best_score:
            best_score = score
            name = line

    # Re-build remaining lines excluding matched name and designation
    remaining_lines = [
        line for line in lines if line != name and line != designation
    ]

    return name, designation, remaining_lines

def extract_address(lines: List[str]) -> tuple[str, List[str]]:
    """Extract address using heuristics (street suffixes, zip codes)."""
    address_list, remaining_lines = extract_all_addresses(lines)
    return ("\n".join(address_list[:1]) if address_list else ""), remaining_lines

def extract_company(lines: List[str], emails: List[str], websites: List[str]) -> str:
    """Extract company name using suffixes or domain names from emails/websites.

    Multiline company names (e.g. ABC / Technologies / Private Limited) are
    merged into a single spaced string when consecutive fragment lines sit
    immediately above a suffix line.

    OCR text with printed spaces is preferred. Domain inference is a last resort
    only when OCR yields no usable company line.
    """
    def _line_has_company_suffix(line: str) -> bool:
        lower_line = line.lower()
        return any(
            re.search(r'\b' + re.escape(suffix) + r'\b', lower_line)
            for suffix in COMPANY_SUFFIXES
        )

    # 1. Look for explicit company suffixes and merge preceding/following fragments
    for i, line in enumerate(lines):
        if not _line_has_company_suffix(line):
            continue
        # Extend forward through consecutive company fragments so
        # "Technologies" + "Private Limited" merge into one name.
        anchor = i
        j = i + 1
        while j < len(lines) and _is_company_name_fragment(lines[j]):
            anchor = j
            j += 1
            if j - i >= 4:
                break
        return _merge_multiline_company(lines, anchor)

    # 2. Prefer spaced OCR brand lines before domain inference
    for i, line in enumerate(lines):
        text = (line or "").strip()
        if " " in text and _is_company_name_fragment(text) and len(text) >= 4:
            return _merge_multiline_company(lines, i)

    # 3. Guess from domain name only when OCR completely failed
    domain_hints = []
    for email in emails:
        parts = email.split('@')
        if len(parts) == 2:
            domain = parts[1].split('.')[0]
            if domain not in ['gmail', 'yahoo', 'hotmail', 'outlook', 'icloud']:
                domain_hints.append(domain)

    for website in websites:
        match = re.search(r'(?:www\.)?([a-zA-Z0-9-]+)\.', website)
        if match:
            domain_hints.append(match.group(1))

    if domain_hints:
        hint = domain_hints[0].lower()
        # Prefer an OCR line that contains this hint (keeps printed spaces)
        for i, line in enumerate(lines):
            compact = re.sub(r'\s+', '', line.lower())
            if hint in compact or hint in line.lower():
                return _merge_multiline_company(lines, i)
        # Fallback: humanize domain (pinnacleadvisors -> Pinnacle Advisors)
        spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", hint)
        spaced = re.sub(r"[-_]", " ", spaced)
        if spaced.isalpha() and len(spaced) > 6:
            for suffix in ("advisors", "solutions", "technologies", "consulting", "group", "labs", "studio"):
                if spaced.endswith(suffix) and len(spaced) > len(suffix):
                    prefix = spaced[: -len(suffix)]
                    return f"{prefix.capitalize()} {suffix.capitalize()}"
        return " ".join(w.capitalize() for w in spaced.split())

    # 4. Fallback: Take the first reasonably short remaining line
    for line in lines:
        if len(line.split()) <= 4:
            return line

    return ""


_ADDRESS_LIKE = re.compile(
    r'\b(?:plot|plt|pin|pincode|postal|zip|road|rd|street|st|floor|colony|nagar|'
    r'house|door|sector|block|address|avenue|lane|suite)\b|\b\d{6}\b',
    re.IGNORECASE,
)
_COMPANY_FRAGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9&.'\- ]{0,40}$")


def _is_company_name_fragment(line: str) -> bool:
    """True for short brand/company fragment lines safe to merge."""
    text = (line or "").strip()
    if not text or len(text) > 45:
        return False
    if EMAIL_IN_LINE_REGEX.search(text) or WEBSITE_IN_LINE_REGEX.search(text):
        return False
    if re.search(r"\+?\d[\d\s\-().]{7,}", text):
        return False
    if _ADDRESS_LIKE.search(text):
        return False
    lower = text.lower()
    if any(re.search(r'\b' + re.escape(title) + r'\b', lower) for title in COMMON_TITLES):
        return False
    if lower in INDIAN_REGIONS:
        return False
    words = text.split()
    if len(words) > 5:
        return False
    return bool(_COMPANY_FRAGMENT.match(text))


def _merge_multiline_company(lines: List[str], anchor_idx: int) -> str:
    """Merge consecutive company fragment lines ending at anchor_idx into one name."""
    if anchor_idx < 0 or anchor_idx >= len(lines):
        return ""
    parts = [lines[anchor_idx].strip()]
    i = anchor_idx - 1
    while i >= 0:
        candidate = lines[i].strip()
        if not _is_company_name_fragment(candidate):
            break
        # Stop if the fragment already contains a full company suffix alone
        # and we already have a longer name — prefer building upward only for
        # short brand tokens.
        if len(candidate.split()) > 4:
            break
        parts.insert(0, candidate)
        i -= 1
        if len(parts) >= 5:
            break
    merged = " ".join(parts)
    return re.sub(r"\s+", " ", merged).strip()

def repair_ocr_mistakes(text: str) -> str:
    """Repair typical spacing issues and common OCR typos in emails and websites."""
    if not text:
        return ""
        
    logger.info("Running pre-extraction OCR text repairs and normalization.")
    
    # 1. Clean spacing around '@' and '.' in emails (e.g. 'john . doe @ gmail . com' -> 'john.doe@gmail.com')
    email_space_pattern = re.compile(
        r'([a-zA-Z0-9._%+-]+)\s*@\s*([a-zA-Z0-9.-]+)\s*\.\s*([a-zA-Z]{2,})'
    )
    def fix_email_space(match):
        fixed = f"{match.group(1).replace(' ', '')}@{match.group(2).replace(' ', '')}.{match.group(3).replace(' ', '')}"
        logger.info(f"[OCR Spacing Repair] Restored email: '{match.group(0)}' -> '{fixed}'")
        return fixed
    text = email_space_pattern.sub(fix_email_space, text)
    
    # 2. Clean spacing in websites (e.g. 'www . domain . com' -> 'www.domain.com')
    web_space_pattern = re.compile(
        r'(?:https?:\/\/\s*)?(?:www\s*\.\s*)?[a-zA-Z0-9-]+\s*\.\s*[a-zA-Z0-9-.]{2,}(?:\/[^\s]*)?', re.IGNORECASE
    )
    def fix_web_space(match):
        orig = match.group(0)
        if '@' in orig:
            return orig
        # Do not treat "No.1151" / "Plot No.12" as website domains.
        if re.search(r'\b(?:no|plot|pin|fl|floor|rd|st)\.\d', orig, re.IGNORECASE):
            return orig
        if re.fullmatch(r'[A-Za-z]{1,4}\.\d+', orig.strip()):
            return orig
        fixed = orig.replace(' ', '')
        fixed = re.sub(r'https?:/+', 'http://' if fixed.startswith('http:') else 'https://', fixed)
        if fixed != orig:
            logger.info(f"[OCR Spacing Repair] Restored website: '{orig}' -> '{fixed}'")
        return fixed
    text = web_space_pattern.sub(fix_web_space, text)
    
    # 3. Typo corrections in domain suffixes
    # Examples: '.c0m' -> '.com', '.0rg' -> '.org', '.n3t' -> '.net'
    typo_suffix_mappings = {
        r'\.c0m\b': '.com',
        r'\.0rg\b': '.org',
        r'\.n3t\b': '.net',
        r'\.c[o0]m\.([a-zA-Z]{2})\b': r'.com.\1',
    }
    for pattern, replacement in typo_suffix_mappings.items():
        orig_text = text
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        if text != orig_text:
            logger.info(f"[OCR Typo Correction] Corrected domain typo using pattern '{pattern}'")
            
    return text

def split_name_parts(name: str) -> tuple[str, str]:
    parts = name.strip().split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    if parts:
        return parts[0], ""
    return "", ""


def classify_phones(phones: List[str]) -> tuple[List[str], List[str]]:
    mobile: List[str] = []
    telephone: List[str] = []
    for phone in phones:
        digits = "".join(c for c in phone if c.isdigit())
        lower = phone.lower()
        if any(k in lower for k in ("mobile", "cell", "mob", "whatsapp")):
            mobile.append(phone)
        elif len(digits) == 10 and digits[0] in "6789":
            mobile.append(phone)
        elif phone.strip().startswith("+") and len(digits) >= 11:
            mobile.append(phone)
        else:
            telephone.append(phone)
    return mobile, telephone


SOCIAL_PATTERNS = [
    (re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w\-./]+", re.I), "LinkedIn"),
    (re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/company/[\w\-./]+", re.I), "LinkedIn"),
    (re.compile(r"(?:https?://)?(?:www\.)?(?:twitter|x)\.com/[\w\-./]+", re.I), "Twitter/X"),
    (re.compile(r"(?:https?://)?(?:www\.)?facebook\.com/[\w\-./]+", re.I), "Facebook"),
    (re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/[\w\-./]+", re.I), "Instagram"),
    (re.compile(r"(?:https?://)?(?:www\.)?youtube\.com/[\w\-./]+", re.I), "YouTube"),
]

GST_PATTERN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z\d]\b", re.I)
TAX_PATTERN = re.compile(r"\b(?:GST|TIN|PAN|VAT|Tax)\s*[:\-]?\s*([A-Z0-9\-/]{8,20})\b", re.I)


def extract_social_links(text: str) -> List[str]:
    found: List[str] = []
    seen = set()
    for pattern, _label in SOCIAL_PATTERNS:
        for match in pattern.findall(text):
            norm = match.lower().rstrip("/")
            if norm not in seen:
                seen.add(norm)
                found.append(match.strip())
    return found


def extract_tax_numbers(text: str) -> List[str]:
    taxes: List[str] = []
    for match in GST_PATTERN.findall(text):
        taxes.append(match.strip())
    for match in TAX_PATTERN.findall(text):
        if match.strip() and match.strip() not in taxes:
            taxes.append(match.strip())
    return taxes


ADDRESS_STREET_PATTERN = re.compile(
    r"\b(?:street|st\.?|avenue|ave\.?|boulevard|blvd\.?|road|rd\.?|drive|drv\.?|dr\.?"
    r"|lane|ln\.?|suite|ste\.?|floor|fl\.?|building|bldg\.?|block|plot|plt\.?"
    r"|sector|phase|layout|nagar|colony|tower|complex|mouza|place|pl\.?|court|ct\.?"
    r"|parkway|pkwy\.?|square|sq\.?|house\s*no\.?|h\.?\s*no\.?|door\s*no\.?"
    r"|p\.?\s*o\.?\s*box|post\s*office)\b",
    re.IGNORECASE,
)
ADDRESS_PLACE_PATTERN = re.compile(
    r"\b(?:mumbai|delhi|bengaluru|bangalore|hyderabad|chennai|kolkata|pune"
    r"|gurugram|gurgaon|noida|ahmedabad|jaipur|kochi|cochin|coimbatore|varanasi"
    r"|bhubaneswar|cuttack|puri|odisha|orissa|ernakulam|alappuzha|alleppey"
    r"|thrissur|trivandrum|thiruvananthapuram|calicut|kozhikode|mangalore|mysore"
    r"|indore|bhopal|lucknow|kanpur|patna|ranchi|raipur|surat|vadodara|nagpur"
    r"|nashik|chandigarh|amritsar|ludhiana|udaipur|jodhpur|guwahati|shillong"
    r"|imphal|agartala|gangtok|shimla|dehradun|haridwar|rishikesh|goa|panaji"
    r"|kerala|tamil\s*nadu|karnataka|maharashtra|gujarat|rajasthan|punjab|haryana"
    r"|bihar|west\s*bengal|uttar\s*pradesh|madhya\s*pradesh|telangana"
    r"|andhra\s*pradesh|india|u\.?\s*p\.?)\b",
    re.IGNORECASE,
)
# US zip (5 or 5+4), Indian 6-digit PIN, Pin-221002, City-751030.
ADDRESS_ZIP_PATTERN = re.compile(
    r"(?:\b(?:pin|pincode|pin\s*code|postal|zip)\b\s*[:.\-]?\s*)?\b\d{5}(?:-\d{4})?\b"
    r"|(?:\b(?:pin|pincode|pin\s*code|postal|zip)\b\s*[:.\-]?\s*)?\b\d{6}\b"
    r"|\b[A-Za-z][A-Za-z.\s]{1,28}-\s*\d{6}\b",
    re.IGNORECASE,
)
ADDRESS_HOUSE_NO_PATTERN = re.compile(
    r"(?:\b(?:plot|plt|house|h\.?\s*no\.?|door\s*no\.?|plot\s*no\.?)\b[\s.:#-]*)\d"
    r"|(?:^\s*(?:no\.?|#)\s*[:.]?\s*\d)"
    r"|(?:^\s*\d{1,4}\s*/\s*\d+)"
    r"|(?:^\s*[A-Za-z]?\d[\w./\s-]*,)",
    re.IGNORECASE,
)
ADDRESS_PREFIX_PATTERN = re.compile(
    r"^(?:address|add(?:ress)?|location|loc)\s*[:\-.]?\s*",
    re.IGNORECASE,
)
ADDRESS_FALSE_POSITIVE_PATTERN = re.compile(
    r"\b(?:ministry|tourism|govt\.?|government|recognized|recognised|wholesale|"
    r"partner|dream|magical|exploring|creating|memories|passion|journey|trusted|"
    r"b2b|dmc\s+of|since\s+\d{4}|tour|tours?|trekking|adventure|holidays?|"
    r"vacations?|explore\s+more|live\s+more|24\s*x\s*7|support\s*no|whatsapp)\b",
    re.IGNORECASE,
)
EMAIL_LINE_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
URL_LINE_PATTERN = re.compile(
    r"(?:https?://|www\.)[\w\-.]+\.[a-zA-Z]{2,}|\b[a-zA-Z0-9][a-zA-Z0-9-]*\.(?:com|co\.in|in|org|net)\b",
    re.IGNORECASE,
)
ADDRESS_BRAND_NOISE_PATTERN = re.compile(
    r"\b(?:pvt\.?|ltd\.?|limited|llp|dmc|travels?|hotels?|vacations?|holidays?|"
    r"tours?|adventure|solutions|technologies|private)\b",
    re.IGNORECASE,
)


def _is_address_excluded_line(line: str) -> bool:
    if EMAIL_LINE_PATTERN.search(line) or URL_LINE_PATTERN.search(line):
        return True
    # City/service-area footers: "Varanasi | Prayagraj | Ayodhya"
    if line.count("|") >= 1 and ADDRESS_PLACE_PATTERN.search(line) and not ADDRESS_HOUSE_NO_PATTERN.search(line):
        return True
    # Company legal names are not postal addresses.
    if any(re.search(rf"\b{re.escape(suffix)}\b", line, re.IGNORECASE) for suffix in COMPANY_SUFFIXES):
        if not ADDRESS_HOUSE_NO_PATTERN.search(line) and not ADDRESS_ZIP_PATTERN.search(line):
            return True
    if ADDRESS_BRAND_NOISE_PATTERN.search(line) and not (
        ADDRESS_HOUSE_NO_PATTERN.search(line)
        or ADDRESS_ZIP_PATTERN.search(line)
        or ADDRESS_STREET_PATTERN.search(line)
    ):
        return True
    if ADDRESS_FALSE_POSITIVE_PATTERN.search(line) and not (
        ADDRESS_HOUSE_NO_PATTERN.search(line) or ADDRESS_ZIP_PATTERN.search(line)
    ):
        return True
    # Leftover labels after phone/email stripping.
    if re.match(r"^(?:id|email|e-?mail|web|www|mobile|phone|tel)\s*:?\s*$", line, re.IGNORECASE):
        return True
    return False


def _is_strong_address_line(line: str) -> bool:
    if not line or len(line) < 4 or _is_address_excluded_line(line):
        return False
    if ADDRESS_STREET_PATTERN.search(line):
        return True
    if ADDRESS_HOUSE_NO_PATTERN.search(line):
        return True
    if ADDRESS_ZIP_PATTERN.search(line):
        return True
    if len(line) > 5 and re.match(r"^\s*[\dA-Za-z]", line) and re.search(r"\d", line) and "," in line:
        return True
    return False


def _is_weak_address_line(line: str) -> bool:
    if not line or len(line) < 4 or _is_address_excluded_line(line):
        return False
    if _is_strong_address_line(line):
        return True
    if not ADDRESS_PLACE_PATTERN.search(line):
        return False
    # Reject short all-caps brand lines that only mention a state/country.
    if line.isupper() and len(line.split()) <= 3 and not re.search(r"\d", line) and "," not in line:
        return False
    return bool(re.search(r"\d", line) or "," in line or len(line.split()) >= 2)


def _is_address_continuation(line: str) -> bool:
    if not line or len(line) < 3 or len(line) > 80:
        return False
    if _is_address_excluded_line(line):
        return False
    if _is_strong_address_line(line) or _is_weak_address_line(line):
        return True
    words = line.split()
    if len(words) > 8:
        return False
    # Landmark phrases between locality lines (not person names).
    if is_likely_name_line(line) and len(words) <= 3:
        return False
    return bool(re.match(r"^[A-Za-z0-9]", line))


def _line_has_address_signal(line: str) -> bool:
    if ADDRESS_STREET_PATTERN.search(line) or ADDRESS_HOUSE_NO_PATTERN.search(line) or ADDRESS_ZIP_PATTERN.search(line):
        return True
    return bool(ADDRESS_PLACE_PATTERN.search(line) and not ADDRESS_FALSE_POSITIVE_PATTERN.search(line))


def _address_block_score(block: str) -> int:
    """Prefer real postal blocks (plot/PIN/road) over country/company leftovers."""
    score = 0
    for line in block.split("\n"):
        if ADDRESS_ZIP_PATTERN.search(line):
            score += 50
        if ADDRESS_HOUSE_NO_PATTERN.search(line):
            score += 40
        if ADDRESS_STREET_PATTERN.search(line):
            score += 30
        if ADDRESS_PLACE_PATTERN.search(line):
            score += 10
        if "," in line:
            score += 5
        if ADDRESS_BRAND_NOISE_PATTERN.search(line):
            score -= 25
    score += min(20, len(block.split("\n")) * 5)
    return score


def extract_all_addresses(lines: List[str]) -> tuple[List[str], List[str]]:
    """Group consecutive address lines into complete multi-line address blocks.

    Multi-line addresses (e.g. building / street / city-PIN on separate lines)
    are joined with newlines so the full address is preserved. Landmark-style
    Indian addresses without a PIN are kept when a city/locality token is present.
    """
    strong = [_is_strong_address_line(line) for line in lines]
    weak = [_is_weak_address_line(line) for line in lines]
    include = [s or w for s, w in zip(strong, weak)]

    # Fill gaps between two address seeds (landmark / locality middle lines).
    seed_idxs = [i for i, flag in enumerate(include) if flag]
    for idx in range(len(lines)):
        if include[idx]:
            continue
        left = next((j for j in reversed(seed_idxs) if j < idx), None)
        right = next((j for j in seed_idxs if j > idx), None)
        if left is None or right is None or (right - left) > 4:
            continue
        if _is_address_excluded_line(lines[idx]):
            continue
        include[idx] = True

    # Sandwich rule: keep short connector lines that sit between address lines.
    for idx in range(1, len(lines) - 1):
        if (
            not include[idx]
            and include[idx - 1]
            and (include[idx + 1] or strong[idx + 1] or weak[idx + 1])
            and len(lines[idx]) <= 70
            and _is_address_continuation(lines[idx])
        ):
            include[idx] = True

    # Expand from seeds to adjacent continuation / weak place lines.
    changed = True
    while changed:
        changed = False
        for idx, line in enumerate(lines):
            if include[idx]:
                continue
            prev_kept = idx > 0 and include[idx - 1]
            next_kept = idx < len(lines) - 1 and include[idx + 1]
            if not (prev_kept or next_kept):
                continue
            if strong[idx] or weak[idx] or _is_address_continuation(line):
                if (
                    is_likely_name_line(line)
                    and not (prev_kept and next_kept)
                    and not weak[idx]
                    and not strong[idx]
                ):
                    continue
                include[idx] = True
                changed = True

    address_blocks: List[str] = []
    remaining_lines: List[str] = []
    current_block: List[str] = []
    current_has_signal = False

    def flush() -> None:
        nonlocal current_block, current_has_signal
        if not current_block:
            return
        if current_has_signal or any(_line_has_address_signal(item) for item in current_block):
            cleaned = [
                ADDRESS_PREFIX_PATTERN.sub("", item).strip()
                for item in current_block
            ]
            cleaned = [item for item in cleaned if item]
            if cleaned:
                address_blocks.append("\n".join(cleaned))
        else:
            remaining_lines.extend(current_block)
        current_block = []
        current_has_signal = False

    for line, keep, is_strong in zip(lines, include, strong):
        if keep:
            current_block.append(line)
            if is_strong or _line_has_address_signal(line):
                current_has_signal = True
        else:
            flush()
            remaining_lines.append(line)
    flush()

    address_blocks.sort(key=_address_block_score, reverse=True)
    return address_blocks, remaining_lines


# Backward-compatible alias used by older helpers/tests.
ADDRESS_LINE_PATTERN = ADDRESS_STREET_PATTERN


def build_confidence(result: Dict[str, Any], raw_text: str) -> Dict[str, float]:
    text_lower = raw_text.lower()

    def score(field_value: str, direct: bool = True) -> float:
        if not field_value:
            return 0.0
        if field_value.lower() in text_lower:
            return 96.0 if direct else 88.0
        return 72.0 if direct else 55.0

    first, last = result.get("firstName", ""), result.get("lastName", "")
    return {
        "fullName": score(result.get("name", "")),
        "firstName": score(first),
        "lastName": score(last),
        "designation": score(result.get("designation", "")),
        "companyName": score(result.get("company", ""), direct=False),
        "phoneNumber": max([score(p) for p in result.get("phones", [])] or [0]),
        "emailAddress": max([score(e) for e in result.get("emails", [])] or [0]),
        "website": max([score(w, False) for w in result.get("websites", [])] or [0]),
        "address": score(result.get("address", ""), False),
        "notes": 50.0 if result.get("notes") else 0.0,
        "gstNumber": max([score(g) for g in result.get("gstNumbers", [])] or [0]),
    }


def parse_business_card(raw_text: str) -> Dict[str, Any]:
    """Parse raw OCR text into structured contact JSON."""
    logger.info("Starting intelligent structured duplicate handling extraction from OCR text")
    
    if not raw_text or not raw_text.strip():
        logger.warning("Empty OCR text passed to parser.")
        return {
            "name": "",
            "firstName": "",
            "lastName": "",
            "designation": "",
            "company": "",
            "phones": [],
            "mobileNumbers": [],
            "telephoneNumbers": [],
            "emails": [],
            "websites": [],
            "addresses": [],
            "socialLinks": [],
            "gstNumbers": [],
            "address": "",
            "phone": "",
            "email": "",
            "notes": "",
            "confidence": {},
        }
    
    # Pre-process raw text to repair spacing anomalies and domain suffix typos
    raw_text = repair_ocr_mistakes(raw_text)
    
    lines = clean_text(raw_text)
    raw_lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    
    # Process and remove distinct fields sequentially to prevent overlaps
    emails, lines = extract_emails(lines)
    websites, lines = extract_websites(lines)
    phones, lines = extract_phones(lines)
    address_list, lines = extract_all_addresses(lines)
    name, designation, lines = extract_name_and_designation(lines)
    company = extract_company(lines, emails, websites)

    # Fallback: first plausible line from raw OCR before field stripping
    if not name:
        for line in raw_lines:
            cleaned = re.sub(r"[^\w\s\.\,\@\-\+\(\)\:\/\|&_]", "", line).strip()
            if is_likely_name_line(cleaned):
                name = cleaned
                break

    if not name and emails:
        name = infer_name_from_email(emails[0])

    first_name, last_name = split_name_parts(name)
    mobile_numbers, telephone_numbers = classify_phones(phones)
    social_links = extract_social_links(raw_text)
    gst_numbers = extract_tax_numbers(raw_text)
    # Notes are user-entered (voice/typed) only — never dump leftover OCR lines.
    notes = ""
    address = address_list[0] if address_list else ""

    result = {
        "name": name.title().strip(),
        "firstName": first_name.title().strip(),
        "lastName": last_name.title().strip(),
        "designation": designation.title().strip(),
        "phone": phones[0] if phones else "",
        "email": emails[0] if emails else "",
        "phones": phones,
        "mobileNumbers": mobile_numbers,
        "telephoneNumbers": telephone_numbers,
        "emails": emails,
        "websites": websites,
        "addresses": address_list,
        "socialLinks": social_links,
        "gstNumbers": gst_numbers,
        "address": address.strip(),
        "company": company.strip(),
        "notes": notes,
    }
    result["confidence"] = build_confidence(result, raw_text)
    
    logger.info(f"Structured extraction completed: {len(phones)} phone(s), {len(emails)} email(s), {len(websites)} website(s).")
    return result

# Additional FCC Grantee Codes for Ham Radio Manufacturers
# Research findings from FCC database searches

ADDITIONAL_GRANTEE_CODES = {
    # Major brands (verified)
    'Btech': '2AJGM',  # Same as Baofeng (BTech is Baofeng's US brand)
    'Puxing': 'W7PPX',  # PUXING ELECTRONICS SCIENCE & TECHNOLOGY
    'Tyt': '2AMJR',  # QUANZHOU WOTONG ELECTRONICS CO
    'Quansheng': '2ANMZ',  # QUANZHOU QUANSHENG ELECTRONICS CO
    'Motorola': 'AZ489',  # MOTOROLA SOLUTIONS INC
    'Vertex': 'K6620',  # Same as Yaesu (Vertex is Yaesu brand)
    'Midland': 'K4R',  # MIDLAND RADIO CORPORATION
    'Azden': 'FRH',  # AZDEN CORPORATION
    'Radioddity': '2AJGM',  # Same as Baofeng/BTech
    'Ailunce': '2AO3E',  # FUJIAN NANAN AILUNCE ELECTRONICS
    'Baojie': '2AFPR',  # QUANZHOU BAOJIE ELECTRIC CO
    'Rexon': 'IJJ',  # REXON ELECTRONICS CORP
    'Zastone': '2AM7H',  # ZASTONE TELECOM EQUIPMENT CO
    'Senhaix': '2AWSI',  # QUANZHOU SENHAIX ELECTRONICS CO
    'Connect Systems': 'XH8',  # CONNECT SYSTEMS INC
    'Tera': '2AL5L',  # FUJIAN QIANZHIDU ELECTRONICS TECH CO (makes Tera radios)
    'Luiton': '2AO8L',  # Same as Radtel (related companies)
    'Sainsionic': '2AJGM',  # Rebrand of Baofeng
    'Tekk': '2AJGM',  # Rebrand of Baofeng
    'Wln': '2ASNS',  # FUJIAN BFSAT INFORMATION TECHNOLOGY CO
    'Ruyage': '2AN5D',  # QUANZHOU RUYAGE ELECTRON CO
    'Marantz': 'FNA',  # MARANTZ AMERICA INC (vintage)
    
    # Lesser-known/vintage brands
    'Standard': 'K66',  # STANDARD COMMUNICATIONS CORP (now Vertex/Yaesu)
    'Radio Shack': 'FRS',  # Various OEM manufacturers
    'Maxon': 'HBG',  # MAXON AMERICA INC
    'Ritron': 'JZP',  # RITRON INC
    
    # Brands that may be rebrands or regional variants
    'Eagle': '2AJGM',  # Likely Baofeng rebrand
    'Vero': '2AJGM',  # Baofeng rebrand
    'Bintolk': '2AJGM',  # Baofeng rebrand
    'Hongxun': '2AL5L',  # Related to Tera
    'Iradio': '2AO8L',  # Related to Radtel
    'Jingtong': '2AJGM',  # Baofeng variant
    'Soontone': '2AJGM',  # Baofeng rebrand
}

# Brands that likely don't have FCC IDs (non-US or vintage)
# These would be left blank in the database
NO_FCC_BRANDS = [
    'Albrecht',  # European brand
    'K-Po',  # European brand
    'Kyodo',  # Japanese vintage
    'Mizuho',  # Japanese vintage
    'Daiwa',  # Japanese vintage
    'Magnum',  # European brand
    'Drag',  # Chinese domestic market
    'Dra',  # Unknown/obscure
    'Navico',  # Marine electronics, not ham radio
    'Blackbox',  # Generic/unknown
    'Bridgecom',  # Rebrand/distributor
    'Cleartone',  # Unknown
    'Cybercom',  # Vintage/obscure
    'Drake',  # Vintage (company no longer makes handhelds)
    'Santec',  # Vintage Japanese
    'Vhf Engineering',  # Vintage/kit manufacturer
    'Wilson Electronics',  # Antenna manufacturer, not radio maker
    'C-Standard',  # Generic brand
    'Kyder',  # Obscure
    'Scom',  # Recent Chinese brand, may not have FCC
    'Kmd / Tssd',  # Rebrands
    'Aea',  # Vintage
    'Adi',  # Obscure
    'Benjamin',  # Unknown
    'Bajeton',  # Chinese domestic
    'Ranger',  # Vintage CB brand
    'Rci',  # Vintage CB brand
    'Talkabout',  # Motorola FRS brand (consumer, not ham)
    'Vgc',  # Unknown
    'Inrico',  # Network radios, different certification
]

if __name__ == '__main__':
    print("Additional Grantee Codes to add:\n")
    for brand, code in sorted(ADDITIONAL_GRANTEE_CODES.items()):
        print(f"    '{brand}': '{code}',")

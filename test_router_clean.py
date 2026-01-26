import re

def clean_router_output(text):
    print(f"Original: {repr(text)}")
    clean_str = re.sub(r'<think>.*?</think>', '', text, flags=re.IGNORECASE | re.DOTALL).strip()
    
    if '</think>' in clean_str.lower():
            parts = re.split(r'</think>', clean_str, flags=re.IGNORECASE)
            clean_str = parts[-1].strip()
            
    clean_str = re.sub(r'</?think>', '', clean_str, flags=re.IGNORECASE).strip()
    clean_str = re.sub(r'^(answer|result|selected|tools|relevant|output):\s*', '', clean_str, flags=re.IGNORECASE).strip()
    
    print(f"Cleaned:  {repr(clean_str)}")
    return clean_str

print("--- Test Cases ---")
# Case 1: Standard
clean_router_output("<think>Reasoning...</think> tool1, tool2")

# Case 2: Missing start tag (The issue you saw)
clean_router_output("Reasoning... </think> tool1, tool2")

# Case 3: Only stray tag
clean_router_output("</think> tool1, tool2")

# Case 4: Weird casing
clean_router_output("<THINK>...</THINK> tool1")

# Case 5: No tags (Normal)
clean_router_output("tool1, tool2")

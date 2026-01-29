import json
import re
from html import escape

def test_stream_parsing(chunks):
    full_response = ""
    full_content = ""
    full_reasoning = ""
    is_reasoning = False
    first_token = True
    
    print("--- START TEST ---")
    
    for line_text in chunks:
        if line_text.startswith("data: "):
            raw_data = line_text[6:]
            if raw_data.strip() == "[DONE]": break
            
            try:
                chunk = json.loads(raw_data)
                choices = chunk.get('choices', [])
                if not choices: continue
                delta = choices[0].get('delta', {})
                
                # Process Content & Reasoning
                content_chunk = delta.get('content', '')
                reasoning_chunk = delta.get('reasoning_content', '')
                
                if reasoning_chunk:
                    if not is_reasoning:
                        is_reasoning = True
                    
                    if first_token:
                        print("DEBUG: first_token reasoning")
                        first_token = False
                        
                    print(f"REASONING: '{reasoning_chunk}'")
                    full_response += reasoning_chunk
                    full_reasoning += reasoning_chunk
                
                if content_chunk:
                    if is_reasoning:
                        print("DEBUG: end of reasoning separator")
                        is_reasoning = False
                        
                    if first_token:
                        print("DEBUG: first_token content")
                        first_token = False
                    
                    print(f"CONTENT: '{content_chunk}'")
                    full_response += content_chunk
                    full_content += content_chunk
                    
            except Exception as e:
                print(f"ERROR: {e}")
    
    print("--- END TEST ---")
    print(f"Full Response: '{full_response}'")
    print(f"Full Content: '{full_content}'")
    print(f"Full Reasoning: '{full_reasoning}'")
    
    # Emulate effectively empty check
    clean_content = re.sub(r'<[^>]*>', '', full_content)
    clean_content = re.sub(r'```[\s\S]*?```', '', clean_content)
    clean_content = clean_content.replace(".", "").replace("\n", "").replace(":", "").strip()
    
    empty_patterns = ["answer", "antwort", "response", "here", "hier", "ok", "okay"]
    temp_content = clean_content.lower()
    for pattern in empty_patterns:
        temp_content = temp_content.replace(pattern, "")
    temp_content = temp_content.strip()
    
    is_effectively_empty = len(temp_content) < 3
    print(f"Is effectively empty: {is_effectively_empty}")

# Test Case 1: Normal Content
print("\nTest Case 1: Normal Content")
test_stream_parsing([
    'data: {"choices":[{"delta":{"content":"Hello"}}]}',
    'data: {"choices":[{"delta":{"content":" world!"}}]}',
    'data: [DONE]'
])

# Test Case 2: Reasoning + Content
print("\nTest Case 2: Reasoning + Content")
test_stream_parsing([
    'data: {"choices":[{"delta":{"reasoning_content":"I should say hello"}}]}',
    'data: {"choices":[{"delta":{"content":"Hello!"}}]}',
    'data: [DONE]'
])

# Test Case 3: Empty Response (only Done)
print("\nTest Case 3: Empty Response")
test_stream_parsing([
    'data: [DONE]'
])

# Test Case 4: Effectively Empty (just "Okay.")
print("\nTest Case 4: Effectively Empty")
test_stream_parsing([
    'data: {"choices":[{"delta":{"content":"Okay."}}]}',
    'data: [DONE]'
])

import os
import sys
from llama_cpp import Llama

# Path to model
MODEL_PATH = os.path.abspath("models/VQ-1_Instruct-q4_k_m.gguf")

if not os.path.exists(MODEL_PATH):
    print(f"❌ Model not found at: {MODEL_PATH}")
    # Try looking for any gguf
    import glob
    ggufs = glob.glob("models/*.gguf")
    if ggufs:
        MODEL_PATH = os.path.abspath(ggufs[0])
        print(f"⚠️  Found alternate model: {MODEL_PATH}")
    else:
        sys.exit(1)

print(f"🚀 Loading model: {MODEL_PATH}")

try:
    llm = Llama(
        model_path=MODEL_PATH,
        n_ctx=2048,
        verbose=True
    )
    print("✅ Model loaded successfully!")
    
    print("\n📝 Testing Generation...")
    output = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": "You are a helpful assistant."}, 
            {"role": "user", "content": "Hello! say something."} 
        ],
        max_tokens=50
    )
    
    print("\n📊 Result:")
    print(output)
    
    choices = output.get("choices", [])
    if choices:
        content = choices[0].get("message", {}).get("content", "")
        print(f"\n🗣️  Content: '{content}'")
    else:
        print("\n❌ No choices returned!")

except Exception as e:
    print(f"\n❌ Error: {e}")
